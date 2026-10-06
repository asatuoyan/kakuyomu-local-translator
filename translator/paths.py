"""Stable project paths, independent of where application modules are stored."""
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = APP_DIR / "web"
