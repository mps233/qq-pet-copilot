#!/bin/bash
# 启动手机仪表盘（默认端口 8787，内网可访问）
#
# **脱离终端运行**：仪表盘是常驻服务，不能随启动它的终端/job 一起退出
# （曾踩坑：用后台 job 启动，job 被回收时仪表盘一起被杀，UI 打不开）。
# 这里用 python -c 起一个 start_new_session=True 的子进程（等价 setsid，
# macOS 没有 setsid 命令），再 exit —— 仪表盘被 init 收养，与终端无关。
# 日志追加到 runs/dashboard_console.log。
#
# 已在运行时提示并退出，不重复启动（防多实例抢 8787）。
# 停止：kill $(lsof -nP -iTCP:8787 -sTCP:LISTEN -t)
set -u
cd "$(dirname "$0")" || exit 1

PORT=8787
# 取参数里的 --port（支持 --port N 与 --port=N 两种写法）
prev=""
for a in "$@"; do
  case "$prev" in --port) PORT="$a";; esac
  case "$a" in --port=*) PORT="${a#--port=}";; esac
  prev="$a"
done

# 已有实例在监听该端口就不重复启动
if command -v lsof >/dev/null 2>&1; then
  existing=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1)
  if [ -n "$existing" ]; then
    echo "仪表盘已在运行（PID $existing，端口 $PORT）"
    echo "访问: http://127.0.0.1:$PORT/"
    exit 0
  fi
fi

mkdir -p runs
PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

# 传参给 dashboard.py（用 python 侧解析，避免 shell 拼字符串注入）
PORT="$PORT" "$PY" - "$@" <<'PYEOF'
import os, subprocess, sys
from pathlib import Path
base = Path.cwd()
port = os.environ.get('PORT', '8787')
log = base / 'runs' / 'dashboard_console.log'
with open(log, 'ab') as f:
    subprocess.Popen(
        [sys.executable, 'dashboard.py', *sys.argv[1:]],
        cwd=str(base), stdin=subprocess.DEVNULL,
        stdout=f, stderr=subprocess.STDOUT,
        start_new_session=True,   # 脱离终端（macOS 无 setsid 命令，用这个）
    )
print(f'仪表盘已启动（端口 {port}，日志 runs/dashboard_console.log）')
print(f'访问: http://127.0.0.1:{port}/')
PYEOF

# 等端口就绪再返回，便于调用方立刻访问
for _ in $(seq 1 20); do
  if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/"; then
    echo "就绪: http://127.0.0.1:$PORT/"
    exit 0
  fi
  sleep 0.5
done
echo "已启动，但端口 $PORT 暂未就绪，请查看 runs/dashboard_console.log" >&2
exit 1
