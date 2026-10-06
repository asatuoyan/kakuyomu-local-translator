"""Persist translated chapters and export full or checkpoint EPUBs."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from translator.languages import language_code, language_suffix, normalize_output
from translator.storage.project_storage import load_translation_state, save_translation_state
from translator.acquisition.source_epub import SourceChapter, compute_content_hash, translated_source_epub
from translator.domain import safe_name


class TranslationBook:
    """Persist completed chapters and build small EPUBs before merging the full book."""

    def __init__(self, source: Path, work_dir: Path, metadata: dict[str, Any], language: str,
                 source_chapters: list[SourceChapter] | None = None, bilingual: bool = False):
        self.work_dir = work_dir
        self.bilingual = bilingual
        self.language = language
        self.path = work_dir / "translation-project.json"
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        saved = load_translation_state(self.path)
        if saved and language_code(saved.get("language", "")) != language_code(language):
            raise ValueError("翻譯專案的目標語言已變更，請使用另一個輸出資料夾。")
        if saved and saved.get("source_hash") != source_hash:
            records = saved["chapters"]
            if source_chapters is None or len(source_chapters) < len(records) or any(
                record["url"] != source_chapters[i].url or
                record["source_hash"] != compute_content_hash(source_chapters[i].paragraphs)
                for i, record in enumerate(records)
            ):
                raise ValueError("原文已變更或章節順序不符，請先核對變更，再使用另一個輸出資料夾。")
            saved["source_hash"] = source_hash
            saved["epub_dirty"] = True
            save_translation_state(self.path, saved)
        self.state = saved or {"source_hash": source_hash, "language": language,
                               "metadata": metadata, "chapters": []}
        self.state["source_path"] = str(source.resolve())
        self.metadata = self.state["metadata"]
        for key in ("title", "description"):
            if self.metadata.get(key):
                self.metadata[key] = normalize_output(self.metadata[key], language)
        self.output = work_dir / f"{safe_name(self.metadata['title'])}_{language_suffix(language)}.epub"
        if bilingual:
            self.output = self.output.with_name(self.output.stem + "_双语对照.epub")

    def completed(self, index: int, chapter: SourceChapter) -> bool:
        records = self.state["chapters"]
        if index > len(records):
            return False
        if records[index - 1]["url"] != chapter.url or (
            records[index - 1]["source_hash"] != compute_content_hash(chapter.paragraphs)
        ):
            raise ValueError("原文章節已變更，請使用另一個輸出資料夾以免覆蓋已譯內容。")
        if "source_paragraphs" not in records[index - 1]:
            records[index - 1]["source_paragraphs"] = chapter.paragraphs
            save_translation_state(self.path, self.state, [index - 1])
        return True

    def save(self, index: int, original: SourceChapter, translated: SourceChapter) -> None:
        if index != len(self.state["chapters"]) + 1:
            raise ValueError("章節必須依原書順序接續寫入。")
        self.state["chapters"].append({
            "url": original.url, "source_hash": compute_content_hash(original.paragraphs),
            "source_paragraphs": original.paragraphs,
            "title": translated.title, "paragraphs": translated.paragraphs,
            "blocks": translated.blocks,
            "images": [{k: str(v) if isinstance(v, Path) else v for k, v in image.items()
                        if k != "data"} for image in translated.images],
        })
        save_translation_state(self.path, self.state, [index - 1])

    def _chapters(self, start: int, end: int) -> list[SourceChapter]:
        chapters = [SourceChapter(url=record["url"], title=record["title"],
                              paragraphs=record["paragraphs"], blocks=record["blocks"],
                              images=record["images"])
                for record in self.state["chapters"][start - 1:end]]
        if self.bilingual:
            for chapter, record in zip(chapters, self.state["chapters"][start - 1:end]):
                originals = record.get("source_paragraphs")
                if originals is None or len(originals) != len(chapter.paragraphs):
                    raise ValueError("双语对照输出缺少匹配的原文，请重新读取原始 EPUB。")
                blocks = chapter.blocks or [{"type": "text", "text": p}
                                            for p in chapter.paragraphs]
                paired = []
                position = 0
                for block in blocks:
                    if block.get("type") == "text":
                        paired.append({"type": "text", "text": originals[position],
                                       "role": "original"})
                        paired.append({**block, "role": "translation"})
                        position += 1
                    else:
                        paired.append(dict(block))
                chapter.blocks = paired
        return chapters

    def _build(self, start: int, end: int, output: Path) -> Path:
        temporary = output.with_suffix(".tmp.epub")
        translated_source_epub(self.metadata, self._chapters(start, end), temporary, self.language)
        temporary.replace(output)
        return output

    def checkpoint(self, index: int, total: int) -> list[Path]:
        outputs = []
        if index % 10 == 0 or index == total:
            start = ((index - 1) // 10) * 10 + 1
            part = self.work_dir / f"{self.output.stem}_{start:04d}-{index:04d}.epub"
            if not part.exists() or self.state.get("epub_dirty"):
                outputs.append(self._build(start, index, part))
        if index % 100 == 0 and (
            self.state.get("merged_up_to", 0) < index or not self.output.exists()
        ):
            outputs.append(self._build(1, index, self.output))
            self.state["merged_up_to"] = index
            save_translation_state(self.path, self.state)
        return outputs

    def finish(self, total: int) -> Path:
        if len(self.state["chapters"]) != total:
            raise ValueError("尚有章節未完成，不能合併全書。")
        output = self._build(1, total, self.output)
        self.state["merged_up_to"] = total
        self.state["epub_dirty"] = False
        save_translation_state(self.path, self.state)
        return output
