#!/bin/bash
# Finder-launchable source entry. Never replaces an existing backend configuration.
set -euo pipefail
cd "$(dirname "$0")"
trap 'result=$?; if [ "$result" -ne 0 ]; then echo "启动未完成。请保留上面的错误提示。"; read -r -p "按回车关闭此窗口。"; fi' EXIT
if [ ! -x .venv/bin/python ]; then
  selected_python=""
  for candidate in python3.13 python3.12 python3.14 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] <= (3, 14)))' 2>/dev/null; then
      selected_python="$candidate"
      break
    fi
  done
  if [ -z "$selected_python" ]; then
    echo "需要 Python 3.10–3.14。也可直接使用 GitHub Actions 提供的 Mac 应用包，无需安装 Python。"
    exit 1
  fi
  "$selected_python" -m venv .venv
fi
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
.venv/bin/python run.py
