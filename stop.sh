#!/bin/bash
# 停止并移除容器（保留数据卷，数据不丢失）
set -e
cd "$(dirname "$0")"
docker compose down
echo "✅ 已停止服务。如需连同数据一起删除，请执行: docker compose down -v"
