"""Conservative, explainable translation warnings for human review."""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class QualityWarning:
    paragraph_index: int
    source: str
    expected_target: str
    category: str
    original_text: str
    translated_text: str


def check_translation_quality(source: list[str], target: list[str]) -> list[QualityWarning]:
    if len(source) != len(target):
        raise ValueError("原文与译文段落数量不一致。")
    warnings = []
    previous = {}
    for index, (original, translated) in enumerate(zip(source, target), 1):
        a, b = original.strip(), translated.strip()
        if not a:
            continue
        reason = ""
        if not b:
            reason = "空译文"
        elif a == b and re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", a):
            reason = "原文未变化"
        elif len(a) >= 30 and len(b) < max(4, len(a) // 8):
            reason = "译文可能过短"
        elif len(a) >= 20 and len(b) > len(a) * 5:
            reason = "译文可能过长"
        elif any(text in b and original_source != a and text not in a
                 for text, original_source in previous.items()):
            reason = "译文疑似重复前文"
        lines = [line.strip() for line in b.splitlines() if len(line.strip()) >= 10]
        if not reason and len(lines) != len(set(lines)):
            reason = "译文内部重复"
        if reason:
            warnings.append(QualityWarning(index, "", "请人工核对", reason, a, b))
        if len(b) >= 10:
            previous[b] = a
    return warnings
