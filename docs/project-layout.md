# 项目目录与开发

业务代码集中在 `translator/`，按职责分包。根目录只保留三个 Python 启动入口、启动批处理、配置示例、依赖清单、README 和许可证；用户配置及运行数据仍保留原路径。

| 位置 | 用途 |
|---|---|
| `run.bat`、`run_gui.bat`、`run_web.bat` | 双击启动入口 |
| `main.py`、`gui.py`、`web_app.py` | CLI、GUI、Web 的兼容启动入口 |
| `translator/engine.py` | 不依赖界面的翻译核心，不创建窗口或请求用户输入 |
| `translator/ui/cli.py`、`translator/ui/file_dialogs.py` | 命令行菜单、交互流程与文件选择弹窗 |
| `translator/acquisition/browser_session.py` | GUI、Web 与 CLI 共用的浏览器会话配置；Cookie 确认由 CLI 处理 |
| `translator/ui/` | Web 服务、GUI 界面、对话框与阅读服务 |
| `translator/ui/web_library.py` | 书库发现、元数据缓存与最近任务 |
| `translator/ui/web_recovery.py`、`translator/ui/task_errors.py` | 自动恢复、停止、重试与失败原因分类 |
| `translator/ui/web_queue.py` | 获取、翻译的持久化队列与独立调度 |
| `translator/ui/web_backups.py`、`translator/storage/backups.py` | 备份浏览、恢复与有数量上限的有效文件快照 |
| `translator/storage/group_backups.py` | 同一时点的项目整组快照、文件校验与可接续的恢复事务 |
| `translator/ui/web_book_updates.py` | 原站增量更新检查与获取后的续译调度 |
| `translator/ui/web_diagnostics.py` | 不含正文、凭据或任意日志的诊断信息导出 |
| `translator/acquisition/` | 网站获取、浏览器正文读取、原文章节与 EPUB 处理 |
| `translator/formats/` | EPUB 追加、文本导入、JAR 与 UMD 格式解析 |
| `translator/translation/` | 翻译流程、获取与翻译流水线、提示词、模型预设、自适应分批与质量检查 |
| `translator/glossary/` | 术语管理与首次译名提取 |
| `translator/storage/` | 项目记录、翻译缓存、章节保存与 EPUB 导出 |
| `translator/reading/` | 已保存作品的读取与阅读缓存 |
| `translator/domain.py`、`translator/languages.py` | 共享章节数据、文本处理与语言设置 |
| `translator/config.py`、`translator/paths.py` | 配置读写与统一的项目、资源路径 |
| `web/` | Web 应用与阅读器的 HTML、JavaScript 静态资源 |
| `tests/` | Python 回归测试与 Node.js 前端交互测试 |
| `scripts/` | 环境安装辅助脚本和本机模型性能测试 |
| `docs/` | 专题文档与目录说明 |
| `translator/version.py` | 统一版本号 |
| `config.example.json`、`requirements.txt` | 配置示例与依赖清单 |
| `config.json`、`output/`、`browser-profile-*/`、`.venv/` | 用户配置、项目数据、浏览器会话与本机环境，路径保持不变 |

直接运行 `python main.py`、`python gui.py` 和 `python web_app.py` 的方式保持可用。`main.py` 执行时启动 `translator.ui.cli`，导入时仍指向 `translator.engine` 的翻译 API；命令行菜单和文件选择函数改从 `translator.ui.cli` 或 `translator.ui.file_dialogs` 导入。`gui`、`web_app` 导入时仍指向各自实现模块。新增代码和测试使用完整包路径，例如 `translator.engine`、`translator.ui.web_app`、`translator.domain`、`translator.acquisition.chapter_reader` 和 `translator.storage.translation_book`。

配置、网页资源和启动脚本统一使用 `translator.paths` 中的项目根目录，业务代码的位置不会改变 `config.json`、`output/`、浏览器配置或旧作品的路径。移动模块时应同时更新测试中的 mock 目标，指向实际实现模块。

从项目根目录执行验证：

```powershell
.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -t . -v
node --test tests/test_web_*.js tests/test_reader_updates.js
```

也支持原来的 `python -m unittest discover`。运行单个测试模块时使用包名，例如 `python -m unittest tests.test_streaming_workflow`。

环境准备仍由根目录的启动批处理自动调用 `scripts/setup_environment.ps1`。模型性能测试可以使用 `python -X utf8 -m scripts.bench_hy_mt --sizes 5 10`，直接运行 `python scripts/bench_hy_mt.py --sizes 5 10` 也可用。
