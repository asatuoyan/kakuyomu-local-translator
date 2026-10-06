"""Noninteractive browser-session setup shared by desktop and Web acquisition."""
import subprocess
import sys
from pathlib import Path
from typing import Any
from playwright.sync_api import BrowserContext, Page


def launch_context(
    playwright: Any, cfg: dict[str, Any], *, prompt_for_cookies: bool = False,
) -> BrowserContext:
    browser = cfg.get("browser_engine", "msedge")
    profile_base = Path(cfg["browser_profile_dir"])
    profile = profile_base if browser == "chromium" else profile_base.with_name(
        f"{profile_base.name}-{browser}"
    )
    options: dict[str, Any] = {
        "headless": bool(cfg.get("headless", False)),
        "viewport": {"width": 1280, "height": 900},
        "locale": "ja-JP",
    }
    if browser in {"msedge", "chrome"}:
        options["channel"] = browser
    elif browser == "custom":
        executable = str(cfg.get("browser_executable", "")).strip()
        if not executable or not Path(executable).is_file():
            raise ValueError("自訂瀏覽器執行檔不存在，請重新選擇瀏覽器。")
        options["executable_path"] = executable
    elif browser == "chromium" and not Path(playwright.chromium.executable_path).is_file():
        print("首次使用 Playwright Chromium，正在安装浏览器……", flush=True)
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    try:
        context = playwright.chromium.launch_persistent_context(str(profile), **options)
    except Exception as exc:
        name = {"msedge": "Microsoft Edge", "chrome": "Google Chrome",
                "chromium": "Playwright Chromium", "custom": "自訂瀏覽器"}.get(browser, browser)
        raise RuntimeError(
            f"無法啟動 {name}。請確認瀏覽器已安裝，或重新執行並選擇其他瀏覽器。\n{exc}"
        ) from exc
    return context


def select_active_page(context: BrowserContext) -> Page:
    pages = [p for p in context.pages if not p.is_closed()]
    if not pages:
        return context.new_page()
    targets = [p for p in pages if "kakuyomu.jp" in p.url or "syosetu.com" in p.url]
    return targets[-1] if targets else pages[-1]
