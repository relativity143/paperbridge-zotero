# PaperBridge 0.2.0 与沉浸式翻译 Zotero 插件比较

核验日期：2026-11-04。本文比较公开源码能确认的任务流程，说明 PaperBridge 0.2.0 的实现和验证边界，不宣称实测速度优于沉浸式翻译。已验证行为见[验证记录](verification.md)。

## 比较对象与证据范围

沉浸式翻译的对象是官方 [zotero-immersivetranslate](https://github.com/immersive-translate/zotero-immersivetranslate) 插件，不是浏览器网页翻译功能。核验时官方公开 `main` 为提交 [`d7bf50f516788e195fb841deb36f070e48b16da6`](https://github.com/immersive-translate/zotero-immersivetranslate/commit/d7bf50f516788e195fb841deb36f070e48b16da6)，日期为 2026-08-21；[package.json](https://github.com/immersive-translate/zotero-immersivetranslate/blob/d7bf50f516788e195fb841deb36f070e48b16da6/package.json#L4) 版本为 **0.0.24**。本地参考副本与此提交一致，相关源码无本地修改。

公开插件会上传 PDF、创建官方云任务、查询进度并下载结果。私有云后端的工作进程数、模型请求并发、缓存策略、服务端版本和实际吞吐量不能由这份前端源码确定。前端一次可并行查询六个任务，不等于服务器同时翻译六篇。

PaperBridge 的比较对象是本仓库的 **0.2.0** 插件与本地服务源码，固定使用 BabelDOC 0.6.4。PDF 解析、布局分析和输出在本机完成，文本发给用户配置的 OpenAI 兼容 API；该 API 可以是远程服务，也可以是本机模型。

## 功能比较

| 项目 | 沉浸式翻译公开 Zotero 插件 0.0.24 | PaperBridge 0.2.0 |
|---|---|---|
| 多篇提交 | 支持多选；逐篇上传和建单，建单后继续下一篇，不等前篇翻译结束 | 支持多选，最多两个提交请求同时执行 |
| 文档处理并行度 | 多个云任务可同时在途；服务器实际运行数量未知 | 默认同时处理两篇；启动参数 `--max-workers` 可设 1–4；另有八个排队位置 |
| 后台查询 | 独立中央监控，每轮最多六个状态请求并行，轮间隔三秒 | 独立于任务窗口；最多四个查询并行，正常两秒，失败后退避至最多三十秒 |
| 自动导入 | 完成后下载并导入 Zotero | 完成后导入，有目标库检查、附件去重及独立的“重试导入” |
| 完成后打开 | `autoOpenPDF` 默认开启，可在设置关闭；开启时直接打开阅读器 | 后台完成不抢焦点；仅任务窗口可见且已聚焦时自动打开，也可手动打开 |
| QPS 与线程 | 插件未提供 QPS/翻译线程设置；云端内部控制未知 | 插件 QPS 默认 1，可设 1–10；同 API 地址和密钥的多篇共享上限；每篇翻译线程上限为 `min(32, QPS × 8)` |
| 重试 | 上传、建单、下载配置三次重试，状态请求十次，默认间隔一秒；另有任务手动重试 | 提交结果不明时可复用任务 ID；可重试导入；引擎和 SDK 的实际重试请求同样经过共享 QPS 限制 |
| 重启 | 保存未完成任务；已有云任务 ID 时恢复查询 | Zotero 重启后恢复查询和导入；本地服务重启会将未完成翻译标为中断，不自动重译 |
| 翻译缓存 | 前端未见翻译内容缓存；云端策略未知 | 使用 BabelDOC 默认翻译缓存；命中时不发送模型请求 |

任务窗口关闭与 Zotero 退出是不同状态。PaperBridge 在任务窗口关闭后仍可查询和导入，前提是 Zotero 仍运行。本地服务可在 Zotero 退出后继续计算，但附件导入要等 Zotero 再次启动；停止本地服务则会中断未完成任务。

共享 QPS 按 API 地址与密钥分组。同组活动任务设置不同时，采用较低的 QPS。例如两篇均设为 2 QPS 时，每篇最多使用 16 个翻译线程，但两篇合计的模型请求仍受共享 2 QPS 限制。线程数用于容纳等待回复的工作，不代表每秒发送的请求数，也不代表论文处理数量。

## 源码定位

沉浸式翻译的固定提交链接：

- [多篇选取、排队与恢复已有云任务](https://github.com/immersive-translate/zotero-immersivetranslate/blob/d7bf50f516788e195fb841deb36f070e48b16da6/src/modules/translate/task.ts#L150)：多选在 150–165 行；建单后继续下一项及复用任务 ID 在 340–396 行。
- [上传、创建云任务并加入监控](https://github.com/immersive-translate/zotero-immersivetranslate/blob/d7bf50f516788e195fb841deb36f070e48b16da6/src/modules/translate/translate.ts#L17)：17–57 行。
- [独立中央监控与自动导入](https://github.com/immersive-translate/zotero-immersivetranslate/blob/d7bf50f516788e195fb841deb36f070e48b16da6/src/modules/translate/task-monitor.ts#L21)：监控在 21–69 行；导入和打开在 324–344 行。任务窗口自己的刷新间隔另见 `task-manager.ts`，窗口关闭清理的是界面刷新。
- [默认自动打开设置](https://github.com/immersive-translate/zotero-immersivetranslate/blob/d7bf50f516788e195fb841deb36f070e48b16da6/addon/prefs.js#L8)、[API 路径及重试参数](https://github.com/immersive-translate/zotero-immersivetranslate/blob/d7bf50f516788e195fb841deb36f070e48b16da6/src/api/index.ts#L28)、[启动时恢复任务](https://github.com/immersive-translate/zotero-immersivetranslate/blob/d7bf50f516788e195fb841deb36f070e48b16da6/src/hooks.ts#L48)。

PaperBridge 的对应实现：

- [插件任务控制器](../addon/content/paperbridge.js)：`startReady`、`refreshJobs`、`schedule`、`importTask` 和 `openResult`。
- [本地任务管理](../service/paperbridge_service/jobs.py)：`JobManager`、`submit`、`start` 和 `_load`；[启动参数](../service/paperbridge_service/server.py)：`--max-workers`。
- [引擎适配](../service/paperbridge_service/worker.py)：`translation_worker_count`、`SharedRateLimiter` 和 `_translate`；[设置界面](../addon/content/window.xhtml)：QPS 提示。
- [固定引擎依赖](../requirements-engine-lock.txt)：BabelDOC 0.6.4。其 `BaseTranslator.translate` / `llm_translate` 在调用模型前查询缓存，`OpenAITranslator` 默认 `ignore_cache=False`；PaperBridge 保留这一行为。

## 0.2.0 验证与不能推出的结论

本次集成验证使用两个真实 BabelDOC 工作进程和本地模拟模型服务，两篇任务在 **24.808 秒**内完成；观察到的 **12 次本地模型请求**符合共享 **2 QPS** 限制，最小请求启动间隔为 **0.5012 秒**，外部模型请求数为 **0**。该验证检查双任务处理、实际引擎输出和跨进程限速是否衔接正确，结果汇总见[验证记录](verification.md)。模拟模型不提供真实翻译质量，测试 PDF、缓存状态、模型延迟与用户论文不同。

这不是与沉浸式翻译的同条件速度对照，也不是商用 API 性能基准，不能据此判断哪一方更快、译文更好或成本更低。若要比较这些结果，需要在同一批原始 PDF、可比模型、语言、输出模式和缓存条件下单独记录端到端耗时、失败率、费用及译文质量。

0.2.0 借鉴的重点是让任务提交、后台查询和界面显示相互独立，并控制完成后打开行为。实际速度仍由 API 响应、共享速率、文本量、缓存、布局和本机资源共同决定。
