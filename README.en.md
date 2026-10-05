# Novel EPUB acquisition and local translation

[简体中文说明书](README.md)

Download original novels from Kakuyomu (カクヨム) and Syosetu (小説家になろう), then translate EPUB files with a local Ollama model. The project provides a local Web interface, a desktop GUI, and a CLI, with resumable projects, glossary management, translation checks, and selective retranslation.

With the default settings, novel text stays between your computer, the novel website, and local Ollama. If you configure a remote `ollama_url`, text is sent to that endpoint.

## Installation and startup

Requirements: Windows 10/11, Python 3.11, and Ollama. Models require additional disk space and memory; closing other GPU-intensive programs can help.

1. Install Python and Ollama.
2. Install a translation model. The default is Q4_K_M:

   ```powershell
   ollama pull hf.co/tencent/Hy-MT2-7B-GGUF:Q4_K_M
   # Optional, if sufficient memory is available:
   ollama pull hf.co/tencent/Hy-MT2-7B-GGUF:Q6_K
   ```

3. Double-click `run_web.bat` for the local Web interface, `run_gui.bat` for the desktop GUI, or `run.bat` for the CLI.

The first launch creates `.venv` and installs dependencies. Later launches only check dependencies when `requirements.txt` or the interpreter changes; ordinary startup does not run pip or install a browser. To force a dependency check, run the selected launcher with `-Update`, such as `run_web.bat -Update`.

`config.example.json` contains defaults. The first launch creates `config.json`; loading fills in missing settings and validates common values and connection addresses. Settings saves preserve unrelated fields and relative paths.

Website acquisition uses an installed Microsoft Edge browser by default. Chrome, a custom Chromium executable, and Playwright Chromium are also supported. Playwright Chromium is downloaded only when you first select it and it is not installed. Translating local EPUB files does not require a browser download.

The interfaces currently use Simplified Chinese labels. This English manual explains their operation; it does not change the interface language. The CLI can offer to download a missing model. Install models before starting GUI or Web tasks.

## Local Web interface

There are two pages: **获取并翻译** (Acquire and translate) and **术语** (Glossary).

### Acquire and translate

1. Start `run_web.bat`; your default browser opens automatically.
2. Enter a Kakuyomu or Syosetu work URL and select an installed translation model.
3. Click **获取并翻译**. Translation starts after the first chapter is acquired, while the acquisition thread continues fetching later chapters. Translation follows the original chapter order. There is no separate preview or full-book confirmation step in the Web interface.
4. Watch the independent acquired/translated chapter counts. Saved chapters appear under **我的作品** (My books), where **阅读** (Read) opens the reader. Once acquisition, translation, and glossary capture have all finished, download the complete EPUB.

Expand **本地 EPUB 与翻译设置** (Local EPUB and translation settings) to enter a local EPUB path or change the output language. A local file is translated directly.

The model dropdown loads installed models from the configured `ollama_url`. It prefers the configured model when available, otherwise the first installed model. Click **刷新模型** (Refresh models) after installing a new model. A missing model or connection failure disables task startup. The server also validates the model before starting. Model selection is locked while a task runs.

Acquisition and translation have separate checkpoints: original text and images go to the acquisition cache; completed translations are saved chapter by chapter in the translation project. Click **停止** (Stop), wait until the task reports that it has stopped, and use **继续上次任务** (Resume a previous task) to select the same work and language again. Downloaded chapters are reused and completed translations are skipped. A failure or cancellation retains those checkpoints and does not export an incomplete full-book EPUB.

Website request delays still apply. A queue with at most three pending messages connects acquisition to translation, so the acquisition thread pauses when translation falls behind. Cancellation waits for the acquisition worker to exit before allowing another task to start; an in-progress website request may take time to finish or time out.

### First-translation names and glossary editing

After each chapter is saved, `glossary_model` extracts proper names from aligned original/translated paragraphs. If unset, the translation model is used. It copies names already present in the translation instead of inventing a new translation. An entry is accepted only when its original term exists and its target appears in the paragraph where that original term first occurs. Accepted entries are saved to the work's `glossary.json` and supplied to later chapters. Existing entries are preserved.

Capture operates at chapter boundaries; different spellings can still occur within the same chapter. AI extraction may omit or misidentify terms. Invalid extraction output stops the task while retaining saved chapters; restarting retries extraction and continues translation.

On **术语** (Glossary), select a work, edit target names, and click **保存修改** (Save changes). **导出 JSON** (Export JSON) and **导入 JSON** (Import JSON) let you send names to another AI for consolidation and import the result. Keep `source` and `target` fields intact:

```json
{
  "entries": [
    {"source": "レオン", "target": "Leon", "category": "人物名"}
  ]
}
```

Imported entries update matching original terms and are saved automatically. Stop a running task before editing or importing names; restart to use the updated glossary. Changes affect subsequent translation and do not rewrite already saved chapters.

### Running the server

The application server listens only on the local computer. No deployment is needed. Keep the launcher terminal open; closing a browser tab does not stop translation, while closing the launcher terminal exits the service. You can also run:

```powershell
.venv\Scripts\python.exe -X utf8 web_app.py --no-browser --port 8765
```

Open the full address printed in the terminal, including its generated path.

## Desktop GUI and CLI workflows

The desktop GUI's top menu provides **获取小说** (Acquire novels), **翻译小说** (Translate novels), and **检查译文** (Check translations). Acquisition offers website downloads and local imports. The translation page links to glossary management. The selected title appears at the top; settings control the model, Ollama address, and theme. The log opens from the bottom status bar. New GUI tasks default to Simplified Chinese; existing `target_language` settings still apply to the CLI and related project workflows.

### Website to original-language EPUB

Supported work URLs:

- `https://kakuyomu.jp/works/...`
- `https://ncode.syosetu.com/.../`
- `https://novel18.syosetu.com/.../`

In the GUI acquisition page, **一键获取并试译** (Acquire and preview) downloads only the selected preview range, defaulting to 20 chapters, and then translates, checks, and opens it for reading. After review, **确认后获取并翻译全部** (Confirm, acquire, and translate all) acquires remaining chapters and continues. Unlike the Web interface, this desktop workflow requires explicit confirmation after previewing. Network workflow state is saved under `network-workflows/` in the output directory.

For manual acquisition, use CLI menu 1 or the GUI download page. Enter the work URL, sign in using the application's browser if needed, load the table of contents, and select a chapter range, such as `21-40`, including both endpoints.

The source EPUB preserves text, the table of contents, illustrations, the description, and the website's official cover when available. Each downloaded chapter is saved to `source-cache.json`. Resuming skips cached chapters; it does not automatically refetch every cached chapter to check for website updates. Kakuyomu's table of contents comes from the complete embedded volume data rather than only the expanded section visible on the page. Listing a chapter does not imply permission to read it.

Default chapter requests are spaced approximately 9–12 seconds apart, with additional image delays. `request_delay_seconds` can increase that interval; acquisition enforces at least eight seconds for chapter requests. Temporary network errors can retry with backoff. Website access blocking stops acquisition.

### EPUB translation and preview review

The desktop GUI uses a preview → review → explicit full-book confirmation workflow:

1. Select a source EPUB, model, and output languages. Set **先试译前 N 章** (Preview the first N chapters), default 20, and click **一键试译** (Preview). The program creates a preview EPUB, term candidates, and a check report, then opens the reader. Short books use their actual chapter count.
2. Review names using **固定术语** (Set glossary terms), filling in and saving target names. Unconfirmed candidates are not automatically added. Use **检查 / 局部重译** (Check / selectively retranslate) to inspect and repair passages.
3. Click **确认后翻译全部** (Confirm and translate all) to continue the remaining chapters. Saved translations are reused. Changing names does not automatically retranslate completed chapters.

Preview and full translation share one resumable project. Preview EPUB filenames contain `_试译_0001-NNNN` and do not overwrite the full-book EPUB. State is saved under `workflows/`. Restarting with the same source and languages can continue. A changed source or an additional language requires an appropriate new preview; full-book continuation checks source identity. CLI menu 2 translates the full book directly.

Supported output languages are Traditional Chinese, Simplified Chinese, English, Japanese, Korean, French, German, and Spanish. Desktop multi-language tasks run languages sequentially. Each language has separate projects, caches, and glossaries under `output/<source filename>_翻譯/<language code>/`; matching legacy Chinese projects retain their directory.

Progress includes completed batches, cache hits, and resumed chapters. While a model is loading or generating, the status can show elapsed time and generated/thinking characters without increasing completed progress. A finished streaming response and aligned paragraph count are required before saving a batch.

Adaptive batching is enabled by default (`adaptive_translation_batches: true`). It starts at up to 800 characters and eight paragraphs, then grows after two fast successful batches. Repetition, alignment errors, retries, or slow batches shrink the next batch. `translation_chunk_chars` and `translation_chunk_paragraphs` remain upper limits. The program estimates context usage, reserves space for prompts, references, and output, and uses a conservative 4096-token fallback when runtime context is unavailable. An individual long paragraph is not forcibly split. Set the option to `false` for fixed batches. Larger batches do not guarantee better translations.

Enable **双语对照输出** (Bilingual output), or set `"bilingual_output": true` for CLI menu 2, to export original paragraphs followed by translations. Images keep their position. The original text is not converted between Chinese scripts; translation paragraphs use indentation and a left rule. Filenames contain `_双语对照`, preserving the plain translated EPUB. Descriptions and contents titles remain translated. Existing translations can be reused for bilingual export.

Completed chapters are skipped even after changing the model. Appended source chapters can continue an existing project when completed URLs and text still match. Changes, removals, or reordering of the translated prefix stop continuation. OpenCC normalizes Chinese titles, descriptions, and translated text to the selected script without changing the interface language.

The EPUB importer supports paragraphs, containers, lists, quotations, headings, line breaks, mixed text/images, and image-only chapters. Image paths are resolved relative to XHTML files, keeping different images that share a filename in different directories. Missing resources produce an error. Output is reformatted and may not preserve all source CSS, fonts, or interactive features.

### Reading saved books

Desktop **实时阅读** (Live reading) shows translated batches with optional original text; updating preserves the reading position. **网页 / 手机阅读** (Web / mobile reading) opens the browser reader and provides LAN addresses. Keep the computer running and connect your phone to the same Wi-Fi. If necessary, allow Python through the Windows private-network firewall.

The reader checks for updates every two seconds and provides chapter navigation, original/translation comparison, font size, night mode, and browser-local reading positions. Its collapsible table of contents groups chapters by language and volume. Volume labels come from chapter-title markers such as 第 X 卷／巻／部; chapters without a marker appear as ungrouped. Previous/next controls appear below the text. The current reader displays text; use an EPUB reader for illustrations. Closing the program stops the reading service; a new launch generates a new address.

**打开已完成小说** (Open a saved book) accepts a translated EPUB or `translation-project.json` / `project.json`, without rerunning translation or starting Ollama. Original text comparison is available when the project contains originals. Legacy projects can still show translations alone; bilingual EPUBs retain their existing bilingual text.

Saved-book indexing runs in the background. Chapters are read on demand, with the latest eight kept in memory. EPUB indexing reads its contents without extracting images. New chapter-file projects cache their reading index in the local user cache and rebuild it when source files change. Legacy monolithic JSON projects require an initial full JSON read. Reader caches do not modify the book or project.

### Append to a Chinese EPUB

CLI menu 3 takes a Japanese EPUB followed by an existing Chinese master EPUB. The master remains unchanged; its text and resources are retained, and newly translated chapters are appended to the contents and reading order. The program detects whether the master mainly uses Simplified or Traditional Chinese. Supply only Japanese chapters not already in the master; the program does not infer chapter correspondence.

### Manual single-chapter continuation

CLI menus 4–5 translate individual webpage chapters or continue older projects. Open a chapter in the application browser, return to the terminal, and press Enter. Enter `m` to change models or `q` to exit safely. EPUB output is rebuilt every ten accumulated chapters, with remaining saved results packaged on exit.

### TXT, Markdown, UMD, and JAR import

CLI menu 7 and the GUI import page support single files and folders. TXT and Markdown are split using chapter/episode markers, English chapter headings, or Markdown headings. Files use natural ordering, including Arabic, full-width, Chinese, and Japanese numerals. Supported UMD and Java ME JAR formats can provide titles, authors, contents, text, and covers.

Common encodings include UTF-8, UTF-16, CP932, GB18030, and Big5. Import detects Simplified Chinese, Traditional Chinese, or Japanese and supports local Markdown images. Text can be packaged as EPUB or imported into editable projects. When a Chinese import has no Japanese original, checks can only assess saved text, not Japanese-to-Chinese accuracy. CLI menu 8 opens the GUI.

## Models, translation checks, and cancellation

Default model choices:

| Choice | Ollama model name |
|---|---|
| Hy-MT2 7B Q4_K_M (default) | `hf.co/tencent/Hy-MT2-7B-GGUF:Q4_K_M` |
| Hy-MT2 7B Q6_K | `hf.co/tencent/Hy-MT2-7B-GGUF:Q6_K` |

Source: [Tencent's official GGUF repository](https://huggingface.co/tencent/Hy-MT2-7B-GGUF). Legacy translation-mode and proofreading settings no longer apply; saved translations are retained.

Run `ollama list` to find installed model names, including tags. Select an installed model from the Web dropdown, GUI settings/translation dropdown, or the CLI's installed-model selection. The CLI also permits entering a name manually. You can set the endpoint and model directly in `config.json`, keeping other fields:

```json
{
  "ollama_url": "http://127.0.0.1:11434",
  "model": "my-translator:latest"
}
```

Replace the example with an actual installed model supporting text chat and your target language. Desktop logs show batches, retries, and errors in a separate, read-only window. They follow new output when at the bottom and preserve position while you scroll up; paths and raw output are retained.

Translation prompts draw on [Tencent's official Hy-MT2 examples](https://huggingface.co/tencent/Hy-MT2-7B#hy-mt2-translation-task-instruction-examples-chinese-english-comparison), separating glossary references, background, and source text. Translation returns plain text rather than JSON and does not run a second proofreading pass. JSON is used separately for glossary extraction. Model-specific profiles can alter prompts and thinking behavior; see [Hy-MT2 30B documentation](docs/hy-mt2-30b.md) and [Murasaki documentation](docs/murasaki.md).

Recognizable echoed instructions or source labels trigger retries without prior translation context, paragraph by paragraph. Persistent contamination fails the batch rather than saving it. Contaminated cache entries are skipped. Completed chapters and exported EPUBs are not automatically rewritten; selectively retranslate affected passages when repairing older output.

General translation requests disable thinking with `think: false`; dedicated profiles may use different settings. A status such as “model thinking · N characters” measures reasoning text, not completed translation. Incomplete inline thinking is not written into EPUB output.

Paragraph mismatches split batches and retry. Obvious copying of previous translations or identical long translations for different originals also triggers retries. These checks catch only some problems and cannot guarantee meaning, completeness, or accuracy. Streaming responses must include a completion marker; truncated, malformed, or length-limited responses are not cached as successful translations.

Temporary model/network failures retry up to five times by default, waiting 10, 20, 40, 60, and 60 seconds. Completed batches are kept.

| Setting | Purpose |
|---|---|
| `translation_chunk_chars` / `translation_chunk_paragraphs` | Batch character/paragraph limits |
| `context_chars` | Previous-translation reference length |
| `translation_max_retries` | Retry count; `0` disables retries |
| `translation_retry_delay_seconds` | Initial retry delay |
| `request_timeout_seconds` | Network request timeout |

Stop/cancel retains completed batches and chapters and prevents late model responses from being saved. It can interrupt retry waits and model-response waiting. The underlying connection may remain active until another response or timeout, and Ollama may briefly keep processing a cancelled request. A new task gets independent cancellation state.

## Glossary checks and selective retranslation

Each work/language directory can contain `glossary.json`; matching local entries override global entries. Global `glossary` applies to Chinese. Other languages use `glossary_by_language`, for example `{"en": {"竜": "dragon"}}`, or their project glossary.

Desktop glossary management supports heuristic candidates and model scanning for people, places, organizations, skills, magic, titles, and special concepts. It offers review and CSV, Excel `.xlsx`, and JSON import/export. Supported desktop JSON structures include entry arrays, categorized objects, and source-to-target dictionaries. Web import/export uses JSON.

Audits report glossary violations by chapter and paragraph. The GUI also flags empty translations, unchanged source text, and unusually short/long translations. These are review clues, not confirmed errors.

After changing a glossary: save it → select the project in **检查译文** (Check translations) → run a check → selectively retranslate the affected paragraph, or use **更多操作** (More actions) to retranslate a named term or all terms. Issue resolution/ignore status is stored in `audit-status.json`.

Selective retranslation only calls the model for matched body paragraphs, retains other text and images, and rebuilds output. Translation projects update full-book and existing partial exports; append projects preserve the master EPUB. Completed chapters remain saved if cancelled; rerunning can finish updating the EPUB. Manual cache deletion is unnecessary.

Audits and selective retranslation support both `project.json` and `translation-project.json`. Legacy projects missing originals request the original EPUB used to create the project, validate file/chapter identity, and restore alignment. Missing originals or misaligned paragraphs produce errors rather than an unchecked “pass.”

## Storage, upgrades, and backups

| File or directory | Contents |
|---|---|
| `translation-project.json` | Translation manifest, source identity, language, and progress |
| `chapters/000001.json`, etc. | Originals, translations, image references, and chapter data; saved atomically |
| `translation-cache.sqlite3` | Translation cache committed transactionally by batch |
| `translation-project.legacy.json` | Backup retained when migrating monolithic JSON |
| `project.json` | Compatible legacy append/text-import project |
| `source-cache.json` | Original chapter download cache |
| `network.json` | Acquisition workflow state and chapter counts |
| `web-tasks.json` | Recent Web task inputs for continuation |
| `assets/`, `source-assets/`, `stream/` | Project resources and streaming acquisition snapshots |

Cache identities include model, language, glossary, chapter identity, original text, paragraph position, and relevant translation settings. Legacy `translation-cache.json` files are retained but lack enough context for new cache hits; completed project chapters still remain reusable. Results present only in an old cache may need retranslation.

Legacy projects migrate to chapter files when written, retaining their old JSON backup. Back up or move the entire project and associated acquisition directory, including image assets and streaming snapshots; copying only the manifest or SQLite file is insufficient. Keep a backup before migration or bulk glossary edits.

## Login and privacy

Each browser uses an application-specific profile directory, such as `browser-profile-msedge`, separate from your everyday browser profile. Login redirects are normal. Complete login in the application's visible browser. In manual CLI workflows, return to the work page before pressing Enter.

In the desktop GUI, **登录 Kakuyomu** (Log in to Kakuyomu) opens a visible login browser. Finish signing in, close that browser, and load the table of contents. Reuse the same application browser type to retain its login state; your ordinary browser login is not synchronized automatically.

Optional `cookies.json` accepts Playwright storage state or a cookie array and imports only Kakuyomu-domain entries. Do not share cookies, browser profiles, private output, or an uncleaned project directory. Runtime files are excluded by `.gitignore`.

The application fetches only chapters normally accessible to the current account. It does not bypass paid, membership, or other access restrictions.

## Troubleshooting

- **Website reports 不正と思われるアクセス:** stop acquisition and avoid repeated refreshes. Later, check access using an ordinary browser before resuming. Cached chapters remain available.
- **Missing contents or expired login:** confirm your account can open the work, then sign in again using the application browser and reload contents.
- **Ollama connection failure:** run `ollama serve` and check the configured address, normally `http://127.0.0.1:11434`.
- **Empty Web model list:** install a model in that Ollama instance and click Refresh models. Confirm the configured endpoint points to the intended instance.
- **Model load failure:** check the exact installed model/quantization tag. Try Q4_K_M when memory is insufficient.
- **Legacy project cannot be audited:** select the original EPUB used to create it. A newly downloaded or repackaged file may fail identity checks.
- **EPUB cannot open or images are missing:** retain the original EPUB, project, and logs; inspect source image references and keep resource directories intact.
- **Dependency startup error:** rerun the launcher with `-Update`.
- **Stopping acquisition takes time:** wait for the in-flight website request to finish or time out. The task remains active until its acquisition worker exits.

## Development checks

Run in the project's virtual environment:

```powershell
.venv\Scripts\python.exe -X utf8 -m unittest discover -v
```

Tests use local fixtures and simulated model/website responses, without downloading novels or calling real Ollama. They cover stream completeness, cancellation, retry/resume, context-aware caches, migration, selective retranslation, EPUB text/images, and simultaneous acquisition/translation. Passing these tests does not establish real-model translation quality or live-website reliability.

## Disclaimer and license

This project is for automation, website parsing, local translation research, and personal study. Rights to original novels, illustrations, and covers remain with their authors and other rights holders. Follow applicable laws and platform terms. Do not publicly distribute unauthorized content, use it for unauthorized commercial gain, bypass access restrictions, or make abusive high-frequency requests. Users are responsible for their use of the software.

Project code is licensed under the [MIT License](LICENSE).
