#!/usr/bin/env python3
"""Install and control PaperBridge's single, user-owned macOS LaunchAgent."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit

LABEL = "com.paperbridge.service"
SOURCE = Path(__file__).resolve().parents[1]
DATA = Path.home() / "Library/Application Support/PaperBridge"
PLIST = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
DOMAIN = "gui/" + str(os.getuid())
TARGET = DOMAIN + "/" + LABEL


def launchctl(*args, check=True):
    result = subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=30)
    if check and result.returncode:
        raise RuntimeError("launchctl " + args[0] + " failed: " + result.stderr.strip()[:500])
    return result


def state():
    result = launchctl("print", TARGET, check=False)
    pid = re.search(r"^\s*pid = (\d+)$", result.stdout, re.M)
    return {"loaded": result.returncode == 0, "pid": int(pid[1]) if pid else None}


def health(timeout=3):
    try:
        pair = json.loads((DATA / "bridge.json").read_text())
        url = urlsplit(pair["url"])
        if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port:
            return None
        if url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
            return None
        request = urllib.request.Request(pair["url"].rstrip("/") + "/v1/health",
            headers={"Authorization": "Bearer " + pair["token"]})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            data = json.load(response)
        return data if data.get("service") == "PaperBridge" and data.get("ok") is True else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def ready(timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = health(timeout=3)
        if result:
            if not result.get("engine", {}).get("available"):
                raise RuntimeError("后台服务已启动，但 PDF 引擎检查未通过。")
            return result
        time.sleep(0.5)
    raise RuntimeError("后台服务未能就绪，请检查 PaperBridge/logs 中的服务日志。")


def run_python(python, code, timeout=60, env=None):
    result = subprocess.run([str(python), "-c", code], capture_output=True,
                            text=True, timeout=timeout, env=env)
    if result.returncode:
        raise RuntimeError("Python runtime validation failed: " + result.stderr[-1200:])
    return json.loads(result.stdout)


def prepare_runtime(source_python):
    runtime = DATA / "runtime"
    python = runtime / "env/bin/python"
    marker = runtime / "ready.json"
    if marker.is_file() and python.is_file():
        return python
    if runtime.exists():
        if not python.is_file() or not (runtime / "python/bin/python3").is_file():
            raise RuntimeError("发现未完成的运行环境；保留现有目录，请先检查 PaperBridge/runtime。")
        print("继续验证已复制的运行环境。", flush=True)
        check = run_python(python, "import sys,json,importlib.metadata; "
            "import babeldoc.format.pdf.high_level,babeldoc.translator.translator,pymupdf; "
            "print(json.dumps({'base':sys.base_prefix,'version':list(sys.version_info[:2]),"
            "'engine':importlib.metadata.version('BabelDOC')}))", timeout=150)
        if check["engine"] != "0.6.4" or not Path(check["base"]).resolve().is_relative_to(runtime.resolve()):
            raise RuntimeError("已复制的环境身份验证未通过。")
        marker.write_text(json.dumps({"engine": check["engine"], "python": check["version"]}))
        marker.chmod(0o600)
        return python
    candidates = [Path(source_python)] if source_python else [SOURCE / ".venv/bin/python"]
    source = next((p.absolute() for p in candidates if p.is_file()), None)
    if not source:
        raise RuntimeError("请先运行 setup.command 安装 PDF 引擎，然后再启用后台服务。")
    metadata = run_python(source, "import sys,sysconfig,json,importlib.metadata; "
        "print(json.dumps({'base':sys.base_prefix,'site':sysconfig.get_path('purelib'),"
        "'version':list(sys.version_info[:2]),'engine':importlib.metadata.version('BabelDOC')}))")
    if metadata["engine"] != "0.6.4" or not [3, 10] <= metadata["version"] < [3, 14]:
        raise RuntimeError("需要 BabelDOC 0.6.4 和 Python 3.10–3.13。")
    base = Path(metadata["base"])
    # The tested distribution is a relocatable CPython build. Other Python builds
    # may embed framework paths; validate the copied interpreter before using it.
    print("正在保存独立运行环境，完成后无需保留终端或原安装文件夹。", flush=True)
    runtime.mkdir(mode=0o700)
    shutil.copytree(base, runtime / "python", symlinks=True,
                    ignore=shutil.ignore_patterns("site-packages", "__pycache__"))
    base_python = runtime / "python/bin/python3"
    subprocess.run([str(base_python), "-m", "venv", "--without-pip", str(runtime / "env")],
                   check=True, capture_output=True, timeout=60)
    target = run_python(python, "import sys,sysconfig,json; "
        "print(json.dumps({'base':sys.base_prefix,'site':sysconfig.get_path('purelib')}))")
    if not Path(target["base"]).resolve().is_relative_to(runtime.resolve()):
        raise RuntimeError("复制的 Python 仍依赖原安装位置，不能作为独立后台环境。")
    shutil.copytree(metadata["site"], target["site"], dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    run_python(python, "import babeldoc.format.pdf.high_level,babeldoc.translator.translator,pymupdf,json; print(json.dumps({'ok':True}))", timeout=150)
    marker.write_text(json.dumps({"engine": "0.6.4", "python": metadata["version"]}))
    marker.chmod(0o600)
    return python


def enable(source_python):
    existing = state()
    if existing["loaded"]:
        ready()
        print(json.dumps({"ok": True, "reused": True, **state()}, ensure_ascii=False))
        return
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    DATA.chmod(0o700)
    # A manual instance owns this same lock. Never interrupt it or start a second
    # service with another data directory, which would lose task idempotency.
    with (DATA / "service.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("已有手动纸桥服务正在运行。请先停止该启动器，再启用后台服务。") from None
        python = prepare_runtime(source_python)
        app = DATA / "app"
        package = app / "paperbridge_service"
        package.mkdir(parents=True, exist_ok=True)
        for path in (SOURCE / "service/paperbridge_service").glob("*.py"):
            shutil.copy2(path, package / path.name)
        logs = DATA / "logs"
        logs.mkdir(exist_ok=True, mode=0o700)
        for name in ("service.log", "error.log"):
            (logs / name).touch(mode=0o600, exist_ok=True)
            (logs / name).chmod(0o600)
        config = {
            "Label": LABEL,
            "ProgramArguments": [str(python), "-m", "paperbridge_service", "--data-dir", str(DATA), "--port", "0"],
            "WorkingDirectory": str(app),
            "EnvironmentVariables": {"PYTHONPATH": str(app), "PYTHONUNBUFFERED": "1",
                                     "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
            "ExitTimeOut": 20, "ProcessType": "Background", "Umask": 0o077,
            "StandardOutPath": str(logs / "service.log"), "StandardErrorPath": str(logs / "error.log"),
        }
        if PLIST.exists():
            prior = plistlib.loads(PLIST.read_bytes())
            if prior.get("Label") != LABEL or prior.get("ProgramArguments", [None])[0] != str(python):
                raise RuntimeError("存在不同的同名 LaunchAgent，已保留，未覆盖。")
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        temporary = PLIST.with_name(PLIST.name + ".tmp")
        temporary.write_bytes(plistlib.dumps(config))
        temporary.chmod(0o600)
        os.replace(temporary, PLIST)
    launchctl("enable", TARGET)
    launchctl("bootstrap", DOMAIN, str(PLIST))
    ready()
    print(json.dumps({"ok": True, "reused": False, **state(), "label": LABEL,
                      "working_directory": str(DATA / "app"), "starts_at_login": True}, ensure_ascii=False))


def stop():
    previous = state()
    launchctl("disable", TARGET)
    if previous["loaded"]:
        launchctl("bootout", TARGET)
    deadline = time.monotonic() + 20
    while previous["pid"] and time.monotonic() < deadline:
        try:
            os.kill(previous["pid"], 0)
        except ProcessLookupError:
            break
        time.sleep(0.25)
    if previous["pid"]:
        try:
            os.kill(previous["pid"], 0)
        except ProcessLookupError:
            pass
        else:
            raise RuntimeError("后台服务已卸载，但原进程尚未退出；请保留日志进行检查。")
    if state()["loaded"]:
        raise RuntimeError("后台服务仍被加载，请检查 launchctl 状态。")
    print("已停用纸桥后台服务及登录自启。任务和配置已保留；再次运行 enable-background.command 可恢复。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["enable", "stop", "status"])
    parser.add_argument("--source-python")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("后台管理目前仅支持 macOS。")
    if args.action == "enable":
        enable(args.source_python)
    elif args.action == "stop":
        stop()
    else:
        current = state()
        result = health() if current["loaded"] else None
        print(json.dumps({**current, "healthy": bool(result),
                          "engine_available": bool(result and result.get("engine", {}).get("available"))}))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
