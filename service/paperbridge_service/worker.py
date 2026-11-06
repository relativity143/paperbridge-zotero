"""Isolated BabelDOC worker: one secret-bearing JSON input, bounded JSON events.

No credentials appear in argv, environment, tracebacks, logs, or output metadata.
The engine runs in its own process group so cancellation also stops its children.
"""
import asyncio
from contextlib import contextmanager
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import uuid

from .config import ENGINE_VERSION, redact, valid_pdf


class SharedRateLimiter:
    """Gate provider attempts across isolated workers without persisting secrets.

    Each live worker holds a lease lock. A cancelled/crashed worker releases that
    lock automatically, so the remaining workers can reclaim its QPS lease.
    Only the final admission check is locked; sleeps never hold the shared lock
    or reserve future request slots. Simultaneous settings use the lowest QPS.
    """

    def __init__(self, directory, endpoint, secret, qps):
        key = hashlib.sha256(json.dumps([endpoint, secret]).encode()).hexdigest()
        self.directory = Path(directory) / key
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._mutex = threading.Lock()
        self._lock = os.open(self.directory / "lock", os.O_CREAT | os.O_RDWR, 0o600)
        self._lease_path = self.directory / (uuid.uuid4().hex + ".lease")
        self._lease = None
        try:
            with self._locked():
                self._lease = os.open(self._lease_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                fcntl.flock(self._lease, fcntl.LOCK_EX)
                os.write(self._lease, str(qps).encode())
        except BaseException:
            self.close()
            raise

    @contextmanager
    def _locked(self):
        fcntl.flock(self._lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(self._lock, fcntl.LOCK_UN)

    def _active_qps(self):
        limits = []
        for path in self.directory.glob("*.lease"):
            fd = os.open(path, os.O_RDWR)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    qps = int(os.read(fd, 32))
                    if not 1 <= qps <= 10:
                        raise ValueError("Invalid rate limit lease")
                    limits.append(qps)
                else:
                    path.unlink()  # Its owner exited; no time-based lease expiry.
            finally:
                os.close(fd)
        return min(limits)

    def wait(self, _request=None):
        with self._mutex:
            while True:
                with self._locked():
                    interval = 1.0 / self._active_qps()
                    now = time.monotonic()
                    state = self.directory / "last-start"
                    previous = float(state.read_text()) if state.exists() else 0.0
                    # Monotonic timestamps can reset at reboot; never wait on a
                    # future timestamp inherited from the previous boot.
                    delay = previous + interval - now if previous <= now else 0.0
                    if delay <= 0:
                        pending = self.directory / (self._lease_path.stem + ".pending")
                        fd = os.open(pending, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
                        try:
                            os.write(fd, str(now).encode())
                        finally:
                            os.close(fd)
                        os.replace(pending, state)
                        return
                time.sleep(delay)

    def close(self):
        with self._mutex:
            if self._lock is None:
                return
            with self._locked():
                self._lease_path.unlink(missing_ok=True)
                if self._lease is not None:
                    os.close(self._lease)
                    self._lease = None
            os.close(self._lock)
            self._lock = None

    def __enter__(self):
        return self

    def __exit__(self, *_error):
        self.close()


def translation_worker_count(qps):
    # Keep requests in flight through an eight-second response latency budget;
    # QPS still gates starts. Bound threads across the two default PDF workers.
    return min(32, qps * 8)


@contextmanager
def protected_asset_downloads(directory):
    """Keep BabelDOC's lazy model/font/CMap downloads safe between workers.

    All three loaders resolve assets.download_file at call time in 0.6.4. The
    original downloader retains its retries and hash verification; readers see
    the final file only after a complete, verified download replaces it.
    """
    from babeldoc.assets import assets
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    original = assets.download_file

    async def download(client=None, url=None, path=None, sha3_256=None):
        path = Path(path)
        key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
        lock = os.open(directory / (key + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        pending = path.with_name(path.name + ".paperbridge-" + uuid.uuid4().hex + ".part")
        try:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.05)
            if assets.verify_file(path, sha3_256):
                return
            fd = os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            await original(client=client, url=url, path=pending, sha3_256=sha3_256)
            # Keep an explicit verification boundary even if the upstream
            # downloader's return behavior changes within the pinned runtime.
            if not assets.verify_file(pending, sha3_256):
                raise ValueError("Engine asset integrity verification failed")
            os.replace(pending, path)
        finally:
            try:
                pending.unlink(missing_ok=True)
            finally:
                os.close(lock)  # Releases the lock after cancellation/errors.

    assets.download_file = download
    try:
        yield
    finally:
        assets.download_file = original


class OCRRequiredError(ValueError):
    pass


class PageSelectionError(ValueError):
    pass


def selected_page_indexes(pages, page_count):
    """Expand validated page ranges, bounded by the actual document length."""
    selected = set()
    for part in (pages or "1-").split(","):
        if "-" in part:
            start, end = part.split("-")
            start, end = int(start or 1), int(end or page_count)
        else:
            start = end = int(part)
        selected.update(range(start - 1, min(end, page_count)))
    return sorted(selected)


def error_message(error):
    name = type(error).__name__ if isinstance(error, BaseException) else "TranslationError"
    messages = {
        "AuthenticationError": "API authentication failed. Check your key and endpoint.",
        "PermissionDeniedError": "API access denied. Check model permissions.",
        "RateLimitError": "The API rate or quota limit was reached.",
        "APIConnectionError": "The translation API could not be reached.",
        "APITimeoutError": "The translation API timed out.",
        "BadRequestError": "The API rejected a translation request. Check model compatibility and settings.",
        "ExtractTextError": "The PDF has insufficient extractable text. OCR may be needed before translation.",
        "ScannedPDFError": "这份 PDF 被识别为扫描版。请补齐 OCR 文字层后重试。",
        "OCRRequiredError": "所选页面没有可提取的文字。请先做 OCR 文字识别，再翻译；仅更换模型无法识别扫描图片。",
        "PageSelectionError": "所选页码不在这份 PDF 中，请检查翻译页码设置。",
        "InputFileGeneratedByBabelDOCError": "This PDF appears to be a previous BabelDOC output. Select the original PDF.",
        "ModuleNotFoundError": "The BabelDOC engine or a dependency is missing. Run the installer again.",
        "FileNotFoundError": "A required PDF or engine asset is missing.",
    }
    return messages.get(name, "PDF translation failed. Check the PDF, API configuration, and engine assets; raw provider details were withheld to protect credentials.")


async def translate(payload, emit):
    config = payload["config"]
    if payload.get("rateLimitDir"):
        with SharedRateLimiter(payload["rateLimitDir"], config["baseUrl"], config["apiKey"], config["qps"]) as limiter:
            with protected_asset_downloads(Path(payload["rateLimitDir"]) / "asset-locks"):
                await _translate(payload, emit, limiter)
    else:
        await _translate(payload, emit)


async def _translate(payload, emit, limiter=None):
    from importlib.metadata import version
    if version("BabelDOC") != ENGINE_VERSION:
        raise RuntimeError("Unsupported BabelDOC version")
    from babeldoc.format.pdf.high_level import async_translate, init
    from babeldoc.format.pdf.translation_config import TranslationConfig, WatermarkOutputMode
    from babeldoc.translator.translator import OpenAITranslator, set_translate_rate_limiter
    import pymupdf

    config = payload["config"]
    output_dir = Path(payload["outputDir"]).resolve()
    working_dir = output_dir / "work"
    input_dir = output_dir / "input"
    input_dir.mkdir(exist_ok=True)
    source = input_dir / "source.pdf"
    shutil.copyfile(payload["inputPath"], source)
    with pymupdf.open(source) as doc:
        if doc.needs_pass:
            emit({"type": "error", "error": "Password-protected PDFs must be unlocked before translation."})
            return
        if not len(doc):
            raise ValueError("PDF has no pages")
        selected = selected_page_indexes(config["pages"], len(doc))
        if not selected:
            raise PageSelectionError("Selected pages are outside the PDF")
        if not any(any(char.isalnum() for char in doc[i].get_text()) for i in selected):
            raise OCRRequiredError("OCR text is required for the selected pages")
    emit({"type": "progress", "stage": "Preparing layout model and fonts (first run may download assets)", "progress": 0})
    init()
    translator = OpenAITranslator(lang_in=config["sourceLang"], lang_out=config["targetLang"], model=config["model"], base_url=config["baseUrl"], api_key=config["apiKey"], send_temperature=False)
    if limiter is not None:
        # HTTPX runs request hooks for every attempt, including OpenAI SDK and
        # BabelDOC retries. Cached translations never reach the HTTP client.
        translator.client._client.event_hooks["request"].append(limiter.wait)
    set_translate_rate_limiter(config["qps"])
    translation_workers = translation_worker_count(config["qps"])
    translation = TranslationConfig(
        translator=translator, input_file=str(source), lang_in=config["sourceLang"], lang_out=config["targetLang"], doc_layout_model=None,
        pages=config["pages"] or None, output_dir=output_dir, working_dir=working_dir,
        debug=False, no_dual=False, no_mono=not config["generateMono"], qps=config["qps"], pool_max_workers=translation_workers,
        watermark_output_mode=WatermarkOutputMode.NoWatermark,
        use_rich_pbar=False, report_interval=0.5,
        use_alternating_pages_dual=config["dualMode"] == "alternating",
        custom_system_prompt=config["customPrompt"] or None,
        auto_extract_glossary=False, save_auto_extracted_glossary=False,
        auto_enable_ocr_workaround=True,
    )
    font_init = getattr(translation.doc_layout_model, "init_font_mapper", None)
    if font_init:
        font_init(translation)
    finished = False
    async for event in async_translate(translation):
        kind = event.get("type")
        if kind in ("progress_start", "progress_update", "progress_end"):
            progress = event.get("overall_progress")
            if not isinstance(progress, (int, float)) or not math.isfinite(progress):
                progress = None
            emit({"type": "progress", "stage": redact(event.get("stage", "Translating"), config["apiKey"]), "progress": min(99.9, max(0, progress)) if progress is not None else None})
        elif kind == "error":
            emit({"type": "error", "error": error_message(event.get("error"))})
            return
        elif kind == "finish":
            result = event["translate_result"]
            outputs = {"dual": str(result.dual_pdf_path)}
            if config["generateMono"]:
                outputs["mono"] = str(result.mono_pdf_path)
            for path in outputs.values():
                p = Path(path).resolve()
                if not p.is_relative_to(output_dir) or not valid_pdf(p):
                    raise ValueError("Invalid generated output")
                with pymupdf.open(p) as doc:
                    if not len(doc) or doc.needs_pass:
                        raise ValueError("Invalid generated PDF")
            emit({"type": "finish", "outputs": outputs})
            finished = True
            break
    if not finished:
        raise RuntimeError("Engine did not finish")


def main():
    # Keep one private output FD for our protocol; discard third-party/native logs.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), sys.stdout.fileno())
        os.dup2(sink.fileno(), sys.stderr.fileno())
    logging.disable(logging.CRITICAL)
    def emit(event):
        protocol.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
        protocol.flush()
    try:
        payload = json.loads(sys.stdin.read(65537))
        asyncio.run(translate(payload, emit))
    except BaseException as error:
        emit({"type": "error", "error": error_message(error)})
        return 1
    finally:
        protocol.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
