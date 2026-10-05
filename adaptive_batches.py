"""Conservative batch sizing using context estimates and observed request results."""
from model_batch_presets import model_batch_preset
from hy_mt_profile import uses_hy_mt_30b_profile
from murasaki_profile import uses_murasaki_profile


class AdaptiveBatcher:
    def __init__(self, cfg, context_length=4096):
        self.cfg = cfg
        self.context_length = context_length
        self.preset = model_batch_preset(cfg.get("model", ""))
        self.hy_mt_30b = uses_hy_mt_30b_profile(cfg)
        self.murasaki = uses_murasaki_profile(cfg) if cfg.get("target_language") else False
        self.ceiling = min(self.preset.ceiling, max(1, int(cfg.get("translation_chunk_chars", 2200))))
        self.item_ceiling = max(1, int(cfg.get("translation_chunk_paragraphs", 40)))
        self.chars = min(self.ceiling, self.preset.chars)
        self.items = min(self.item_ceiling, self.preset.paragraphs)
        self.clean = 0
        self.degraded = False

    def mark_degraded(self):
        self.degraded = True

    def take(self, remaining, reference_chars=0):
        # Estimate both source and output tokens, reserving room for instructions.
        # Direct Hy-MT translation has no explicit reasoning allocation. Reserve
        # approximately two tokens per source and two per translated character.
        ratio = 4 if self.hy_mt_30b else 5
        budget = max(128, (self.context_length - 512 - reference_chars * 2) // ratio)
        if self.hy_mt_30b:
            # Stay below the profile's 4096 output-token recommendation.
            budget = min(budget, (4096 - 256) // 2)
        limit = min(self.chars, self.ceiling, budget)
        if (self.murasaki and sum(map(len, remaining)) <= min(self.ceiling, budget)
                and len(remaining) <= self.item_ceiling and not self.degraded):
            self.degraded = False
            return list(remaining)
        if (self.hy_mt_30b and self.cfg.get("hy_mt_prefer_whole_chapter", True)
                and sum(map(len, remaining)) <= min(self.ceiling, budget)
                and len(remaining) <= self.item_ceiling):
            self.degraded = False
            return list(remaining)
        batch, size = [], 0
        for paragraph in remaining:
            if batch and (size + len(paragraph) > limit or len(batch) >= self.items):
                break
            batch.append(paragraph)
            size += len(paragraph)
        self.degraded = False
        return batch

    def observe(self, seconds):
        if self.degraded or seconds > 45:
            self.chars = max(min(200, self.ceiling), self.chars // 2)
            self.items = max(1, self.items // 2)
            self.clean = 0
        elif seconds < 30:
            self.clean += 1
            if self.clean >= 2:
                self.chars = min(self.ceiling, max(self.chars + 1, int(self.chars * 1.25)))
                self.items = min(self.item_ceiling, self.items + 2)
                self.clean = 0
        else:
            self.clean = 0
