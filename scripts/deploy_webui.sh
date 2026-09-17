#!/usr/bin/env bash
# WebUI 前端部署：本机构建 dist → scp 到 debsvc（dist 不进 git，随脚本同步）。
# 用法: scripts/deploy_webui.sh [host] [dest]      默认 debsvc:/srv/yacmemo/yacmemo/webui
set -euo pipefail

HOST="${1:-debsvc}"
DEST="${2:-/srv/yacmemo/yacmemo/webui}"

cd "$(dirname "$0")/../frontend"
echo "==> npm ci + build"
npm ci --silent
npm run build
cd ..

echo "==> rsync dist -> $HOST:$DEST/dist"
tar czf - -C yacmemo/webui/dist . | ssh "$HOST" "mkdir -p '$DEST/dist' && find '$DEST/dist' -mindepth 1 -delete && tar xzf - -C '$DEST/dist'"

echo "==> 验证 /ui/ 响应"
ssh "$HOST" "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9721/ui/ && echo ' <- /ui/'"
echo "完成。纯前端更新无需重启服务；若同时更新了后端代码请 git pull 后 systemctl restart yacmemo。"
