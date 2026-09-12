#!/bin/bash
# 一键启动：构建并后台启动 MySQL + 风控系统
set -e

cd "$(dirname "$0")"

echo "🚀 正在构建并启动风控系统..."
docker compose up -d --build

echo ""
echo "✅ 启动完成"
echo "   访问地址: http://localhost:8002/"
echo "   查看日志: docker compose logs -f app"
echo "   停止服务: docker compose down"
