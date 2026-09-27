"""Task state and worker-message handling for the desktop interface."""

import queue
import tkinter as tk
from tkinter import ttk

from source_epub import WorkInfo


class TaskStateMixin:
    def _set_busy(self, busy: bool, status_msg: str = ""):
        if busy:
            self._busy_button_states = []

            def disable(parent):
                for widget in parent.winfo_children():
                    if (isinstance(widget, (ttk.Button, tk.Button))
                            and widget not in (self.log_toggle, self.function_button,
                                               self.settings_button)
                            and widget not in self.navigation_buttons):
                        self._busy_button_states.append((widget, str(widget.cget("state"))))
                        widget.configure(state=tk.DISABLED)
                    disable(widget)

            disable(self)
        else:
            for widget, state in getattr(self, "_busy_button_states", []):
                if widget.winfo_exists():
                    widget.configure(state=state)
            self._busy_button_states = []
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
                elif msg_type == "status":
                    self.status_var.set(payload)
                elif msg_type == "error":
                    self._set_busy(False)
                    self.status_var.set("執行未完成 · 請查看日誌")
                    if self.audit_state_var.get() == "检查中":
                        self.audit_state_var.set("检查失败")
                        self.audit_empty_var.set("检查失败，请查看运行日志后重试")
                    if not self.log_panel.winfo_manager():
                        self._toggle_log()
                    self.log_toggle.configure(text="运行日志 ·")
                    self.log_text.insert(tk.END, f"[錯誤] {payload}\n")
                    self.log_text.see(tk.END)
                    self._dialogs.showerror("執行錯誤", payload)
                elif msg_type == "toc_loaded":
                    self._set_busy(False)
                    work: WorkInfo = payload
                    self.current_work = work
                    self._update_current_work()
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
                    self._dialogs.showinfo("下載完成", f"日文原文 EPUB 已成功生成：\n{payload}")
                elif msg_type == "translate_complete":
                    self._set_busy(False)
                    self.log_text.insert(tk.END, f"EPUB 翻譯完成：\n{payload}\n")
                    self._dialogs.showinfo("翻譯完成", f"各語言 EPUB 已成功生成：\n{payload}")
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
                    summary = (f"{v.source} → {v.expected_target}" if v.source else
                               (v.suggested_fix or v.translated_text or v.original_text))
                    status = self.audit_saved_status.get(self._audit_item_key(ch_title, v), "待核对")
                    item = self.violation_tree.insert("", tk.END, values=(
                        f"{ch_title} · {v.paragraph_index}", v.category,
                        summary[:90], status))
                    self.audit_items[item] = (ch_title, v)
                elif msg_type == "audit_complete":
                    self._set_busy(False)
                    self._update_audit_count()
                    if payload:
                        self.audit_empty.pack_forget()
                        self.audit_results.pack(fill=tk.BOTH, expand=True)
                    else:
                        self.audit_empty_var.set("未发现问题")
                elif msg_type == "retranslate_complete":
                    self._set_busy(False)
                    self._on_audit_project_change()
                    self.audit_state_var.set("译文已更新 · 请重新检查")
                    self.audit_empty_var.set("译文已更新，请重新检查以刷新问题列表")
                    self.log_text.insert(tk.END, f"局部重譯完成！EPUB 已更新：{payload}\n")
                    self._dialogs.showinfo("局部重譯完成", f"受影響章節已局部重譯完成，EPUB 電子書已更新：\n{payload}")
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)
