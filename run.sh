#!/bin/bash
# macOS 启动脚本：控制台调度器（透传参数，如 --test coins）
cd "$(dirname "$0")" || exit 1
exec .venv/bin/python scenarios/runner.py "$@"
