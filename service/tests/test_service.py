"""Unit/API contract tests. Workers and chat provider here are explicit MOCKS.
These tests validate lifecycle/security, not translation quality or engine assets.
"""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
import uuid

from paperbridge_service.config import APIError, validate_config, validate_input
from paperbridge_service.jobs import JobManager, atomic_json
from paperbridge_service.provider import test_provider
from paperbridge_service.server import BridgeServer

KEY = "private-fixture-token-DO-NOT-PERSIST"
CONFIG = {"baseUrl": "https://example.invalid/v1", "apiKey": KEY, "model": "mock-model", "sourceLang": "en", "targetLang": "zh", "qps": 2, "pages": "", "dualMode": "side-by-side", "generateMono": True, "customPrompt": ""}
FAKE = Path(__file__).with_name("fake_worker.py")
READY = lambda: {"available": True, "version": "mock-only"}


def wait_for(manager, job_id, statuses, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job["status"] in statuses:
            return job
        time.sleep(0.02)
    raise AssertionError("Task did not reach expected status: " + str(manager.get(job_id)))


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="paperbridge-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pdf = self.root / "input.pdf"
        self.pdf.write_bytes(b"%PDF-1.7\nINPUT-FIXTURE\n%%EOF")

    def manager(self, mode="success", **options):
        manager = JobManager(self.root / "runtime", worker_command=[sys.executable, str(FAKE), mode], engine_check=READY, **options)
        self.addCleanup(manager.close)
        return manager

    def submit(self, manager, job_id=None):
        return manager.submit({"id": job_id or str(uuid.uuid4()), "inputPath": str(self.pdf), "config": CONFIG.copy()})


class ValidationTests(BaseCase):
    def test_mono_output_is_strictly_opt_in(self):
        legacy = {k: v for k, v in CONFIG.items() if k != "generateMono"}
        self.assertFalse(validate_config(legacy)["generateMono"])
        self.assertFalse(validate_config(CONFIG | {"generateMono": False})["generateMono"])
        self.assertTrue(validate_config(CONFIG)["generateMono"])
        for value in ("false", "true", 0, 1, None):
            with self.subTest(value=value), self.assertRaises(APIError):
                validate_config(CONFIG | {"generateMono": value})

    def test_config_ranges_and_url_normalization(self):
        raw = CONFIG | {"pages": "1, 3-5,7-,-2", "baseUrl": "https://example.invalid/v1/chat/completions/"}
        config = validate_config(raw)
        self.assertEqual(config["pages"], "1,3-5,7-,-2")
        self.assertEqual(config["baseUrl"], "https://example.invalid/v1")

    def test_reject_unsafe_and_invalid_configuration(self):
        variants = [{"baseUrl": "https://user:secret@example.invalid/v1"}, {"baseUrl": "https://example.invalid/v1?key=secret"}, {"baseUrl": "http://example.invalid/v1"}, {"qps": 0}, {"qps": 11}, {"qps": True}, {"qps": 1.5}, {"pages": "0"}, {"pages": "5-2"}, {"pages": "1; rm -rf"}, {"dualMode": "arbitrary"}, {"command": "sh"}, {"apiKey": "secret\nvalue"}]
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(APIError):
                validate_config(CONFIG | variant)

    def test_input_must_be_absolute_real_pdf(self):
        for path in ("relative.pdf", str(self.root / "missing.pdf")):
            with self.assertRaises(APIError):
                validate_input(path)
        self.pdf.write_text("not a PDF")
        with self.assertRaises(APIError):
            validate_input(str(self.pdf))


class QueueTests(BaseCase):
    def test_two_documents_run_together_and_third_waits(self):
        manager = self.manager("sleep", start=False)
        first, second, third = [self.submit(manager) for _ in range(3)]
        manager.start()
        wait_for(manager, first["id"], {"running"})
        wait_for(manager, second["id"], {"running"}, timeout=2)
        self.assertEqual(manager.get(third["id"])["status"], "queued")
        with manager.lock:
            self.assertEqual(set(manager.processes), {first["id"], second["id"]})
            first_proc, second_proc = manager.processes[first["id"]], manager.processes[second["id"]]
        manager.cancel(first["id"])
        wait_for(manager, third["id"], {"running"})
        self.assertIsNotNone(first_proc.poll())
        self.assertIsNone(second_proc.poll())
        self.assertEqual(manager.get(second["id"])["status"], "running")
        self.assertEqual(manager.get(first["id"])["status"], "cancelled")

    def test_one_worker_preserves_queue_capacity(self):
        manager = self.manager("sleep", max_workers=1, max_queue=1)
        first = self.submit(manager)
        wait_for(manager, first["id"], {"running"})
        second = self.submit(manager)
        self.assertEqual(manager.get(second["id"])["status"], "queued")
        with self.assertRaises(APIError) as error:
            self.submit(manager)
        self.assertEqual(error.exception.status, 429)
        manager.cancel(second["id"])
        replacement = self.submit(manager)
        self.assertEqual(manager.get(replacement["id"])["status"], "queued")
        manager.cancel(first["id"])
        wait_for(manager, replacement["id"], {"running"})

    def test_shutdown_stops_all_workers_and_does_not_resume_history(self):
        manager = self.manager("sleep", start=False)
        jobs = [self.submit(manager) for _ in range(3)]
        manager.start()
        manager.start()  # Repeated starts must not create additional workers.
        for job in jobs[:2]:
            wait_for(manager, job["id"], {"running"})
        with manager.lock:
            processes = list(manager.processes.values())
        manager.close()
        self.assertEqual(len(processes), 2)
        self.assertTrue(all(proc.poll() is not None for proc in processes))
        self.assertTrue(all(not thread.is_alive() for thread in manager.threads))
        self.assertTrue(all(job["status"] == "interrupted" for job in manager.list()))
        self.assertFalse(manager.configs)
        self.assertFalse(manager.processes)
        self.assertTrue(manager.pending.empty())
        self.assertEqual(manager.pending.unfinished_tasks, 0)
        manager.start()  # A closed manager cannot restart old work.
        manager.close()
        restored = self.manager(start=False)
        self.assertEqual(len(restored.list()), 3)
        self.assertTrue(all(job["status"] == "interrupted" for job in restored.list()))
        self.assertTrue(restored.pending.empty())
        self.assertNotIn(KEY, restored.metadata.read_text())

    def test_worker_and_queue_limits_are_bounded(self):
        for workers in (0, 5, -1, True, 1.5):
            with self.subTest(workers=workers), self.assertRaises(ValueError):
                self.manager(start=False, max_workers=workers)
        for capacity in (0, -1, True, 1.5):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                self.manager(start=False, max_queue=capacity)

    def test_default_job_completes_with_only_bilingual_output(self):
        manager = self.manager()
        config = {k: v for k, v in CONFIG.items() if k != "generateMono"}
        job = manager.submit({"id": str(uuid.uuid4()), "inputPath": str(self.pdf), "config": config})
        done = wait_for(manager, job["id"], {"completed", "failed"})
        self.assertEqual(done["status"], "completed")
        self.assertEqual(set(done["outputs"]), {"dual"})
        self.assertFalse((Path(done["outputs"]["dual"]).parent / "mono.pdf").exists())

    def test_opted_in_job_requires_mono_output(self):
        manager = self.manager("dual_only")
        job = self.submit(manager)
        self.assertEqual(wait_for(manager, job["id"], {"failed", "completed"})["status"], "failed")

    def test_completed_outputs_idempotency_and_secret_absence(self):
        manager = self.manager()
        job = self.submit(manager)
        done = wait_for(manager, job["id"], {"completed", "failed"})
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["progress"], 100)
        self.assertEqual(set(done["outputs"]), {"dual", "mono"})
        self.assertEqual(manager.submit({"id": job["id"]}), done)
        persisted = manager.metadata.read_text()
        self.assertNotIn(KEY, persisted)
        self.assertNotIn("apiKey", persisted)
        self.assertNotIn("config", persisted)
        self.assertNotIn("rateLimitDir", persisted)
        self.assertNotIn(KEY, " ".join(manager.worker_command))
        self.assertEqual(stat.S_IMODE(manager.metadata.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(manager.rate_limit_dir.stat().st_mode), 0o700)

    def test_missing_output_never_completes(self):
        manager = self.manager("missing")
        job = self.submit(manager)
        self.assertEqual(wait_for(manager, job["id"], {"failed", "completed"})["status"], "failed")

    def test_output_cannot_escape_job_folder(self):
        manager = self.manager("escape")
        job = self.submit(manager)
        self.assertEqual(wait_for(manager, job["id"], {"failed", "completed"})["status"], "failed")

    def test_worker_error_is_redacted(self):
        manager = self.manager("error")
        job = self.submit(manager)
        done = wait_for(manager, job["id"], {"failed"})
        self.assertNotIn(KEY, json.dumps(done))
        self.assertNotIn(KEY, manager.metadata.read_text())

    def test_running_cancel_terminates_worker(self):
        manager = self.manager("sleep")
        job = self.submit(manager)
        wait_for(manager, job["id"], {"running"})
        with manager.lock:
            proc = manager.processes.get(job["id"])
        self.assertIsNotNone(proc)
        result = manager.cancel(job["id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNotNone(proc.poll())
        self.assertEqual(manager.cancel(job["id"])["status"], "cancelled")

    def test_bounded_fifo_and_queued_cancel(self):
        manager = self.manager(start=False, max_queue=2)
        first, second = self.submit(manager), self.submit(manager)
        with self.assertRaises(APIError) as error:
            self.submit(manager)
        self.assertEqual(error.exception.status, 429)
        self.assertEqual(list(manager.pending.queue), [first["id"], second["id"]])
        manager.cancel(first["id"])
        self.assertNotIn(first["id"], manager.configs)
        replacement = self.submit(manager)
        self.assertEqual(list(manager.pending.queue), [second["id"], replacement["id"]])
        manager.start()
        wait_for(manager, second["id"], {"completed"})
        self.assertEqual(manager.get(first["id"])["status"], "cancelled")

    def test_restart_interrupts_without_requeue(self):
        runtime = self.root / "runtime"
        runtime.mkdir()
        job_id = str(uuid.uuid4())
        atomic_json(runtime / "jobs.json", [{"id": job_id, "status": "running", "progress": 20, "stage": "old run", "createdAt": "2026-01-01T00:00:00Z"}])
        manager = self.manager(start=False)
        self.assertEqual(manager.get(job_id)["status"], "interrupted")
        self.assertTrue(manager.pending.empty())
        self.assertFalse(manager.configs)

    def test_missing_engine_rejects_before_enqueue(self):
        manager = self.manager(start=False)
        manager.engine_check = lambda: {"available": False}
        with self.assertRaises(APIError) as error:
            self.submit(manager)
        self.assertEqual(error.exception.status, 503)
        self.assertEqual(manager.list(), [])

    def test_single_instance_lock_protects_live_history(self):
        manager = self.manager(start=False)
        job = self.submit(manager)
        with self.assertRaises(RuntimeError):
            JobManager(manager.data_dir, start=False)
        self.assertEqual(manager.get(job["id"])["status"], "queued")

    def test_invalid_id_and_unknown_job(self):
        manager = self.manager(start=False)
        for invalid in ("../x", "ABC", None):
            with self.assertRaises(APIError):
                manager.get(invalid)
        with self.assertRaises(APIError) as error:
            manager.get(str(uuid.uuid4()))
        self.assertEqual(error.exception.status, 404)


class HTTPTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.manager_instance = self.manager(start=False)
        self.server = BridgeServer(self.manager_instance, port=0, token="fixture-bridge-token", probe=lambda c: {"ok": True, "sample": "mock"})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        all_headers = {"Authorization": "Bearer fixture-bridge-token"}
        if body is not None:
            all_headers["Content-Type"] = "application/json"
            body = json.dumps(body)
        all_headers.update(headers or {})
        connection.request(method, path, body=body, headers=all_headers)
        response = connection.getresponse()
        result = (response.status, dict(response.headers), json.loads(response.read()))
        connection.close()
        return result

    def test_authentication_host_origin_and_no_cors(self):
        for headers, expected in [({"Authorization": ""}, 401), ({"Authorization": "Bearer wrong"}, 401), ({"Origin": "https://evil.example"}, 403), ({"Origin": "null"}, 403), ({"Host": "evil.example"}, 403)]:
            with self.subTest(headers=headers):
                status, response_headers, _ = self.request("GET", "/v1/health", headers=headers)
                self.assertEqual(status, expected)
                self.assertNotIn("Access-Control-Allow-Origin", response_headers)
        status, _, health = self.request("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["engine"]["available"])
        self.assertEqual(health["maxConcurrentJobs"], 2)
        self.assertEqual(health["queueCapacity"], 8)
        pairing = self.manager_instance.data_dir / "bridge.json"
        self.assertEqual(stat.S_IMODE(pairing.stat().st_mode), 0o600)
        self.assertEqual(json.loads(pairing.read_text())["url"], self.server.url)

    def test_api_jobs_list_get_cancel_and_duplicate(self):
        job_id = str(uuid.uuid4())
        body = {"id": job_id, "inputPath": str(self.pdf), "config": CONFIG}
        status, _, job = self.request("POST", "/v1/jobs", body)
        self.assertEqual((status, job["status"]), (200, "queued"))
        self.assertEqual(self.request("GET", "/v1/jobs/" + job_id)[2], job)
        self.assertEqual(len(self.request("GET", "/v1/jobs")[2]["jobs"]), 1)
        self.assertEqual(self.request("POST", "/v1/jobs", {"id": job_id})[2], job)
        cancelled = self.request("POST", "/v1/jobs/" + job_id + "/cancel")[2]
        self.assertEqual(cancelled["status"], "cancelled")

    def test_test_route_is_authenticated_and_validates_config(self):
        self.assertEqual(self.request("POST", "/v1/test", {"config": CONFIG})[0], 200)
        self.assertEqual(self.request("POST", "/v1/test", {"config": CONFIG | {"qps": 99}})[0], 400)
        self.assertEqual(self.request("POST", "/v1/test", {"config": CONFIG}, {"Authorization": ""})[0], 401)

    def test_bad_routes_and_large_body_rejected(self):
        self.assertEqual(self.request("GET", "/v1/jobs/..%2Fsecret")[0], 404)
        self.assertEqual(self.request("POST", "/v1/jobs", {"padding": "x" * 70000})[0], 413)
        self.assertEqual(self.request("OPTIONS", "/v1/health")[0], 404)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.mode = "success"
        owner = self
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append((self.path, self.headers.get("Authorization"), payload))
                if owner.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "/stolen")
                    self.end_headers()
                    return
                if owner.mode == "error":
                    self.send_response(401)
                    body = json.dumps({"error": "secret " + KEY}).encode()
                else:
                    self.send_response(200)
                    body = json.dumps({"choices": [{"message": {"content": ("hello " + KEY) if owner.mode == "echo-key" else "你好"}}]}).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = validate_config(CONFIG | {"baseUrl": f"http://127.0.0.1:{self.server.server_port}/v1"})
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def test_probe_performs_chat_generation(self):
        result = test_provider(self.config)
        self.assertTrue(result["ok"])
        self.assertEqual(result["sample"], "你好")
        path, auth, body = self.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(auth, "Bearer " + KEY)
        self.assertEqual(body["model"], "mock-model")
        self.assertEqual(body["max_tokens"], 32)
        self.assertTrue(body["messages"])

    def test_provider_errors_never_expose_secret(self):
        self.mode = "error"
        with self.assertRaises(APIError) as error:
            test_provider(self.config)
        self.assertNotIn(KEY, str(error.exception))
        self.assertIn("401", str(error.exception))

    def test_redirects_are_not_followed_with_credentials(self):
        self.mode = "redirect"
        with self.assertRaises(APIError):
            test_provider(self.config)
        self.assertEqual(len(self.requests), 1)

    def test_provider_echo_of_key_is_redacted(self):
        self.mode = "echo-key"
        self.assertNotIn(KEY, json.dumps(test_provider(self.config)))


if __name__ == "__main__":
    unittest.main()
