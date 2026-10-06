"""Backup browsing independent of readable project metadata."""
from pathlib import Path

from translator.storage.backups import list_backups, restore


class BackupMixin:
    def recover_backup_transactions(self):
        from translator.storage.group_backups import recover_group_restore
        root = Path(self.cfg["output_dir"])
        for journal in root.glob("**/.backups/groups/restore.json"):
            recover_group_restore(journal.parent.parent.parent)

    def _backup_idle(self):
        if self.restart_requested or any(task.get("running") or task.get("recovering")
                for task in (self.task, self.acquisition_task)):
            raise ValueError("请先暂停全部任务，等待保存完成，再操作整组备份")

    def create_group_backup(self, body):
        from translator.storage.group_backups import create_group
        with self.lock:
            self._backup_idle()
            folder = self._backup_root(body.get("project", ""))
            return {"id": create_group(folder)}

    def restore_group_backup(self, body):
        from translator.storage.group_backups import restore_group
        with self.lock:
            self._backup_idle()
            folder = self._backup_root(body.get("project", ""))
            restore_group(folder, body.get("id"))
            if self.task.get("project") == body.get("project"):
                self.task.update(output="", percent=0, message="整组备份已恢复，请继续翻译重新生成 EPUB")
                self.task.pop("counts", None)
            self._json_cache.clear()
            self._glossary_counts.clear()
            self._source_completeness.clear()
            self._discovery_until = 0
            return {"restored": True}
    def _backup_root(self, identity):
        root = Path(self.cfg["output_dir"]).resolve()
        folder = (root / str(identity)).resolve()
        if not folder.is_relative_to(root) or not folder.is_dir():
            raise ValueError("作品目录不存在")
        return folder

    def backups(self, body):
        root = Path(self.cfg["output_dir"]).resolve()
        if not body.get("project"):
            # .backups lives directly inside each record's parent directory.
            folders = sorted({p.parent.parent if p.parent.name == "chapters" else p.parent
                              for p in root.rglob(".backups") if p.is_dir()})
            folders = sorted(set(folders) | {p.parent for p in root.rglob("translation-project.json")
                                           if ".backups" not in p.parts})
            return {"projects": [str(p.relative_to(root)) for p in folders
                                  if p.is_relative_to(root)]}
        folder = self._backup_root(body["project"])
        records = []
        for parent in (folder, folder / "chapters"):
            history = parent / ".backups"
            if not history.is_dir():
                continue
            for entry in sorted(history.iterdir()):
                if not entry.is_dir():
                    continue
                record = parent / entry.name
                if record.name not in {"glossary.json", "translation-project.json", "project.json", "network.json"} and parent.name != "chapters":
                    continue
                for saved in list_backups(record):
                    records.append({**saved, "file": str(record.relative_to(folder))})
        from translator.storage.group_backups import list_groups
        return {"backups": records, "groups": list_groups(folder)}

    def restore_backup(self, body):
        with self.lock:
            if self.restart_requested or any(task.get("running") or task.get("recovering")
                    for task in (self.task, self.acquisition_task)):
                raise ValueError("请先停止获取、翻译和自动恢复，再恢复备份")
            folder = self._backup_root(body.get("project", ""))
            path = (folder / str(body.get("file", ""))).resolve()
            if not path.is_relative_to(folder) or path.name not in {
                "glossary.json", "translation-project.json", "project.json", "network.json"
            } and not (path.parent == folder / "chapters" and path.suffix == ".json"):
                raise ValueError("无效的备份文件")
            restore(path, body.get("id"))
            self._json_cache.clear()
            self._glossary_counts.clear()
            self._source_completeness.clear()
            self._discovery_until = 0
            return {"restored": True}
