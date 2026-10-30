#!/usr/bin/env bash
# ============================================================================
# 10-migrate.sh · 建表 + 跑迁移
#
# 在 rails 容器里执行（复用镜像里的 gem，不需要源码构建）：
#   bundle exec rails db:prepare
#
# db:prepare = 库不存在则创建 + 跑完所有 pending 迁移。
# 本项目的库已由 scripts/00-bootstrap-db.sh 在共享 PG 上建好，这里只跑迁移。
#
# 幂等：可重复执行。
# ============================================================================

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT"

echo "==> 等待 rails 容器起来（迁移要在 DB 连通后跑）"
docker compose up -d rails
for _ in $(seq 1 60); do
  if docker compose exec -T rails wget -q -O- http://127.0.0.1:3000/health >/dev/null 2>&1; then
    break
  fi
  sleep 5
done

echo "==> 执行迁移"
docker compose run --rm rails bundle exec rails db:prepare

echo "==> 迁移状态"
docker compose run --rm rails bundle exec rails db:version
