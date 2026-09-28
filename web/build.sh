#!/usr/bin/env bash
# 构建新版前端（React + TS）→ web/dist/
#
# 用 esbuild 单文件打包，**不用 vite/rollup**：那套会拉一堆平台原生模块
# （@rollup/rollup-darwin-arm64、@esbuild/darwin-arm64），在 pnpm 的链接布局下
# 反复出 ERR_DLOPEN_FAILED。esbuild 一个二进制就够了，产物一样能用。
#
# 依赖：node（macOS 上 `brew install node`；Windows 上装 Node.js 即可）
# 用法：./web/build.sh        然后在浏览器打开 http://<host>:8787/next/
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v node >/dev/null 2>&1; then
  echo "找不到 node。macOS: brew install node    Windows: 装 Node.js LTS" >&2
  exit 1
fi

if [ ! -d node_modules ]; then
  echo "首次运行：安装依赖…"
  if command -v pnpm >/dev/null 2>&1; then pnpm install; else npm install; fi
fi

# esbuild 的可执行文件：优先 .bin，退回平台包（pnpm 布局下 .bin 可能没建）
ESB=node_modules/.bin/esbuild
if [ ! -x "$ESB" ]; then
  PLATFORM=$(node -p "process.platform + '-' + process.arch")
  ESB="node_modules/@esbuild/${PLATFORM}/bin/esbuild"
fi
if [ ! -x "$ESB" ]; then
  echo "找不到 esbuild 可执行文件，先跑一次 npm install" >&2
  exit 1
fi

mkdir -p dist
"$ESB" src/main.tsx \
  --bundle --outfile=dist/app.js \
  --loader:.tsx=tsx --loader:.ts=ts \
  --jsx=automatic --format=esm --platform=browser \
  --target=es2022 --minify \
  --define:process.env.NODE_ENV='"production"'

# 入口 HTML：把 dev 用的 /src/main.tsx 换成构建产物
sed 's#/src/main.tsx#./app.js#' index.html > dist/index.html

SIZE=$(wc -c < dist/app.js | tr -d ' ')
echo "✓ 构建完成（app.js ${SIZE} 字节）→ 打开 http://<host>:8787/next/"
