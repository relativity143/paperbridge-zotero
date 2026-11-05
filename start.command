#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
runtime_python="${PAPERBRIDGE_PYTHON:-.venv/bin/python}"
if [ ! -x "$runtime_python" ]; then
  printf '%s\n' '请先运行 setup.command 安装本地 PDF 引擎。'
  exit 1
fi
export PYTHONPATH="$PWD/service${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1
printf '%s\n' '纸桥翻译本地服务正在启动。使用时保持此窗口打开；按 Ctrl+C 停止。'
exec "$runtime_python" -m paperbridge_service "$@"
