"""Tencent Hy-MT2 30B-A3B Ollama profile; see docs/hy-mt2-30b.md."""
import re

def uses_hy_mt_30b_profile(cfg):
    name = re.sub(r"[^a-z0-9]", "", cfg.get("model", "").lower())
    return "hymt230b" in name


def reference_context_limit(cfg):
    return min(max(0, int(cfg.get("context_chars", 1200))),
               max(0, int(cfg.get("hy_mt_context_chars", 256))))


def translation_payload(paragraphs, cfg, previous_context="", *, retry=False):
    from translator.translation.translation_prompt import build_prompt
    prompt = build_prompt(paragraphs, cfg, previous_context,
                          context_limit=reference_context_limit(cfg))
    return {
        "model": cfg["model"], "think": False,
        "messages": [{"role": "user", "content": prompt}],
        "options": {"temperature": 0.7, "top_p": 1.0, "top_k": 0,
                    "min_p": 0.0, "repeat_penalty": 1.05 if retry else 1.0,
                    "num_predict": 4096,
                    "num_ctx": int(cfg.get("hy_mt_num_ctx", 4096))},
    }
