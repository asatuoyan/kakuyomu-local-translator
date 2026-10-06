"""Read completed translation batches without moving the reader's position."""
import tkinter as tk
from tkinter import ttk


class LiveReader(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("实时阅读")
        self.geometry("1000x720")
        self.keys = []
        self.bilingual = tk.BooleanVar(value=app.tr_bilingual_var.get())
        ttk.Checkbutton(self, text="原文 / 译文对照", variable=self.bilingual,
                        command=self.render).pack(anchor="w", padx=12, pady=8)
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.chapters = tk.Listbox(body, width=28, exportselection=False)
        self.chapters.pack(side="left", fill="y")
        self.chapters.bind("<<ListboxSelect>>", lambda _: self.render(reset=True))
        scrollbar = ttk.Scrollbar(body)
        scrollbar.pack(side="right", fill="y")
        self.text = tk.Text(body, wrap="word", font=("Microsoft YaHei UI", 16),
                            padx=20, pady=16, yscrollcommand=scrollbar.set, state="disabled")
        self.text.pack(fill="both", expand=True)
        scrollbar.configure(command=self.text.yview)
        self.refresh()

    def refresh(self):
        for key, record in self.app.reading_chapters.items():
            if key not in self.keys:
                self.keys.append(key)
                self.chapters.insert("end", f"{key[0]} · {key[1]} · {record[0]}")
        if self.keys and not self.chapters.curselection():
            self.chapters.selection_set(0)
        self.render()

    def render(self, reset=False):
        selection = self.chapters.curselection()
        position = self.text.index("@0,0")
        content = "译文完成后会自动显示在这里。"
        if selection:
            title, originals, translations = self.app.reading_chapters[self.keys[selection[0]]]
            paragraphs = []
            for original, translated in zip(originals, translations):
                if translated:
                    paragraphs.append(f"{original}\n\n{translated}" if self.bilingual.get() else translated)
            content = title + "\n\n" + "\n\n".join(paragraphs)
            if not paragraphs:
                content += "正在等待本章第一批译文……"
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self.text.configure(state="disabled")
        self.text.yview("1.0" if reset else position)
