"""Murasaki v0.2 Chinese translation profile; see docs/murasaki.md."""
import re

from translator.languages import language_code


class ThinkingBudgetExceeded(RuntimeError):
    """Reasoning exceeded the application's character budget."""


def uses_murasaki_profile(cfg):
    name = cfg.get("model", "").lower()
    return ("murasaki" in name and ("8b" in name or "14b" in name)
            and language_code(cfg["target_language"]).startswith("zh-"))


def relevant_glossary(paragraphs, glossary):
    """Keep all terms present in this batch, including explicit one-character terms."""
    text = "\n".join(paragraphs)
    return {source: target for source, target in glossary.items() if source and source in text}


def translation_payload(paragraphs, cfg, *, retry=False):
    size = sum(map(len, paragraphs))
    short = len(paragraphs) == 1 and size <= 80
    mode = cfg.get("murasaki_thinking_mode", "auto")
    if mode not in ("auto", "on", "off"):
        raise ValueError("murasaki_thinking_mode 必须是 auto、on 或 off。")
    think = mode == "on" or (mode == "auto" and not short)
    system = (
        "你负责日文 ACGN 独立短句的中文翻译。忠实保留信息和指代，避免增加背景或情节。指代不明时保留模糊，不补出原文未提供的主语。"
        if short else
        "你负责日文轻小说的中文翻译。先分析叙事语气、人物指代与动作关系，再给出自然连贯的译文。"
    )
    if think:
        system += "仅做必要的简短分析，避免反复推演。分析放在 <think> 标签内，随后只输出译文。"
    else:
        system += "直接输出译文，不输出分析、思考标签或说明。"
        system = system.replace("先分析叙事语气、人物指代与动作关系，再给出自然连贯的译文。",
                                "准确保留人物指代与动作关系，译文自然连贯。")
    system += "保持原文段落数量与顺序，段落之间用空行分隔。"
    target = "简体中文" if language_code(cfg["target_language"]) == "zh-Hans" else "繁体中文"
    system += (f"译文使用{target}。只翻译用户消息中 <source_text> 内的原文。"
               "任务指令和术语表仅用于指导翻译，不属于小说正文。"
               "最终输出仅包含译文，不复述提示词、不添加‘请翻译’、‘译文如下’或原文标签。")
    glossary = relevant_glossary(paragraphs, cfg.get("glossary", {}))
    if glossary:
        from translator.glossary.manager import format_glossary_prompt
        system += "\n〖术语表〗\n" + format_glossary_prompt(glossary)
    return {
        "model": cfg["model"], "think": think,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": "请翻译：\n<source_text>\n" + "\n\n".join(paragraphs) + "\n</source_text>"}],
        "options": {"temperature": 0.5 if retry else 0.3,
                    "repeat_penalty": 1.1 if retry else 1.05,
                    "num_predict": max(4096, size * 4 + 1024) if think else max(256, size * 4 + 128)},
    }


def translation_only(content):
    """Ollama normally separates thinking; handle legacy inline tags as well."""
    content = re.sub(r"<think\s*>.*?</think\s*>", "", content, flags=re.S | re.I)
    if re.search(r"<think\s*>", content, re.I):
        raise RuntimeError("Murasaki 思考内容未结束，没有完整译文。")
    if re.search(r"</think\s*>", content, re.I):
        content = re.split(r"</think\s*>", content, maxsplit=1, flags=re.I)[1]
    return content.strip()
