"""Short-lived native path dialogs for interactive entry points."""
from pathlib import Path


class DialogCancelled(Exception):
    """The user cancelled selection; no console fallback should follow."""


_UNAVAILABLE = object()


def _run_dialog(method, *, module="filedialog", **options):
    root = None
    owns_root = False
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
        root = tk._default_root
        if root is None:
            root = tk.Tk()
            owns_root = True
            root.withdraw()
        provider = filedialog if module == "filedialog" else messagebox
        return getattr(provider, method)(parent=root, **options)
    except Exception:
        return _UNAVAILABLE
    finally:
        if owns_root and root is not None:
            try:
                root.destroy()
            except Exception:
                pass


def _selected_path(selected, title, *, default=None):
    if selected is _UNAVAILABLE:
        value = input(f"{title}: ").strip().strip('"')
        selected = value or (str(default) if default is not None else "")
    if not selected:
        raise DialogCancelled()
    return Path(selected)


def select_epub_file(title="选择 EPUB 电子书"):
    return select_file(title, [("EPUB", "*.epub"), ("所有文件", "*.*")])


def choose_web_source():
    """Web requests cannot fall back to console input or expose a browser file path."""
    selected = _run_dialog("askopenfilename", title="重新选择原文 EPUB", filetypes=[("EPUB", "*.epub")])
    if selected is _UNAVAILABLE:
        raise ValueError("文件选择窗口无法打开，请在恢复设置中输入原文 EPUB 的完整路径")
    if not selected:
        return {"cancelled": True}
    path = Path(selected).resolve()
    if not path.is_file() or path.suffix.lower() != ".epub":
        raise ValueError("请选择有效的本地 EPUB 文件")
    return {"source": str(path)}


def select_file(title="选择文件", filetypes=None):
    types = filetypes or [("术语表", "*.csv *.xlsx *.xls *.json"), ("所有文件", "*.*")]
    return _selected_path(_run_dialog("askopenfilename", title=title, filetypes=types), title)


def select_directory(title="选择项目文件夹"):
    return _selected_path(_run_dialog("askdirectory", title=title, mustexist=True), title)


def select_save_file(title="保存术语表", *, initial_path=None, filetypes=None):
    options = {"title": title, "filetypes": filetypes or [("Excel", "*.xlsx"), ("CSV", "*.csv"), ("JSON", "*.json")]}
    if initial_path is not None:
        initial_path = Path(initial_path)
        options.update(initialdir=str(initial_path.parent), initialfile=initial_path.name, defaultextension=initial_path.suffix)
    return _selected_path(_run_dialog("asksaveasfilename", **options), title, default=initial_path)


def select_import_source():
    title = "选择导入来源"
    folder = _run_dialog("askyesnocancel", module="messagebox", title=title,
                         message="批量导入整个文件夹？\n选择“是”打开文件夹，“否”打开单个文件。")
    if folder is _UNAVAILABLE:
        return _selected_path(_UNAVAILABLE, title)
    if folder is None:
        raise DialogCancelled()
    if folder:
        return select_directory(title)
    return select_file(title, [("小说文件", "*.txt *.md *.umd *.jar"), ("所有文件", "*.*")])
