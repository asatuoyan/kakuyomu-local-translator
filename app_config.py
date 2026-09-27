"""Load, validate, and update persistent application settings."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from project_storage import atomic_json


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
TRANSLATION_MODELS = {
    "Hy-MT2 7B Q6_K": "hf.co/tencent/Hy-MT2-7B-GGUF:Q6_K",
    "Hy-MT2 7B Q4_K_M": "hf.co/tencent/Hy-MT2-7B-GGUF:Q4_K_M",
}
DEFAULT_MODEL = TRANSLATION_MODELS["Hy-MT2 7B Q4_K_M"]

_POSITIVE_INTS = ("translation_chunk_chars", "translation_chunk_paragraphs")
_NONNEGATIVE_INTS = ("translation_max_retries", "context_chars")
_POSITIVE_NUMBERS = ("request_timeout_seconds",)
_NONNEGATIVE_NUMBERS = ("translation_retry_delay_seconds", "request_delay_seconds", "temperature")


def _defaults(app_dir: Path) -> dict[str, Any]:
    return json.loads((app_dir / "config.example.json").read_text(encoding="utf-8"))


def validate_config(raw: Mapping[str, Any], app_dir: Path = APP_DIR) -> dict[str, Any]:
    """Return compatible settings with defaults and clear errors for invalid values."""
    if not isinstance(raw, Mapping):
        raise ValueError("config.json 必须是 JSON 对象。")
    config = _defaults(app_dir)
    config.update(raw)
    if not isinstance(config.get("model"), str) or not config["model"].strip():
        config["model"] = DEFAULT_MODEL
    else:
        config["model"] = config["model"].strip()
    for key in ("ollama_url", "output_dir", "browser_profile_dir"):
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(f"config.json 中的 {key} 必须是非空字符串。")
    parsed_url = urlparse(config["ollama_url"])
    if parsed_url.scheme not in ("http", "https") or not parsed_url.hostname:
        raise ValueError("config.json 中的 ollama_url 必须是 http:// 或 https:// 地址。")
    if config.get("ui_theme", "light") not in ("light", "dark"):
        config["ui_theme"] = "light"
    for key in _POSITIVE_INTS + _NONNEGATIVE_INTS:
        try:
            value = int(config[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"config.json 中的 {key} 必须是整数。") from exc
        minimum = 1 if key in _POSITIVE_INTS else 0
        original = config[key]
        if (isinstance(original, bool) or value < minimum
                or (isinstance(original, float) and not original.is_integer())
                or (isinstance(original, str) and str(value) != original.strip())):
            raise ValueError(f"config.json 中的 {key} 必须不小于 {minimum}。")
        config[key] = value
    for key in _POSITIVE_NUMBERS + _NONNEGATIVE_NUMBERS:
        try:
            value = float(config[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"config.json 中的 {key} 必须是数字。") from exc
        if isinstance(config[key], bool) or not math.isfinite(value) or value < 0 or (key in _POSITIVE_NUMBERS and value == 0):
            relation = "大于" if key in _POSITIVE_NUMBERS else "不小于"
            raise ValueError(f"config.json 中的 {key} 必须{relation} 0。")
        config[key] = value
    for key in ("glossary", "glossary_by_language"):
        if not isinstance(config[key], dict):
            raise ValueError(f"config.json 中的 {key} 必须是对象。")
    if not isinstance(config["headless"], bool):
        raise ValueError("config.json 中的 headless 必须是布尔值。")
    return config


def load_config(path: Path = CONFIG_PATH, app_dir: Path = APP_DIR) -> dict[str, Any]:
    if not path.exists():
        atomic_json(path, _defaults(app_dir))
    config = validate_config(json.loads(path.read_text(encoding="utf-8")), app_dir)
    for key in ("output_dir", "browser_profile_dir"):
        config[key] = str((app_dir / config[key]).resolve())
    return config


def update_config(changes: Mapping[str, Any], path: Path = CONFIG_PATH,
                  app_dir: Path = APP_DIR) -> None:
    """Persist only changed settings while keeping unrelated user values intact."""
    if any(key.startswith("_") for key in changes):
        raise ValueError("运行时任务状态不能写入 config.json。")
    raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else _defaults(app_dir)
    merged = dict(raw)
    merged.update(changes)
    validate_config(merged, app_dir)
    atomic_json(path, merged)
