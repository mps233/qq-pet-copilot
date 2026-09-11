#!/bin/bash
# 启动手机仪表盘（默认端口 8787，内网可访问）
cd "$(dirname "$0")" || exit 1
exec .venv/bin/python dashboard.py "$@"
