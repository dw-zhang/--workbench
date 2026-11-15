#!/usr/bin/env bash
# 一键部署到 Linux 服务器（需要 ssh 免密或输一次密码）
# 用法:  ./deploy.sh 用户@服务器IP [目标目录，默认 /opt/workbench]
# 示例:  ./deploy.sh user@192.168.1.10
set -e
DEST=${1:?用法: ./deploy.sh 用户@服务器IP [目标目录]}
DIR=${2:-/opt/workbench}
cd "$(dirname "$0")"

echo "==> 同步文件到 $DEST:$DIR ..."
ssh "$DEST" "sudo mkdir -p $DIR && sudo chown \$(whoami) $DIR" 2>/dev/null || ssh "$DEST" "mkdir -p $DIR"
rsync -az --delete --exclude data --exclude __pycache__ --exclude .DS_Store \
  ./ "$DEST:$DIR/"

echo "==> 服务器上构建并启动 ..."
ssh "$DEST" "cd $DIR && (docker compose up -d --build 2>/dev/null || docker-compose up -d --build) && sleep 2 && (docker compose ps 2>/dev/null || docker-compose ps)"

HOST=${DEST#*@}
echo ""
echo "✅ 部署完成：http://$HOST:8787"
echo "   下一步：打开网页 → 设置 → 钉钉连接 → 填凭证 → 测试连接 → 保存"
