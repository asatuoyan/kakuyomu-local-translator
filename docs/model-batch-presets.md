# 模型初始分批预设

| 模型名称识别 | 初始原文字数目标 | 初始段落数 | 自适应字数上限 |
|---|---:|---:|---:|
| Hy-MT2 30B-A3B（含本机 hy-mt2-30b） | 1024 | 16 | 2200 |
| Hy-MT2 7B | 800 | 12 | 2200 |
| Hy-MT2 1.8B | 512 | 8 | 1536 |
| Murasaki 8B / 14B | 1024 | 16 | 1536 |
| 未识别模型 | 800 | 8 | 2200 |

这些值是本程序的初始分批目标，不是模型硬限制，也不是测速结论。
Murasaki 上限参考[官方前端建议](https://github.com/soundstarrain/Murasaki-Translator/blob/main/README.md)；
腾讯各尺寸与未知模型采用保守工程预设，后续随成功率和耗时调整。

较小的 `translation_chunk_chars` 和 `translation_chunk_paragraphs` 配置优先。
实际批次还受上下文预算、术语表、前文参考和整段边界影响，可能小于目标；
单个超长段落继续保留整段，可能超过目标。

EPUB 任务优先读取 Ollama `/api/ps` 的运行上下文；尚未加载时读取
`/api/show` 中 `num_ctx`，缺失或探测失败保守采用 4096。
不会使用 GGUF 原生最大上下文自动扩大显存分配。
日志显示匹配的预设、当前目标、上限和运行窗口。

混元 30B 的具体策略见 [专用说明](hy-mt2-30b.md)：
它的预算系数为 4，默认最多带 256 字前文，请求窗口默认 4096。
`hy_mt_prefer_whole_chapter=true` 时，剩余整章能放下则优先一次提交，
否则才按照表中的目标分批；上限和生成预算继续生效。

仅在 `adaptive_translation_batches` 未关闭时应用。
新模型可以在 `model_batch_presets.py` 的识别表中增加，不需要修改分批控制器。
