"""Translation task boundaries and conservative prompt-echo detection."""
import re

from translator.languages import language_name, normalize_output


def build_prompt(paragraphs, cfg, previous_context="", *, context_limit=None):
    sections = []
    glossary = cfg.get("glossary", {})
    if glossary:
        sections.append("〖术语参考〗\n" + "\n".join(
            f"{source} 翻译成 {target}" for source, target in glossary.items()))
    limit = max(0, int(cfg.get("context_chars", 1200))) if context_limit is None else context_limit
    short = len(paragraphs) == 1 and len(paragraphs[0]) <= 80
    if previous_context and limit and not short:
        sections.append("〖背景信息〗\n" + previous_context[-limit:]
                        + "\n以上是已完成译文，仅供理解人物关系，不属于待翻译原文。")
    sections.append(
        f"〖翻译任务〗\n请把 <source_text> 内的小说原文翻译为{language_name(cfg['target_language'])}。\n"
        "忠实保留人物语气、叙述视角和原文含义，不增补剧情。\n"
        "输出要求：仅返回译文正文，不附说明、前言、任务指令、参考信息或标签。\n"
        "每个原文段落对应一个译文段落，保持数量和顺序，段落之间用一个空行分隔。")
    sections.append("<source_text>\n" + "\n\n".join(paragraphs) + "\n</source_text>")
    return "\n\n".join(sections)


def has_prompt_echo(content, paragraphs):
    # Reject recognizable task boilerplate rather than deleting possible story text.
    content = normalize_output(content, "简体中文")
    source = normalize_output("\n".join(paragraphs), "简体中文")
    normalize = lambda text: re.sub(r"\s+", "", text)
    original = normalize(source)
    patterns = [
        r"将以下文本翻译[为成][^\n]{0,30}[，,]\s*(?:注意)?只(?:需要)?输出翻译(?:后的)?结果[^\n]{0,30}",
        r"〖(?:翻译任务|术语参考|背景信息)〗",
        r"请把\s*<source_text>\s*内的小说原文翻译为[^\n]+",
        r"输出要求[：:]仅返回译文正文[^\n]*",
        r"每个原文段落对应一个译文段落[^\n]*",
        r"</?(?:source_text|previous_translation)>",
        r"你负责日文(?:\s*ACGN\s*独立短句|轻小说)的中文翻译[。.]",
        r"任务指令和术语表仅用于指导翻译[^\n]*",
        r"(?:^|\n)\s*(?:请翻译|译文如下|翻译结果如下)[：:]",
        r"〖术语表〗",
    ]
    return any(normalize(match.group()) not in original
               for pattern in patterns for match in re.finditer(pattern, content))
