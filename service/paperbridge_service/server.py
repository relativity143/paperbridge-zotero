"""Authenticated loopback-only HTTP control plane; no CORS and no remote bind."""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import threading

from . import __version__
from .config import APIError, validate_config
from .jobs import JobManager, atomic_json
from .provider import test_provider


def default_data_dir():
    override = os.environ.get("PAPERBRIDGE_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "PaperBridge"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "paperbridge"


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self, manager, port=0, token=None, probe=None):
        self.manager = manager
        self.token = token or secrets.token_urlsafe(32)
        self.probe = probe or test_provider
        self.request_slots = threading.BoundedSemaphore(24)
        self.probe_slot = threading.BoundedSemaphore(1)
        super().__init__(("127.0.0.1", port), BridgeHandler)
        self.url = f"http://127.0.0.1:{self.server_port}"
        atomic_json(manager.data_dir / "bridge.json", {"url": self.url, "token": self.token})

    def process_request(self, request, address):
        if not self.request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.request_slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.request_slots.release()

    def handle_error(self, request, client_address):
        # Never dump request objects, exception bodies, or secret-bearing headers.
        pass


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "PaperBridge"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        pass

    def _send(self, status, value):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _authorize(self):
        hosts = self.headers.get_all("Host", [])
        expected = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if len(hosts) != 1 or hosts[0] not in expected:
            raise APIError("Untrusted Host.", 403)
        # Privileged Zotero requests omit Origin. Ordinary browser origins,
        # including the opaque 'null' origin, are deliberately rejected.
        if self.headers.get_all("Origin", []):
            raise APIError("Browser-origin requests are not allowed.", 403)
        auth = self.headers.get_all("Authorization", [])
        expected_auth = "Bearer " + self.server.token
        if len(auth) != 1 or not hmac.compare_digest(auth[0].encode("utf-8"), expected_auth.encode("utf-8")):
            raise APIError("Authentication required.", 401)

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise APIError("Chunked requests are not supported.", 400)
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdigit():
            raise APIError("Content-Length is required.", 411)
        size = int(lengths[0])
        if not 0 < size <= 65536:
            raise APIError("JSON request is too large or empty.", 413)
        if self.headers.get_content_type() != "application/json":
            raise APIError("Content-Type must be application/json.", 415)
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise APIError("Incomplete request body.", 400)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise APIError("Invalid JSON.", 400) from None
        if not isinstance(value, dict):
            raise APIError("JSON body must be an object.", 400)
        return value

    def _dispatch(self, method):
        try:
            self._authorize()
            path = self.path
            if "?" in path or "#" in path or "%" in path:
                raise APIError("Invalid route.", 404)
            if method == "GET" and path == "/v1/health":
                result = {"ok": True, "service": "PaperBridge", "version": __version__, "engine": self.server.manager.engine_check(), "queueCapacity": self.server.manager.pending.maxsize, "maxConcurrentJobs": self.server.manager.max_workers}
            elif method == "GET" and path == "/v1/jobs":
                result = {"jobs": self.server.manager.list()}
            elif method == "POST" and path == "/v1/test":
                body = self._body()
                if set(body) != {"config"}:
                    raise APIError("Expected config only.")
                config = validate_config(body["config"])
                if not self.server.probe_slot.acquire(blocking=False):
                    raise APIError("An API test is already running.", 429)
                try:
                    result = self.server.probe(config)
                finally:
                    self.server.probe_slot.release()
            elif method == "POST" and path == "/v1/jobs":
                result = self.server.manager.submit(self._body())
            elif path.startswith("/v1/jobs/"):
                suffix = path[len("/v1/jobs/"):]
                if method == "POST" and suffix.endswith("/cancel") and suffix.count("/") == 1:
                    result = self.server.manager.cancel(suffix[:-len("/cancel")])
                elif method == "GET" and "/" not in suffix:
                    result = self.server.manager.get(suffix)
                else:
                    raise APIError("Route not found.", 404)
            else:
                raise APIError("Route not found.", 404)
            self._send(200, result)
        except APIError as error:
            self._send(error.status, {"error": str(error)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            self._send(500, {"error": "Internal service error. No provider credentials or raw error details are logged."})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")


def main():
    parser = argparse.ArgumentParser(description="PaperBridge authenticated local PDF translation companion")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--port", type=int, default=0, help="Loopback port; 0 selects an available port automatically")
    parser.add_argument("--max-workers", type=int, choices=range(1, 5), default=2, help="Concurrent PDF translations (default: 2; use 1 to reduce memory use)")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be from 0 to 65535 (0 selects an available port)")
    try:
        manager = JobManager(args.data_dir, max_workers=args.max_workers)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    try:
        server = BridgeServer(manager, args.port)
    except OSError:
        manager.close()
        print("PaperBridge could not bind the loopback port. Another service may already be running.", file=sys.stderr)
        return 1
    def stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"PaperBridge {__version__} listening at {server.url}; pairing file: {manager.data_dir / 'bridge.json'}", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        manager.close()
    return 0
