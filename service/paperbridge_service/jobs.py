"""Bounded concurrent FIFO task manager with private, restart-safe metadata."""
from datetime import datetime, timezone
import copy
import importlib.metadata
import importlib
from functools import lru_cache
import importlib.util
import json
import math
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid

from .config import APIError, ENGINE_VERSION, redact, valid_pdf, validate_config, validate_input

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


def atomic_json(path, data):
    path = Path(path)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(data, out, ensure_ascii=False, allow_nan=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


@lru_cache(maxsize=1)
def engine_status():
    try:
        installed = importlib.metadata.version("BabelDOC")
    except importlib.metadata.PackageNotFoundError:
        installed = None
    compatible_python = (3, 10) <= sys.version_info[:2] < (3, 14)
    ready = False
    if installed == ENGINE_VERSION and compatible_python:
        try:
            importlib.import_module("babeldoc.format.pdf.high_level")
            importlib.import_module("babeldoc.translator.translator")
            importlib.import_module("pymupdf")
            ready = True
        except Exception:
            pass
    return {"available": ready, "version": installed, "requiredVersion": ENGINE_VERSION, "python": ".".join(map(str, sys.version_info[:3])), "assets": "Downloaded on first translation if absent", "detail": "Engine imports passed" if ready else "Install the pinned requirements with Python 3.10-3.13, then restart the service"}


def valid_id(value):
    if not isinstance(value, str):
        raise APIError("id must be a canonical UUID.")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except ValueError:
        raise APIError("id must be a canonical UUID.") from None
    return value


class JobManager:
    def __init__(self, data_dir, max_queue=8, worker_command=None, engine_check=None, start=True, max_workers=2):
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 4:
            raise ValueError("max_workers must be an integer from 1 to 4.")
        if isinstance(max_queue, bool) or not isinstance(max_queue, int) or max_queue < 1:
            raise ValueError("max_queue must be a positive integer.")
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data_dir, 0o700)
        # Lock before reading history: a second start must not interrupt live jobs.
        import fcntl
        self.lock_file = open(self.data_dir / "service.lock", "a+")
        os.chmod(self.data_dir / "service.lock", 0o600)
        try:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock_file.close()
            raise RuntimeError("Another PaperBridge service is already using this data directory.") from None
        self.output_root = self.data_dir / "jobs"
        self.output_root.mkdir(exist_ok=True, mode=0o700)
        self.rate_limit_dir = self.data_dir / "rate-limits"
        self.rate_limit_dir.mkdir(exist_ok=True, mode=0o700)
        os.chmod(self.rate_limit_dir, 0o700)
        self.metadata = self.data_dir / "jobs.json"
        self.lock = threading.RLock()
        self.close_lock = threading.Lock()
        self.jobs = {}
        self.configs = {}
        self.pending = queue.Queue(maxsize=max_queue)
        self.max_workers = max_workers
        self.worker_command = worker_command or [sys.executable, "-m", "paperbridge_service.worker"]
        self.engine_check = engine_check or engine_status
        self.stopping = threading.Event()
        self.processes = {}
        self.threads = []
        self.closed = False
        try:
            self._load()
        except Exception:
            self.lock_file.close()
            raise
        if start:
            self.start()

    def start(self):
        with self.lock:
            if self.threads or self.stopping.is_set():
                return
            self.threads = [threading.Thread(target=self._loop, name=f"paperbridge-worker-{index + 1}", daemon=True) for index in range(self.max_workers)]
            for thread in self.threads:
                thread.start()

    def _load(self):
        if not self.metadata.exists():
            return
        try:
            if self.metadata.stat().st_size > 10 * 1024 * 1024:
                raise ValueError()
            records = json.loads(self.metadata.read_text(encoding="utf-8"))
            if not isinstance(records, list):
                raise ValueError()
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError()
                valid_id(record.get("id"))
                if record.get("status") not in TERMINAL | {"queued", "running"}:
                    raise ValueError()
                # Persisted records never contain provider configuration.
                safe = {key: record[key] for key in ("id", "status", "progress", "stage", "error", "outputs", "createdAt") if key in record}
                if safe["status"] in {"queued", "running"}:
                    safe.update(status="interrupted", stage="Interrupted by service restart", error="The previous service stopped. Submit a new task to retry; no automatic retry was made.")
                self.jobs[safe["id"]] = safe
            self._save()
        except (OSError, ValueError, TypeError, APIError):
            # Never silently discard history and permit duplicate billed work.
            raise RuntimeError("Task history is unreadable. Preserve jobs.json and choose a fresh data directory or restore its backup.") from None

    def _save(self):
        atomic_json(self.metadata, list(self.jobs.values()))

    def list(self):
        with self.lock:
            return copy.deepcopy(list(self.jobs.values()))

    def get(self, job_id):
        valid_id(job_id)
        with self.lock:
            if job_id not in self.jobs:
                raise APIError("Task not found.", 404)
            return copy.deepcopy(self.jobs[job_id])

    def submit(self, raw):
        if not isinstance(raw, dict) or set(raw) - {"id", "inputPath", "config"}:
            raise APIError("Invalid job request.")
        job_id = valid_id(raw.get("id"))
        with self.lock:
            if job_id in self.jobs:
                return copy.deepcopy(self.jobs[job_id])
        config = validate_config(raw.get("config"))
        input_path = validate_input(raw.get("inputPath"))
        if not self.engine_check()["available"]:
            raise APIError("BabelDOC 0.6.4 is unavailable. Install the pinned engine with Python 3.10–3.13 and start PaperBridge using that environment.", 503)
        with self.lock:
            if job_id in self.jobs:
                return copy.deepcopy(self.jobs[job_id])
            if self.stopping.is_set():
                raise APIError("The service is stopping.", 503)
            if self.pending.full():
                raise APIError("The translation queue is full. Wait for a task to finish.", 429)
            output_dir = self.output_root / job_id
            if output_dir.exists() or output_dir.is_symlink():
                raise APIError("The task output folder already exists; use a new task ID.", 409)
            output_dir.mkdir(mode=0o700)
            job = {"id": job_id, "status": "queued", "progress": 0, "stage": "Queued", "createdAt": datetime.now(timezone.utc).isoformat()}
            self.jobs[job_id] = job
            self.configs[job_id] = {"inputPath": input_path, "config": config, "outputDir": str(output_dir), "rateLimitDir": str(self.rate_limit_dir)}
            self._save()
            self.pending.put_nowait(job_id)
            return copy.deepcopy(job)

    def cancel(self, job_id):
        valid_id(job_id)
        proc = None
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise APIError("Task not found.", 404)
            if job["status"] in TERMINAL:
                return copy.deepcopy(job)
            job.update(status="cancelled", stage="Cancelled")
            self.configs.pop(job_id, None)
            # Remove a queued cancellation using public queue operations. The
            # manager lock prevents submission; an already dequeued item is
            # handled by _run's status check. This immediately frees its slot.
            retained = []
            while True:
                try:
                    pending_id = self.pending.get_nowait()
                except queue.Empty:
                    break
                self.pending.task_done()
                if pending_id != job_id:
                    retained.append(pending_id)
            for pending_id in retained:
                self.pending.put_nowait(pending_id)
            proc = self.processes.get(job_id)
            self._save()
            result = copy.deepcopy(job)
        if proc:
            self._kill(proc)
        return result

    @staticmethod
    def _kill(proc):
        if proc.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)
        except ProcessLookupError:
            pass

    def close(self):
        # A signal and a caller may both close the service. Keep the instance
        # lock until every worker has exited, and make subsequent closes inert.
        with self.close_lock:
            if self.closed:
                return
            self.stopping.set()
            with self.lock:
                processes = list(self.processes.values())
                for job in self.jobs.values():
                    if job["status"] not in TERMINAL:
                        job.update(status="interrupted", stage="Service stopped", error="Submit a new task to retry; no automatic retry was made.")
                self.configs.clear()
                while True:
                    try:
                        self.pending.get_nowait()
                        self.pending.task_done()
                    except queue.Empty:
                        break
                self._save()
            for proc in processes:
                self._kill(proc)
            for thread in self.threads:
                thread.join(timeout=8)
            if any(thread.is_alive() for thread in self.threads):
                raise RuntimeError("A translation worker has not stopped; the service directory remains locked.")
            self.lock_file.close()
            self.closed = True

    def _loop(self):
        while not self.stopping.is_set():
            try:
                job_id = self.pending.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self._run(job_id)
            finally:
                self.pending.task_done()

    def _run(self, job_id):
        proc = None
        payload = None
        outputs = None
        failure = None
        last_saved = 0
        try:
            with self.lock:
                job = self.jobs[job_id]
                if job["status"] != "queued" or self.stopping.is_set():
                    return
                payload = self.configs.pop(job_id)
                job.update(status="running", progress=0, stage="Starting translation engine")
                self._save()
                env = os.environ.copy()
                env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
                env["PYTHONUNBUFFERED"] = "1"
                proc = subprocess.Popen(self.worker_command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=payload["outputDir"], env=env, text=True, encoding="utf-8", errors="replace", start_new_session=os.name == "posix")
                self.processes[job_id] = proc
            try:
                proc.stdin.write(json.dumps(payload, ensure_ascii=False))
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            while True:
                line = proc.stdout.readline(65537)
                if not line:
                    break
                if len(line) > 65536:
                    failure = "The engine emitted an invalid oversized event."
                    self._kill(proc)
                    break
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict):
                    continue
                with self.lock:
                    if self.jobs[job_id]["status"] != "running":
                        break
                    if event.get("type") == "progress":
                        progress = event.get("progress")
                        if not isinstance(progress, (int, float)) or isinstance(progress, bool) or not math.isfinite(progress):
                            progress = None
                        job["progress"] = min(99.9, max(0, progress)) if progress is not None else job["progress"]
                        job["stage"] = redact(event.get("stage", "Translating"), payload["config"]["apiKey"])
                        if time.monotonic() - last_saved > 1:
                            self._save()
                            last_saved = time.monotonic()
                    elif event.get("type") == "finish":
                        outputs = event.get("outputs")
                    elif event.get("type") == "error":
                        failure = redact(event.get("error", "PDF translation failed."), payload["config"]["apiKey"])
            code = proc.wait()
            with self.lock:
                if self.jobs[job_id]["status"] != "running":
                    return
                if failure or code != 0:
                    job.update(status="failed", stage="Failed", error=failure or "The translation engine stopped unexpectedly. Check engine installation and available memory.")
                else:
                    accepted = self._validate_outputs(outputs, payload["outputDir"], payload["config"]["generateMono"])
                    job.update(status="completed", stage="Completed", progress=100, outputs=accepted)
                self._save()
        except Exception:
            with self.lock:
                if self.jobs[job_id]["status"] == "running":
                    self.jobs[job_id].update(status="failed", stage="Failed", error="Translation failed or valid PDF outputs were not produced. Check the engine and PDF before retrying.")
                    self._save()
        finally:
            if proc:
                self._kill(proc)
                if proc.stdout:
                    proc.stdout.close()
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
            with self.lock:
                self.processes.pop(job_id, None)
                self.configs.pop(job_id, None)
            payload = None

    @staticmethod
    def _validate_outputs(outputs, output_dir, generate_mono):
        expected = {"dual", "mono"} if generate_mono else {"dual"}
        if not isinstance(outputs, dict) or set(outputs) != expected:
            raise ValueError("Missing output")
        accepted = {}
        root = Path(output_dir).resolve()
        for kind, path in outputs.items():
            p = Path(path).resolve(strict=True)
            if p.parent != root or not valid_pdf(p):
                raise ValueError("Invalid output path")
            accepted[kind] = str(p)
        if accepted["dual"] == accepted.get("mono"):
            raise ValueError("Outputs must be distinct")
        return accepted
