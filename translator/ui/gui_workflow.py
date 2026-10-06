"""Desktop controls for preview, review and confirmed continuation."""
from pathlib import Path
import threading
import tkinter as tk
from tkinter import ttk

from translator.storage.project_storage import load_json
from translator.translation.translation_workflow import workflow_path, ready_for_full


class WorkflowMixin:
    def _setup_network_controls(self):
        frame = ttk.LabelFrame(self.tab_download, text="从网址一键获取并翻译", padding=6)
        frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 4), before=self.dl_info_frame)
        row = ttk.Frame(frame)
        row.pack(fill=tk.X)
        ttk.Label(row, text="先试译前").pack(side=tk.LEFT)
        ttk.Spinbox(row, from_=1, to=99999, textvariable=self.tr_preview_count, width=6).pack(side=tk.LEFT, padx=6)
        ttk.Label(row, text="章 · 模型").pack(side=tk.LEFT, padx=(0, 8))
        self.network_model_box = ttk.Combobox(row, textvariable=self.tr_model_var, state="readonly",
            postcommand=lambda: self.network_model_box.configure(values=self.tr_model_box.cget("values")))
        self.network_model_box.pack(side=tk.LEFT, fill=tk.X, expand=True)
        actions = ttk.Frame(frame)
        actions.pack(fill=tk.X, pady=(6, 0))
        self.btn_network_preview = ttk.Button(actions, text="一键获取并试译", style="Accent.TButton",
                                              command=self._action_network_workflow)
        self.btn_network_preview.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_network_full = ttk.Button(actions, text="确认后获取并翻译全部", state=tk.DISABLED,
                                           command=lambda: self._action_network_workflow(full=True))
        self.btn_network_full.pack(side=tk.LEFT)
        self.dl_url_var.trace_add("write", lambda *_: self._sync_network_workflow())
        for variable in self.tr_language_vars.values():
            variable.trace_add("write", lambda *_: self._sync_network_workflow())
        self._sync_network_workflow()

    def _sync_network_workflow(self):
        if not hasattr(self, "btn_network_full"):
            return
        from translator.acquisition.network_workflow import network_path
        ready = False
        try:
            saved = load_json(network_path(self.dl_url_var.get(), self.cfg), {})
            source = Path(saved.get("source", ""))
            state = load_json(workflow_path(source, self.cfg), {}) if source.is_file() else {}
            languages = [code for code, var in self.tr_language_vars.items() if var.get()]
            ready = ready_for_full(state, source, languages)
        except (OSError, ValueError):
            pass
        self.btn_network_full.configure(state=tk.NORMAL if ready and not getattr(self, "task_busy", False) else tk.DISABLED)

    def _action_network_workflow(self, *, full=False, url=None):
        from translator.acquisition.network_workflow import run_network_workflow
        from translator.acquisition.source_epub import normalize_work_url
        from translator.engine import ensure_model, TranslationCancelled
        if getattr(self, "task_busy", False):
            return
        try:
            online_url = normalize_work_url(url or self.dl_url_var.get())
            count = int(self.tr_preview_count.get().strip())
            if count < 1:
                raise ValueError("试译章节数必须大于零。")
            model = self.tr_model_var.get().strip()
            if not model:
                raise ValueError("请先选择已安装的翻译模型。")
            languages = [code for code, var in self.tr_language_vars.items() if var.get()]
            if not languages:
                raise ValueError("请在翻译页至少选择一种输出语言，默认简体中文。")
        except ValueError as exc:
            self._dialogs.showwarning("请检查输入", str(exc))
            return
        self.dl_url_var.set(online_url)
        self.cfg["model"] = model
        self.stop_requested = False
        self.workflow_running = True
        self.reading_chapters.clear()
        self.reading_book = online_url
        if self.web_reader_server is not None:
            self.web_reader_server.reset(online_url)
        task_cfg = {**self.cfg, "bilingual_output": self.tr_bilingual_var.get(),
                    "_translation_cancelled": lambda: self.stop_requested,
                    "_reading_update": lambda *payload: self.log_queue.put(("reading", payload)),
                    "_translation_activity": lambda message: self.log_queue.put(("status", message))}
        self._set_busy(True, "正在获取剩余章节并翻译……" if full else f"正在获取并试译前 {count} 章……", cancellable=True)
        def worker():
            try:
                ensure_model(task_cfg, interactive=False)
                def progress(percent, message):
                    self.log_queue.put(("progress", percent))
                    self.log_queue.put(("status", message))
                state = run_network_workflow(online_url, task_cfg, languages, count, full=full,
                    progress=progress, source_ready=lambda source, work: self.log_queue.put(("network_source_ready", (source, work))))
                self.log_queue.put(("progress", 100.0))
                self.log_queue.put(("workflow_complete", state))
            except TranslationCancelled:
                self.log_queue.put(("translate_stopped", None))
            except Exception as exc:
                self.log_queue.put(("error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _network_source_ready(self, payload):
        source, work = payload
        self.current_work = work
        self.tr_epub_var.set(str(source))
        self.reading_book = str(source.resolve())
        if self.web_reader_server is not None:
            self.web_reader_server.reset(self.reading_book)
        self.dl_toc_listbox.delete(0, tk.END)
        for i, episode in enumerate(work.episodes, 1):
            self.dl_toc_listbox.insert(tk.END, f"{i}. {episode['title']}")
        self.dl_info_lbl.configure(text=f"作品：{work.title} · 目录 {len(work.episodes)} 章")
        self._select_page("translate")

    def _setup_workflow_controls(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(row, text="先试译前").pack(side=tk.LEFT)
        self.tr_preview_count = tk.StringVar(value="20")
        ttk.Spinbox(row, from_=1, to=99999, textvariable=self.tr_preview_count, width=6).pack(side=tk.LEFT, padx=6)
        ttk.Label(row, text="章").pack(side=tk.LEFT, padx=(0, 12))
        ttk.Button(row, text="固定术语", command=self._workflow_terms).pack(side=tk.LEFT, padx=3)
        ttk.Button(row, text="检查 / 局部重译", command=self._workflow_check).pack(side=tk.LEFT, padx=3)
        ttk.Button(row, text="阅读试译", command=self._workflow_read).pack(side=tk.LEFT, padx=3)
        self.workflow_status = tk.StringVar(value="选择小说 → 一键试译 → 审核术语与译文 → 确认翻译全部")
        label = ttk.Label(parent, textvariable=self.workflow_status, style="Muted.TLabel", wraplength=780)
        label.pack(anchor=tk.W, fill=tk.X, pady=(4, 0))
        label.bind("<Configure>", lambda event: label.configure(wraplength=max(1, event.width)))
        self.tr_epub_var.trace_add("write", lambda *_: self._sync_workflow())
        for variable in self.tr_language_vars.values():
            variable.trace_add("write", lambda *_: self._sync_workflow())

    def _sync_workflow(self):
        if not hasattr(self, "btn_full_tr"):
            return
        source_text = self.tr_epub_var.get().strip()
        languages = [code for code, var in self.tr_language_vars.items() if var.get()]
        state = {}
        if source_text:
            try:
                state = load_json(workflow_path(Path(source_text), self.cfg), {})
            except (OSError, ValueError):
                pass
        self.workflow_state = state
        ready = bool(source_text) and ready_for_full(state, Path(source_text), languages)
        busy = getattr(self, "workflow_running", False) or getattr(self, "task_busy", False)
        self.btn_full_tr.configure(state=tk.NORMAL if ready and not busy else tk.DISABLED)
        if not busy:
            if ready:
                records = [state["previews"][code] for code in languages]
                counts = " / ".join(str(r["chapters"]) for r in records)
                issues = sum(r["issues"] for r in records)
                self.workflow_status.set(f"已试译 {counts} 章 · {issues} 条待核对线索 · 审核后可确认翻译全部")
                if state.get("stage") == "complete":
                    self.workflow_status.set("全书翻译完成 · 可检查、局部重译和阅读")
            else:
                self.workflow_status.set("选择小说 → 一键试译 → 审核术语与译文 → 确认翻译全部")

    def _workflow_result(self):
        self._sync_workflow()
        languages = [code for code, var in self.tr_language_vars.items() if var.get()]
        state = getattr(self, "workflow_state", {})
        for code in languages:
            result = state.get("full_outputs", {}).get(code) if state.get("stage") == "complete" else None
            result = result or state.get("previews", {}).get(code)
            if result:
                return result
        self._dialogs.showinfo("请先试译", "先选择小说并点击“一键试译”，完成后即可审核和阅读。")
        return None

    def _workflow_terms(self):
        result = self._workflow_result()
        if not result:
            return
        self.proj_dir_var.set(result["project"])
        self.glossary_scope_var.set("project")
        self._refresh_glossary_table()
        self._select_page("glossary")
        candidates = load_json(Path(result["candidates"]), {}).get("candidates", [])
        if candidates:
            self._workflow_candidate_dialog(candidates)

    def _workflow_candidate_dialog(self, candidates):
        dialog = tk.Toplevel(self)
        dialog.title("确认试译术语")
        dialog.geometry("650x480")
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text="选择候选词，填写确定的译名后加入术语表；不确定的词可以跳过。", wraplength=600).pack(anchor=tk.W)
        actions = ttk.Frame(frame)
        actions.pack(side=tk.BOTTOM, fill=tk.X, pady=(10, 0))
        target = tk.StringVar()
        ttk.Label(actions, text="确定译名").pack(side=tk.LEFT)
        ttk.Entry(actions, textvariable=target).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        tree = ttk.Treeview(frame, columns=("source", "count"), show="headings", selectmode="browse")
        tree.heading("source", text="候选原文")
        tree.heading("count", text="出现次数")
        tree.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        for c in candidates:
            tree.insert("", tk.END, values=(c["source"], c["count"]))
        tree.bind("<<TreeviewSelect>>", lambda _: target.set(""))
        def accept():
            selection = tree.selection()
            if not selection or not target.get().strip():
                return
            from translator.glossary.manager import GlossaryEntry, merge_glossaries
            source = tree.item(selection[0], "values")[0]
            self.current_glossary_entries = merge_glossaries(
                self.current_glossary_entries, [GlossaryEntry(source=source, target=target.get().strip())], overwrite=True)
            self._filter_glossary_view()
            self._action_save_glossary_gui(silent=True)
            tree.delete(selection[0])
            target.set("")
        ttk.Button(actions, text="加入并保存", command=accept).pack(side=tk.RIGHT)

    def _workflow_check(self):
        result = self._workflow_result()
        if result:
            self.audit_proj_var.set(result["project"])
            self._select_page("audit")
            self._action_audit_compliance()

    def _workflow_read(self):
        result = self._workflow_result()
        if not result:
            return
        from translator.reading.saved_reading import SavedReading
        limit = None if self.workflow_state.get("stage") == "complete" else result["chapters"]
        def worker():
            try:
                self.log_queue.put(("saved_reading_ready", SavedReading(
                    Path(result["project"]) / "translation-project.json", chapter_limit=limit)))
            except Exception as exc:
                self.log_queue.put(("saved_reading_error", str(exc)))
        self.saved_reading_loading = True
        threading.Thread(target=worker, daemon=True).start()

    def _workflow_complete(self, state):
        self.workflow_running = False
        self._set_busy(False)
        self.workflow_state = state
        self._sync_workflow()
        self._sync_network_workflow()
        results = state["full_outputs"] if state["stage"] == "complete" else state["previews"]
        for code, result in results.items():
            self._append_log(f"[{code}] {result['chapters']} 章 · 检查线索 {result['issues']} 条 · {result['output']}\n")
        self.status_var.set("全书翻译与检查完成" if state["stage"] == "complete" else "试译完成，等待你审核并确认翻译全部")
        self._workflow_read()
