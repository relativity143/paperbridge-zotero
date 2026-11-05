# Architecture

PaperBridge contains a privileged Zotero add-on and a separate authenticated local service.

```text
Zotero: select PDFs and confirm tasks
  → authenticated loopback HTTP service
  → bounded queue (2 document workers by default)
  → BabelDOC: extract text and reconstruct layout
  → user's OpenAI-compatible API: translate text
  → local bilingual PDF
  → Zotero background poller: import and name attachment
```

The add-on owns credential storage, task preferences, library permissions, menus, imports and filenames. The task window is a view of the controller; closing it does not stop polling. Startup resumes saved query/import work without opening that window.

The service owns a data-directory lock, job history, worker lifecycle and output validation. Each PDF has an isolated worker process group. Cancellation stops only that group. Restarted services mark unfinished jobs interrupted and do not automatically resubmit potentially billed work.

Provider settings remain in memory and pass to workers via standard input. The shared limiter groups endpoint/key pairs by a digest and persists only leases/timing, not raw credentials. HTTP hooks also gate retries. Per-asset locks and verified atomic replacement protect parallel engine-resource downloads.

The service requires a pairing token and validates Host/Origin headers. Outputs must be valid PDFs within their job directory. Import is idempotent, with task tags recovering relationships after file-organizer changes.

BabelDOC 0.6.4 is pinned because its internal Python interfaces require contract/integration checks when upgrading. See [service details](../service/README.md), [verification](verification.md) and [reference comparison](immersive-comparison-2026-11-04.md).

