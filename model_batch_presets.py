"""Initial batch targets, not model context limits or guaranteed safe lengths."""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class BatchPreset:
    name: str
    chars: int
    paragraphs: int
    ceiling: int


def model_batch_preset(model):
    name = re.sub(r"[^a-z0-9]", "", model.lower())
    if "hymt230b" in name:
        return BatchPreset("Hy-MT2 30B-A3B", 1024, 16, 2200)
    if "hymt27b" in name:
        return BatchPreset("Hy-MT2 7B", 800, 12, 2200)
    if "hymt218b" in name:
        return BatchPreset("Hy-MT2 1.8B", 512, 8, 1536)
    if "murasaki" in name and ("8b" in name or "14b" in name):
        return BatchPreset("Murasaki 8B/14B", 1024, 16, 1536)
    return BatchPreset("通用保守预设", 800, 8, 2200)
