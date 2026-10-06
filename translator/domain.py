"""Chapter data and shared text normalization."""
from __future__ import annotations

from dataclasses import dataclass, field
import re


@dataclass
class Episode:
    url: str
    work_title: str
    episode_title: str
    paragraphs: list[str]
    blocks: list[dict[str, str]] = field(default_factory=list)


def safe_name(value: str, fallback: str = "未命名作品") -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return value[:100] or fallback


def normalize_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t\u3000]+", " ", value)
    return value.strip()
