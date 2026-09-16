#!/usr/bin/env bash
# 构建 WebUI 前端 → yacmemo/webui/dist/（app.py STATIC_DIR 的取用位置）。
#
# 服务器通常没有 npm/Node：在开发机跑本脚本，再把产物同步过去，例如：
#   scp -r yacmemo/webui/dist <server>:/srv/yacmemo/yacmemo/webui/
set -euo pipefail

frontend="$(cd "$(dirname "$0")/../frontend" && pwd)"
cd "$frontend"

if ! command -v npm >/dev/null 2>&1; then
  echo "错误：未找到 npm（需要 Node 18+）。请在有 Node 的开发机上构建。" >&2
  exit 1
fi

if [ -f package-lock.json ]; then
  npm ci
else
  npm install
fi

npm run build

echo "构建完成 → $(cd ../yacmemo/webui/dist && pwd)"
