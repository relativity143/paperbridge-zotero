#!/usr/bin/env python3
"""Build a source-and-XPI macOS bundle without runtimes or personal state."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {"README.md", "README.en.md", "LICENSE", "NOTICE.md", "CHANGELOG.md",
              ".gitignore", "requirements-engine-lock.txt", "setup.command", "start.command",
              "enable-background.command", "stop-background.command"}
SOURCE_DIRS = {"addon", "service", "tests", "scripts", "docs"}
SOURCE_SUFFIXES = {".py", ".js", ".cjs", ".json", ".xhtml", ".css", ".svg", ".md", ".txt"}


def release_files():
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if path.is_symlink():
            continue
        if relative.as_posix() in ROOT_FILES and path.is_file():
            yield path, relative
        elif (relative.parts[0] in SOURCE_DIRS and path.is_file()
              and not any(p.startswith(".") or p == "__pycache__" for p in relative.parts)
              and path.suffix in SOURCE_SUFFIXES):
            yield path, relative


def main():
    subprocess.run([sys.executable, str(ROOT / "scripts/build.py")], check=True)
    version = json.loads((ROOT / "addon/manifest.json").read_text())["version"]
    output = ROOT / "dist"
    xpi = output / f"paperbridge-{version}.xpi"
    bundle = output / f"paperbridge-{version}-macos.zip"
    entries = list(release_files()) + [(xpi, Path("dist") / xpi.name),
        (xpi.with_suffix(".xpi.sha256"), Path("dist") / (xpi.name + ".sha256"))]
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for source, relative in entries:
            info = zipfile.ZipInfo((Path("paperbridge-zotero") / relative).as_posix(), (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if source.suffix == ".command" else 0o100644) << 16
            archive.writestr(info, source.read_bytes())
    with zipfile.ZipFile(bundle) as archive:
        assert archive.testzip() is None
    checksums = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in (xpi, bundle))
    (output / "SHA256SUMS.txt").write_text(checksums)
    print(f"Built {bundle.name}: {len(entries)} files, {bundle.stat().st_size} bytes")


if __name__ == "__main__":
    main()
