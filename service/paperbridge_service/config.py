"""Strict public input validation; no provider configuration is persisted."""
from pathlib import Path
import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

ENGINE_VERSION = "0.6.4"
MAX_PDF_BYTES = 1024 * 1024 * 1024


class APIError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _string(value, name, limit, required=False):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise APIError(f"Invalid {name}.")
    value = value.strip()
    if required and not value:
        raise APIError(f"{name} is required.")
    return value


def validate_config(raw):
    if not isinstance(raw, dict):
        raise APIError("config must be an object.")
    allowed = {"baseUrl", "apiKey", "model", "sourceLang", "targetLang", "qps", "pages", "dualMode", "generateMono", "customPrompt"}
    if set(raw) - allowed:
        raise APIError("Unsupported configuration option.")
    config = {
        "baseUrl": _string(raw.get("baseUrl", ""), "baseUrl", 2048, True),
        "apiKey": _string(raw.get("apiKey", ""), "apiKey", 4096, True),
        "model": _string(raw.get("model", ""), "model", 200, True),
        "sourceLang": _string(raw.get("sourceLang", "en"), "sourceLang", 80, True),
        "targetLang": _string(raw.get("targetLang", "zh"), "targetLang", 80, True),
        "customPrompt": _string(raw.get("customPrompt", ""), "customPrompt", 8000),
        "pages": _string(raw.get("pages", ""), "pages", 1000),
        "dualMode": raw.get("dualMode", "side-by-side"),
        "generateMono": raw.get("generateMono", False),
        "qps": raw.get("qps", 2),
    }
    if any(c in config["apiKey"] for c in "\r\n"):
        raise APIError("Invalid apiKey.")
    try:
        url = urlsplit(config["baseUrl"])
        port = url.port
        if url.scheme not in ("https", "http") or not url.hostname or url.username is not None or url.password is not None or url.query or url.fragment:
            raise ValueError()
        if any(c.isspace() for c in config["baseUrl"]):
            raise ValueError()
        if url.scheme == "http":
            local = url.hostname == "localhost"
            try:
                local = local or ipaddress.ip_address(url.hostname).is_loopback
            except ValueError:
                pass
            if not local:
                raise APIError("Use HTTPS for remote APIs; HTTP is allowed only for a loopback API.")
        path = url.path.rstrip("/")
        if path.endswith("/chat/completions"):
            path = path[:-len("/chat/completions")]
        config["baseUrl"] = urlunsplit((url.scheme, url.netloc, path, "", ""))
    except (ValueError, TypeError):
        raise APIError("baseUrl must be a valid HTTP(S) API base URL without credentials, query, or fragment.") from None
    qps = config["qps"]
    if isinstance(qps, bool) or not isinstance(qps, int) or not 1 <= qps <= 10:
        raise APIError("qps must be an integer from 1 to 10.")
    if config["dualMode"] not in ("side-by-side", "alternating"):
        raise APIError("dualMode must be side-by-side or alternating.")
    if not isinstance(config["generateMono"], bool):
        raise APIError("generateMono must be a boolean.")
    pages = config["pages"].replace(" ", "")
    if pages:
        for part in pages.split(","):
            if not re.fullmatch(r"(?:[1-9][0-9]{0,5}(?:-[1-9][0-9]{0,5})?|[1-9][0-9]{0,5}-|-[1-9][0-9]{0,5})", part):
                raise APIError("pages must contain positive page numbers or ranges, e.g. 1,3-5,7-.")
            if "-" in part:
                start, end = part.split("-")
                if start and end and int(start) > int(end):
                    raise APIError("Page range start must not exceed its end.")
    config["pages"] = pages
    return config


def valid_pdf(path):
    try:
        p = Path(path)
        if not p.is_file() or p.suffix.lower() != ".pdf" or not 8 <= p.stat().st_size <= MAX_PDF_BYTES:
            return False
        with p.open("rb") as f:
            return b"%PDF-" in f.read(1024)
    except (OSError, ValueError, TypeError):
        return False


def validate_input(path):
    if not isinstance(path, str) or "\x00" in path or not Path(path).is_absolute():
        raise APIError("inputPath must be an absolute PDF path.")
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        raise APIError("The selected PDF is unavailable.") from None
    if not valid_pdf(resolved):
        raise APIError("The selected file is not a readable PDF, or exceeds 1 GiB.")
    return str(resolved)


def redact(text, secret):
    # Provider bodies and exception strings are not displayed. This protects
    # the additional user-visible fields (sample text and progress stage).
    text = str(text)
    if secret:
        text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?i)Bearer\s+[^\s]+", "Bearer [redacted]", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[redacted]", text)
    return text[:500]
