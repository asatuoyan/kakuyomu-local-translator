"""Bounded acquisition queue; one ordered translator with independent checkpoints."""
import queue
import threading
from pathlib import Path

from network_workflow import acquire_source, network_path
from project_storage import atomic_json, load_json
from source_epub import SourceChapter, build_source_epub, extract_epub_chapters


def run_streaming_workflow(url, cfg, language, progress=None, counts=None):
    from main import translate_epub_language, translation_work_dir, TranslationCancelled, check_translation_cancelled
    folder = network_path(url, cfg).parent
    folder.mkdir(parents=True, exist_ok=True)
    messages = queue.Queue(maxsize=3)
    stopped = threading.Event()
    failure = []
    result = []
    cancelled = cfg.get("_translation_cancelled", lambda: False)
    task_cfg = {**cfg, "_translation_cancelled": lambda: stopped.is_set() or cancelled()}
    tally = {"acquired": 0, "translated": 0, "total": 0}
    lock = threading.Lock()

    def report(kind, percent, message):
        with lock:
            if counts:
                counts(dict(tally))
            if progress:
                combined = (tally["acquired"] + tally["translated"]) / max(1, tally["total"] * 2) * 99
                progress(combined, message)

    def put(message):
        while True:
            check_translation_cancelled(task_cfg)
            try:
                messages.put(message, timeout=.1)
                return
            except queue.Full:
                pass

    def ready(source, work, cache):
        # A stable snapshot isolates translation from the final EPUB replacement.
        prefix = []
        for episode in work.episodes:
            record = cache.get(episode["url"])
            if not record:
                break
            images = [{**image, "data": (folder / image["local_path"]).read_bytes()}
                      for image in record.get("images", [])]
            prefix.append(SourceChapter(episode["url"], record["title"], record["paragraphs"],
                                        record["blocks"], images))
        snapshot = folder / "stream" / source.name
        cover = None
        saved = load_json(network_path(url, cfg), {})
        if saved.get("cover_media") and (folder / "cover.bin").is_file():
            cover = ((folder / "cover.bin").read_bytes(), saved["cover_media"])
        build_source_epub(work, prefix, snapshot, cover=cover)
        if prefix:
            metadata, normalized_prefix = extract_epub_chapters(snapshot, folder / "stream" / "prefix-assets")
        else:
            metadata = {"title": work.title, "author": work.author, "description": work.description}
            normalized_prefix = []
        with lock:
            tally["total"] = len(work.episodes)
        put(("ready", snapshot, len(work.episodes), metadata, normalized_prefix))

    def chapter_ready(index, chapter):
        # Parse one chapter through the same EPUB importer as normal jobs, so
        # paragraph, image and chapter identities remain compatible on resume.
        single = folder / "stream" / "incoming.epub"
        from source_epub import WorkInfo
        work = WorkInfo(url, "chapter", "", "", "", [])
        build_source_epub(work, [chapter], single)
        _, normalized = extract_epub_chapters(single, folder / "stream" / "assets")
        if len(normalized) != 1:
            raise ValueError("获取章节缺少可翻译正文")
        normalized[0].url = f"epub://chapter_{index:04d}"
        with lock:
            tally["acquired"] = index
        report("acquisition", 0, f"已获取 {index}/{tally['total']} 章 · 翻译同步进行")
        put(("chapter", normalized[0]))

    def producer():
        try:
            result.append(acquire_source(url, task_cfg, 1, full=True,
                progress=lambda p, m: report("acquisition", p, m),
                work_ready=ready, chapter_ready=chapter_ready))
        except Exception as exc:
            failure.append(exc)
        finally:
            if not stopped.is_set() and not cancelled():
                try:
                    put(("end",))
                except TranslationCancelled:
                    pass

    thread = threading.Thread(target=producer, daemon=True)
    thread.start()

    def receive():
        while True:
            check_translation_cancelled(task_cfg)
            try:
                return messages.get(timeout=.1)
            except queue.Empty:
                if not thread.is_alive():
                    if failure:
                        raise failure[0]
                    raise RuntimeError("获取流程提前结束")

    def chapters():
        while True:
            message = receive()
            if message[0] == "end":
                if failure:
                    raise failure[0]
                return
            if message[0] != "chapter":
                raise RuntimeError("获取章节顺序异常")
            yield message[1]

    def finalize():
        thread.join()
        if failure:
            raise failure[0]
        return result[0][0]

    def translated(index):
        with lock:
            tally["translated"] = index
        report("translation", 0, f"已翻译 {index}/{tally['total']} 章 · 首次译名已保存")

    try:
        first = receive()
        if first[0] != "ready":
            if failure:
                raise failure[0]
            raise ValueError("无法读取作品目录")
        snapshot, total, metadata, prefix = first[1:]
        translation_cfg = {**task_cfg, "_chapter_stream": chapters(), "_stream_total": total,
                           "_stream_finalize": finalize, "_chapter_completed": translated,
                           "_stream_metadata": metadata, "_stream_prefix": prefix}
        output = translate_epub_language(snapshot, translation_cfg, language,
                                        lambda p, m: report("translation", p, m))
        path = network_path(url, cfg)
        state = load_json(path, {})
        state.update(stage="complete", translated_chapters=total)
        atomic_json(path, state)
        return result[0][0], output, translation_work_dir(snapshot, cfg, language)
    except Exception as exc:
        stopped.set()
        # Never allow an old producer to write cache files after a new task starts.
        thread.join()
        path = network_path(url, cfg)
        state = load_json(path, {})
        state.update(stage="stopped" if isinstance(exc, TranslationCancelled) else "failed", error=str(exc))
        atomic_json(path, state)
        raise
    finally:
        stopped.set()
