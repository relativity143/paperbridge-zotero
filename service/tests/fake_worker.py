"""TEST DOUBLE ONLY: protocol/queue fixture; does not perform PDF translation."""
import json
from pathlib import Path
import sys
import time
payload = json.load(sys.stdin)
mode = sys.argv[1]
out = Path(payload["outputDir"])
print(json.dumps({"type": "progress", "stage": "mock engine", "progress": 35}), flush=True)
if mode == "sleep":
    time.sleep(120)
elif mode == "error":
    print(json.dumps({"type": "error", "error": "fixture " + payload["config"]["apiKey"]}), flush=True)
elif mode == "missing":
    print(json.dumps({"type": "finish", "outputs": {"dual": str(out/"missing.pdf"), "mono": str(out/"missing2.pdf")}}), flush=True)
else:
    target = out.parent if mode == "escape" else out
    dual, mono = target/"dual.pdf", target/"mono.pdf"
    # Signature fixture only; actual BabelDOC integration is tested separately.
    dual.write_bytes(b"%PDF-1.7\nMOCK-DUAL\n%%EOF")
    outputs = {"dual": str(dual)}
    if payload["config"]["generateMono"] and mode != "dual_only":
        mono.write_bytes(b"%PDF-1.7\nMOCK-MONO\n%%EOF")
        outputs["mono"] = str(mono)
    print(json.dumps({"type": "finish", "outputs": outputs}), flush=True)
