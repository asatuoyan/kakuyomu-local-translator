"""Settings window and Ollama model discovery for the desktop interface."""

from __future__ import annotations

import queue
import re
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from app_config import CONFIG_PATH, TRANSLATION_MODELS, update_config
from main import installed_models


def model_choices(installed: list[str], selected: str) -> list[str]:
    """Keep the selected model visible alongside installed and recommended models."""
    return list(dict.fromkeys(name for name in
                              [*installed, selected.strip(), *TRANSLATION_MODELS.values()] if name))


class SettingsDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("设置")
        self.geometry("560x460")
        self.configure(background=app.palette["background"])
        app._set_titlebar_theme(self)

        frame = ttk.Frame(self, padding=20)
        frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        ttk.Label(frame, text="模型与连接", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor=tk.W, pady=(0, 14))
        ttk.Label(frame, text="翻译模型").pack(anchor=tk.W)
        self.model_var = tk.StringVar(value=app.tr_model_var.get())
        model_row = ttk.Frame(frame)
        model_row.pack(fill=tk.X, pady=(4, 4))
        self.model_box = ttk.Combobox(
            model_row, textvariable=self.model_var,
            values=model_choices([], self.model_var.get()),
        )
        self.model_box.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.model_status = tk.StringVar(value="可选择已安装模型，或输入模型名称。")
        ttk.Label(frame, textvariable=self.model_status, style="Muted.TLabel").pack(anchor=tk.W, pady=(0, 12))
        ttk.Label(frame, text="Ollama 地址").pack(anchor=tk.W)
        self.url_var = tk.StringVar(value=app.cfg["ollama_url"])
        ttk.Entry(frame, textvariable=self.url_var).pack(fill=tk.X, pady=(4, 14))
        self.refresh_button = ttk.Button(model_row, text="刷新模型", command=self.refresh_models)
        self.refresh_button.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(frame, text="界面主题").pack(anchor=tk.W)
        self.theme_var = tk.StringVar(value="深色模式" if app.ui_theme == "dark" else "浅色模式")
        ttk.Combobox(frame, textvariable=self.theme_var, values=("浅色模式", "深色模式"),
                     state="readonly").pack(fill=tk.X, pady=(4, 8))
        ttk.Button(frame, text="保存设置", command=self.save, style="Accent.TButton").pack(anchor=tk.E, pady=(12, 0))

        self.update_idletasks()
        required_height = frame.winfo_reqheight() + 24
        self.minsize(500, max(440, required_height))
        self.geometry(f"560x{max(460, required_height)}")
        self.refresh_models()

    def refresh_models(self):
        url = self.url_var.get().strip().rstrip("/")
        if not re.match(r"^https?://[^\s/]+", url):
            self.model_status.set("请先输入有效的 Ollama 地址。")
            return
        self.refresh_button.configure(state=tk.DISABLED)
        self.model_status.set("正在读取 Ollama 模型……")
        result_queue = queue.Queue(maxsize=1)

        def fetch():
            try:
                result_queue.put((sorted(installed_models({"ollama_url": url})), None))
            except Exception as exc:
                result_queue.put((None, str(exc)))

        def show_result():
            if not self.winfo_exists():
                return
            try:
                names, error = result_queue.get_nowait()
            except queue.Empty:
                self.app.after(100, show_result)
                return
            self.refresh_button.configure(state=tk.NORMAL)
            if url != self.url_var.get().strip().rstrip("/"):
                self.model_status.set("地址已改变，请重新刷新模型。")
                return
            if error:
                self.model_status.set(f"读取失败：{error}")
                return
            choices = model_choices(names, self.model_var.get())
            self.model_box.configure(values=choices)
            self.app.tr_model_box.configure(values=choices)
            self.model_status.set(f"已读取 {len(names)} 个已安装模型；也可输入模型名称。")

        threading.Thread(target=fetch, daemon=True).start()
        self.app.after(100, show_result)

    def save(self):
        url = self.url_var.get().strip().rstrip("/")
        if not re.match(r"^https?://[^\s/]+", url):
            messagebox.showerror("地址无效", "请输入以 http:// 或 https:// 开头的 Ollama 地址。")
            return
        model = self.model_var.get().strip()
        if not model:
            messagebox.showerror("模型无效", "请输入或选择翻译模型。")
            return
        update_config({"model": model, "ollama_url": url, "ui_theme": self.app.ui_theme}, CONFIG_PATH)
        self.app.cfg.update(model=model, ollama_url=url)
        self.app.tr_model_var.set(model)
        if model not in self.app.tr_model_box["values"]:
            self.app.tr_model_box.configure(values=(*self.app.tr_model_box["values"], model))
        if (self.theme_var.get() == "深色模式") != (self.app.ui_theme == "dark"):
            self.app._toggle_theme()
        self.destroy()
