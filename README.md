<p align="center"><img src="addon/content/icon.svg" width="88" alt="PaperBridge 纸桥翻译"></p>

# 纸桥翻译 · PaperBridge

**在 Zotero 中用自己的 API 翻译论文，自动保存双语 PDF**

A Zotero add-on for bilingual academic PDFs, powered by local BabelDOC processing and your own OpenAI-compatible API.

[English](README.en.md) · [下载安装包](https://github.com/relativity143/paperbridge-zotero/releases/latest) · [反馈问题](https://github.com/relativity143/paperbridge-zotero/issues)

当前版本 **0.2.1**。面向 **macOS、Zotero 8–10**；已在 Apple Silicon / Zotero 10.0.7 验证。由 Zotero 插件和本地 PDF 服务组成，首次设置后服务在后台运行，不需要一直开着启动器。

## 功能

- **自选模型和 API**：填写 OpenAI Chat Completions 兼容接口、模型和密钥，也支持兼容该协议的本地模型。
- **双语 PDF**：同页左右对照或原文/译文交替页，保留原 PDF；额外单语译文默认关闭；新生成的 PDF 不附加引擎宣传水印。
- **多篇翻译**：支持多选论文，默认同时处理两篇，另有八个排队位置；可单独取消任务。
- **后台自动导入**：关闭任务窗口后继续查询和回存；后台完成不打开新标签、不抢焦点。
- **自动命名**：“第一作者 - 年份 - 标题”；英文期刊优先英文原题，中文期刊或中文条目使用已有中文题名。
- **共享限速与缓存**：同 API 地址和密钥的任务共享 QPS，重试同样限速，保留 BabelDOC 翻译缓存。
- **翻译偏好**：源/目标语言、页码范围、术语提示；已有文字层的扫描 PDF 可用 OCR 兼容处理。

## 安装（macOS）

准备 Zotero 8–10、可用的 `python3` 与 `pip`，以及兼容的 API。首次安装需要联网下载 Python 3.12、引擎及依赖；首次翻译还可能下载布局模型、字体等资源。

1. 从 [Releases](https://github.com/relativity143/paperbridge-zotero/releases/latest) 下载 **`paperbridge-0.2.1-macos.zip`** 并解压。它包含源码、安装脚本和 `dist/paperbridge-0.2.1.xpi`，不包含预下载的 Python 或模型。
2. 在解压后的 `paperbridge-zotero` 文件夹中运行 **`setup.command`**，安装本地引擎。
3. 运行 **`enable-background.command`** 安装后台服务。完成后可关闭终端；以后登录 macOS 自动启动。
4. 在 Zotero 打开 **工具 → 插件**，从齿轮菜单选择 **从文件安装插件 / Install Add-on From File**，安装 `dist/paperbridge-0.2.1.xpi`。
5. 打开 **工具 → 纸桥翻译：任务与设置**，填写 API 配置并保存测试。

如果双击脚本无法打开，在终端进入项目文件夹运行：

```bash
bash setup.command
bash enable-background.command
```

从 Git 克隆时先运行 `python3 scripts/build.py`，在 `dist/` 生成 XPI。仅安装 XPI 不会安装本地引擎。Windows/Linux 安装包尚未提供。

## 使用

选中一篇或多篇论文/PDF，右键选择 **纸桥翻译：双语 PDF**。确认任务与设置后点击 **开始翻译**。结果自动作为附件回到原文献，原 PDF 保留。

默认仅生成双语版。需要额外译文时，在 **API 与设置 → 翻译偏好** 勾选 **额外生成单语译文 PDF**；对新任务生效。

关闭翻译窗口后，Zotero 仍运行即可自动导入。完全退出 Zotero 时，本地服务可继续翻译，导入等待下次启动。停止服务会中断未完成任务，重启后需手动重试，不会自动重复提交模型请求。

## API 设置

| 设置 | 填写方式 |
| --- | --- |
| API Base URL | 接口根地址，通常以 `/v1` 结尾，例如 `https://api.example.com/v1`；不要填完整 `/chat/completions` |
| 模型 | 服务商实际支持的模型标识 |
| API Key | 对应地址的密钥，保存在 Zotero 登录凭据管理器 |
| QPS | 每秒启动请求的上限，整数 1–10；同地址和密钥的任务共享限制 |
| 本地模型 | 例如 `http://127.0.0.1:11434/v1`；本机接口可不填密钥 |

远程 API 必须使用 HTTPS。当前支持 **OpenAI Chat Completions**，不直接支持 Claude Messages、Gemini 或 Responses 原生协议。测试按钮会发送一条短生成请求；费用由所选 API 服务商决定。

API 延迟、论文长度、缓存和本机排版都会影响总耗时。两篇同时处理可减少串行等待，但不会把共享 QPS 翻倍。每篇最多使用 `min(32, QPS × 8)` 个翻译线程等待模型回复；线程数、每秒请求数、同时处理的论文数是不同概念。

## 数据与隐私

- PDF 解析、排版和生成在本机完成；抽取的文本发送给你配置的 API。
- 密钥保存在 Zotero 凭据管理器，不写入源码、普通设置或任务记录；服务只在内存和工作进程标准输入中传递密钥。
- 服务只监听 `127.0.0.1`，使用随机令牌配对，不接受普通浏览器来源的请求。
- 任务和输出位于 `~/Library/Application Support/PaperBridge/`；BabelDOC 缓存在 `~/.cache/babeldoc/`。
- 分发内容不包含用户密钥、配对文件、原论文、翻译结果或个人任务记录。

## 更新与停止

**从 0.2.0 升到 0.2.1，水印修复只需更新本地服务；原有 0.2.0 插件可继续使用。** 新安装请使用同版本完整包。 等当前翻译结束后，在新版文件夹运行：

```bash
bash stop-background.command
bash enable-background.command
```

然后通过 Zotero 插件管理器安装新版 XPI。不要直接覆盖运行中的 Zotero profile 内的插件文件，以免触发脚本缓存不一致。任务、PDF 和 API 设置会保留。

停止脚本同时关闭登录自启；启用脚本可恢复。`start.command` 用于前台调试，只在后台服务已停用时使用。当前采用手动更新。

## 常见问题与限制

- **找不到本地服务**：检查启用脚本是否完成；日志在 `~/Library/Application Support/PaperBridge/logs/`。
- **扫描件失败**：不自带 OCR 识别。没有可提取文字的页面需先补 OCR 文字层；更换模型不能替代这一步。
- **速度不稳定**：检查模型延迟、API 限额与 QPS；多篇合计不会突破共享请求速率。
- **特殊排版**：加密 PDF 需先解锁，复杂公式、字体和表格可能失败或出现布局差异；不保证逐像素还原。
- **文档并行数**：默认 2，前台服务启动参数可设 `--max-workers 1` 至 `4`，插件界面暂不提供此选项。

## 开发与验证

```bash
python3 scripts/build.py
node --test tests/*.test.cjs
PYTHONPATH=service .venv/bin/python -m unittest discover -s service/tests -v
```

Node.js 仅用于插件测试。Python 测试使用本地模拟接口；完整引擎适配测试需先安装 `requirements-engine-lock.txt`。

| 目录 | 内容 |
| --- | --- |
| `addon/` | Zotero 插件与界面 |
| `service/` | 本地服务、BabelDOC 适配与测试 |
| `tests/` | 插件控制器和启动测试 |
| `scripts/` | 打包与 macOS 后台服务管理 |
| `docs/` | 架构、验证及参考项目比较 |
| `dist/` | 本地安装包，Git 忽略，通过 Releases 分发 |

0.2.0 已通过 33 项插件测试、40 项服务测试，并完成真实 BabelDOC 双任务及 Zotero 后台导入验证。真实模型质量与商用 API 性能需单独评价。详见[验证记录](docs/verification.md)、[架构](docs/architecture.md)和[版本记录](CHANGELOG.md)。

## 参考与许可证

使用 [BabelDOC](https://github.com/funstory-ai/BabelDOC) 0.6.4 处理 PDF，通过 [REA](https://github.com/morluto/rea) 分析过 [Immersive Translate Zotero 插件](https://github.com/immersive-translate/zotero-immersivetranslate)的工作流。PaperBridge 不是沉浸式翻译官方版，不依赖其账号或云翻译服务。

源码采用 [AGPL-3.0-or-later](LICENSE)，第三方依赖遵循各自许可证。参见[来源说明](NOTICE.md)及[参考插件比较](docs/immersive-comparison-2026-11-04.md)。

## Keywords / 关键词

`zotero` · `zotero-plugin` · `paperbridge` · `pdf-translation` · `bilingual-pdf` · `babeldoc` · `openai-compatible` · `custom-api` · `academic-reading` · `paper-translation` · `translation` · `macos` · `python` · `javascript`

Zotero 翻译插件、论文翻译、双语 PDF、自定义 API、本地排版、后台导入、学术阅读。
