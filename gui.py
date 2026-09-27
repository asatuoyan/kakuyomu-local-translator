"""
GUI Application for Kakuyomu / Syosetu Local Translator
Simple, intuitive, and modern Tkinter interface.
"""
from __future__ import annotations

import csv
import ctypes
import hashlib
import json
import queue
import random
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any
import tkinter as tk
from tkinter import ttk, messagebox as tk_messagebox, filedialog as tk_filedialog
from opencc import OpenCC

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
    Episode,
    TranslationBook,
    TranslationCancelled,
    _translated_chapter,
    _choose_browser,
    _launch_context,
    _select_range,
    add_or_update_project,
    ensure_model,
    load_config,
    load_json,
    atomic_json,
    TRANSLATION_MODELS,
    safe_name,
    select_active_page,
    translate_episode,
)
from playwright.sync_api import sync_playwright
from epub_append import build_extended_epub, create_project_from_epub, inspect_epub
from languages import LANGUAGES, language_name
from gui_settings import SettingsDialog
from gui_task_state import TaskStateMixin
from app_config import update_config
from main import (translate_epub_language, load_review_project, MissingProjectSource,
                  audit_project, project_glossary, retranslate_project)


# Restrained system palette with clear type hierarchy and one accent color.
UI_PALETTES = {
    "light": {
        "background": "#F5F5F7", "surface": "#FFFFFF", "container": "#E8E8ED",
        "line": "#E5E5EA", "border": "#D2D2D7", "text": "#1D1D1F",
        "secondary": "#3A3A3C", "muted": "#515154", "subtle": "#6E6E73",
        "disabled": "#86868B", "accent": "#0071E3", "nav_selected": "#E8F2FF",
        "nav_hover": "#ECECF0", "hover_source": "#1D1D1F",
        "hover_alpha": 0.06,
    },
    "dark": {
        "background": "#161617", "surface": "#242426", "container": "#363638",
        "line": "#3A3A3C", "border": "#48484A", "text": "#F5F5F7",
        "secondary": "#E5E5EA", "muted": "#C7C7CC", "subtle": "#A1A1A6",
        "disabled": "#8E8E93", "accent": "#2997FF", "nav_selected": "#183550",
        "nav_hover": "#303033", "hover_source": "#FFFFFF",
        "hover_alpha": 0.08,
    },
}


def blend_color(foreground: str, background: str, alpha: float) -> str:
    """Flatten the guide's translucent hover color for opaque Tk widgets."""
    return "#" + "".join(
        f"{round(int(foreground[i:i + 2], 16) * alpha + int(background[i:i + 2], 16) * (1 - alpha)):02X}"
        for i in (1, 3, 5)
    )


_ui_converter = OpenCC("t2s")


def ui_text(value: str) -> str:
    """Convert interface copy without changing project or novel data."""
    return _ui_converter.convert(value)


class _LocalizedDialogs:
    def __init__(self, provider):
        self.provider = provider

    def __getattr__(self, name):
        method = getattr(self.provider, name)

        def invoke(*args, **kwargs):
            if self.provider is tk_messagebox:
                args = tuple(ui_text(arg) if isinstance(arg, str) else arg for arg in args)
                kwargs = {key: ui_text(value) if key in ("title", "message", "detail") and isinstance(value, str)
                          else value for key, value in kwargs.items()}
            elif isinstance(kwargs.get("title"), str):
                kwargs["title"] = ui_text(kwargs["title"])
                if "filetypes" in kwargs:
                    kwargs["filetypes"] = [(ui_text(label), pattern)
                                          for label, pattern in kwargs["filetypes"]]
            return method(*args, **kwargs)

        return invoke


messagebox = _LocalizedDialogs(tk_messagebox)
filedialog = _LocalizedDialogs(tk_filedialog)


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


class PageHost(ttk.Frame):
    """Keep pages stacked without exposing a second navigation bar."""

    def __init__(self, parent):
        super().__init__(parent)
        self._pages = []
        self._selected = None
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

    def add(self, page, *, text=""):
        page.grid(row=0, column=0, sticky="nsew")
        self._pages.append(page)
        if self._selected is None:
            self.select(page)

    def select(self, page=None):
        if page is None:
            return str(self._selected) if self._selected is not None else ""
        if isinstance(page, int):
            page = self._pages[page]
        if page not in self._pages:
            raise ValueError("页面未添加到容器。")
        page.tkraise()
        self._selected = page
        self.event_generate("<<NotebookTabChanged>>")


class TranslatorGUI(TaskStateMixin, tk.Tk):
    _dialogs = messagebox

    def __init__(self):
        super().__init__()
        self.title(ui_text("小說下載與本地 AI 翻譯器 (Kakuyomu / 小說家になろう)"))
        self.geometry("1120x760")
        self.minsize(980, 680)

        self.cfg = load_config()
        self.log_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.active_thread: threading.Thread | None = None
        self.stop_requested = False
        self.ui_theme = self.cfg.get("ui_theme", "light")
        if self.ui_theme not in UI_PALETTES:
            self.ui_theme = "light"

        # Apply ttk style
        self.style = ttk.Style(self)
        try:
            self.style.theme_use("clam")
        except Exception:
            pass
        self._configure_styles()

        self._init_ui()
        self.after_idle(lambda: self._set_titlebar_theme(self))
        self._localize_ui()
        self._poll_queue()

    def _set_titlebar_theme(self, window):
        """Match the Windows title bar to the selected light or dark palette."""
        if sys.platform != "win32" or not window.winfo_exists():
            return
        dark = self.ui_theme == "dark"
        if getattr(window, "_titlebar_dark", None) == dark:
            return
        try:
            get_parent = ctypes.windll.user32.GetParent
            get_parent.argtypes = [ctypes.c_void_p]
            get_parent.restype = ctypes.c_void_p
            client = window.winfo_id()
            hwnd = get_parent(client) or client
            value = ctypes.c_int(int(dark))
            set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
            set_attribute.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                      ctypes.c_void_p, ctypes.c_ulong]
            set_attribute.restype = ctypes.c_long
            for attribute in (20, 19):
                result = set_attribute(
                    hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
                )
                if result == 0:
                    break
            palette = UI_PALETTES[self.ui_theme]
            for attribute, color in ((35, palette["background"]), (36, palette["text"])):
                red, green, blue = (int(color[i:i + 2], 16) for i in (1, 3, 5))
                colorref = ctypes.c_uint(red | (green << 8) | (blue << 16))
                set_attribute(hwnd, attribute, ctypes.byref(colorref), ctypes.sizeof(colorref))
            window._titlebar_dark = dark
        except (AttributeError, OSError):
            pass

    def _localize_ui(self):
        """Convert labels created or updated by GUI callbacks."""
        def visit(widget):
            if isinstance(widget, (ttk.Label, ttk.Button, ttk.Checkbutton,
                                   ttk.Radiobutton, ttk.LabelFrame, tk.Label, tk.Button)):
                value = widget.cget("text")
                if value:
                    converted = ui_text(value)
                    if converted != value:
                        widget.configure(text=converted)
            if isinstance(widget, ttk.Treeview):
                for column in widget["columns"]:
                    value = widget.heading(column, "text")
                    converted = ui_text(value)
                    if converted != value:
                        widget.heading(column, text=converted)
            if isinstance(widget, tk.Toplevel):
                widget.title(ui_text(widget.title()))
                self._set_titlebar_theme(widget)
            for child in widget.winfo_children():
                visit(child)

        visit(self)
        self.after(1000, self._localize_ui)

    def _configure_styles(self):
        p = UI_PALETTES[self.ui_theme]
        self.palette = p
        hover = blend_color(p["hover_source"], p["surface"], p["hover_alpha"])
        accent_hover = blend_color(p["hover_source"], p["accent"], p["hover_alpha"])
        self.configure(background=p["background"])
        self.option_add("*Font", ("Microsoft YaHei UI", 14))
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
        self.style.configure(".", font=("Microsoft YaHei UI", 14),
                             background=p["surface"], foreground=p["text"],
                             bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"],
                             troughcolor=p["surface"], focuscolor=p["accent"], selectbackground=p["accent"],
                             selectforeground="#FFFFFF")
        self.style.map(".", foreground=[("disabled", p["disabled"])])
        self.style.configure("TFrame", background=p["surface"])
        self.style.configure("Shell.TFrame", background=p["background"])
        self.style.configure("TLabel", background=p["surface"], foreground=p["text"])
        self.style.configure("Hero.TLabel", background=p["background"],
                             foreground=p["text"], font=("Microsoft YaHei UI", 24, "bold"))
        self.style.configure("Subtitle.TLabel", background=p["background"],
                             foreground=p["muted"], font=("Microsoft YaHei UI", 13))
        self.style.configure("Status.TLabel", background=p["background"],
                             foreground=p["secondary"], font=("Microsoft YaHei UI", 13))
        for name in ("TButton", "TMenubutton"):
            self.style.configure(name, padding=(12, 7), background=p["container"],
                                 borderwidth=0, relief="flat", font=("Microsoft YaHei UI", 13, "bold"))
            self.style.map(name, background=[("disabled", p["container"]), ("pressed", hover), ("active", hover)],
                           foreground=[("disabled", p["disabled"]), ("!disabled", p["text"])])
        self.style.configure("TEntry", padding=6, fieldbackground=p["surface"], insertcolor=p["text"],
                             bordercolor=p["border"], relief="flat")
        self.style.map("TEntry", bordercolor=[("focus", p["accent"])])
        self.style.configure("TCombobox", padding=6, fieldbackground=p["surface"],
                             background=p["surface"], arrowcolor=p["muted"])
        self.style.map("TCombobox", fieldbackground=[("readonly", p["surface"])],
                       foreground=[("disabled", p["disabled"]), ("readonly", p["text"])],
                       background=[("active", hover)], bordercolor=[("focus", p["accent"])])
        for name in ("TRadiobutton", "TCheckbutton"):
            self.style.configure(name, indicatorbackground=p["surface"], indicatorforeground=p["text"])
            self.style.map(name, background=[("active", p["background"])],
                           indicatorbackground=[("disabled", p["surface"]), ("selected", p["accent"])],
                           indicatorforeground=[("selected", "#FFFFFF")])
        self.style.configure("TLabelframe", background=p["surface"], bordercolor=p["line"],
                             borderwidth=1, relief="solid")
        self.style.configure("TLabelframe.Label", background=p["surface"], foreground=p["text"],
                             font=("Microsoft YaHei UI", 14, "bold"))
        self.style.configure("Muted.TLabel", foreground=p["muted"])
        self.style.configure("Title.TLabel", background=p["background"],
                             font=("Microsoft YaHei UI", 24, "bold"))
        self.style.configure("Accent.TButton", background=p["accent"], foreground="#FFFFFF",
                             padding=(16, 8), font=("Microsoft YaHei UI", 14, "bold"))
        self.style.map("Accent.TButton",
                       background=[("disabled", p["container"]), ("pressed", accent_hover), ("active", accent_hover)],
                       foreground=[("disabled", p["disabled"]), ("!disabled", "#FFFFFF")])
        self.style.configure("PageHost.TFrame", background=p["surface"])
        self.style.configure("SourceSelected.TButton", background=p["nav_selected"],
                             foreground=p["accent"], font=("Microsoft YaHei UI", 13, "bold"))
        self.style.map("SourceSelected.TButton", background=[("active", p["nav_selected"])],
                       foreground=[("active", p["accent"])])
        self.style.configure("Treeview", rowheight=36, background=p["surface"], fieldbackground=p["surface"],
                             foreground=p["text"], font=("Microsoft YaHei UI", 13), borderwidth=0)
        self.style.configure("Treeview.Heading", padding=(8, 7), background=p["container"], foreground=p["secondary"],
                             font=("Microsoft YaHei UI", 13, "bold"), relief="flat")
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
                self._set_titlebar_theme(widget)
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
        self._set_titlebar_theme(self)
        self._save_preferences()

    def _init_ui(self):
        # Three main functions share a single menu in the top bar.
        top = ttk.Frame(self, style="Shell.TFrame", padding=(24, 18, 24, 12))
        top.pack(fill=tk.X)
        self.function_button = ttk.Button(top, text="获取小说 ▾", command=self._show_function_menu)
        self.function_button.pack(side=tk.LEFT)
        self.current_work_var = tk.StringVar(value="")
        self.current_work_label = ttk.Label(top, textvariable=self.current_work_var,
                                             style="Status.TLabel", anchor=tk.CENTER)
        self.current_work_label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=24)
        self.settings_button = ttk.Button(top, text="⚙ 设置", command=self._open_settings)
        self.settings_button.pack(side=tk.RIGHT)
        self.function_choice = tk.StringVar(value="获取小说")
        self.function_menu = tk.Menu(self, tearoff=0)
        for label, key in (("获取小说", "download"), ("翻译小说", "translate"), ("检查译文", "audit")):
            self.function_menu.add_radiobutton(label=label, value=label, variable=self.function_choice,
                                                command=lambda page=key: self._select_page(page))
        self.bind("<Escape>", lambda _: self.function_menu.unpost())

        body = ttk.Frame(self, style="Shell.TFrame")
        body.pack(fill=tk.BOTH, expand=True)
        self.notebook = PageHost(body)
        self.notebook.configure(style="PageHost.TFrame")
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=24, pady=(4, 10))

        self.tab_download = ttk.Frame(self.notebook, padding=20)
        self.tab_import = ttk.Frame(self.notebook, padding=20)
        self.tab_glossary = ttk.Frame(self.notebook, padding=20)
        self.tab_translate = ttk.Frame(self.notebook, padding=20)
        self.tab_audit = ttk.Frame(self.notebook, padding=20)

        self.notebook.add(self.tab_download, text="下载")
        self.notebook.add(self.tab_import, text="导入")
        self.notebook.add(self.tab_translate, text="翻译")
        self.notebook.add(self.tab_glossary, text="术语")
        self.notebook.add(self.tab_audit, text="检查")
        self.page_tabs = {
            "download": self.tab_download, "import": self.tab_import,
            "translate": self.tab_translate, "glossary": self.tab_glossary,
            "audit": self.tab_audit,
        }
        self.notebook.bind("<<NotebookTabChanged>>", self._sync_navigation)

        self._setup_tab_download()
        self._setup_tab_import()
        self._setup_tab_glossary()
        self._setup_tab_translate()
        self._setup_tab_audit()
        self._add_page_actions()
        for variable in (self.tr_epub_var, self.proj_dir_var, self.audit_proj_var, self.import_title_var):
            variable.trace_add("write", lambda *_: self._update_current_work())

        # Bottom Global Log / Status Bar
        footer = ttk.Frame(self, style="Shell.TFrame", padding=(24, 6, 24, 14))
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        status_row = ttk.Frame(footer, style="Shell.TFrame")
        status_row.pack(fill=tk.X)
        self.status_var = tk.StringVar(value="就緒")
        self.status_var.trace_add("write", lambda *_: self._localize_status())
        self.status_bar = ttk.Label(status_row, textvariable=self.status_var,
                                    anchor=tk.W, style="Status.TLabel")
        self.status_bar.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.log_toggle = ttk.Button(status_row, text="运行日志", command=self._toggle_log)
        self.log_toggle.pack(side=tk.RIGHT, padx=(8, 0))
        self.btn_stop = ttk.Button(status_row, text="停止", command=self._action_stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.RIGHT, padx=6)
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_bar = ttk.Progressbar(footer, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill=tk.X, pady=(6, 0))
        self.log_panel = ttk.Frame(footer, style="Shell.TFrame", padding=(0, 8, 0, 0))
        bot_frame = self.log_panel

        self.log_text = tk.Text(bot_frame, height=4, wrap=tk.WORD, font=("Microsoft YaHei UI", 13),
                                bg=self.palette["surface"], fg=self.palette["secondary"],
                                relief=tk.FLAT, padx=10, pady=8)
        log_scroll = ttk.Scrollbar(bot_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._sync_navigation()

    def _show_function_menu(self):
        self.function_menu.tk_popup(self.function_button.winfo_rootx(),
                                     self.function_button.winfo_rooty() + self.function_button.winfo_height())

    def _add_page_actions(self):
        self.navigation_buttons = []
        self.source_buttons = {}
        def nav_button(parent, label, key):
            button = ttk.Button(parent, text=label, command=lambda: self._select_page(key))
            button.pack(side=tk.LEFT, padx=(0, 8))
            self.navigation_buttons.append(button)
            if key in ("download", "import"):
                self.source_buttons.setdefault(key, []).append(button)

        self.source_switch = ttk.Frame(self.tab_download)
        self.source_switch.pack(fill=tk.X, pady=(0, 12), before=self.tab_download.winfo_children()[0])
        nav_button(self.source_switch, "网络下载", "download")
        nav_button(self.source_switch, "本地导入", "import")

        import_switch = ttk.Frame(self.tab_import)
        import_switch.pack(fill=tk.X, pady=(0, 12), before=self.tab_import.winfo_children()[0])
        nav_button(import_switch, "网络下载", "download")
        nav_button(import_switch, "本地导入", "import")

        translate_actions = ttk.Frame(self.tab_translate)
        translate_actions.pack(fill=tk.X, pady=(0, 12), before=self.tab_translate.winfo_children()[0])
        nav_button(translate_actions, "术语管理", "glossary")
        glossary_actions = ttk.Frame(self.tab_glossary)
        glossary_actions.pack(fill=tk.X, pady=(0, 12), before=self.tab_glossary.winfo_children()[0])
        nav_button(glossary_actions, "← 返回翻译", "translate")

    def _select_page(self, key: str):
        self.notebook.select(self.page_tabs[key])

    def _sync_navigation(self, _event=None):
        active = self.notebook.select()
        if active in (str(self.tab_download), str(self.tab_import)):
            label = "获取小说"
        elif active in (str(self.tab_translate), str(self.tab_glossary)):
            label = "翻译小说"
        else:
            label = "检查译文"
        self.function_choice.set(label)
        self.function_button.configure(text=f"{label} ▾")
        for key, buttons in self.source_buttons.items():
            for button in buttons:
                button.configure(style="SourceSelected.TButton" if active == str(self.page_tabs[key]) else "TButton")
        self._update_current_work()

    def _toggle_log(self):
        self.log_toggle.configure(text="运行日志")
        if self.log_panel.winfo_manager():
            self.log_panel.pack_forget()
            self.log_toggle.configure(text="运行日志")
        else:
            self.log_panel.pack(fill=tk.X)
            self.log_toggle.configure(text="收起日志")
            self.log_text.see(tk.END)

    def _update_current_work(self):
        active = self.notebook.select()
        path = ""
        if active == str(self.tab_translate) and hasattr(self, "tr_epub_var"):
            path = self.tr_epub_var.get().strip()
        elif active == str(self.tab_glossary) and hasattr(self, "proj_dir_var"):
            path = self.proj_dir_var.get().strip()
        elif active == str(self.tab_audit) and hasattr(self, "audit_proj_var"):
            path = self.audit_proj_var.get().strip()
        elif active == str(self.tab_download) and getattr(self, "current_work", None):
            path = self.current_work.title
        elif active == str(self.tab_import) and hasattr(self, "import_title_var"):
            path = self.import_title_var.get().strip()
        name = Path(path).stem if path else ""
        self.current_work_var.set(f"{name[:28]}{'…' if len(name) > 28 else ''}" if name else "")

    def _save_preferences(self):
        update_config({"ui_theme": self.ui_theme}, CONFIG_PATH)

    def _open_settings(self):
        SettingsDialog(self)

    def _localize_status(self):
        value = self.status_var.get()
        converted = ui_text(value)
        if converted != value:
            self.status_var.set(converted)

    # ==========================================
    # Tab 1: Download Novel
    # ==========================================
    def _setup_tab_download(self):
        f = self.tab_download

        # URL Frame
        url_frame = ttk.LabelFrame(f, text="作品网址", padding=10)
        url_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(url_frame, text="网址", font=("Microsoft YaHei UI", 14, "bold")).grid(row=0, column=0, sticky=tk.W, pady=4)
        self.dl_url_var = tk.StringVar(value="https://ncode.syosetu.com/n2027ci/")
        url_entry = ttk.Entry(url_frame, textvariable=self.dl_url_var, width=65)
        url_entry.grid(row=0, column=1, sticky=tk.EW, padx=6, pady=4)
        url_frame.columnconfigure(1, weight=1)

        btn_fetch_toc = ttk.Button(url_frame, text="读取目录", command=self._action_fetch_toc)
        btn_fetch_toc.grid(row=0, column=2, padx=4, pady=4)

        # Work info display
        info_frame = ttk.LabelFrame(f, text="章节范围", padding=10)
        info_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        self.dl_info_lbl = ttk.Label(info_frame, text="输入网址后读取目录。", wraplength=800)
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
        self.dl_toc_listbox = tk.Listbox(info_frame, height=8, selectmode=tk.EXTENDED, font=("Microsoft YaHei UI", 14))
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
            messagebox.showwarning("提示", "请先点击「读取目录」。")
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
        src_frame = ttk.LabelFrame(f, text="导入文件 · TXT / MD / UMD / JAR", padding=10)
        src_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(src_frame, text="檔案/資料夾：").grid(row=0, column=0, sticky=tk.W, pady=4)
        self.import_path_var = tk.StringVar()
        entry_p = ttk.Entry(src_frame, textvariable=self.import_path_var, width=50)
        entry_p.grid(row=0, column=1, sticky=tk.EW, padx=6, pady=4)
        src_frame.columnconfigure(1, weight=1)

        # Merged source selection button
        btn_src_menu = ttk.Menubutton(src_frame, text="选择来源")
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

        ttk.Label(meta_frame, text="檔案編碼：").grid(row=1, column=0, sticky=tk.W, pady=4)
        self.import_encoding_var = tk.StringVar(value="🤖 自动检测编码")
        cb_enc = ttk.Combobox(
            meta_frame,
            textvariable=self.import_encoding_var,
            values=[
                "🤖 自动检测编码",
                "UTF-8",
                "UTF-8-SIG (含BOM)",
                "Big5 (繁体中文)",
                "GB18030 / GBK (简体中文)",
                "Shift-JIS / CP932 (日文)",
                "EUC-JP (日文)",
                "UTF-16 LE",
                "UTF-16 BE",
            ],
            state="readonly",
            width=18,
        )
        cb_enc.grid(row=1, column=1, sticky=tk.W, padx=4, pady=4)

        ttk.Label(meta_frame, text="章節切分規則：").grid(row=1, column=2, sticky=tk.W, padx=(8, 0), pady=4)
        self.import_rule_var = tk.StringVar(value="默认智能正则 (话/章/卷/Chapter/序章/番外等)")
        cb_rule = ttk.Combobox(
            meta_frame,
            textvariable=self.import_rule_var,
            values=[
                "默认智能正则 (话/章/卷/Chapter/序章/番外等)",
                "Markdown 标题 (#, ##)",
                "自定义正则表达式",
            ],
            state="readonly",
            width=26,
        )
        cb_rule.grid(row=1, column=3, sticky=tk.W, padx=4, pady=4)

        ttk.Label(meta_frame, text="語系設定：").grid(row=2, column=0, sticky=tk.W, pady=4)
        self.import_lang_var = tk.StringVar(value="🤖 自动检测语系")
        cb_lang = ttk.Combobox(
            meta_frame,
            textvariable=self.import_lang_var,
            values=[
                "🤖 自动检测语系",
                "🇹🇼 中文小说 (繁体中文)",
                "🇨🇳 中文小说 (简体中文)",
                "🇯🇵 日文原文",
            ],
            state="readonly",
            width=18,
        )
        cb_lang.grid(row=2, column=1, sticky=tk.W, padx=4, pady=4)

        self.import_sort_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(meta_frame, text="按章节序号排序", variable=self.import_sort_var).grid(row=2, column=2, columnspan=2, sticky=tk.W, pady=2)

        self.import_custom_regex_var = tk.StringVar()
        self.entry_custom_regex = ttk.Entry(meta_frame, textvariable=self.import_custom_regex_var, width=32)
        self.entry_custom_regex.grid(row=3, column=1, columnspan=3, sticky=tk.EW, padx=4, pady=2)
        self.entry_custom_regex.grid_remove()

        def _on_rule_change(evt=None):
            if self.import_rule_var.get() == "自定义正则表达式":
                self.entry_custom_regex.grid()
            else:
                self.entry_custom_regex.grid_remove()
        cb_rule.bind("<<ComboboxSelected>>", _on_rule_change)

        # Chapter Preview Frame
        prev_frame = ttk.LabelFrame(f, text="章節解析預覽列表", padding=8)
        prev_frame.pack(fill=tk.BOTH, expand=True, pady=4)

        tree_cols = ("index", "title", "paragraphs", "chars", "url")
        self.import_tree = ttk.Treeview(prev_frame, columns=tree_cols, show="headings", height=5)
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
        tree_x_scroll = ttk.Scrollbar(prev_frame, orient=tk.HORIZONTAL, command=self.import_tree.xview)
        self.import_tree.configure(yscrollcommand=tree_scroll.set, xscrollcommand=tree_x_scroll.set)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        tree_x_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.import_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.import_status_lbl = ttk.Label(f, text="选择文件或文件夹后预览章节。", style="Muted.TLabel")
        self.import_status_lbl.pack(fill=tk.X, pady=(2, 4))

        # Action Buttons
        action_bar = ttk.Frame(f)
        action_bar.pack(fill=tk.X, pady=4)

        ttk.Button(action_bar, text="预览章节", command=self._action_import_preview).pack(side=tk.LEFT, padx=4)
        ttk.Button(action_bar, text="扫描术语", command=self._action_import_extract_candidates).pack(side=tk.LEFT, padx=4)

        # 1-Click Pack EPUB Button
        ttk.Button(action_bar, text="生成 EPUB", style="Accent.TButton",
                   command=self._action_import_to_epub).pack(side=tk.RIGHT, padx=4)

        # Merged Export & Processing Menu
        btn_export_menu = ttk.Menubutton(action_bar, text="项目操作")
        export_menu = tk.Menu(btn_export_menu, tearoff=0)
        export_menu.add_command(label="导入为中文项目", command=self._action_import_as_chinese_project)
        export_menu.add_command(label="建立项目并翻译", command=self._action_import_and_translate)
        export_menu.add_command(label="仅建立空白翻译项目", command=self._action_import_create_project_only)
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
        if rule == "Markdown 标题 (#, ##)":
            return r"^\s*#{1,3}\s+(.+)$"
        elif rule == "自定义正则表达式":
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
            if self.import_lang_var.get() == "🤖 自动检测语系":
                if detected_lang == "ja":
                    self.import_lang_var.set("🇯🇵 日文原文")
                elif detected_lang == "zh-Hans":
                    self.import_lang_var.set("🇨🇳 中文小说 (简体中文)")
                else:
                    self.import_lang_var.set("🇹🇼 中文小说 (繁体中文)")
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
        elif "简" in lang_sel:
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
        target_lang = "簡體中文" if "简" in lang_sel else "繁體中文"

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
                "中文项目建立成功",
                f"中文项目已建立完成（已载入 {len(self.imported_chapters)} 章译文）：\n{work_dir.resolve()}\n\n是否立即切换至「译文检查」？",
            )
            self.log_text.insert(tk.END, f"已建立中文项目：{work_dir.resolve()}\n")
            if ans:
                self.audit_proj_var.set(str(work_dir.resolve()))
                self.notebook.select(self.tab_audit)
                self._action_audit_compliance()
        except Exception as exc:
            messagebox.showerror("建立项目失败", str(exc))

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
            messagebox.showinfo("项目建立成功", f"翻译项目已建立完成：\n{work_dir.resolve()}\n\n您可随时切换至「翻译小说」或「术语管理」继续处理。")
            self.log_text.insert(tk.END, f"已建立翻译项目：{work_dir.resolve()}\n")
        except Exception as exc:
            messagebox.showerror("建立项目失败", str(exc))

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

        # Category filter
        filter_bar = ttk.Frame(f)
        filter_bar.pack(fill=tk.X, pady=(8, 12))

        ttk.Label(filter_bar, text="類別篩選：").pack(side=tk.LEFT)
        self.category_filter_var = tk.StringVar(value="全部")
        cb_cat = ttk.Combobox(filter_bar, textvariable=self.category_filter_var, values=["全部"] + list(VALID_CATEGORIES), state="readonly", width=12)
        cb_cat.pack(side=tk.LEFT, padx=4)
        cb_cat.bind("<<ComboboxSelected>>", lambda *_: self._filter_glossary_view())

        # Action Buttons
        btn_box = ttk.Frame(f)
        btn_box.pack(fill=tk.X, pady=(0, 14))

        # Term editing actions
        ttk.Button(btn_box, text="新增术语", command=self._action_add_term).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_box, text="编辑选中", command=self._action_edit_term).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_box, text="删除选中", command=self._action_delete_term).pack(side=tk.LEFT, padx=3)
        ttk.Separator(btn_box, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)

        # Merged Extraction & Recognition Menu
        btn_extract_menu = ttk.Menubutton(btn_box, text="提取术语")
        extract_menu = tk.Menu(btn_extract_menu, tearoff=0)
        extract_menu.add_command(label="快速提取候选术语", command=self._action_extract_candidates_gui)
        extract_menu.add_command(label="模型识别实体 (Ollama)", command=self._action_ai_scan_entities_gui)
        btn_extract_menu["menu"] = extract_menu
        btn_extract_menu.pack(side=tk.LEFT, padx=2)

        # Merged Import & Export Menu
        btn_io_menu = ttk.Menubutton(btn_box, text="导入与导出")
        io_menu = tk.Menu(btn_io_menu, tearoff=0)
        io_menu.add_command(label="导入术语表 (CSV / Excel / JSON)", command=self._action_import_glossary_gui)
        io_menu.add_command(label="导出术语表 (Excel / CSV / JSON)", command=self._action_export_glossary_gui)
        btn_io_menu["menu"] = io_menu
        btn_io_menu.pack(side=tk.LEFT, padx=2)

        ttk.Button(btn_box, text="保存更改", style="Accent.TButton",
                   command=self._action_save_glossary_gui).pack(side=tk.RIGHT, padx=3)

        # Table (Treeview)
        table_frame = ttk.Frame(f)
        table_frame.pack(fill=tk.BOTH, expand=True)

        columns = ("source", "target", "category", "note")
        self.glossary_tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        self.glossary_tree.heading("source", text="原文")
        self.glossary_tree.heading("target", text="中文譯名 (Target)")
        self.glossary_tree.heading("category", text="類別 (Category)")
        self.glossary_tree.heading("note", text="備註說明 (Note)")

        self.glossary_tree.column("source", width=220, anchor=tk.W)
        self.glossary_tree.column("target", width=220, anchor=tk.W)
        self.glossary_tree.column("category", width=120, anchor=tk.CENTER)
        self.glossary_tree.column("note", width=300, anchor=tk.W)

        tree_scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.glossary_tree.yview)
        tree_x_scroll = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.glossary_tree.xview)
        self.glossary_tree.configure(yscrollcommand=tree_scroll.set, xscrollcommand=tree_x_scroll.set)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        tree_x_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.glossary_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.glossary_tree.bind("<Double-1>", lambda *_: self._action_edit_term())

        self.current_glossary_entries: list[GlossaryEntry] = []
        self._refresh_glossary_table()

    def _action_select_glossary_project(self):
        selected = filedialog.askdirectory(title="选择作品项目文件夹", initialdir=self.cfg.get("output_dir", "output"))
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

        cat_filter = self.category_filter_var.get()

        for e in self.current_glossary_entries:
            if cat_filter != "全部" and e.category != cat_filter:
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
        dialog.after_idle(lambda: self._set_titlebar_theme(dialog))
        dialog.title("新增/編輯術語" if entry else "新增術語")
        dialog.geometry("450x260")
        dialog.transient(self)
        dialog.grab_set()

        frm = ttk.Frame(dialog, padding=15)
        frm.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frm, text="原文：").grid(row=0, column=0, sticky=tk.W, pady=4)
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
                messagebox.showerror("错误", "原文与译名都不能为空。")
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
            update_config({"glossary": self.cfg["glossary"]}, CONFIG_PATH)
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
        dialog.after_idle(lambda: self._set_titlebar_theme(dialog))
        dialog.title(f"候選專有名詞確認與審核（共 {len(candidates)} 筆）")
        dialog.geometry("820x540")
        dialog.transient(self)

        lbl = ttk.Label(dialog, text="勾選欲加入術語表的候選名詞，可雙擊修改中文譯名或分類：", padding=10)
        lbl.pack(anchor=tk.W)

        tree_frame = ttk.Frame(dialog, padding=(10, 0))
        tree_frame.pack(fill=tk.BOTH, expand=True)

        cols = ("source", "target", "category", "count", "note")
        tree = ttk.Treeview(tree_frame, columns=cols, show="headings", selectmode="extended")
        tree.heading("source", text="原文")
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
        src_frame = ttk.LabelFrame(f, text="日文 EPUB", padding=12)
        src_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(src_frame, text="日文 EPUB 檔案：").grid(row=0, column=0, sticky=tk.W, pady=4)
        self.tr_epub_var = tk.StringVar()
        entry_epub = ttk.Entry(src_frame, textvariable=self.tr_epub_var, width=60)
        entry_epub.grid(row=0, column=1, sticky=tk.EW, padx=6, pady=4)
        src_frame.columnconfigure(1, weight=1)

        ttk.Button(src_frame, text="選擇 EPUB...", command=self._action_select_tr_epub).grid(row=0, column=2, padx=4, pady=4)

        # Target Language & Translation Mode
        opt_frame = ttk.LabelFrame(f, text="输出语言与模型", padding=12)
        opt_frame.pack(fill=tk.X, pady=(4, 8))
        language_row = ttk.Frame(opt_frame)
        language_row.pack(fill=tk.X, pady=(0, 12))

        self.tr_language_vars = {}
        for index, (code, (label, _)) in enumerate(LANGUAGES.items()):
            variable = tk.BooleanVar(value=code == "zh-Hans")
            self.tr_language_vars[code] = variable
            ttk.Checkbutton(language_row, text=label, variable=variable).grid(
                row=index // 4, column=index % 4, sticky=tk.W, padx=(0, 18), pady=3)
        ttk.Label(opt_frame, text="可多选；每种语言生成独立 EPUB。",
                  style="Muted.TLabel").pack(anchor=tk.W, pady=(0, 6))

        self.tr_model_var = tk.StringVar(value=self.cfg["model"])
        ttk.Label(opt_frame, text="翻译模型").pack(anchor=tk.W, pady=(8, 4))
        self.tr_model_box = ttk.Combobox(
            opt_frame, textvariable=self.tr_model_var,
            values=list(dict.fromkeys([self.cfg["model"], *TRANSLATION_MODELS.values()])),
        )
        self.tr_model_box.pack(fill=tk.X)
        ttk.Label(opt_frame, text="可在设置中读取 Ollama 已安装模型。",
                  style="Muted.TLabel").pack(anchor=tk.W, pady=(4, 0))

        # Progress / action buttons
        btn_box = ttk.Frame(f)
        btn_box.pack(fill=tk.X, pady=6)

        self.btn_start_tr = ttk.Button(btn_box, text="開始翻譯", style="Accent.TButton", command=self._action_start_translate)
        self.btn_start_tr.pack(side=tk.RIGHT, padx=4)

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
        languages = [code for code, variable in self.tr_language_vars.items() if variable.get()]
        if not languages:
            messagebox.showwarning("提示", "請至少選擇一種輸出語言。")
            return
        model = self.tr_model_var.get().strip()
        if not model:
            messagebox.showwarning("提示", "请先选择或输入翻译模型。")
            return
        self.cfg["model"] = model

        task_cfg = dict(self.cfg)
        task_cfg["_translation_cancelled"] = lambda: self.stop_requested
        self.stop_requested = False
        self._set_busy(True, f"正在開始翻譯《{source.stem}》……")
        self.btn_stop.config(state=tk.NORMAL)

        def _worker():
            outputs = []
            failures = []
            try:
                ensure_model(task_cfg, interactive=False)
                for language_index, code in enumerate(languages):
                    if self.stop_requested:
                        raise TranslationCancelled()

                    def progress(percent, message):
                        overall = (language_index * 100 + percent) / len(languages)
                        self.log_queue.put(("progress", overall))
                        self.log_queue.put(("status", message))

                    try:
                        output = translate_epub_language(source, task_cfg, code, progress)
                        outputs.append(str(output))
                        self.log_queue.put(("log", f"[{language_name(code)}] 已完成：{output}\n"))
                    except TranslationCancelled:
                        raise
                    except Exception as exc:
                        failures.append(f"{language_name(code)}：{exc}")
                        self.log_queue.put(("log", f"[{language_name(code)}] 未完成：{exc}\n"))
                if failures:
                    summary = "\n".join(failures)
                    if outputs:
                        summary += "\n\n已成功輸出：\n" + "\n".join(outputs)
                    self.log_queue.put(("error", summary))
                else:
                    self.log_queue.put(("progress", 100.0))
                    self.log_queue.put(("translate_complete", "\n".join(outputs)))
            except TranslationCancelled:
                self.log_queue.put(("log", "已停止翻譯，保留各語言已完成的進度，可重新開始接續。\n"))
                self.log_queue.put(("translate_stopped", None))
            except Exception as exc:
                self.log_queue.put(("error", f"翻譯失敗：{exc}"))

        threading.Thread(target=_worker, daemon=True).start()

    # ==========================================
    # Tab 4: Audit & Scope Re-translation
    # ==========================================
    def _setup_tab_audit(self):
        f = self.tab_audit
        self.audit_proj_var = tk.StringVar()
        self.re_terms_var = tk.StringVar()
        self.re_all_var = tk.BooleanVar(value=False)
        self.audit_items = {}
        self.audit_state_var = tk.StringVar(value="尚未检查")
        self.audit_name_var = tk.StringVar(value="未选择项目")
        self.audit_path_var = tk.StringVar(value="")

        ttk.Label(f, text="译文检查", style="Title.TLabel").pack(anchor=tk.W, pady=(0, 2))
        ttk.Label(f, text="检查术语一致性与潜在翻译问题", style="Muted.TLabel").pack(anchor=tk.W, pady=(0, 14))
        project_row = ttk.Frame(f)
        project_row.pack(fill=tk.X, pady=(0, 3))
        ttk.Label(project_row, textvariable=self.audit_name_var,
                  font=("Microsoft YaHei UI", 14, "bold")).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.audit_start_button = ttk.Button(project_row, text="开始检查", style="Accent.TButton",
                                              command=self._action_audit_compliance, state=tk.DISABLED)
        self.audit_start_button.pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(project_row, text="选择项目", command=self._action_select_audit_proj).pack(side=tk.RIGHT)
        self.audit_path_label = ttk.Label(f, textvariable=self.audit_path_var, style="Muted.TLabel")
        self.audit_path_label.pack(anchor=tk.W, fill=tk.X, pady=(0, 12))
        self.audit_path_label.bind("<Enter>", self._show_audit_path_tip)
        self.audit_path_label.bind("<Leave>", self._hide_audit_path_tip)
        ttk.Label(f, textvariable=self.audit_state_var, style="Muted.TLabel").pack(anchor=tk.W, pady=(0, 8))

        self.audit_empty = ttk.Frame(f)
        self.audit_empty.pack(fill=tk.BOTH, expand=True)
        empty_center = ttk.Frame(self.audit_empty)
        empty_center.place(relx=.5, rely=.42, anchor=tk.CENTER)
        self.audit_empty_var = tk.StringVar(value="选择翻译项目，检查术语一致性与译文问题")
        ttk.Label(empty_center, textvariable=self.audit_empty_var,
                  style="Muted.TLabel").pack(pady=(0, 12))
        ttk.Button(empty_center, text="选择项目", command=self._action_select_audit_proj).pack()

        self.audit_results = ttk.Frame(f)
        list_frame = ttk.Frame(self.audit_results)
        list_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        columns = ("position", "type", "summary", "status")
        self.violation_tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse", height=7)
        for key, label, width, stretch in (("position", "位置", 180, False),
                                            ("type", "问题类型", 125, False),
                                            ("summary", "问题摘要", 380, True),
                                            ("status", "处理状态", 100, False)):
            self.violation_tree.heading(key, text=label)
            self.violation_tree.column(key, width=width, minwidth=width if not stretch else 180,
                                       stretch=stretch, anchor=tk.W)
        scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.violation_tree.yview)
        self.violation_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.violation_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.violation_tree.bind("<<TreeviewSelect>>", self._show_audit_detail)

        self.audit_detail = ttk.Frame(self.audit_results)
        ttk.Separator(self.audit_detail).pack(fill=tk.X, pady=(0, 8))
        self.audit_detail_text = tk.Text(self.audit_detail, height=7, wrap=tk.WORD,
                                          font=("Microsoft YaHei UI", 12), state=tk.DISABLED,
                                          relief=tk.FLAT, padx=8, pady=6)
        self.audit_detail_text.pack(fill=tk.BOTH, expand=True)
        detail_actions = ttk.Frame(self.audit_detail)
        detail_actions.pack(fill=tk.X, pady=(7, 0))
        ttk.Button(detail_actions, text="标记已处理", command=lambda: self._set_audit_item_status("已处理"))\
            .pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(detail_actions, text="忽略", command=lambda: self._set_audit_item_status("已忽略"))\
            .pack(side=tk.LEFT)
        ttk.Button(detail_actions, text="重译此段", command=self._retranslate_selected_paragraph)\
            .pack(side=tk.RIGHT)
        ttk.Button(detail_actions, text="更多操作", command=self._open_audit_more)\
            .pack(side=tk.RIGHT, padx=(0, 8))
        self.audit_proj_var.trace_add("write", lambda *_: self._on_audit_project_change())

    def _on_audit_project_change(self):
        path = self.audit_proj_var.get().strip()
        self.audit_name_var.set(Path(path).name if path else "未选择项目")
        self.audit_path_var.set(path if len(path) <= 90 else f"{path[:42]}…{path[-42:]}")
        self.audit_start_button.configure(state=tk.NORMAL if path else tk.DISABLED)
        self.audit_state_var.set("尚未检查")
        self.audit_empty_var.set("点击“开始检查”，查看术语一致性与译文问题" if path else
                                 "选择翻译项目，检查术语一致性与译文问题")
        self.audit_results.pack_forget()
        self.audit_detail.pack_forget()
        self.audit_empty.pack(fill=tk.BOTH, expand=True)
        for item in self.violation_tree.get_children():
            self.violation_tree.delete(item)
        self.audit_items.clear()

    def _show_audit_path_tip(self, event):
        path = self.audit_proj_var.get().strip()
        if not path or path == self.audit_path_var.get():
            return
        tip = tk.Toplevel(self)
        tip.overrideredirect(True)
        tip.geometry(f"+{event.x_root + 12}+{event.y_root + 12}")
        ttk.Label(tip, text=path, padding=6).pack()
        self.audit_path_tip = tip

    def _hide_audit_path_tip(self, _event=None):
        tip = getattr(self, "audit_path_tip", None)
        if tip and tip.winfo_exists():
            tip.destroy()
        self.audit_path_tip = None

    def _audit_item_key(self, chapter, violation):
        return f"{chapter}\u0000{violation.paragraph_index}\u0000{violation.category}\u0000{violation.source}"

    def _show_audit_detail(self, _event=None):
        selected = self.violation_tree.selection()
        if not selected:
            self.audit_detail.pack_forget()
            return
        chapter, violation = self.audit_items[selected[0]]
        reason = violation.suggested_fix or (
            f"原文包含“{violation.source}”，建议译为“{violation.expected_target}”。"
            if violation.source else "请人工核对这一段的译文。")
        content = (f"位置：{chapter} · 第 {violation.paragraph_index} 段\n"
                   f"原文：{violation.original_text}\n"
                   f"译文：{violation.translated_text}\n"
                   f"建议译法：{violation.expected_target or '—'}\n"
                   f"问题原因：{reason}")
        self.audit_detail_text.configure(state=tk.NORMAL)
        self.audit_detail_text.delete("1.0", tk.END)
        self.audit_detail_text.insert("1.0", content)
        self.audit_detail_text.configure(state=tk.DISABLED)
        if not self.audit_detail.winfo_manager():
            self.audit_detail.pack(fill=tk.X)

    def _set_audit_item_status(self, status):
        selected = self.violation_tree.selection()
        if not selected:
            return
        item = selected[0]
        values = list(self.violation_tree.item(item, "values"))
        values[3] = status
        self.violation_tree.item(item, values=values)
        chapter, violation = self.audit_items[item]
        path = Path(self.audit_proj_var.get().strip()) / "audit-status.json"
        saved = load_json(path, {})
        saved[self._audit_item_key(chapter, violation)] = status
        atomic_json(path, saved)
        self._update_audit_count()

    def _update_audit_count(self):
        total = len(self.violation_tree.get_children())
        pending = sum(self.violation_tree.set(item, "status") == "待核对"
                      for item in self.violation_tree.get_children())
        self.audit_state_var.set(f"发现 {pending} 项待核对（共 {total} 项）" if total else "未发现问题")

    def _retranslate_selected_paragraph(self):
        selected = self.violation_tree.selection()
        if not selected:
            return
        chapter, violation = self.audit_items[selected[0]]
        path = Path(self.audit_proj_var.get().strip())
        if not messagebox.askyesno("重译此段", f"将重新翻译《{chapter}》第 {violation.paragraph_index} 段并更新 EPUB，继续？"):
            return
        self.stop_requested = False
        task_cfg = dict(self.cfg)
        task_cfg["_translation_cancelled"] = lambda: self.stop_requested
        self._set_busy(True, "正在重译选中段落……")
        self.btn_stop.config(state=tk.NORMAL)
        location = (chapter, violation.paragraph_index, violation.original_text)

        def progress(percent, title):
            self.log_queue.put(("progress", percent))
            self.log_queue.put(("status", title))

        def worker():
            try:
                ensure_model(task_cfg, interactive=False)
                output = retranslate_project(path, task_cfg, [], progress,
                                             paragraph_location=location)
                self.log_queue.put(("retranslate_complete", str(output)))
            except TranslationCancelled:
                self.log_queue.put(("translate_stopped", None))
            except Exception as exc:
                self.log_queue.put(("error", f"段落重译失败：{exc}"))
        threading.Thread(target=worker, daemon=True).start()

    def _open_audit_more(self):
        window = tk.Toplevel(self)
        window.title("更多操作 · 局部重译")
        window.geometry("460x205")
        window.configure(background=self.palette["background"])
        self._set_titlebar_theme(window)
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        ttk.Label(frame, text="指定术语（多个术语用逗号分隔）").pack(anchor=tk.W)
        ttk.Entry(frame, textvariable=self.re_terms_var).pack(fill=tk.X, pady=(6, 10))
        ttk.Checkbutton(frame, text="全部术语", variable=self.re_all_var).pack(anchor=tk.W)
        def run():
            window.destroy()
            self._action_scope_retranslate_gui()
        ttk.Button(frame, text="分析范围并局部重译", command=run,
                   style="Accent.TButton").pack(anchor=tk.E, pady=(10, 0))

    def _action_select_audit_proj(self):
        fpath = filedialog.askdirectory(title="选择作品项目文件夹", initialdir=self.cfg.get("output_dir", "output"))
        if fpath:
            self.audit_proj_var.set(fpath)

    def _load_review_project(self, path):
        try:
            return load_review_project(path)
        except MissingProjectSource:
            source = filedialog.askopenfilename(title="选择创建旧项目时的原始 EPUB", filetypes=[("EPUB", "*.epub")])
            if not source:
                return None
            return load_review_project(path, Path(source))

    def _action_audit_compliance(self):
        path_text = self.audit_proj_var.get().strip()
        if not path_text:
            return
        path = Path(path_text)
        try:
            project = self._load_review_project(path)
            if project is None:
                return
            entries = project_glossary(self.cfg, path, project)
        except Exception as exc:
            messagebox.showerror("项目错误", str(exc))
            return
        self._set_busy(True, "正在检查术语与译文……")
        self.audit_state_var.set("检查中")
        self.audit_empty_var.set("正在检查，请稍候…")
        self.audit_results.pack_forget()
        self.audit_empty.pack(fill=tk.BOTH, expand=True)
        for item in self.violation_tree.get_children():
            self.violation_tree.delete(item)
        self.audit_items.clear()
        self.audit_detail.pack_forget()
        self.audit_saved_status = load_json(path / "audit-status.json", {})

        def _worker():
            try:
                results = audit_project(project, entries)
                for title, violation in results:
                    self.log_queue.put(("violation", (title, violation)))
                self.log_queue.put(("audit_complete", len(results)))
            except Exception as exc:
                self.log_queue.put(("error", f"检查失败：{exc}"))
        threading.Thread(target=_worker, daemon=True).start()

    def _action_scope_retranslate_gui(self):
        path = Path(self.audit_proj_var.get().strip())
        value = self.re_terms_var.get().strip()
        if self.re_all_var.get():
            value = "all"
        if not value:
            messagebox.showwarning("提示", "请输入需要重译的术语，或选择“全部术语”。")
            return
        try:
            project = self._load_review_project(path)
            if project is None:
                return
            entries = project_glossary(self.cfg, path, project)
            terms = list(entries_to_dict(entries)) if value.lower() == "all" else [t for t in re.split(r"[,，、\s]+", value) if t]
            affected = find_affected_chapters(project.get("chapters", []), terms)
        except Exception as exc:
            messagebox.showerror("项目错误", str(exc))
            return
        if not affected:
            messagebox.showinfo("提示", "没有受影响的段落。")
            return
        count = sum(len(item["affected_paragraphs"]) for item in affected)
        if not messagebox.askyesno("局部重译", f"将重译 {len(affected)} 章中的 {count} 个段落并更新 EPUB，继续？"):
            return
        self.stop_requested = False
        task_cfg = dict(self.cfg)
        task_cfg["_translation_cancelled"] = lambda: self.stop_requested
        self._set_busy(True, "正在局部重译……")
        self.btn_stop.config(state=tk.NORMAL)

        def progress(percent, title):
            self.log_queue.put(("progress", percent))
            self.log_queue.put(("status", title))

        def _worker():
            try:
                ensure_model(task_cfg, interactive=False)
                output = retranslate_project(path, task_cfg, terms, progress)
                self.log_queue.put(("retranslate_complete", str(output)))
            except TranslationCancelled:
                self.log_queue.put(("log", "已停止。已完成章节已保存；再次局部重译会更新 EPUB。\n"))
                self.log_queue.put(("translate_stopped", None))
            except Exception as exc:
                self.log_queue.put(("error", f"局部重译失败：{exc}"))
        threading.Thread(target=_worker, daemon=True).start()

def main():
    app = TranslatorGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
