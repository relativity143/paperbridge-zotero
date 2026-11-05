"""Small, real chat-generation probe. Never logs or returns provider error bodies."""
import json
import ssl
import urllib.error
import urllib.request
from .config import APIError, redact


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def test_provider(config):
    payload = {"model": config["model"], "messages": [{"role": "user", "content": "Translate the word Hello into " + config["targetLang"] + ". Output only the translation."}], "max_tokens": 32, "stream": False}
    request = urllib.request.Request(config["baseUrl"] + "/chat/completions", data=json.dumps(payload).encode("utf-8"), headers={"Authorization": "Bearer " + config["apiKey"], "Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    context = ssl.create_default_context()
    try:
        import certifi
        context.load_verify_locations(certifi.where())
    except ImportError:
        pass
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
    try:
        with opener.open(request, timeout=45) as response:
            data = response.read(65537)
            if len(data) > 65536:
                raise APIError("The API test response was unexpectedly large.", 502)
            result = json.loads(data)
        sample = result["choices"][0]["message"]["content"]
        if not isinstance(sample, str) or not sample.strip():
            raise ValueError()
        return {"ok": True, "model": config["model"], "sample": redact(sample.strip(), config["apiKey"])}
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        messages = {401: "API authentication failed (HTTP 401). Check the key and endpoint.", 403: "API access denied (HTTP 403). Check model permissions.", 404: "Chat endpoint or model not found (HTTP 404). Check baseUrl and model.", 429: "API rate or quota limit reached (HTTP 429)."}
        raise APIError(messages.get(status, f"Chat generation failed (HTTP {status}); the provider response was withheld to protect credentials."), 502) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise APIError("Could not reach the API securely. Check the address, certificate, proxy, and network.", 502) from None
    except (ValueError, KeyError, IndexError, TypeError):
        raise APIError("The API did not return a non-empty OpenAI-compatible chat completion.", 502) from None
