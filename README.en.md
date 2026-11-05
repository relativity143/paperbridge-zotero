# PaperBridge — bilingual PDFs for Zotero

Translate academic PDFs with your own OpenAI-compatible API, process layouts locally with BabelDOC, and import bilingual results into Zotero automatically.

[中文说明](README.md) · [Download](https://github.com/relativity143/paperbridge-zotero/releases/latest) · [Issues](https://github.com/relativity143/paperbridge-zotero/issues)

**Version 0.2.0 · macOS · Zotero 8–10 · AGPL-3.0-or-later**

## Features

- Bring your own Chat Completions endpoint, model and key, including compatible local models.
- Side-by-side or alternating-page bilingual PDFs. Extra monolingual output is optional and disabled by default.
- Two documents processed concurrently by default, eight additional queued jobs and individual cancellation.
- Automatic import while the task window is closed; background completion does not open tabs or steal focus.
- Author–year–title filenames, preferring the original English title except for Chinese journals or Chinese-language items.
- Shared API request limits across documents, including retries, with BabelDOC translation caching.

## Install on macOS

You need Zotero 8–10, a working `python3`/`pip`, and an OpenAI-compatible API. Apple Silicon with Zotero 10.0.7 has been validated. Windows/Linux installers are not provided.

1. Download `paperbridge-0.2.0-macos.zip` from [Releases](https://github.com/relativity143/paperbridge-zotero/releases/latest) and extract it. It contains source, scripts and `dist/paperbridge-0.2.0.xpi`, but not the Python runtime or model assets.
2. Run `setup.command` to download Python 3.12 and pinned dependencies, then `enable-background.command` to install the login service. You can close the terminal afterward.
3. In Zotero, use **Tools → Plugins → gear menu → Install Add-on From File** and select the XPI in `dist/`.
4. Open **Tools → 纸桥翻译：任务与设置** and configure your API. The plugin interface is primarily Chinese.

If double-clicking scripts does not work, run `bash setup.command` and `bash enable-background.command` in the project directory. For a Git checkout, first run `python3 scripts/build.py`. Installing only the XPI does not install the engine. The first translation may download layout models and fonts.

Select papers or PDFs, choose **纸桥翻译：双语 PDF** from the context menu, review tasks and start translation. Results become attachments of the original item; originals are preserved.

## API and background behavior

Use the API root, typically ending in `/v1`, rather than the full `/chat/completions` URL. Remote endpoints require HTTPS; loopback HTTP endpoints such as `http://127.0.0.1:11434/v1` may omit a key. Native Claude Messages, Gemini and Responses protocols are not directly supported.

QPS accepts integers 1–10 and is shared by active documents with the same endpoint/key. Differing settings use the lowest active limit. Each document may use `min(32, QPS × 8)` threads waiting for responses. More documents do not multiply the shared rate or guarantee a speedup.

Closing the task window does not stop translation or import. If Zotero exits, the service may continue translating, but import waits for Zotero to restart. Stopping the service interrupts unfinished jobs; they are not resubmitted automatically.

## Data handling

PDF extraction, layout and generation run locally. Extracted text goes to your configured API. Keys live in Zotero's credential manager, not ordinary settings or task records. The authenticated service binds only to `127.0.0.1` and rejects browser-origin requests.

State and outputs live in `~/Library/Application Support/PaperBridge/`; BabelDOC cache lives in `~/.cache/babeldoc/`. Releases contain no user keys, pairing tokens, documents or task history. API tests make a short real generation request; provider charges may apply.

## Update and stop

Update both the service and add-on. Wait until all jobs finish, run `bash stop-background.command`, then run `bash enable-background.command` from the new folder. Install the XPI through Zotero's plugin manager, rather than overwriting live profile files. Settings and outputs are preserved.

The stop script also disables login startup; the enable script restores it. `start.command` is for foreground debugging while the background service is stopped. Updates are manual.

## Limitations and development

Image-only pages need OCR first; the plugin does not recognize text in images itself. Existing text layers can use compatibility handling. Encrypted PDFs need unlocking, and complex fonts, tables or formulas may fail or produce layout differences. Speed depends on the model, API limits, PDF and local resources.

```bash
python3 scripts/build.py
node --test tests/*.test.cjs
PYTHONPATH=service .venv/bin/python -m unittest discover -s service/tests -v
```

Tests use local mock providers; full adapter tests need the pinned engine installed. Version 0.2.0 passed 33 add-on tests and 40 service tests, plus actual BabelDOC concurrency and native Zotero background-import checks. These are not commercial-model speed or quality benchmarks. See [verification](docs/verification.md), [architecture](docs/architecture.md) and [changelog](CHANGELOG.md).

PaperBridge references the workflow of the [Immersive Translate Zotero plugin](https://github.com/immersive-translate/zotero-immersivetranslate), analyzed using [REA](https://github.com/morluto/rea), and uses [BabelDOC](https://github.com/funstory-ai/BabelDOC) 0.6.4. It is an independent project. Source uses [AGPL-3.0-or-later](LICENSE); see [NOTICE](NOTICE.md).

**Keywords:** zotero, zotero-plugin, paperbridge, pdf-translation, bilingual-pdf, babeldoc, openai-compatible, custom-api, academic-reading, paper-translation, translation, macos, python, javascript.

