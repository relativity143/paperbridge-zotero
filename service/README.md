# PaperBridge local service / 纸桥翻译本地服务

The companion accepts authenticated requests from the Zotero add-on and runs BabelDOC in a separate process. It produces an original/translation bilingual PDF, with an optional monolingual translation PDF, using the engine's layout reconstruction. Translation quality depends on the PDF and configured model.

This document describes version **0.2.0**. Update both the service and the add-on (`dist/paperbridge-0.2.0.xpi`); see the [verification record](../docs/verification.md) for validated behavior and limits. Wait for active jobs to finish before stopping and updating an existing service.

## Runtime

Use Python 3.10–3.13 (3.12 recommended). Install the pinned engine requirements in a virtual environment. The root installer can use the complete engine lock file; `service/requirements.txt` records the required direct pins.

From this directory:

```sh
python -m pip install -r requirements.txt
python -m paperbridge_service
```

Alternatively set `PYTHONPATH` to this `service` directory and invoke its environment's Python with `-m paperbridge_service`. The default `--port 0` selects an available loopback port and records it in `bridge.json`. Optional arguments: `--port 19876` to select a specific port, `--data-dir /absolute/private/runtime/path`, and `--max-workers 1` through `--max-workers 4` to set concurrent PDF jobs (default: 2; use 1 to reduce memory use). `PAPERBRIDGE_DATA_DIR` also overrides the runtime directory.

Default runtime: macOS `~/Library/Application Support/PaperBridge`; Linux `$XDG_DATA_HOME/paperbridge` (default `~/.local/share/paperbridge`). The server only binds `127.0.0.1`. It creates `bridge.json` with mode 0600 containing the URL and a fresh random bearer token. A directory lock prevents a second process from modifying active task history.

BabelDOC model/font assets may download on the first translation. Health reports whether the pinned engine imports successfully, not whether every asset is already cached. Normal engine caches are maintained by BabelDOC. The adapter disables automatic glossary extraction and debug logs. Scanned PDFs need usable text/OCR; encrypted PDFs must be unlocked first.

The 2026-10-25 worker update enables BabelDOC's `auto_enable_ocr_workaround` for scans that already have OCR text. Before engine initialization or model calls, it rejects selected page ranges with no extractable alphanumeric text, and ranges entirely outside the document. It does not perform OCR or detect every partially missing text layer; those pages must be repaired before translation. Known scan/OCR errors have actionable messages without exposing provider details. This worker-only update applies to the next task without restarting the service.

## API

Every supported route requires `Authorization: Bearer <bridge token>`. Use a privileged Zotero HTTP request without an Origin header. Host must match the loopback server. All browser Origin values, including `null`, are rejected; CORS is not enabled.

- `GET /v1/health`: service status, actual cached import readiness for the pinned engine, `queueCapacity`, and `maxConcurrentJobs`.
- `POST /v1/test`: `{config}`; performs a small `/chat/completions` generation.
- `POST /v1/jobs`: `{id, inputPath, config}`; `id` is a canonical UUID and duplicate IDs return the original task without billing again.
- `GET /v1/jobs`: `{jobs:[...]}`.
- `GET /v1/jobs/<id>`: task status.
- `POST /v1/jobs/<id>/cancel`: stop a queued/running task.

Config keys are `baseUrl`, `apiKey`, `model`, `sourceLang` (default `en`), `targetLang` (default `zh`), `qps` (integer 1–10; the service fallback is 2, while the add-on explicitly defaults to 1), `pages` (blank or e.g. `1,3-5,7-`), `dualMode` (`side-by-side` or `alternating`), `generateMono`, and `customPrompt`. HTTPS is required for remote endpoints; loopback APIs may use HTTP. The base URL should include the provider's API prefix, commonly `/v1`. Unknown options and embedded URL credentials are rejected.

In 0.2.0, each PDF's translation pool uses `min(32, qps * 8)` threads to absorb response latency. A separate cross-process limiter groups active workers by API endpoint and key, and gates actual HTTP request attempts, including provider SDK and BabelDOC retries. Workers with different QPS settings for the same endpoint/key use the lowest active limit. Completed, cancelled, or crashed workers release their rate-limit leases. Cached translations do not reach the HTTP client. For example, two PDFs configured at 2 QPS may each have 16 translation threads, while their combined provider attempts remain limited to 2 QPS. This controls admission rate, not the provider's response time, token rate, or translation quality.

As of 0.1.2, `generateMono` is an optional boolean, default `false` (including clients that omit it). The worker sets BabelDOC `no_mono=true` by default and returns only `outputs.dual`. Setting `generateMono=true` additionally requires `outputs.mono`. The queue validates the exact requested output set. This change requires updating and restarting the idle service as well as updating the Zotero addon; the addon accepts historical completed tasks containing both outputs.

Public task fields: `id`, `status`, `progress` (0–100), `stage`, `createdAt`, optional `error`, and on completion `outputs: {dual, mono?}` with absolute paths. By default, two jobs run at a time and up to eight wait; `--max-workers` allows 1–4 running jobs. Each job has an isolated worker process. Cancelling frees a queue slot and terminates that job's worker process group. A stopped/crashed service marks unfinished jobs `interrupted` on restart and never automatically resubmits them.

The 0.2.0 add-on polls and imports completed results even when its task window is closed, provided Zotero remains running. Background import does not open reader tabs or steal focus; automatic opening occurs only while the task window is visible and already focused. The independent service may keep processing after Zotero exits, but attachment import waits until Zotero starts again. A service restart is different: interrupted translations require user-directed resubmission.

Provider settings exist only in memory and are sent to workers through stdin. They are absent from command-line arguments and persisted task records. The rate limiter stores an endpoint/key hash, lease limits and timing metadata, not raw credentials. Native/third-party worker stdout/stderr are suppressed; the worker emits only bounded structured progress and sanitized errors. The probe does not follow redirects with credentials. All requested output PDFs must exist inside the task's own directory; the real worker opens them with PyMuPDF before signalling completion.

## Verification

```sh
python -m unittest discover -s tests -v
```

`test_service.py` uses explicit mock workers and a local mock chat endpoint to validate lifecycle, authentication, redaction, cancellation, output boundaries and restart behavior. `test_worker_contract.py` additionally checks adapter options against installed BabelDOC 0.6.4 signatures and validates fixture PDFs using PyMuPDF; its translation events are mocked. These tests do not assert translation quality. Real engine integration with a local mock model is documented separately by the packaging verification.

The 0.2.0 integration check ran two real BabelDOC workers against a local mock model: both jobs completed in 24.808 seconds, and 12 local model requests respected their shared 2 QPS limit (minimum observed start gap: 0.5012 seconds; external model requests: 0). This verifies the two-worker output and rate-limit path; it is not a speed comparison with Immersive Translate or a benchmark of a commercial model. See the [source comparison and verification limits](../docs/immersive-comparison-2026-11-04.md) and [verification record](../docs/verification.md).
