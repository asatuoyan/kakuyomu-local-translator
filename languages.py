"""Output languages and deterministic Chinese script normalization."""
from functools import lru_cache


LANGUAGES = {
    "zh-Hant": ("繁體中文", "繁中"),
    "zh-Hans": ("簡體中文", "简中"),
    "en": ("英文", "en"),
    "ja": ("日文", "ja"),
    "ko": ("韓文", "ko"),
    "fr": ("法文", "fr"),
    "de": ("德文", "de"),
    "es": ("西班牙文", "es"),
}
ALIASES = {
    "简体中文": "zh-Hans", "繁体中文": "zh-Hant",
    "zh-cn": "zh-Hans", "zh-sg": "zh-Hans", "zh-tw": "zh-Hant", "zh-hk": "zh-Hant",
    "english": "en", "japanese": "ja", "korean": "ko", "french": "fr",
    "german": "de", "spanish": "es", "英语": "en", "英語": "en", "韩文": "ko",
}


def language_code(value: str) -> str:
    value = value.strip()
    for code, (label, _) in LANGUAGES.items():
        if value == label or value.lower() == code.lower():
            return code
    if value.lower() in ALIASES:
        return ALIASES[value.lower()]
    raise ValueError(f"不支援的輸出語言：{value}")


def language_name(value: str) -> str:
    return LANGUAGES[language_code(value)][0]


def language_suffix(value: str) -> str:
    return LANGUAGES[language_code(value)][1]


@lru_cache(maxsize=2)
def _converter(code: str):
    try:
        from opencc import OpenCC
    except ImportError as exc:
        raise ValueError("簡繁自動轉換需要 OpenCC，請重新使用 run_gui.bat 或 run.bat 安裝依賴。") from exc
    return OpenCC("s2t" if code == "zh-Hant" else "t2s")


def normalize_output(text: str, language: str) -> str:
    code = language_code(language)
    return _converter(code).convert(text) if code.startswith("zh-") else text


def output_glossary(cfg: dict, language: str) -> dict:
    """Chinese defaults must not leak into translations in other languages."""
    code = language_code(language)
    mappings = cfg.get("glossary_by_language", {})
    glossary = mappings.get(code, cfg.get("glossary", {}) if code.startswith("zh-") else {})

    def convert(value):
        if isinstance(value, str):
            return normalize_output(value, code)
        if isinstance(value, dict):
            return {key: item if key == "source" else convert(item) for key, item in value.items()}
        if isinstance(value, list):
            return [convert(item) for item in value]
        return value

    return convert(glossary)
