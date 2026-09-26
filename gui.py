"""
GUI Application for Kakuyomu / Syosetu Local Translator
Simple, intuitive, and modern Tkinter interface.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import queue
import random
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from glossary_manager import (
    VALID_CATEGORIES,
    CandidateTerm,
    GlossaryEntry,
    GlossaryViolation,
    check_glossary_compliance,
    dict_to_entries,
    entries_to_dict,
    export_glossary_to_file,
    extract_candidate_terms,
    find_affected_chapters,
    format_glossary_prompt,
    get_effective_glossary,
    import_glossary_from_file,
    load_project_glossary,
    merge_glossaries,
    save_project_glossary,
    scan_novel_entities,
)
from source_epub import (
    SourceChapter,
    WorkInfo,
    build_source_epub,
    compute_content_hash,
    download_binary,
    download_chapter_images,
    extract_epub_chapters,
    extract_source_chapter,
    extract_work_info,
    get_chapter_id,
    normalize_work_url,
    open_work_page,
    translated_source_epub,
)
from text_importer import (
    COMMON_CHAPTER_PATTERNS,
    DEFAULT_SPLIT_REGEX,
    create_project_from_text,
    detect_text_language,
    get_language_display_name,
    import_text_source,
    import_text_to_epub,
)
from main import (
    CONFIG_PATH,
    APP_DIR,
    Episode,
    TranslationBook,
    TranslationCancelled,
    _translated_chapter,
    _choose_browser,
    _launch_context,
    _select_range,
    add_or_update_project,
    ensure_model,
    installed_models,
    load_config,
    load_json,
    atomic_json,
    proofread_episode,
    safe_name,
    select_active_page,
    translate_episode,
)
from playwright.sync_api import sync_playwright
from epub_append import build_extended_epub, create_project_from_epub, inspect_epub


# Based on the supplied color guide, with a softer, higher-contrast light theme.
UI_PALETTES = {
    "light": {
        "background": "#DDE3EB", "surface": "#EDF1F6", "container": "#CBD5E2",
        "line": "#B6C1D0", "border": "#9CAABD", "text": "#151A29",
        "secondary": "#253247", "muted": "#39485E", "subtle": "#526179",
        "disabled": "#64748B", "accent": "#456AFF", "hover_source": "#1E253C",
        "hover_alpha": 0.05,
    },
    "dark": {
        "background": "#101010", "surface": "#1F2024", "container": "#2E2E31",
        "line": "#3A3B40", "border": "#414248", "text": "#EDEFF2",
        "secondary": "#D5D6DA", "muted": "#C8C8CC", "subtle": "#7D7E80",
        "disabled": "#AAADB5", "accent": "#456AFF", "hover_source": "#FFFFFF",
        "hover_alpha": 0.10,
    },
}


def blend_color(foreground: str, background: str, alpha: float) -> str:
    """Flatten the guide's translucent hover color for opaque Tk widgets."""
    return "#" + "".join(
        f"{round(int(foreground[i:i + 2], 16) * alpha + int(background[i:i + 2], 16) * (1 - alpha)):02X}"
        for i in (1, 3, 5)
    )


class TextRedirector:
    """Redirects console output / log messages to a Tkinter text widget."""
    def __init__(self, text_widget: tk.Text, queue_obj: queue.Queue):
        self.text_widget = text_widget
        self.queue = queue_obj

    def write(self, str_val: str):
        if str_val:
            self.queue.put(("log", str_val))

    def flush(self):
        pass


class TranslatorGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("小說下載與本地 AI 翻譯器 (Kakuyomu / 小說家になろう)")
        self.geometry("1020x760")
        self.minsize(880, 640)

        self.cfg = load_config()
        self.log_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.active_thread: threading.Thread | None = None
        self.stop_requested = False
        self.ui_theme = "light"

        # Apply ttk style
        self.style = ttk.Style(self)
        try:
            self.style.theme_use("clam")
        except Exception:
            pass
        self._configure_styles()

        self._init_ui()
        self._poll_queue()

    def _configure_styles(self):
        p = UI_PALETTES[self.ui_theme]
        self.palette = p
        hover = blend_color(p["hover_source"], p["surface"], p["hover_alpha"])
        accent_hover = blend_color(p["hover_source"], p["accent"], p["hover_alpha"])
        self.configure(background=p["background"])
        self.option_add("*Font", ("Microsoft JhengHei UI", 11))
        for widget in ("Listbox", "Text", "Menu"):
            self.option_add(f"*{widget}.background", p["surface"])
            self.option_add(f"*{widget}.foreground", p["text"])
        self.option_add("*Listbox.selectBackground", p["accent"])
        self.option_add("*Listbox.selectForeground", "#FFFFFF")
        self.option_add("*Text.selectBackground", p["accent"])
        self.option_add("*Text.selectForeground", "#FFFFFF")
        self.option_add("*Text.insertBackground", p["text"])
        self.option_add("*Menu.activeBackground", hover)
        self.option_add("*Menu.activeForeground", p["text"])
        self.option_add("*Menu.disabledForeground", p["disabled"])
        self.option_add("*Toplevel.background", p["background"])
        self.option_add("*TCombobox*Listbox.background", p["surface"])
        self.option_add("*TCombobox*Listbox.foreground", p["text"])
        self.style.configure(".", font=("Microsoft JhengHei UI", 11),
                             background=p["background"], foreground=p["text"],
                             bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"],
                             troughcolor=p["surface"], focuscolor=p["accent"], selectbackground=p["accent"],
                             selectforeground="#FFFFFF")
        self.style.map(".", foreground=[("disabled", p["disabled"])])
        for name in ("TButton", "TMenubutton"):
            self.style.configure(name, padding=(10, 6), background=p["surface"])
            self.style.map(name, background=[("disabled", p["surface"]), ("pressed", hover), ("active", hover)],
                           foreground=[("disabled", p["disabled"]), ("!disabled", p["text"])])
        self.style.configure("TEntry", padding=5, fieldbackground=p["surface"], insertcolor=p["text"])
        self.style.configure("TCombobox", padding=4, fieldbackground=p["surface"],
                             background=p["surface"], arrowcolor=p["muted"])
        self.style.map("TCombobox", fieldbackground=[("readonly", p["surface"])],
                       foreground=[("disabled", p["disabled"]), ("readonly", p["text"])],
                       background=[("active", hover)])
        for name in ("TRadiobutton", "TCheckbutton"):
            self.style.configure(name, indicatorbackground=p["surface"], indicatorforeground=p["text"])
            self.style.map(name, background=[("active", p["background"])],
                           indicatorbackground=[("disabled", p["surface"]), ("selected", p["accent"])],
                           indicatorforeground=[("selected", "#FFFFFF")])
        self.style.configure("TLabelframe", bordercolor=p["border"], borderwidth=1, relief="solid")
        self.style.configure("TLabelframe.Label", foreground=p["secondary"],
                             font=("Microsoft JhengHei UI", 11, "bold"))
        self.style.configure("Muted.TLabel", foreground=p["muted"])
        self.style.configure("Title.TLabel", font=("Microsoft JhengHei UI", 20, "bold"))
        self.style.configure("Accent.TButton", background=p["accent"], foreground="#FFFFFF")
        self.style.map("Accent.TButton",
                       background=[("disabled", p["container"]), ("pressed", accent_hover), ("active", accent_hover)],
                       foreground=[("disabled", p["disabled"]), ("!disabled", "#FFFFFF")])
        self.style.configure("TNotebook", borderwidth=0)
        self.style.configure("TNotebook.Tab", padding=(16, 9), background=p["container"],
                             foreground=p["secondary"], font=("Microsoft JhengHei UI", 11, "bold"))
        self.style.map("TNotebook.Tab", background=[("selected", p["accent"]), ("active", hover)],
                       foreground=[("selected", "#FFFFFF")])
        self.style.configure("Treeview", rowheight=33, background=p["surface"], fieldbackground=p["surface"],
                             foreground=p["text"])
        self.style.configure("Treeview.Heading", padding=(6, 7), background=p["container"], foreground=p["secondary"],
                             font=("Microsoft JhengHei UI", 11, "bold"))
        self.style.map("Treeview.Heading", background=[("active", hover)])
        self.style.map("Treeview", background=[("selected", p["accent"])],
                       foreground=[("selected", "#FFFFFF")])
        self.style.configure("Horizontal.TProgressbar", background=p["accent"], troughcolor=p["surface"],
                             lightcolor=p["accent"], darkcolor=p["accent"])
        self.style.configure("TSeparator", background=p["line"])
        for name in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
            self.style.configure(name, background=p["container"], arrowcolor=p["muted"])
            self.style.map(name, background=[("active", hover), ("pressed", hover)])
        self._recolor_widgets(self, hover)

    def _recolor_widgets(self, parent, hover):
        p = self.palette
        for widget in parent.winfo_children():
            if isinstance(widget, (tk.Text, tk.Listbox)):
                widget.configure(background=p["surface"], foreground=p["text"],
                                 selectbackground=p["accent"], selectforeground="#FFFFFF",
                                 highlightbackground=p["border"], highlightcolor=p["accent"])
                if isinstance(widget, tk.Text):
                    widget.configure(insertbackground=p["text"])
            elif isinstance(widget, tk.Menu):
                widget.configure(background=p["surface"], foreground=p["text"],
                                 activebackground=hover, activeforeground=p["text"], disabledforeground=p["disabled"])
            elif isinstance(widget, tk.Toplevel):
                widget.configure(background=p["background"])
            elif isinstance(widget, ttk.Combobox):
                # A previously opened dropdown keeps its own classic Tk colors.
                popup_list = f"{widget}.popdown.f.l"
                if int(self.tk.call("winfo", "exists", popup_list)):
                    self.tk.call(popup_list, "configure", "-background", p["surface"],
                                 "-foreground", p["text"], "-selectbackground", p["accent"],
                                 "-selectforeground", "#FFFFFF")
            self._recolor_widgets(widget, hover)

    def _toggle_theme(self):
        self.ui_theme = "dark" if self.ui_theme == "light" else "light"
        self._configure_styles()
        self.theme_button.configure(text="淺色模式" if self.ui_theme == "dark" else "深色模式")

    def _init_ui(self):
        # Top banner
        top_frame = ttk.Frame(self, padding=(20, 14))
        top_frame.pack(side=tk.TOP, fill=tk.X)
        self.theme_button = ttk.Button(top_frame, text="深色模式", command=self._toggle_theme)
        self.theme_button.pack(side=tk.RIGHT)

        title_lbl = ttk.Label(
            top_frame,
            text="小說翻譯工作室",
            style="Title.TLabel",
        )
        title_lbl.pack(anchor=tk.W)

        author_lbl = ttk.Label(
            top_frame,
            text="下載或匯入原文，再用本機 AI 製作中文 EPUB。",
            style="Muted.TLabel",
        )
        author_lbl.pack(anchor=tk.W, pady=(3, 0))

        # Main Notebook Tabs
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=20, pady=(4, 12))

        self.tab_download = ttk.Frame(self.notebook, padding=10)
        self.tab_import = ttk.Frame(self.notebook, padding=10)
        self.tab_glossary = ttk.Frame(self.notebook, padding=10)
        self.tab_translate = ttk.Frame(self.notebook, padding=10)
        self.tab_audit = ttk.Frame(self.notebook, padding=10)

        self.notebook.add(self.tab_download, text="下載小說")
        self.notebook.add(self.tab_import, text="匯入檔案")
        self.notebook.add(self.tab_translate, text="翻譯 EPUB")
        self.notebook.add(self.tab_glossary, text="術語表")
        self.notebook.add(self.tab_audit, text="檢查與重譯")

        self._setup_tab_download()
        self._setup_tab_import()
        self._setup_tab_glossary()
        self._setup_tab_translate()
        self._setup_tab_audit()

        # Bottom Global Log / Status Bar
        footer = ttk.Frame(self, padding=(20, 0, 20, 12))
        footer.pack(side=tk.BOTTOM, fill=tk.X, before=self.notebook)
        status_row = ttk.Frame(footer)
        status_row.pack(fill=tk.X)
        self.status_var = tk.StringVar(value="就緒")
        self.status_bar = ttk.Label(status_row, textvariable=self.status_var, anchor=tk.W)
        self.status_bar.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.log_toggle = ttk.Button(status_row, text="顯示日誌", command=self._toggle_log)
        self.log_toggle.pack(side=tk.RIGHT)
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_bar = ttk.Progressbar(footer, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill=tk.X, pady=(8, 0))
        self.log_panel = ttk.Frame(footer, padding=(0, 8, 0, 0))
        bot_frame = self.log_panel

        self.log_text = tk.Text(bot_frame, height=5, wrap=tk.WORD, font=("Microsoft JhengHei UI", 11),
                                bg=self.palette["surface"], fg=self.palette["secondary"],
                                relief=tk.FLAT, padx=10, pady=8)
        log_scroll = ttk.Scrollbar(bot_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    def _toggle_log(self):
        if self.log_panel.winfo_manager():
            self.log_panel.pack_forget()
            self.log_toggle.configure(text="顯示日誌")
        else:
            self.log_panel.pack(fill=tk.X)
            self.log_toggle.configure(text="收合日誌")
            self.log_text.see(tk.END)

    # ==========================================
    # Tab 1: Download Novel
    # ==========================================
    def _setup_tab_download(self):
        f = self.tab_download

        # URL Frame
        url_frame = ttk.LabelFrame(f, text="1 · 貼上作品網址", padding=10)
        url_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(url_frame, text="小說網址：", font=("Microsoft JhengHei UI", 11, "bold")).grid(row=0, column=0, sticky=tk.W, pady=4)
        self.dl_url_var = tk.StringVar(value="https://ncode.syosetu.com/n2027ci/")
        url_entry = ttk.Entry(url_frame, textvariable=self.dl_url_var, width=65)
        url_entry.grid(row=0, column=1, sticky=tk.EW, padx=6, pady=4)
        url_frame.columnconfigure(1, weight=1)

        btn_fetch_toc = ttk.Button(url_frame, text="讀取作品目錄", command=self._action_fetch_toc)
        btn_fetch_toc.grid(row=0, column=2, padx=4, pady=4)

        # Work info display
        info_frame = ttk.LabelFrame(f, text="2 · 選擇下載範圍", padding=10)
        info_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        self.dl_info_lbl = ttk.Label(info_frame, text="尚未讀取作品目錄。請輸入作品網址後點選「讀取作品目錄」。", wraplength=800)
        self.dl_info_lbl.pack(anchor=tk.W, pady=4)

        range_frame = ttk.Frame(info_frame)
        range_frame.pack(fill=tk.X, pady=6)

        ttk.Label(range_frame, text="下載範圍：從第").pack(side=tk.LEFT)
        self.dl_start_var = tk.StringVar(value="1")
        self.dl_start_entry = ttk.Entry(range_frame, textvariable=self.dl_start_var, width=6)
        self.dl_start_entry.pack(side=tk.LEFT, padx=4)

        ttk.Label(range_frame, text="章  到第").pack(side=tk.LEFT)
        self.dl_end_var = tk.StringVar(value="1")
        self.dl_end_entry = ttk.Entry(range_frame, textvariable=self.dl_end_var, width=6)
        self.dl_end_entry.pack(side=tk.LEFT, padx=4)
        ttk.Label(range_frame, text="章（含起止章）").pack(side=tk.LEFT)

        # Chapter Listbox preview
        self.dl_toc_listbox = tk.Listbox(info_frame, height=10, selectmode=tk.EXTENDED, font=("Microsoft JhengHei UI", 11))
        toc_scroll = ttk.Scrollbar(info_frame, orient=tk.VERTICAL, command=self.dl_toc_listbox.yview)
        self.dl_toc_listbox.configure(yscrollcommand=toc_scroll.set)
        toc_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.dl_toc_listbox.pack(fill=tk.BOTH, expand=True, pady=4)

        # Download button
        btn_box = ttk.Frame(f)
        btn_box.pack(fill=tk.X, pady=4)

        self.btn_start_dl = ttk.Button(btn_box, text="下載 EPUB", style="Accent.TButton", command=self._action_start_download)
        self.btn_start_dl.pack(side=tk.RIGHT, padx=4)

        self.current_work: WorkInfo | None = None

    def _action_fetch_toc(self):
        url = self.dl_url_var.get().strip()
        if not url:
            messagebox.showwarning("提示", "請輸入小說網址。")
            return
        try:
            work_url = normalize_work_url(url)
            self.dl_url_var.set(work_url)
        except Exception as exc:
            messagebox.showerror("網址錯誤", str(exc))
            return

        self._set_busy(True, "正在讀取作品目錄……")
        self.dl_toc_listbox.delete(0, tk.END)

        def _worker():
            try:
                with sync_playwright() as p:
                    ctx = _launch_context(p, self.cfg)
                    page = select_active_page(ctx)
                    if "kakuyomu.jp" in work_url:
                        open_work_page(page, work_url, allow_login=True)
                    else:
                        page.goto(work_url, wait_until="domcontentloaded", timeout=120_000)
                    work = extract_work_info(page, work_url)
                    ctx.close()
                self.log_queue.put(("toc_loaded", work))
            except Exception as exc:
                self.log_queue.put(("error", f"讀取目錄失敗：{exc}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _action_start_download(self):
        if not self.current_work:
            messagebox.showwarning("提示", "請先點選「讀取作品目錄」。")
            return
        try:
            start = int(self.dl_start_var.get().strip())
            end = int(self.dl_end_var.get().strip())
            total = len(self.current_work.episodes)
            if not (1 <= start <= end <= total):
                messagebox.showerror("範圍錯誤", f"章節編號範圍無效，請輸入 1 至 {total} 之間的範圍。")
                return
        except ValueError:
            messagebox.showerror("輸入錯誤", "請輸入有效的數字範圍。")
            return

        work = self.current_work
        self._set_busy(True, f"正在下載《{work.title}》第 {start} 至 {end} 章……")

        def _worker():
            try:
                output_root = Path(self.cfg["output_dir"])
                work_dir = output_root / safe_name(work.title) / f"日文原文_{start:04d}-{end:04d}"
                assets_dir = work_dir / "source-assets"
                assets_dir.mkdir(parents=True, exist_ok=True)
                cache_path = work_dir / "source-cache.json"
                cache: dict[str, Any] = load_json(cache_path, {})
                chapters: list[SourceChapter] = []

                # Init project glossary
                eff_entries = get_effective_glossary(self.cfg, work_dir)
                if not (work_dir / "glossary.json").exists() and eff_entries:
                    save_project_glossary(work_dir, eff_entries)

                delay = max(float(self.cfg.get("request_delay_seconds", 8.0)), 8.0)
                selected = work.episodes[start - 1:end]

                with sync_playwright() as p:
                    ctx = _launch_context(p, self.cfg)
                    page = select_active_page(ctx)
                    if "kakuyomu.jp" in work.url:
                        open_work_page(page, work.url, allow_login=True)

                    for idx, item in enumerate(selected, start):
                        if self.stop_requested:
                            self.log_queue.put(("log", "使用者取消下載。\n"))
                            break

                        pct = ((idx - start) / len(selected)) * 100
                        self.log_queue.put(("progress", pct))

                        chapter_url = item["url"]
                        expected_cid = get_chapter_id(chapter_url)
                        record = cache.get(chapter_url) or cache.get(expected_cid)
                        if record and record.get("paragraphs"):
                            curr_hash = record.get("content_hash") or compute_content_hash(record["paragraphs"])
                            chapter = SourceChapter(
                                url=chapter_url,
                                title=record["title"],
                                paragraphs=record["paragraphs"],
                                blocks=record["blocks"],
                                chapter_id=record.get("chapter_id", expected_cid),
                                content_hash=curr_hash,
                            )
                            for image in record.get("images", []):
                                local = work_dir / image["local_path"]
                                if local.exists():
                                    img_dict = dict(image)
                                    img_dict["data"] = local.read_bytes()
                                    chapter.images.append(img_dict)
                            self.log_queue.put(("log", f"[{idx}/{end}] 使用快取：{chapter.title} (ID: {chapter.chapter_id})\n"))
                        else:
                            self.log_queue.put(("log", f"[{idx}/{end}] 下載中：{item['title']}\n"))
                            chapter = extract_source_chapter(page, item["url"], item["title"])
                            download_chapter_images(ctx, chapter)
                            saved_images = []
                            for image in chapter.images:
                                local = assets_dir / image["name"]
                                if not local.exists():
                                    local.write_bytes(image["data"])
                                saved_images.append({k: v for k, v in image.items() if k != "data"} | {
                                    "local_path": str(local.relative_to(work_dir))
                                })
                            cache[item["url"]] = {
                                "chapter_id": chapter.chapter_id,
                                "content_hash": chapter.content_hash,
                                "title": chapter.title,
                                "paragraphs": chapter.paragraphs,
                                "blocks": chapter.blocks,
                                "images": saved_images,
                                "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            }
                            atomic_json(cache_path, cache)
                            time.sleep(delay + random.uniform(1.0, 3.5))
                        chapters.append(chapter)

                    cover = None
                    if work.cover_url:
                        try:
                            cover = download_binary(ctx, work.cover_url)
                        except Exception as exc:
                            self.log_queue.put(("log", f"封面下載失敗：{exc}\n"))

                    out_epub = work_dir / f"{safe_name(work.title)}_日文_{start:04d}-{end:04d}.epub"
                    build_source_epub(work, chapters, out_epub, cover=cover)
                    atomic_json(work_dir / "source-project.json", {
                        "work_url": work.url, "title": work.title, "author": work.author,
                        "start": start, "end": end, "chapter_urls": [c.url for c in chapters],
                    })
                    ctx.close()

                self.log_queue.put(("progress", 100.0))
                self.log_queue.put(("download_complete", str(out_epub)))
            except Exception as exc:
                self.log_queue.put(("error", f"下載失敗：{exc}"))

        threading.Thread(target=_worker, daemon=True).start()

    # ==========================================
    # Tab 2: TXT / Markdown Batch Importer
    # ==========================================
    def _setup_tab_import(self):
        f = self.tab_import

        # Source Selection Frame
        src_frame = ttk.LabelFrame(f, text="匯入文字或電子書 · TXT / MD / UMD / JAR", padding=10)
        src_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(src_frame, text="檔案/資料夾：").grid(row=0, column=0, sticky=tk.W, pady=4)
        self.import_path_var = tk.StringVar()
        entry_p = ttk.Entry(src_frame, textvariable=self.import_path_var, width=50)
        entry_p.grid(row=0, column=1, sticky=tk.EW, padx=6, pady=4)
        src_frame.columnconfigure(1, weight=1)

        # Merged source selection button
        btn_src_menu = ttk.Menubutton(src_frame, text=" 📂 選擇來源 ▾ ")
        src_menu = tk.Menu(btn_src_menu, tearoff=0)
        src_menu.add_command(label="📄 選擇文字檔 (*.txt, *.md)...", command=self._action_select_import_file)
        src_menu.add_command(label="📱 選擇 UMD 電子書 (*.umd)...", command=self._action_select_import_umd)
        src_menu.add_command(label="☕ 選擇 JAR 電子書 (*.jar)...", command=self._action_select_import_jar)
        src_menu.add_separator()
        src_menu.add_command(label="📁 選擇多話章節資料夾...", command=self._action_select_import_dir)
        btn_src_menu["menu"] = src_menu
        btn_src_menu.grid(row=0, column=2, padx=4, pady=4)

        # Meta & Split options
        meta_frame = ttk.Frame(src_frame)
        meta_frame.grid(row=1, column=0, columnspan=3, sticky=tk.EW, pady=6)

        ttk.Label(meta_frame, text="作品名稱：").grid(row=0, column=0, sticky=tk.W, pady=4)
        self.import_title_var = tk.StringVar()
        ttk.Entry(meta_frame, textvariable=self.import_title_var, width=16).grid(row=0, column=1, sticky=tk.W, padx=4, pady=4)

        ttk.Label(meta_frame, text="作者名稱：").grid(row=0, column=2, sticky=tk.W, padx=(8, 0), pady=4)
        self.import_author_var = tk.StringVar(value="未知作者")
        ttk.Entry(meta_frame, textvariable=self.import_author_var, width=12).grid(row=0, column=3, sticky=tk.W, padx=4, pady=4)

        ttk.Label(meta_frame, text="檔案編碼：").grid(row=0, column=4, sticky=tk.W, padx=(8, 0), pady=4)
        self.import_encoding_var = tk.StringVar(value="🤖 自動偵測編碼")
        cb_enc = ttk.Combobox(
            meta_frame,
            textvariable=self.import_encoding_var,
            values=[
                "🤖 自動偵測編碼",
                "UTF-8",
                "UTF-8-SIG (含BOM)",
                "Big5 (繁體中文)",
                "GB18030 / GBK (簡體中文)",
                "Shift-JIS / CP932 (日文)",
                "EUC-JP (日文)",
                "UTF-16 LE",
                "UTF-16 BE",
            ],
            state="readonly",
            width=18,
        )
        cb_enc.grid(row=0, column=5, sticky=tk.W, padx=4, pady=4)

        ttk.Label(meta_frame, text="章節切分規則：").grid(row=1, column=0, sticky=tk.W, pady=4)
        self.import_rule_var = tk.StringVar(value="預設智能正則 (話/章/卷/Chapter/序章/番外等)")
        cb_rule = ttk.Combobox(
            meta_frame,
            textvariable=self.import_rule_var,
            values=[
                "預設智能正則 (話/章/卷/Chapter/序章/番外等)",
                "Markdown 標題 (#, ##)",
                "自訂正則表達式",
            ],
            state="readonly",
            width=32,
        )
        cb_rule.grid(row=1, column=1, columnspan=2, sticky=tk.W, padx=4, pady=4)

        ttk.Label(meta_frame, text="語系設定：").grid(row=1, column=3, sticky=tk.W, padx=(8, 0), pady=4)
        self.import_lang_var = tk.StringVar(value="🤖 自動偵測語系")
        cb_lang = ttk.Combobox(
            meta_frame,
            textvariable=self.import_lang_var,
            values=[
                "🤖 自��偵測語系",
                "🇹🇼 中文小說 (繁體中文)",
                "🇨🇳 中文小說 (簡體中文)",
                "🇯🇵 日文原文",
            ],
            state="readonly",
            width=18,
        )
        cb_lang.grid(row=1, column=4, columnspan=2, sticky=tk.W, padx=4, pady=4)

        self.import_sort_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(meta_frame, text="🔀 依章節序號排序", variable=self.import_sort_var).grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=2)

        self.import_custom_regex_var = tk.StringVar()
        self.entry_custom_regex = ttk.Entry(meta_frame, textvariable=self.import_custom_regex_var, width=32)
        self.entry_custom_regex.grid(row=2, column=2, columnspan=2, sticky=tk.W, padx=4, pady=2)
        self.entry_custom_regex.grid_remove()

        def _on_rule_change(evt=None):
            if self.import_rule_var.get() == "自訂正則表達式":
                self.entry_custom_regex.grid()
            else:
                self.entry_custom_regex.grid_remove()
        cb_rule.bind("<<ComboboxSelected>>", _on_rule_change)

        # Chapter Preview Frame
        prev_frame = ttk.LabelFrame(f, text="章節解析預覽列表", padding=8)
        prev_frame.pack(fill=tk.BOTH, expand=True, pady=4)

        tree_cols = ("index", "title", "paragraphs", "chars", "url")
        self.import_tree = ttk.Treeview(prev_frame, columns=tree_cols, show="headings", height=9)
        self.import_tree.heading("index", text="序號")
        self.import_tree.heading("title", text="章節標題")
        self.import_tree.heading("paragraphs", text="段落數")
        self.import_tree.heading("chars", text="預估字數")
        self.import_tree.heading("url", text="來源識別")

        self.import_tree.column("index", width=55, anchor=tk.CENTER)
        self.import_tree.column("title", width=320, anchor=tk.W)
        self.import_tree.column("paragraphs", width=80, anchor=tk.CENTER)
        self.import_tree.column("chars", width=90, anchor=tk.CENTER)
        self.import_tree.column("url", width=200, anchor=tk.W)

        tree_scroll = ttk.Scrollbar(prev_frame, orient=tk.VERTICAL, command=self.import_tree.yview)
        self.import_tree.configure(yscrollcommand=tree_scroll.set)
        self.import_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.import_status_lbl = ttk.Label(f, text="請選擇檔案或資料夾後點選「預覽解析章節」。", style="Muted.TLabel")
        self.import_status_lbl.pack(fill=tk.X, pady=(2, 4))

        # Action Buttons
        action_bar = ttk.Frame(f)
        action_bar.pack(fill=tk.X, pady=4)

        ttk.Button(action_bar, text="🔍 預覽解析章節", command=self._action_import_preview).pack(side=tk.LEFT, padx=4)
        ttk.Button(action_bar, text="✨ 掃描候選術語", command=self._action_import_extract_candidates).pack(side=tk.LEFT, padx=4)

        # 1-Click Pack EPUB Button
        ttk.Button(action_bar, text="📦 一鍵打包為 EPUB 電子書", command=self._action_import_to_epub).pack(side=tk.RIGHT, padx=4)

        # Merged Export & Processing Menu
        btn_export_menu = ttk.Menubutton(action_bar, text=" 🚀 專案導出與處理 ▾ ")
        export_menu = tk.Menu(btn_export_menu, tearoff=0)
        export_menu.add_command(label="📋 導入為中文專案 (術語稽核/潤色/接續)", command=self._action_import_as_chinese_project)
        export_menu.add_command(label="⚡ 建立專案並翻譯 (日翻中)", command=self._action_import_and_translate)
        export_menu.add_command(label="📁 僅建立空白翻譯專案", command=self._action_import_create_project_only)
        btn_export_menu["menu"] = export_menu
        btn_export_menu.pack(side=tk.RIGHT, padx=4)

    def _action_select_import_file(self):
        fpath = filedialog.askopenfilename(
            title="選擇文字檔或電子書",
            filetypes=[
                ("電子書與文字檔 (*.txt, *.md, *.umd, *.jar)", "*.txt;*.md;*.markdown;*.umd;*.jar"),
                ("純文字 / Markdown 檔案 (*.txt, *.md)", "*.txt;*.md;*.markdown"),
                ("UMD 電子書 (*.umd)", "*.umd"),
                ("JAR 電子書 (*.jar)", "*.jar"),
                ("所有檔案", "*.*"),
            ]
        )
        if fpath:
            self.import_path_var.set(fpath)
            p = Path(fpath)
            if not self.import_title_var.get():
                self.import_title_var.set(p.stem)
            self._action_import_preview()

    def _action_select_import_umd(self):
        fpath = filedialog.askopenfilename(
            title="選擇 UMD 電子書檔案",
            filetypes=[("UMD 電子書 (*.umd)", "*.umd"), ("所有檔案", "*.*")]
        )
        if fpath:
            self.import_path_var.set(fpath)
            p = Path(fpath)
            if not self.import_title_var.get():
                self.import_title_var.set(p.stem)
            self._action_import_preview()

    def _action_select_import_jar(self):
        fpath = filedialog.askopenfilename(
            title="選擇 JAR 電子書檔案",
            filetypes=[("JAR 電子書 (*.jar)", "*.jar"), ("所有檔案", "*.*")]
        )
        if fpath:
            self.import_path_var.set(fpath)
            p = Path(fpath)
            if not self.import_title_var.get():
                self.import_title_var.set(p.stem)
            self._action_import_preview()

    def _action_select_import_dir(self):
        dpath = filedialog.askdirectory(
            title="選擇包含章節文字檔的資料夾",
        )
        if dpath:
            self.import_path_var.set(dpath)
            p = Path(dpath)
            if not self.import_title_var.get():
                self.import_title_var.set(p.name)
            self._action_import_preview()

    def _get_import_regex(self) -> str | None:
        rule = self.import_rule_var.get()
        if rule == "Markdown 標題 (#, ##)":
            return r"^\s*#{1,3}\s+(.+)$"
        elif rule == "自訂正則表達式":
            custom = self.import_custom_regex_var.get().strip()
            return custom if custom else None
        return None

    def _get_import_encoding(self) -> str | None:
        enc_sel = self.import_encoding_var.get()
        if "Big5" in enc_sel:
            return "big5"
        elif "GB18030" in enc_sel or "GBK" in enc_sel:
            return "gb18030"
        elif "Shift-JIS" in enc_sel or "CP932" in enc_sel:
            return "cp932"
        elif "EUC-JP" in enc_sel:
            return "euc-jp"
        elif "UTF-16 LE" in enc_sel:
            return "utf-16le"
        elif "UTF-16 BE" in enc_sel:
            return "utf-16be"
        elif "UTF-8-SIG" in enc_sel:
            return "utf-8-sig"
        elif enc_sel == "UTF-8":
            return "utf-8"
        return None

    def _action_import_preview(self):
        path_str = self.import_path_var.get().strip()
        if not path_str or not Path(path_str).exists():
            messagebox.showwarning("提示", "請先選擇有效的檔案或資料夾路徑。")
            return

        src_p = Path(path_str)
        t_input = self.import_title_var.get().strip() or (src_p.stem if src_p.is_file() else src_p.name)
        self.import_title_var.set(t_input)
        a_input = self.import_author_var.get().strip() or "未知作者"
        pattern = self._get_import_regex()
        enc = self._get_import_encoding()
        do_sort = self.import_sort_var.get()

        for item in self.import_tree.get_children():
            self.import_tree.delete(item)

        try:
            work, chapters = import_text_source(
                src_p,
                title=t_input,
                author=a_input,
                split_pattern=pattern,
                encoding=enc,
                sort_chapters=do_sort,
            )
            self.imported_work = work
            self.imported_chapters = chapters
            if work.title and work.title != t_input:
                self.import_title_var.set(work.title)
            if work.author and work.author != "未知作者":
                self.import_author_var.set(work.author)

            total_chars = 0
            sample_paras = []
            for idx, ch in enumerate(chapters, 1):
                c_cnt = sum(len(p) for p in ch.paragraphs)
                total_chars += c_cnt
                if len(sample_paras) < 30:
                    sample_paras.extend(ch.paragraphs[:3])
                self.import_tree.insert("", tk.END, values=(
                    idx, ch.title, len(ch.paragraphs), f"{c_cnt:,} 字", ch.url
                ))
            detected_lang = detect_text_language(sample_paras)
            lang_label = get_language_display_name(detected_lang)
            if self.import_lang_var.get() == "🤖 自動偵測語系":
                if detected_lang == "ja":
                    self.import_lang_var.set("🇯🇵 日文原文")
                elif detected_lang == "zh-Hans":
                    self.import_lang_var.set("🇨🇳 中文小說 (簡體中文)")
                else:
                    self.import_lang_var.set("🇹🇼 中文小說 (繁體中文)")
            self.import_status_lbl.config(
                text=f"成功解析《{work.title}》（作者：{work.author}，語系：{lang_label}），共 {len(chapters)} 章，累計約 {total_chars:,} 字。"
            )
            self.log_text.insert(tk.END, f"批次導入解析完成：共 {len(chapters)} 章，約 {total_chars:,} 字（偵測為 {lang_label}）。\n")
        except Exception as exc:
            messagebox.showerror("解析失敗", f"無法解析文字來源：{exc}")

    def _action_import_to_epub(self):
        if not hasattr(self, "imported_chapters") or not self.imported_chapters:
            self._action_import_preview()
            if not hasattr(self, "imported_chapters") or not self.imported_chapters:
                return

        path_str = self.import_path_var.get().strip()
        src_p = Path(path_str)
        t_input = self.import_title_var.get().strip() or src_p.stem
        a_input = self.import_author_var.get().strip() or "未知作者"
        pattern = self._get_import_regex()
        enc = self._get_import_encoding()
        do_sort = self.import_sort_var.get()

        lang_sel = self.import_lang_var.get()
        if "日" in lang_sel:
            lang_code = "日文"
            out_name = f"{safe_name(t_input)}_日文原文.epub"
        elif "簡" in lang_sel:
            lang_code = "簡體中文"
            out_name = f"{safe_name(t_input)}_中文.epub"
        elif "繁" in lang_sel:
            lang_code = "繁體中文"
            out_name = f"{safe_name(t_input)}_中文.epub"
        else:
            lang_code = "auto"
            out_name = f"{safe_name(t_input)}.epub"

        out_p = Path(self.cfg.get("output_dir", "output")) / out_name
        self._set_busy(True, f"正在打包生成 EPUB：{out_name}……")

        def _worker():
            try:
                res_epub = import_text_to_epub(
                    src_p,
                    out_p,
                    title=t_input,
                    author=a_input,
                    language=lang_code,
                    split_pattern=pattern,
                    encoding=enc,
                    sort_chapters=do_sort,
                )
                self.log_queue.put(("download_complete", str(res_epub.resolve())))
            except Exception as exc:
                self.log_queue.put(("error", f"EPUB 打包失敗：{exc}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _action_import_as_chinese_project(self):
        if not hasattr(self, "imported_chapters") or not self.imported_chapters:
            self._action_import_preview()
            if not hasattr(self, "imported_chapters") or not self.imported_chapters:
                return

        path_str = self.import_path_var.get().strip()
        src_p = Path(path_str)
        t_input = self.import_title_var.get().strip() or src_p.stem
        a_input = self.import_author_var.get().strip() or "未知作者"
        pattern = self._get_import_regex()
        enc = self._get_import_encoding()
        do_sort = self.import_sort_var.get()
        lang_sel = self.import_lang_var.get()
        target_lang = "簡體中文" if "簡" in lang_sel else "繁體中文"

        work_dir = Path(self.cfg.get("output_dir", "output")) / safe_name(f"{t_input}_中文專案")
        try:
            create_project_from_text(
                src_p,
                work_dir,
                title=t_input,
                author=a_input,
                language=target_lang,
                split_pattern=pattern,
                as_translated=True,
                encoding=enc,
                sort_chapters=do_sort,
            )
            out_epub = Path(self.cfg.get("output_dir", "output")) / f"{safe_name(t_input)}_中文.epub"
            if not out_epub.exists():
                import_text_to_epub(
                    src_p,
                    out_epub,
                    title=t_input,
                    author=a_input,
                    language=target_lang,
                    split_pattern=pattern,
                    encoding=enc,
                    sort_chapters=do_sort,
                )

            ans = messagebox.askyesno(
                "中文專案建立成功",
                f"中文專案已建立完成（已載入 {len(self.imported_chapters)} 章譯文）：\n{work_dir.resolve()}\n\n是否立即切換至「一致性檢查與局部重譯」進行術語合規審核？",
            )
            self.log_text.insert(tk.END, f"已建立中文專案：{work_dir.resolve()}\n")
            if ans:
                self.audit_proj_var.set(str(work_dir.resolve()))
                self.notebook.select(self.tab_audit)
                self._action_audit_compliance()
        except Exception as exc:
            messagebox.showerror("建立專案失敗", str(exc))

    def _action_import_create_project_only(self):
        if not hasattr(self, "imported_chapters") or not self.imported_chapters:
            self._action_import_preview()
            if not hasattr(self, "imported_chapters") or not self.imported_chapters:
                return

        path_str = self.import_path_var.get().strip()
        src_p = Path(path_str)
        t_input = self.import_title_var.get().strip() or src_p.stem
        a_input = self.import_author_var.get().strip() or "未知作者"
        pattern = self._get_import_regex()
        enc = self._get_import_encoding()
        do_sort = self.import_sort_var.get()

        work_dir = Path(self.cfg.get("output_dir", "output")) / safe_name(f"{t_input}_中文翻译")
        try:
            create_project_from_text(
                src_p,
                work_dir,
                title=t_input,
                author=a_input,
                split_pattern=pattern,
                as_translated=False,
                encoding=enc,
                sort_chapters=do_sort,
            )
            out_epub = Path(self.cfg.get("output_dir", "output")) / f"{safe_name(t_input)}_日文原文.epub"
            if not out_epub.exists():
                import_text_to_epub(
                    src_p,
                    out_epub,
                    title=t_input,
                    author=a_input,
                    split_pattern=pattern,
                    encoding=enc,
                    sort_chapters=do_sort,
                )
            messagebox.showinfo("專案建立成功", f"翻譯專案已建立完成：\n{work_dir.resolve()}\n\n您可隨時切換至「本機 AI 翻譯」或「術語表管理」進行處理。")
            self.log_text.insert(tk.END, f"已建立翻譯專案：{work_dir.resolve()}\n")
        except Exception as exc:
            messagebox.showerror("建立專案失敗", str(exc))

    def _action_import_and_translate(self):
        if not hasattr(self, "imported_chapters") or not self.imported_chapters:
            self._action_import_preview()
            if not hasattr(self, "imported_chapters") or not self.imported_chapters:
                return

        path_str = self.import_path_var.get().strip()
        src_p = Path(path_str)
        t_input = self.import_title_var.get().strip() or src_p.stem
        a_input = self.import_author_var.get().strip() or "未知作者"
        pattern = self._get_import_regex()
        enc = self._get_import_encoding()
        do_sort = self.import_sort_var.get()

        out_name = f"{safe_name(t_input)}_日文原文.epub"
        out_p = Path(self.cfg.get("output_dir", "output")) / out_name

        try:
            if not out_p.exists():
                import_text_to_epub(
                    src_p,
                    out_p,
                    title=t_input,
                    author=a_input,
                    split_pattern=pattern,
                    encoding=enc,
                    sort_chapters=do_sort,
                )
            self.tr_epub_var.set(str(out_p.resolve()))
            self.notebook.select(self.tab_translate)
            self._action_start_translate()
        except Exception as exc:
            messagebox.showerror("轉換失敗", str(exc))

    def _action_import_extract_candidates(self):
        if not hasattr(self, "imported_chapters") or not self.imported_chapters:
            self._action_import_preview()
            if not hasattr(self, "imported_chapters") or not self.imported_chapters:
                return

        self._set_busy(True, "正在分析導入文本並統計候選名詞……")
        texts = []
        for ch in self.imported_chapters:
            texts.extend(ch.paragraphs)

        def _worker():
            try:
                candidates = extract_candidate_terms(texts, min_count=2, top_n=120)
                self.log_queue.put(("candidates_ready", candidates))
            except Exception as exc:
                self.log_queue.put(("error", f"提取候選詞失敗：{exc}"))

        threading.Thread(target=_worker, daemon=True).start()

    # ==========================================
    # Tab 3: Glossary Manager
    # ==========================================
    def _setup_tab_glossary(self):
        f = self.tab_glossary

        # Top bar: Scope selector (Global vs Project)
        top_bar = ttk.LabelFrame(f, text="術語表範圍與設定", padding=8)
        top_bar.pack(fill=tk.X, pady=(0, 6))

        self.glossary_scope_var = tk.StringVar(value="global")
        rb_global = ttk.Radiobutton(top_bar, text="全域術語表 (config.json)", variable=self.glossary_scope_var, value="global", command=self._refresh_glossary_table)
        rb_global.pack(side=tk.LEFT, padx=6)

        rb_proj = ttk.Radiobutton(top_bar, text="特定作品獨立術語表 (glossary.json)", variable=self.glossary_scope_var, value="project", command=self._refresh_glossary_table)
        rb_proj.pack(side=tk.LEFT, padx=6)

        self.proj_dir_var = tk.StringVar(value="")
        btn_choose_proj = ttk.Button(top_bar, text="選擇作品資料夾...", command=self._action_select_glossary_project)
        btn_choose_proj.pack(side=tk.LEFT, padx=6)

        self.lbl_active_glossary_path = ttk.Label(top_bar, text="全域 config.json", style="Muted.TLabel")
        self.lbl_active_glossary_path.pack(side=tk.LEFT, padx=8)

        # Search & Filter
        filter_bar = ttk.Frame(f)
        filter_bar.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(filter_bar, text="搜尋：").pack(side=tk.LEFT)
        self.glossary_search_var = tk.StringVar()
        self.glossary_search_var.trace_add("write", lambda *_: self._filter_glossary_view())
        entry_search = ttk.Entry(filter_bar, textvariable=self.glossary_search_var, width=25)
        entry_search.pack(side=tk.LEFT, padx=4)

        ttk.Label(filter_bar, text="類別篩選：").pack(side=tk.LEFT, padx=(12, 0))
        self.category_filter_var = tk.StringVar(value="全部")
        cb_cat = ttk.Combobox(filter_bar, textvariable=self.category_filter_var, values=["全部"] + list(VALID_CATEGORIES), state="readonly", width=12)
        cb_cat.pack(side=tk.LEFT, padx=4)
        cb_cat.bind("<<ComboboxSelected>>", lambda *_: self._filter_glossary_view())

        # Action Buttons
        btn_box = ttk.Frame(f)
        btn_box.pack(fill=tk.X, pady=(0, 6))

        # Term editing actions
        ttk.Button(btn_box, text="➕ 新增術語", command=self._action_add_term).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_box, text="✏️ 編輯選中", command=self._action_edit_term).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_box, text="🗑️ 刪除選中", command=self._action_delete_term).pack(side=tk.LEFT, padx=2)
        ttk.Separator(btn_box, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)

        # Merged Extraction & Recognition Menu
        btn_extract_menu = ttk.Menubutton(btn_box, text=" 🔍 術語提取/識別 ▾ ")
        extract_menu = tk.Menu(btn_extract_menu, tearoff=0)
        extract_menu.add_command(label="⚡ 快速提取候選術語 (秒級統計)", command=self._action_extract_candidates_gui)
        extract_menu.add_command(label="🤖 AI 深度實體識別 (Ollama)", command=self._action_ai_scan_entities_gui)
        btn_extract_menu["menu"] = extract_menu
        btn_extract_menu.pack(side=tk.LEFT, padx=2)

        # Merged Import & Export Menu
        btn_io_menu = ttk.Menubutton(btn_box, text=" 📁 匯入/匯出 ▾ ")
        io_menu = tk.Menu(btn_io_menu, tearoff=0)
        io_menu.add_command(label="📥 匯入術語表 (CSV / Excel / JSON)...", command=self._action_import_glossary_gui)
        io_menu.add_command(label="📤 匯出術語表 (Excel / CSV / JSON)...", command=self._action_export_glossary_gui)
        btn_io_menu["menu"] = io_menu
        btn_io_menu.pack(side=tk.LEFT, padx=2)

        ttk.Button(btn_box, text="💾 儲存更新", command=self._action_save_glossary_gui).pack(side=tk.RIGHT, padx=2)

        # Table (Treeview)
        table_frame = ttk.Frame(f)
        table_frame.pack(fill=tk.BOTH, expand=True)

        columns = ("source", "target", "category", "note")
        self.glossary_tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        self.glossary_tree.heading("source", text="日文原文 (Source)")
        self.glossary_tree.heading("target", text="中文譯名 (Target)")
        self.glossary_tree.heading("category", text="類別 (Category)")
        self.glossary_tree.heading("note", text="備註說明 (Note)")

        self.glossary_tree.column("source", width=220, anchor=tk.W)
        self.glossary_tree.column("target", width=220, anchor=tk.W)
        self.glossary_tree.column("category", width=120, anchor=tk.CENTER)
        self.glossary_tree.column("note", width=300, anchor=tk.W)

        tree_scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.glossary_tree.yview)
        self.glossary_tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.glossary_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.glossary_tree.bind("<Double-1>", lambda *_: self._action_edit_term())

        self.current_glossary_entries: list[GlossaryEntry] = []
        self._refresh_glossary_table()

    def _action_select_glossary_project(self):
        selected = filedialog.askdirectory(title="選擇作品專案資料夾", initialdir=self.cfg.get("output_dir", "output"))
        if selected:
            self.proj_dir_var.set(selected)
            self.glossary_scope_var.set("project")
            self._refresh_glossary_table()

    def _get_active_glossary_path(self) -> Path:
        if self.glossary_scope_var.get() == "project" and self.proj_dir_var.get():
            return Path(self.proj_dir_var.get()) / "glossary.json"
        return CONFIG_PATH

    def _refresh_glossary_table(self):
        target_path = self._get_active_glossary_path()
        self.lbl_active_glossary_path.config(text=str(target_path.resolve()))

        if self.glossary_scope_var.get() == "project":
            if self.proj_dir_var.get():
                p = Path(self.proj_dir_var.get())
                self.current_glossary_entries = load_project_glossary(p)
                if not self.current_glossary_entries and not (p / "glossary.json").exists():
                    # fallback to global
                    self.current_glossary_entries = dict_to_entries(self.cfg.get("glossary", {}))
            else:
                self.current_glossary_entries = []
        else:
            self.current_glossary_entries = dict_to_entries(self.cfg.get("glossary", {}))

        self._filter_glossary_view()

    def _filter_glossary_view(self):
        for item in self.glossary_tree.get_children():
            self.glossary_tree.delete(item)

        q = self.glossary_search_var.get().strip().lower()
        cat_filter = self.category_filter_var.get()

        for e in self.current_glossary_entries:
            if cat_filter != "全部" and e.category != cat_filter:
                continue
            if q and (q not in e.source.lower() and q not in e.target.lower() and q not in e.note.lower()):
                continue
            self.glossary_tree.insert("", tk.END, values=(e.source, e.target, e.category, e.note))

    def _action_add_term(self):
        self._term_edit_dialog(None)

    def _action_edit_term(self):
        selected = self.glossary_tree.selection()
        if not selected:
            messagebox.showinfo("提示", "請先點選要編輯的條目。")
            return
        vals = self.glossary_tree.item(selected[0], "values")
        entry = GlossaryEntry(source=vals[0], target=vals[1], category=vals[2], note=vals[3])
        self._term_edit_dialog(entry)

    def _action_delete_term(self):
        selected = self.glossary_tree.selection()
        if not selected:
            messagebox.showinfo("提示", "請先點選要刪除的條目。")
            return
        if not messagebox.askyesno("確認刪除", f"確認刪除選中的 {len(selected)} 筆術語？"):
            return

        to_remove = set()
        for sel in selected:
            vals = self.glossary_tree.item(sel, "values")
            to_remove.add(vals[0])

        self.current_glossary_entries = [e for e in self.current_glossary_entries if e.source not in to_remove]
        self._filter_glossary_view()
        self._action_save_glossary_gui(silent=True)

    def _term_edit_dialog(self, entry: GlossaryEntry | None):
        dialog = tk.Toplevel(self)
        dialog.title("新增/編輯術語" if entry else "新增術語")
        dialog.geometry("450x260")
        dialog.transient(self)
        dialog.grab_set()

        frm = ttk.Frame(dialog, padding=15)
        frm.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frm, text="日文原文：").grid(row=0, column=0, sticky=tk.W, pady=4)
        src_var = tk.StringVar(value=entry.source if entry else "")
        entry_src = ttk.Entry(frm, textvariable=src_var, width=32)
        entry_src.grid(row=0, column=1, sticky=tk.EW, pady=4)

        ttk.Label(frm, text="中文譯名：").grid(row=1, column=0, sticky=tk.W, pady=4)
        tgt_var = tk.StringVar(value=entry.target if entry else "")
        entry_tgt = ttk.Entry(frm, textvariable=tgt_var, width=32)
        entry_tgt.grid(row=1, column=1, sticky=tk.EW, pady=4)

        ttk.Label(frm, text="分類：").grid(row=2, column=0, sticky=tk.W, pady=4)
        cat_var = tk.StringVar(value=entry.category if entry else "特殊設定")
        cb_cat = ttk.Combobox(frm, textvariable=cat_var, values=list(VALID_CATEGORIES), state="readonly", width=30)
        cb_cat.grid(row=2, column=1, sticky=tk.EW, pady=4)

        ttk.Label(frm, text="備註說明：").grid(row=3, column=0, sticky=tk.W, pady=4)
        note_var = tk.StringVar(value=entry.note if entry else "")
        entry_note = ttk.Entry(frm, textvariable=note_var, width=32)
        entry_note.grid(row=3, column=1, sticky=tk.EW, pady=4)

        def _save():
            s = src_var.get().strip()
            t = tgt_var.get().strip()
            c = cat_var.get().strip()
            n = note_var.get().strip()
            if not s or not t:
                messagebox.showerror("錯誤", "日文原文與中文譯名皆不可為空。")
                return
            new_e = GlossaryEntry(source=s, target=t, category=c, note=n)
            # update
            self.current_glossary_entries = merge_glossaries(self.current_glossary_entries, [new_e], overwrite=True)
            self._filter_glossary_view()
            self._action_save_glossary_gui(silent=True)
            dialog.destroy()

        btn_box = ttk.Frame(frm)
        btn_box.grid(row=4, column=0, columnspan=2, pady=(15, 0))
        ttk.Button(btn_box, text="確定儲存", command=_save).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_box, text="取消", command=dialog.destroy).pack(side=tk.LEFT, padx=6)

    def _action_save_glossary_gui(self, silent: bool = False):
        target_path = self._get_active_glossary_path()
        if self.glossary_scope_var.get() == "project" and self.proj_dir_var.get():
            save_project_glossary(Path(self.proj_dir_var.get()), self.current_glossary_entries)
            if not silent:
                messagebox.showinfo("成功", f"已成功儲存作品專屬術語表：\n{target_path}")
        else:
            self.cfg["glossary"] = entries_to_dict(self.current_glossary_entries)
            atomic_json(CONFIG_PATH, self.cfg)
            if not silent:
                messagebox.showinfo("成功", f"已成功更新至全域設定檔：\n{CONFIG_PATH}")

    def _action_import_glossary_gui(self):
        fpath = filedialog.askopenfilename(
            title="選擇術語表檔案",
            filetypes=[("支援格式", "*.csv;*.xlsx;*.xls;*.json"), ("CSV 檔案", "*.csv"), ("Excel 試算表", "*.xlsx;*.xls"), ("JSON 檔案", "*.json"), ("所有檔案", "*.*")]
        )
        if not fpath:
            return
        try:
            entries = import_glossary_from_file(Path(fpath))
            if not entries:
                messagebox.showinfo("提示", "檔案中未讀取到有效術語條目。")
                return
            self.current_glossary_entries = merge_glossaries(self.current_glossary_entries, entries, overwrite=True)
            self._filter_glossary_view()
            self._action_save_glossary_gui(silent=True)
            messagebox.showinfo("匯入成功", f"成功匯入 {len(entries)} 筆術語條目！")
        except Exception as exc:
            messagebox.showerror("匯入失敗", str(exc))

    def _action_export_glossary_gui(self):
        if not self.current_glossary_entries:
            messagebox.showinfo("提示", "目前術語表為空，無法匯出。")
            return
        fpath = filedialog.asksaveasfilename(
            title="匯出術語表",
            defaultextension=".xlsx",
            filetypes=[("Excel 試算表 (*.xlsx)", "*.xlsx"), ("CSV 檔案 (*.csv)", "*.csv"), ("JSON 檔案 (*.json)", "*.json")]
        )
        if not fpath:
            return
        try:
            export_glossary_to_file(self.current_glossary_entries, Path(fpath))
            messagebox.showinfo("匯出成功", f"術語表已匯出至：\n{fpath}")
        except Exception as exc:
            messagebox.showerror("匯出失敗", str(exc))

    def _action_extract_candidates_gui(self):
        # Pick source: EPUB or project dir
        fpath = filedialog.askopenfilename(
            title="選擇要提取候選名詞的日文 EPUB 電子書",
            filetypes=[("EPUB 電子書", "*.epub"), ("所有檔案", "*.*")]
        )
        if not fpath:
            return
        self._set_busy(True, "正在提取日文文本並統計候選術語……")

        def _worker():
            try:
                tmp = Path(self.cfg["output_dir"]) / ".tmp_extract"
                tmp.mkdir(parents=True, exist_ok=True)
                _, chapters = extract_epub_chapters(Path(fpath), tmp)
                texts = []
                for ch in chapters:
                    texts.extend(ch.paragraphs)
                candidates = extract_candidate_terms(texts, min_count=2, top_n=100)
                self.log_queue.put(("candidates_ready", candidates))
            except Exception as exc:
                self.log_queue.put(("error", f"提取候選詞失敗：{exc}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _action_ai_scan_entities_gui(self):
        fpath = filedialog.askopenfilename(
            title="選擇要 AI 深度識別實體的日文 EPUB 電子書",
            filetypes=[("EPUB 電子書", "*.epub"), ("所有檔案", "*.*")]
        )
        if not fpath:
            return
        self._set_busy(True, "正在採樣內文並由 Ollama AI 深度識別 7 大分類專有名詞……")

        def _worker():
            try:
                tmp = Path(self.cfg["output_dir"]) / ".tmp_scan"
                tmp.mkdir(parents=True, exist_ok=True)
                _, chapters = extract_epub_chapters(Path(fpath), tmp)
                texts = []
                for ch in chapters:
                    texts.extend(ch.paragraphs)
                ensure_model(self.cfg, interactive=False)
                entries = scan_novel_entities(texts, self.cfg)
                self.log_queue.put(("ai_entities_ready", entries))
            except Exception as exc:
                self.log_queue.put(("error", f"AI 識別實體失敗：{exc}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _candidate_review_dialog(self, candidates: list[CandidateTerm] | list[GlossaryEntry]):
        dialog = tk.Toplevel(self)
        dialog.title(f"候選專有名詞確認與審核（共 {len(candidates)} 筆）")
        dialog.geometry("820x540")
        dialog.transient(self)

        lbl = ttk.Label(dialog, text="勾選欲加入術語表的候選名詞，可雙擊修改中文譯名或分類：", padding=10)
        lbl.pack(anchor=tk.W)

        tree_frame = ttk.Frame(dialog, padding=(10, 0))
        tree_frame.pack(fill=tk.BOTH, expand=True)

        cols = ("source", "target", "category", "count", "note")
        tree = ttk.Treeview(tree_frame, columns=cols, show="headings", selectmode="extended")
        tree.heading("source", text="日文原文")
        tree.heading("target", text="中文推薦譯名")
        tree.heading("category", text="類別")
        tree.heading("count", text="出現頻次")
        tree.heading("note", text="備註 / 例句")

        tree.column("source", width=180)
        tree.column("target", width=180)
        tree.column("category", width=100, anchor=tk.CENTER)
        tree.column("count", width=80, anchor=tk.CENTER)
        tree.column("note", width=240)

        scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        for c in candidates:
            if isinstance(c, CandidateTerm):
                tree.insert("", tk.END, values=(c.source, c.suggested_target or c.source, c.category, c.count, c.contexts[0] if c.contexts else c.note))
            else:
                tree.insert("", tk.END, values=(c.source, c.target, c.category, 1, c.note))

        btn_frame = ttk.Frame(dialog, padding=10)
        btn_frame.pack(fill=tk.X)

        def _import_all():
            new_entries = []
            for item in tree.get_children():
                vals = tree.item(item, "values")
                new_entries.append(GlossaryEntry(source=vals[0], target=vals[1], category=vals[2], note=vals[4]))
            self.current_glossary_entries = merge_glossaries(self.current_glossary_entries, new_entries, overwrite=False)
            self._filter_glossary_view()
            self._action_save_glossary_gui(silent=True)
            messagebox.showinfo("完成", f"已成功將 {len(new_entries)} 筆候選術語加入術語表！")
            dialog.destroy()

        ttk.Button(btn_frame, text="一鍵全部匯入術語表", command=_import_all).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn_frame, text="關閉", command=dialog.destroy).pack(side=tk.RIGHT, padx=4)

    # ==========================================
    # Tab 3: Translate Novel
    # ==========================================
    def _setup_tab_translate(self):
        f = self.tab_translate

        # Source Selection
        ttk.Label(f, text="選擇原文與翻譯模式，即可開始。已完成的進度會自動接續。",
                  style="Muted.TLabel").pack(anchor=tk.W, pady=(2, 12))
        src_frame = ttk.LabelFrame(f, text="1 · 選擇日文 EPUB", padding=14)
        src_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(src_frame, text="日文 EPUB 檔案：").grid(row=0, column=0, sticky=tk.W, pady=4)
        self.tr_epub_var = tk.StringVar()
        entry_epub = ttk.Entry(src_frame, textvariable=self.tr_epub_var, width=60)
        entry_epub.grid(row=0, column=1, sticky=tk.EW, padx=6, pady=4)
        src_frame.columnconfigure(1, weight=1)

        ttk.Button(src_frame, text="選擇 EPUB...", command=self._action_select_tr_epub).grid(row=0, column=2, padx=4, pady=4)

        # Target Language & Translation Mode
        opt_frame = ttk.LabelFrame(f, text="2 · 設定中文用字與模式", padding=14)
        opt_frame.pack(fill=tk.X, pady=(4, 8))
        language_row = ttk.Frame(opt_frame)
        language_row.pack(fill=tk.X, pady=(0, 12))

        ttk.Label(language_row, text="輸出用字：").pack(side=tk.LEFT)
        self.tr_lang_var = tk.StringVar(value="繁體中文")
        ttk.Radiobutton(language_row, text="繁體中文", variable=self.tr_lang_var, value="繁體中文").pack(side=tk.LEFT, padx=8)
        ttk.Radiobutton(language_row, text="簡體中文", variable=self.tr_lang_var, value="簡體中文").pack(side=tk.LEFT, padx=8)

        self.tr_mode_var = tk.StringVar(value="custom")
        custom_model = self.cfg.get("custom_model", self.cfg.get("model", ""))
        modes = ttk.Frame(opt_frame)
        modes.pack(fill=tk.X)
        modes.columnconfigure(1, weight=1)
        for row, (value, title, description) in enumerate([
            ("custom", "自訂模型", custom_model or "使用設定檔中的模型"),
            ("fast", "快速", "Qwen3 8B · 適合優先追求速度"),
            ("quality", "品質", "Qwen3 14B · 翻譯較慢，記憶體需求較高"),
            ("hybrid", "翻譯＋校對", "8B 初譯，再由 14B 校對"),
        ]):
            ttk.Radiobutton(modes, text=title, variable=self.tr_mode_var, value=value).grid(
                row=row, column=0, sticky=tk.W, padx=(0, 20), pady=7)
            description_label = ttk.Label(modes, text=description, style="Muted.TLabel", wraplength=550)
            description_label.grid(row=row, column=1, sticky=tk.EW, pady=7)
            description_label.bind("<Configure>", lambda event: event.widget.configure(
                wraplength=max(100, event.width)))

        # Progress / action buttons
        btn_box = ttk.Frame(f)
        btn_box.pack(fill=tk.X, pady=6)

        self.btn_start_tr = ttk.Button(btn_box, text="開始翻譯", style="Accent.TButton", command=self._action_start_translate)
        self.btn_start_tr.pack(side=tk.RIGHT, padx=4)

        self.btn_stop = ttk.Button(btn_box, text=" 中止 ", command=self._action_stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.RIGHT, padx=4)

    def _action_select_tr_epub(self):
        fpath = filedialog.askopenfilename(
            title="選擇需要翻譯的日文 EPUB",
            filetypes=[("EPUB 電子書", "*.epub"), ("所有檔案", "*.*")]
        )
        if fpath:
            self.tr_epub_var.set(fpath)

    def _action_stop(self):
        self.stop_requested = True
        self.status_var.set("正在中止操作……")

    def _action_start_translate(self):
        epub_str = self.tr_epub_var.get().strip()
        if not epub_str or not Path(epub_str).exists():
            messagebox.showwarning("提示", "請先選擇有效的日文 EPUB 檔案。")
            return

        source = Path(epub_str)
        lang = self.tr_lang_var.get()
        mode_val = self.tr_mode_var.get()

        # Update model profile
        if mode_val == "fast":
            self.cfg["profile"] = "快速"
            self.cfg["model"] = "qwen3:8b"
            self.cfg["dual_stage"] = False
        elif mode_val == "quality":
            self.cfg["profile"] = "質量"
            self.cfg["model"] = "qwen3:14b"
            self.cfg["dual_stage"] = False
        elif mode_val == "hybrid":
            self.cfg["profile"] = "混合"
            self.cfg["model"] = "qwen3:8b"
            self.cfg["review_model"] = "qwen3:14b"
            self.cfg["dual_stage"] = True
        else:
            self.cfg["profile"] = "自訂"
            self.cfg["model"] = self.cfg.get("custom_model", self.cfg["model"])
            self.cfg["dual_stage"] = False

        self.cfg["target_language"] = lang
        self.stop_requested = False
        self._set_busy(True, f"正在開始翻譯《{source.stem}》……")
        self.btn_stop.config(state=tk.NORMAL)

        def _worker():
            try:
                ensure_model(self.cfg, interactive=False)
                if self.cfg.get("dual_stage"):
                    ensure_model(self.cfg, self.cfg["review_model"], interactive=False)

                work_dir = Path(self.cfg["output_dir"]) / safe_name(source.stem + "_中文翻譯")
                work_dir.mkdir(parents=True, exist_ok=True)
                assets_dir = work_dir / "assets"

                metadata, chapters = extract_epub_chapters(source, assets_dir)
                eff_entries = get_effective_glossary(self.cfg, work_dir)
                if not (work_dir / "glossary.json").exists() and eff_entries:
                    save_project_glossary(work_dir, eff_entries)

                ch_cfg = dict(self.cfg)
                ch_cfg["target_language"] = lang
                ch_cfg["glossary"] = entries_to_dict(eff_entries)
                ch_cfg["_translation_cancelled"] = lambda: self.stop_requested

                # Translate metadata
                meta_values = [metadata.get("title", source.stem)]
                if metadata.get("description"):
                    meta_values.append(metadata["description"])
                meta_episode = Episode(url="epub://metadata", work_title="", episode_title="作品資訊",
                                       paragraphs=meta_values,
                                       blocks=[{"type": "text", "text": v} for v in meta_values])
                meta_draft = translate_episode(meta_episode, ch_cfg, work_dir)
                meta_final = proofread_episode(meta_episode, meta_draft, ch_cfg, work_dir) if ch_cfg.get("dual_stage") else meta_draft
                metadata["title"] = meta_final[0]
                if len(meta_final) > 1:
                    metadata["description"] = meta_final[1]

                book = TranslationBook(source, work_dir, metadata, lang)
                for idx, ch in enumerate(chapters, 1):
                    if self.stop_requested:
                        self.log_queue.put(("log", "使用者中止翻譯。\n"))
                        break

                    pct = ((idx - 1) / len(chapters)) * 100
                    self.log_queue.put(("progress", pct))
                    self.log_queue.put(("log", f"[{idx}/{len(chapters)}] 翻譯中：{ch.title}\n"))
                    if book.completed(idx, ch):
                        self.log_queue.put(("log", "已接續上次完成的章節。\n"))
                        for output in book.checkpoint(idx, len(chapters)):
                            self.log_queue.put(("log", f"已儲存 EPUB：{output.name}\n"))
                        continue

                    ep = Episode(url=ch.url, work_title="", episode_title=ch.title,
                                 paragraphs=ch.paragraphs, blocks=ch.blocks)
                    title_ep = Episode(url=ch.url + "#title", work_title="", episode_title=ch.title,
                                       paragraphs=[ch.title], blocks=[{"type": "text", "text": ch.title}])
                    t_draft = translate_episode(title_ep, ch_cfg, work_dir)
                    t_final = proofread_episode(title_ep, t_draft, ch_cfg, work_dir)[0] if ch_cfg.get("dual_stage") else t_draft[0]
                    ep.episode_title = t_final

                    drafts = translate_episode(ep, ch_cfg, work_dir)
                    translations = proofread_episode(ep, drafts, ch_cfg, work_dir) if ch_cfg.get("dual_stage") else drafts

                    translated_ch = _translated_chapter(ch, translations)
                    translated_ch.title = t_final
                    book.save(idx, ch, translated_ch)
                    for output in book.checkpoint(idx, len(chapters)):
                        self.log_queue.put(("log", f"已儲存 EPUB：{output.name}\n"))

                if not self.stop_requested:
                    out_epub = book.finish(len(chapters))
                    self.log_queue.put(("progress", 100.0))
                    self.log_queue.put(("translate_complete", str(out_epub)))
                else:
                    self.log_queue.put(("log", "已儲存章節，可重新啟動後接續翻譯。\n"))
                    self.log_queue.put(("translate_stopped", None))
            except TranslationCancelled:
                self.log_queue.put(("log", "已停止翻譯，保留已完成的快取，可重新開始接續。\n"))
                self.log_queue.put(("translate_stopped", None))
            except Exception as exc:
                self.log_queue.put(("error", f"翻譯失敗：{exc}"))

        threading.Thread(target=_worker, daemon=True).start()

    # ==========================================
    # Tab 4: Audit & Scope Re-translation
    # ==========================================
    def _setup_tab_audit(self):
        f = self.tab_audit

        top_f = ttk.LabelFrame(f, text="作品專案與合規性稽核", padding=10)
        top_f.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(top_f, text="專案資料夾：").grid(row=0, column=0, sticky=tk.W, pady=4)
        self.audit_proj_var = tk.StringVar()
        entry_proj = ttk.Entry(top_f, textvariable=self.audit_proj_var, width=55)
        entry_proj.grid(row=0, column=1, sticky=tk.EW, padx=6, pady=4)
        top_f.columnconfigure(1, weight=1)

        ttk.Button(top_f, text="選擇專案...", command=self._action_select_audit_proj).grid(row=0, column=2, padx=4, pady=4)
        ttk.Button(top_f, text="一鍵檢查譯文合規性", command=self._action_audit_compliance).grid(row=0, column=3, padx=4, pady=4)

        # Violations Table
        lbl_v = ttk.Label(f, text="術語合規警示清單（日文原文有出現但中文譯文未落實）：", font=("Microsoft JhengHei UI", 11, "bold"))
        lbl_v.pack(anchor=tk.W, pady=(4, 2))

        v_frame = ttk.Frame(f)
        v_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        v_cols = ("chapter", "para", "category", "source", "expected", "original", "translated")
        self.violation_tree = ttk.Treeview(v_frame, columns=v_cols, show="headings", selectmode="extended")
        self.violation_tree.heading("chapter", text="章節")
        self.violation_tree.heading("para", text="段落")
        self.violation_tree.heading("category", text="分類")
        self.violation_tree.heading("source", text="日文原文")
        self.violation_tree.heading("expected", text="應譯為")
        self.violation_tree.heading("original", text="原文片段")
        self.violation_tree.heading("translated", text="譯文片段")

        self.violation_tree.column("chapter", width=120)
        self.violation_tree.column("para", width=60, anchor=tk.CENTER)
        self.violation_tree.column("category", width=90, anchor=tk.CENTER)
        self.violation_tree.column("source", width=130)
        self.violation_tree.column("expected", width=130)
        self.violation_tree.column("original", width=220)
        self.violation_tree.column("translated", width=220)

        v_scroll = ttk.Scrollbar(v_frame, orient=tk.VERTICAL, command=self.violation_tree.yview)
        self.violation_tree.configure(yscrollcommand=v_scroll.set)
        v_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.violation_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Local Re-translation box
        re_box = ttk.LabelFrame(f, text="按術語影響範圍局部重譯 (免全本重翻)", padding=10)
        re_box.pack(fill=tk.X, pady=(0, 4))

        ttk.Label(re_box, text="指定日文術語：").pack(side=tk.LEFT)
        self.re_terms_var = tk.StringVar()
        entry_terms = ttk.Entry(re_box, textvariable=self.re_terms_var, width=35)
        entry_terms.pack(side=tk.LEFT, padx=6)
        ttk.Label(re_box, text="(多個術語以逗號分隔，或輸入 'all')").pack(side=tk.LEFT)

        ttk.Button(re_box, text="分析影響範圍並局部重譯", command=self._action_scope_retranslate_gui).pack(side=tk.RIGHT, padx=4)

    def _action_select_audit_proj(self):
        fpath = filedialog.askdirectory(title="選擇作品專案資料夾", initialdir=self.cfg.get("output_dir", "output"))
        if fpath:
            self.audit_proj_var.set(fpath)

    def _action_audit_compliance(self):
        p_str = self.audit_proj_var.get().strip()
        if not p_str or not Path(p_str).exists():
            messagebox.showwarning("提示", "請先選擇作品專案資料夾。")
            return

        p = Path(p_str)
        proj_file = p / "project.json"
        if not proj_file.exists():
            messagebox.showerror("錯誤", "專案中找不到 project.json。")
            return

        self._set_busy(True, "正在審���專案譯文術語合規性……")
        for item in self.violation_tree.get_children():
            self.violation_tree.delete(item)

        def _worker():
            try:
                project = load_json(proj_file, {})
                eff_entries = get_effective_glossary(self.cfg, p)
                total_v = 0
                for ch in project.get("chapters", []):
                    ch_title = ch.get("title", "")
                    ja = ch.get("japanese", []) or ch.get("source_paragraphs", [])
                    zh = ch.get("translation", []) or ch.get("paragraphs", [])
                    if ja and zh:
                        violations, _ = check_glossary_compliance(ja, zh, eff_entries)
                        for v in violations:
                            total_v += 1
                            self.log_queue.put(("violation", (ch_title, v)))
                self.log_queue.put(("audit_complete", total_v))
            except Exception as exc:
                self.log_queue.put(("error", f"合規審查失敗：{exc}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _action_scope_retranslate_gui(self):
        p_str = self.audit_proj_var.get().strip()
        if not p_str or not Path(p_str).exists():
            messagebox.showwarning("提示", "請先選擇作品專案資料夾。")
            return
        p = Path(p_str)
        proj_file = p / "project.json"
        if not proj_file.exists():
            messagebox.showerror("錯誤", "專案中找不到 project.json。")
            return

        terms_str = self.re_terms_var.get().strip()
        if not terms_str:
            messagebox.showwarning("提示", "請輸入需要局部重譯的日文術語。")
            return

        project = load_json(proj_file, {})
        chapters = project.get("chapters", [])
        eff_entries = get_effective_glossary(self.cfg, p)
        eff_dict = entries_to_dict(eff_entries)

        if terms_str.lower() == "all":
            query_terms = list(eff_dict.keys())
        else:
            query_terms = [t.strip() for t in re.split(r"[,，、\s]+", terms_str) if t.strip()]

        affected = find_affected_chapters(chapters, query_terms)
        if not affected:
            messagebox.showinfo("無受影響章節", "專案中未找到包含指定術語的章節段落，無須重譯。")
            return

        total_paras = sum(len(a["affected_paragraphs"]) for a in affected)
        msg = f"分析結果：共 {len(affected)} / {len(chapters)} 個章節（{total_paras} 個段落）受影響。\n\n是否立即執行局部重譯並更新 EPUB？"
        if not messagebox.askyesno("確認局部重譯", msg):
            return

        self._set_busy(True, f"正在對 {len(affected)} 個受影響章節進行局部重譯……")

        def _worker():
            try:
                ensure_model(self.cfg, interactive=False)
                if self.cfg.get("dual_stage"):
                    ensure_model(self.cfg, self.cfg["review_model"], interactive=False)

                ch_cfg = dict(self.cfg)
                ch_cfg["target_language"] = project.get("language", self.cfg.get("target_language", "繁體中文"))
                ch_cfg["glossary"] = eff_dict

                for idx, item in enumerate(affected, 1):
                    ch_idx = item["index"] - 1
                    ch_rec = chapters[ch_idx]
                    title = ch_rec.get("title", f"第 {item['index']} 章")

                    pct = (idx / len(affected)) * 100
                    self.log_queue.put(("progress", pct))
                    self.log_queue.put(("log", f"[局部重譯 {idx}/{len(affected)}] {title}\n"))

                    ja_paras = ch_rec.get("japanese", []) or ch_rec.get("source_paragraphs", [])
                    if not ja_paras:
                        continue

                    blocks = ch_rec.get("blocks", [])
                    ep_blocks = []
                    for b in blocks:
                        if b.get("type") == "image":
                            ep_blocks.append({"type": "image", "url": b.get("key", ""), "alt": b.get("alt", "")})
                        else:
                            ep_blocks.append({"type": "text", "text": ""})

                    ep = Episode(url=ch_rec.get("url", f"ch_{item['index']}"), work_title="",
                                 episode_title=title, paragraphs=ja_paras, blocks=ep_blocks)

                    drafts = translate_episode(ep, ch_cfg, p)
                    translations = proofread_episode(ep, drafts, ch_cfg, p) if ch_cfg.get("dual_stage") else drafts

                    images = ch_rec.get("images", [])
                    add_or_update_project(ep, translations, p, ch_cfg, images,
                                          drafts=drafts if ch_cfg.get("dual_stage") else None, build_now=False)

                rebuilt = load_json(proj_file, {})
                out_epub = build_extended_epub(rebuilt, p)
                self.log_queue.put(("progress", 100.0))
                self.log_queue.put(("retranslate_complete", str(out_epub)))
            except Exception as exc:
                self.log_queue.put(("error", f"局部重譯失敗：{exc}"))

        threading.Thread(target=_worker, daemon=True).start()

    # ==========================================
    # Global State & Helpers
    # ==========================================
    def _set_busy(self, busy: bool, status_msg: str = ""):
        if busy:
            self.status_var.set(status_msg)
            self.progress_var.set(0.0)
            if hasattr(self, "btn_start_dl"):
                self.btn_start_dl.config(state=tk.DISABLED)
            if hasattr(self, "btn_start_tr"):
                self.btn_start_tr.config(state=tk.DISABLED)
        else:
            self.status_var.set("就緒")
            if hasattr(self, "btn_start_dl"):
                self.btn_start_dl.config(state=tk.NORMAL)
            if hasattr(self, "btn_start_tr"):
                self.btn_start_tr.config(state=tk.NORMAL)
            if hasattr(self, "btn_stop"):
                self.btn_stop.config(state=tk.DISABLED)

    def _poll_queue(self):
        try:
            while True:
                msg_type, payload = self.log_queue.get_nowait()
                if msg_type == "log":
                    self.log_text.insert(tk.END, payload)
                    self.log_text.see(tk.END)
                elif msg_type == "progress":
                    self.progress_var.set(payload)
                elif msg_type == "error":
                    self._set_busy(False)
                    self.status_var.set("執行未完成 · 請查看日誌")
                    if not self.log_panel.winfo_manager():
                        self._toggle_log()
                    self.log_text.insert(tk.END, f"[錯誤] {payload}\n")
                    self.log_text.see(tk.END)
                    messagebox.showerror("執行錯誤", payload)
                elif msg_type == "toc_loaded":
                    self._set_busy(False)
                    work: WorkInfo = payload
                    self.current_work = work
                    self.dl_info_lbl.config(
                        text=f"作品：{work.title} | 作者：{work.author or '未提供'} | 可訪問章節：{len(work.episodes)} 章"
                    )
                    self.dl_start_var.set("1")
                    self.dl_end_var.set(str(len(work.episodes)))
                    for i, ep in enumerate(work.episodes, 1):
                        self.dl_toc_listbox.insert(tk.END, f"{i:4d}. {ep['title']}")
                    self.log_text.insert(tk.END, f"成功讀取作品《{work.title}》目錄，共 {len(work.episodes)} 章。\n")
                elif msg_type == "download_complete":
                    self._set_busy(False)
                    self.log_text.insert(tk.END, f"日文 EPUB 下載完成：{payload}\n")
                    messagebox.showinfo("下載完成", f"日文原文 EPUB 已成功生成：\n{payload}")
                elif msg_type == "translate_complete":
                    self._set_busy(False)
                    self.log_text.insert(tk.END, f"中文 EPUB 翻譯完成：{payload}\n")
                    messagebox.showinfo("翻譯完成", f"中文 EPUB 電子書已成功生成：\n{payload}")
                elif msg_type == "translate_stopped":
                    self._set_busy(False)
                elif msg_type == "candidates_ready":
                    self._set_busy(False)
                    self._candidate_review_dialog(payload)
                elif msg_type == "ai_entities_ready":
                    self._set_busy(False)
                    self._candidate_review_dialog(payload)
                elif msg_type == "violation":
                    ch_title, v = payload
                    self.violation_tree.insert("", tk.END, values=(
                        ch_title, v.paragraph_index, v.category, v.source, v.expected_target,
                        v.original_text[:50], v.translated_text[:50]
                    ))
                elif msg_type == "audit_complete":
                    self._set_busy(False)
                    total_v = payload
                    if total_v == 0:
                        messagebox.showinfo("合規審查結果", "專案中所有章節皆符合術語表規範，未發現違規！")
                    else:
                        messagebox.showwarning("合規審查結果", f"審查完成，共發現 {total_v} 處術語合規警示。")
                elif msg_type == "retranslate_complete":
                    self._set_busy(False)
                    self.log_text.insert(tk.END, f"局部重譯完成！EPUB 已更新：{payload}\n")
                    messagebox.showinfo("局部重譯完成", f"受影響章節已局部重譯完成，EPUB 電子書已更新：\n{payload}")
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)


def main():
    app = TranslatorGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
