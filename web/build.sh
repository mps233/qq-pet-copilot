#!/usr/bin/env bash
# 构建新版前端（React + TS）→ web/dist/
#
# 用 Vite：dev 有热更新（npm run dev），构建走 rollup。
# 注意：node 必须用**用户自己装的**（官方/Homebrew 的 node 都能加载 rollup 的原生模块；
# 某些受限环境里 node 启用了 Library Validation，会因签名 Team ID 不同拒绝加载
# rollup.darwin-arm64.node，报 ERR_DLOPEN_FAILED —— 那种情况下换官方 node 即可）。
#
# 依赖：node + npm（macOS: brew install node）
# 用法：./web/build.sh        然后打开 http://<host>:8787/next/
set -euo pipefail
cd "$(dirname "$0")"

# 把常见的用户级 node 位置加进 PATH（GUI 启动的 shell 往往没有这些路径）
for d in "$HOME/.local/bin" "$HOME/.local/node/bin" /opt/homebrew/bin /usr/local/bin; do
  if [ -x "$d/node" ]; then export PATH="$d:$PATH"; break; fi
done

if ! command -v node >/dev/null 2>&1; then
  echo "找不到 node。macOS: brew install node    Windows: 装 Node.js LTS" >&2
  exit 1
fi

if [ ! -d node_modules ]; then
  echo "首次运行：安装依赖…"
  if command -v pnpm >/dev/null 2>&1; then pnpm install; else npm install; fi
fi

echo "node: $(node -v)"
node node_modules/vite/bin/vite.js build

SIZE=$(wc -c < dist/assets/*.js 2>/dev/null | tail -1 | tr -d ' ')
echo "✓ 构建完成（bundle ${SIZE} 字节）→ 打开 http://<host>:8787/"
