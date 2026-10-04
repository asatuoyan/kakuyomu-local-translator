"""Tencent Hy-MT2 30B-A3B Ollama profile; see docs/hy-mt2-30b.md."""
import re

from languages import language_code


def uses_hy_mt_30b_profile(cfg):
    name = re.sub(r"[^a-z0-9]", "", cfg.get("model", "").lower())
    return "hymt230b" in name


def reference_context_limit(cfg):
    return min(max(0, int(cfg.get("context_chars", 1200))),
               max(0, int(cfg.get("hy_mt_context_chars", 256))))


def translation_payload(paragraphs, cfg, previous_context="", *, retry=False):
    names = {"zh-Hans": "简体中文", "zh-Hant": "繁体中文", "en": "英语",
             "ja": "日语", "ko": "韩语", "fr": "法语", "de": "德语", "es": "西班牙语"}
    target = names[language_code(cfg["target_language"])]
    sections = []
    glossary = cfg.get("glossary", {})
    if glossary:
        sections.append("参考下面的翻译：\n" + "\n".join(
            f"{source} 翻译成 {translation}" for source, translation in glossary.items()))
    context_chars = reference_context_limit(cfg)
    short = len(paragraphs) == 1 and len(paragraphs[0]) <= 80
    if previous_context and context_chars and not short:
        sections.append("〖背景信息〗\n" + previous_context[-context_chars:]
                        + "\n以上仅作背景参考，不要复制、续写或再次翻译。")
    if len(paragraphs) > 1:
        sections.append("输出要求：每个原文段落对应一个译文段落，顺序一致，用空行分隔。不要输出本条指令。")
    instruction = f"将以下文本翻译为{target}，注意只需要输出翻译后的结果，不要额外解释："
    sections.extend([instruction, "〖待翻译文本〗\n" + "\n\n".join(paragraphs)])
    return {
        "model": cfg["model"], "think": False,
        "messages": [{"role": "user", "content": "\n\n".join(sections)}],
        "options": {"temperature": 0.7, "top_p": 1.0, "top_k": 0,
                    "min_p": 0.0, "repeat_penalty": 1.05 if retry else 1.0,
                    "num_predict": 4096,
                    "num_ctx": int(cfg.get("hy_mt_num_ctx", 4096))},
    }
