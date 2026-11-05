# Changelog

## 0.2.0 — 2026-11-04

- Process two PDFs concurrently by default; bounded configuration supports 1–4.
- Poll/import independently of the task window and resume saved query/import work at Zotero startup.
- Import silently in the background; auto-open only while the task window is visible and focused.
- Share API limits across worker processes, including retries, and increase capacity for waiting responses.
- Protect concurrent model/font/CMap downloads with locks and verified atomic replacement.
- Show active, queued and imported task counts.

The public repository and release bundle were prepared on 2026-11-05. Packaging now stays in `dist/`, setup guidance uses the current version, and scripts do not depend on a private development-directory layout. The add-on binary and translation behavior remain the tested 0.2.0 version.

## Earlier versions

- **0.1.5:** extend compatibility to Zotero 10.
- **0.1.4:** bypass stale controller caches during native add-on updates.
- **0.1.3:** author–year–title naming and attachment recovery after file-organizer changes.
- **0.1.2:** extra monolingual output becomes opt-in, disabled by default.
- **0.1.1:** repair legacy relations causing sync errors; add macOS background-service management.
- **0.1.0:** initial add-on, local engine service, custom API settings and bilingual attachment import.
