# Verification

## Version 0.2.0

Recorded on 2026-11-04: macOS Apple Silicon, Zotero 10.0.7, Python 3.12 and BabelDOC 0.6.4. These are development acceptance results, not guarantees for all documents, operating systems or providers.

| Check | Result |
| --- | --- |
| Add-on tests | 33 passed: background import, recovery, bounded submissions/queries, shutdown, naming and deduplication |
| Service tests | 40 passed: worker pool, cancellation, authentication, shared QPS/retries, OCR checks and resource protection |
| Actual engine | Two distinct synthetic PDFs processed by overlapping BabelDOC workers; valid bilingual-only results |
| Rate limit | 12 mock-provider requests at aggregate 2 QPS; minimum observed start interval 0.5012 seconds |
| Native Zotero | Version 0.2.0 active/compatible, both menus present, settings and source relationships retained |
| Background import | Three pending results imported with task window closed; all seven acceptance-batch outputs existed, reader count unchanged |
| Packaging | Deterministic XPI matched installed and tested add-on contents |

Real-engine mock integration took 24.808 seconds with cached assets and no external model requests. It validates concurrency, output production and rate limiting, not translation quality, commercial API performance or superiority to Immersive Translate.

Native import acceptance used existing completed outputs without new model requests. Personal PDFs, task history, pairing files and credentials are excluded from this repository.

## Reproduce

After installing pinned dependencies via `setup.command`:

```bash
node --test tests/*.test.cjs
PYTHONPATH=service .venv/bin/python -m unittest discover -s service/tests -v
python3 scripts/build.py
```

Included tests use local mock servers/workers and do not send text to external translation APIs. Full adapter tests require the installed engine. Native acceptance additionally requires installation through Zotero's plugin manager; unit tests alone cannot prove installation.

## Limits

- Apple Silicon macOS is validated; not every Zotero 8–10 combination has been tested.
- Image-only pages need OCR first. OCR compatibility is not a text recognizer.
- Encrypted PDFs and complex font/formula/table layouts need separate evaluation.
- Model quality, speed, provider quotas and billing depend on the configured API.

See [the reference comparison](immersive-comparison-2026-11-04.md) for its evidence boundaries.
