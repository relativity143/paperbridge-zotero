"""Adapter contract test against installed BabelDOC 0.6.4 signatures.
Translation events are MOCKED; fixture PDF validation uses real PyMuPDF.
No models are downloaded and no external translation API is called.
"""
import asyncio
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from paperbridge_service.config import validate_config
from paperbridge_service.worker import SharedRateLimiter, protected_asset_downloads, translate, error_message, translation_worker_count

try:
    INSTALLED = importlib.metadata.version("BabelDOC") == "0.6.4"
except importlib.metadata.PackageNotFoundError:
    INSTALLED = False


class SharedRateLimiterTests(unittest.TestCase):
    def test_worker_pool_is_bounded_and_allows_slow_responses(self):
        self.assertEqual([translation_worker_count(qps) for qps in (1, 2, 4, 10)], [8, 16, 32, 32])

    def test_two_processes_share_request_start_budget(self):
        script = """
import json, sys, time
from paperbridge_service.worker import SharedRateLimiter
with SharedRateLimiter(sys.argv[1], 'https://example.invalid/v1', 'fixture-secret', 4) as limiter:
    starts = []
    for _ in range(6):
        limiter.wait()
        starts.append(time.monotonic())
    print(json.dumps(starts))
"""
        with tempfile.TemporaryDirectory(prefix="paperbridge-shared-rate-") as tmp:
            processes = []
            try:
                for _ in range(2):
                    processes.append(subprocess.Popen([sys.executable, "-c", script, tmp], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                starts = []
                for process in processes:
                    output, error = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 0, error)
                    starts.extend(json.loads(output))
                starts.sort()
                self.assertEqual(len(starts), 12)
                self.assertGreaterEqual(min(b - a for a, b in zip(starts, starts[1:])), 0.23)
                self.assertFalse(list(Path(tmp).rglob("*.lease")))
                persisted = " ".join(p.name + p.read_text() for p in Path(tmp).rglob("*") if p.is_file())
                self.assertNotIn("fixture-secret", persisted)
                self.assertNotIn("example.invalid", persisted)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                    process.communicate(timeout=5)

    def test_lowest_active_qps_and_crashed_lease_recovery(self):
        script = """
import os, sys
from paperbridge_service.worker import SharedRateLimiter
limiter = SharedRateLimiter(sys.argv[1], 'endpoint', 'fixture', 1)
os._exit(0)
"""
        with tempfile.TemporaryDirectory(prefix="paperbridge-rate-leases-") as tmp:
            with SharedRateLimiter(tmp, "endpoint", "fixture", 10) as fast:
                with SharedRateLimiter(tmp, "endpoint", "fixture", 2) as slow:
                    with fast._locked():
                        self.assertEqual(fast._active_qps(), 2)
                subprocess.run([sys.executable, "-c", script, tmp], check=True, timeout=5, capture_output=True)
                with fast._locked():
                    self.assertEqual(fast._active_qps(), 10)
                self.assertEqual(len(list(Path(tmp).rglob("*.lease"))), 1)

    def test_sleep_releases_cross_process_lock_and_reserves_no_future_slots(self):
        import fcntl
        with tempfile.TemporaryDirectory(prefix="paperbridge-rate-sleep-") as tmp:
            with SharedRateLimiter(tmp, "endpoint", "fixture", 1) as limiter:
                limiter.wait()
                previous = (limiter.directory / "last-start").read_text()
                def check_sleep(_delay):
                    lock = os.open(limiter.directory / "lock", os.O_RDWR)
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        self.assertEqual((limiter.directory / "last-start").read_text(), previous)
                    finally:
                        os.close(lock)
                    raise InterruptedError("Fixture cancellation while waiting")
                with patch("paperbridge_service.worker.time.sleep", side_effect=check_sleep):
                    with self.assertRaises(InterruptedError):
                        limiter.wait()


@unittest.skipUnless(INSTALLED, "BabelDOC 0.6.4 is not installed; adapter contract requires the engine venv")
class WorkerContractTests(unittest.TestCase):
    def test_asset_download_is_atomic_and_shared_between_concurrent_callers(self):
        from babeldoc.assets import assets
        data = b"Complete local asset fixture"
        digest = hashlib.sha3_256(data).hexdigest()
        async def exercise(root):
            started, release = asyncio.Event(), asyncio.Event()
            calls = []
            target = root / "model.onnx"
            async def fake_download(client=None, url=None, path=None, sha3_256=None):
                calls.append(path)
                path.write_bytes(b"Partial fixture")
                started.set()
                await release.wait()
                path.write_bytes(data)
            with patch.object(assets, "download_file", side_effect=fake_download) as original:
                with protected_asset_downloads(root / "locks"):
                    first = asyncio.create_task(assets.download_file(path=target, sha3_256=digest))
                    second = asyncio.create_task(assets.download_file(path=target, sha3_256=digest))
                    await started.wait()
                    self.assertFalse(target.exists(), "A partial asset must never be published")
                    release.set()
                    await asyncio.wait_for(asyncio.gather(first, second), 2)
                self.assertIs(assets.download_file, original)
            self.assertEqual(len(calls), 1)
            self.assertNotEqual(calls[0], target)
            self.assertEqual(target.read_bytes(), data)
            self.assertFalse(list(root.glob("*.part")))
        with tempfile.TemporaryDirectory(prefix="paperbridge-assets-atomic-") as tmp:
            asyncio.run(exercise(Path(tmp)))

    def test_asset_cancellation_cleans_partial_and_releases_lock(self):
        from babeldoc.assets import assets
        data = b"Complete local asset fixture"
        digest = hashlib.sha3_256(data).hexdigest()
        async def exercise(root):
            started, release = asyncio.Event(), asyncio.Event()
            target = root / "font.ttf"
            async def fake_download(client=None, url=None, path=None, sha3_256=None):
                path.write_bytes(b"Partial fixture")
                started.set()
                await release.wait()
                path.write_bytes(data)
            with patch.object(assets, "download_file", side_effect=fake_download):
                with protected_asset_downloads(root / "locks"):
                    task = asyncio.create_task(assets.download_file(path=target, sha3_256=digest))
                    await started.wait()
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertFalse(target.exists())
                    self.assertFalse(list(root.glob("*.part")))
                    release.set()
                    await asyncio.wait_for(assets.download_file(path=target, sha3_256=digest), 2)
            self.assertEqual(target.read_bytes(), data)
        with tempfile.TemporaryDirectory(prefix="paperbridge-assets-cancel-") as tmp:
            asyncio.run(exercise(Path(tmp)))

    def test_model_font_and_cmap_loaders_use_protected_downloader(self):
        from babeldoc.assets import assets
        data = b"Complete local asset fixture"
        digest = hashlib.sha3_256(data).hexdigest()
        model_upstream = next(iter(assets.DOC_LAYOUT_ONNX_MODEL_URL))
        font_upstream = next(iter(assets.FONT_URL_BY_UPSTREAM.keys() & assets.CMAP_URL_BY_UPSTREAM.keys()))
        metadata = {"fixture.ttf": {"sha3_256": digest}}
        calls = []
        async def fake_download(client=None, url=None, path=None, sha3_256=None):
            calls.append(path)
            path.write_bytes(data)
        async def exercise(root):
            with patch.object(assets, "download_file", side_effect=fake_download), patch.object(assets, "get_cache_file_path", side_effect=lambda name, group: root / name), patch.object(assets, "get_fastest_upstream_for_model", return_value=(model_upstream, None)), patch.object(assets, "get_fastest_upstream_for_font", return_value=(font_upstream, metadata)), patch.object(assets, "DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256", digest), patch.object(assets, "CMAP_METADATA", {"fixture.json": {"sha3_256": digest}}):
                with protected_asset_downloads(root / "locks"):
                    model = await assets.get_doclayout_onnx_model_path_async()
                    font, _metadata = await assets.get_font_and_metadata_async("fixture.ttf", fastest_upstream=font_upstream, font_metadata=metadata)
                    cmap = await assets.get_cmap_file_path_async("fixture")
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(path.suffix == ".part" for path in calls))
            self.assertTrue(all(path.read_bytes() == data for path in (model, font, cmap)))
            self.assertFalse(list(root.glob("*.part")))
        with tempfile.TemporaryDirectory(prefix="paperbridge-asset-loaders-") as tmp:
            asyncio.run(exercise(Path(tmp)))

    def test_httpx_hook_gates_sdk_retry_attempts_without_network(self):
        import httpx
        import openai
        starts = []
        def respond(request):
            starts.append(time.monotonic())
            if len(starts) == 1:
                return httpx.Response(429, headers={"retry-after": "0.001"}, json={"error": {"message": "Local retry fixture"}})
            return httpx.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 0, "model": "fixture", "choices": [{"index": 0, "message": {"role": "assistant", "content": "fixture"}, "finish_reason": "stop"}]})
        with tempfile.TemporaryDirectory(prefix="paperbridge-rate-retry-") as tmp:
            with SharedRateLimiter(tmp, "https://example.invalid/v1", "fixture", 10) as limiter:
                with openai.OpenAI(base_url="https://example.invalid/v1", api_key="fixture", max_retries=1, http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
                    client._client.event_hooks["request"].append(limiter.wait)
                    result = client.chat.completions.create(model="fixture", messages=[{"role": "user", "content": "fixture"}])
                    self.assertEqual(result.choices[0].message.content, "fixture")
        self.assertEqual(len(starts), 2)
        self.assertGreaterEqual(starts[1] - starts[0], 0.09)

    def test_installed_signatures_configuration_progress_and_pdf_outputs(self):
        from babeldoc.format.pdf import high_level, translation_config
        from babeldoc.translator import translator
        import pymupdf
        config_signature = inspect.signature(translation_config.TranslationConfig)
        translator_signature = inspect.signature(translator.OpenAITranslator)
        config = validate_config({"baseUrl": "https://example.invalid/v1", "apiKey": "worker-fixture-secret", "model": "fixture", "sourceLang": "en", "targetLang": "zh", "pages": "1", "dualMode": "alternating", "generateMono": True, "qps": 2, "customPrompt": "Keep scientific notation."})
        with tempfile.TemporaryDirectory(prefix="paperbridge-adapter-") as tmp:
            root = Path(tmp)
            source = root / "original.pdf"
            out = root / "outputs"
            out.mkdir()
            with pymupdf.open() as doc:
                page = doc.new_page()
                page.insert_text((72, 72), "TEST FIXTURE: no translation quality claim.")
                doc.save(source)
            captured = {}
            def make_translator(**kwargs):
                translator_signature.bind(**kwargs)
                captured["translator"] = kwargs
                captured["hooks"] = {"request": []}
                return SimpleNamespace(client=SimpleNamespace(_client=SimpleNamespace(event_hooks=captured["hooks"])))
            def make_config(**kwargs):
                config_signature.bind(**kwargs)
                captured["config"] = kwargs
                return SimpleNamespace(**(kwargs | {"doc_layout_model": SimpleNamespace()}))
            async def fake_translate(received):
                yield {"type": "progress_start", "stage": "start"}
                yield {"type": "progress_update", "stage": "mock layout", "overall_progress": 43.5}
                yield {"type": "progress_end", "stage": "finish", "overall_progress": 100}
                for filename in ("dual.pdf", "mono.pdf"):
                    with pymupdf.open(source) as doc:
                        doc.save(out / filename)
                yield {"type": "finish", "translate_result": SimpleNamespace(dual_pdf_path=out/"dual.pdf", mono_pdf_path=out/"mono.pdf")}
            events = []
            with patch.object(translator, "OpenAITranslator", side_effect=make_translator), patch.object(translator, "set_translate_rate_limiter") as rate, patch.object(translation_config, "TranslationConfig", side_effect=make_config), patch.object(high_level, "init"), patch.object(high_level, "async_translate", side_effect=fake_translate):
                asyncio.run(translate({"inputPath": str(source), "outputDir": str(out), "rateLimitDir": str(root / "rate-limits"), "config": config}, events.append))
            rate.assert_called_once_with(2)
            options = captured["config"]
            self.assertFalse(options["no_dual"])
            self.assertFalse(options["no_mono"])
            self.assertFalse(options["auto_extract_glossary"])
            self.assertTrue(options["auto_enable_ocr_workaround"])
            self.assertFalse(options["save_auto_extracted_glossary"])
            self.assertEqual(options["custom_system_prompt"], "Keep scientific notation.")
            self.assertEqual(options["pages"], "1")
            self.assertTrue(options["use_alternating_pages_dual"])
            self.assertEqual(options["pool_max_workers"], 16)
            self.assertEqual(len(captured["hooks"]["request"]), 1)
            self.assertFalse(list((root / "rate-limits").rglob("*.lease")))
            self.assertFalse(captured["translator"]["send_temperature"])
            progress = [event.get("progress") for event in events if event["type"] == "progress"]
            self.assertIn(43.5, progress)
            self.assertIn(99.9, progress)
            self.assertEqual(events[-1]["type"], "finish")
            self.assertNotIn(config["apiKey"], str(events))
            self.assertEqual(set(events[-1]["outputs"]), {"dual", "mono"})
            for path in events[-1]["outputs"].values():
                with pymupdf.open(path) as doc:
                    self.assertEqual(len(doc), 1)

    def test_slow_responses_overlap_without_exceeding_qps(self):
        """Exercise the adapter's pool with BabelDOC's real request limiter.

        Replies are delayed local fixtures, not HTTP calls or real translations.
        The regression is concurrency stuck at QPS despite waiting on responses.
        """
        from babeldoc.format.pdf import high_level, translation_config
        from babeldoc.translator import translator
        from babeldoc.utils.priority_thread_pool_executor import PriorityThreadPoolExecutor
        import pymupdf

        config = validate_config({"baseUrl": "https://example.invalid/v1", "apiKey": "fixture", "model": "fixture", "qps": 2})
        limiter = translator.RateLimiter(config["qps"])
        starts = []
        lock = threading.Lock()
        active = peak = 0

        def slow_reply(text, _rate_limit_params):
            nonlocal active, peak
            with lock:
                starts.append(time.monotonic())
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(1.6)
                return "fixture:" + text
            finally:
                with lock:
                    active -= 1

        stub = SimpleNamespace(ignore_cache=True, translate_call_count=0, do_llm_translate=slow_reply)
        captured = {}
        def make_config(**kwargs):
            return SimpleNamespace(**(kwargs | {"doc_layout_model": SimpleNamespace()}))

        with tempfile.TemporaryDirectory(prefix="paperbridge-concurrency-") as tmp:
            root = Path(tmp)
            source = root / "original.pdf"
            out = root / "outputs"
            out.mkdir()
            with pymupdf.open() as doc:
                doc.new_page().insert_text((72, 72), "Local delayed-response fixture.")
                doc.save(source)

            async def fake_translate(received):
                self.assertTrue(received.no_mono)
                start = time.monotonic()
                with PriorityThreadPoolExecutor(max_workers=received.pool_max_workers) as executor:
                    futures = [executor.submit(translator.BaseTranslator.llm_translate, stub, str(i), True) for i in range(6)]
                    self.assertEqual([f.result() for f in futures], ["fixture:" + str(i) for i in range(6)])
                captured.update(seconds=round(time.monotonic() - start, 3), peak=peak)
                with pymupdf.open(source) as doc:
                    doc.save(out / "dual.pdf")
                yield {"type": "finish", "translate_result": SimpleNamespace(dual_pdf_path=out/"dual.pdf", mono_pdf_path=None)}

            events = []
            with patch.object(translator, "_translate_rate_limiter", limiter), patch.object(translator, "OpenAITranslator", return_value=stub), patch.object(translation_config, "TranslationConfig", side_effect=make_config), patch.object(high_level, "init"), patch.object(high_level, "async_translate", side_effect=fake_translate):
                asyncio.run(translate({"inputPath": str(source), "outputDir": str(out), "config": config}, events.append))
            self.assertEqual(events[-1]["type"], "finish")
        self.assertEqual(len(starts), 6)
        self.assertEqual(set(events[-1]["outputs"]), {"dual"})
        self.assertGreater(peak, config["qps"], "Slow replies must not reduce concurrency to the per-second start limit")
        self.assertLessEqual(peak, 16)
        self.assertGreaterEqual(min(b - a for a, b in zip(starts, starts[1:])), 0.48)
        print("Delayed-response fixture (not a real API benchmark): " + json.dumps(captured))

    def test_textless_selected_pages_stop_before_engine_or_model(self):
        from babeldoc.format.pdf import high_level
        import pymupdf
        with tempfile.TemporaryDirectory(prefix="paperbridge-ocr-preflight-") as tmp:
            root = Path(tmp)
            source = root / "mixed.pdf"
            out = root / "outputs"
            out.mkdir()
            with pymupdf.open() as doc:
                doc.new_page()
                doc.new_page().insert_text((72, 72), "The other page has text.")
                doc.save(source)
            config = validate_config({"baseUrl": "https://example.invalid/v1", "apiKey": "fixture", "model": "fixture", "pages": "1"})
            with patch.object(high_level, "init", side_effect=AssertionError("Textless input reached engine initialization")):
                with self.assertRaisesRegex(ValueError, "OCR"):
                    asyncio.run(translate({"inputPath": str(source), "outputDir": str(out), "config": config}, lambda e: None))

    def test_scan_errors_explain_ocr_without_exposing_raw_details(self):
        from babeldoc.babeldoc_exception.BabelDOCException import ScannedPDFError
        message = error_message(ScannedPDFError("provider-private-fixture"))
        self.assertIn("OCR", message)
        self.assertNotIn("provider-private-fixture", message)


if __name__ == "__main__":
    unittest.main()
