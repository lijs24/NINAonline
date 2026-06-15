#!/usr/bin/env bash
# 星枢 NINA Web —— Linux/macOS 启动脚本
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

if [ ! -x ".venv/bin/python" ]; then
  echo "[安装] 创建虚拟环境..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r backend/requirements.txt
fi

export NINAWEB_PROVIDER="${NINAWEB_PROVIDER:-sim}"
export NINAWEB_PORT="${NINAWEB_PORT:-8788}"
echo "[启动] provider=$NINAWEB_PROVIDER 端口=$NINAWEB_PORT"
echo "       局域网访问: http://<本机IP>:$NINAWEB_PORT/"
cd backend
exec ../.venv/bin/python run.py
