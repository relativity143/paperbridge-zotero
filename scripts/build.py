#!/usr/bin/env python3
"""Build a deterministic, dependency-free Zotero XPI."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    addon = ROOT / "addon"
    manifest = json.loads((addon / "manifest.json").read_text())
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    target = output / f"paperbridge-{manifest['version']}.xpi"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(addon.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            info = zipfile.ZipInfo(path.relative_to(addon).as_posix(), (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".xpi.sha256").write_text(f"{digest}  {target.name}\n")
    print(f"Built {target.name}: {target.stat().st_size} bytes; SHA256 {digest}")


if __name__ == "__main__":
    main()
