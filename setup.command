#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
printf '%s\n' '纸桥翻译：安装本地 PDF 引擎（首次运行需下载依赖）'
mkdir -p .tools .runtime .cache
export UV_CACHE_DIR="$PWD/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$PWD/.runtime/python"
export UV_NATIVE_TLS=true
if [ ! -x .tools/bin/uv ]; then
  python3 -m pip install --disable-pip-version-check --target .tools uv==0.9.10
fi
if [ ! -x .venv/bin/python ]; then
  .tools/bin/uv venv --python 3.12 .venv
fi
.tools/bin/uv pip install --python .venv/bin/python -r requirements-engine-lock.txt
printf '\n%s\n' '安装完成。双击 enable-background.command 启用后台服务，再通过 Zotero 插件管理器安装 dist 文件夹中的 XPI。'
printf '%s\n' '从 Git 克隆时，可先运行 python3 scripts/build.py 生成安装包。'
printf '%s\n' '首次翻译还会下载 BabelDOC 所需的布局模型和字体；具体耗时取决于网络。'
