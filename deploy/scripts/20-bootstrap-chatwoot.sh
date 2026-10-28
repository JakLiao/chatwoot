#!/usr/bin/env bash
# ============================================================================
# 20-bootstrap-chatwoot.sh · 引导初始化 Chatwoot 并回填 .env
#
# 前置：scripts/00-bootstrap-db.sh + scripts/10-migrate.sh 已经跑过，
#       且 rails 服务已经起来。
#
# 产出（回填进 .env）：
#   CHATWOOT_ACCOUNT_ID    采集账户 id
#   CHATWOOT_INBOX_ID      API 频道收件箱 id
#   CHATWOOT_ACCESS_TOKEN  Application API 访问令牌
#
# 幂等：重复执行不会重复建账户/收件箱/标签/属性，只会刷新令牌。
# ============================================================================

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT"

ENV_FILE="$ROOT/.env"

echo "==> 启动 rails"
docker compose up -d rails

echo "==> 等待 rails 就绪"
ready=0
for _ in $(seq 1 60); do
  if docker compose exec -T rails wget -q -O- http://127.0.0.1:3000/health >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 5
done
if [ "$ready" -ne 1 ]; then
  echo "rails 一直不健康 —— 看日志：docker compose logs --tail=100 rails" >&2
  exit 1
fi

echo "==> 执行引导初始化"
if ! OUTPUT="$(docker compose run --rm -v "$ROOT:/bootstrap:ro" \
  rails bundle exec rails runner /bootstrap/scripts/bootstrap_chatwoot.rb 2>&1)"; then
  echo "$OUTPUT"
  echo "引导初始化失败" >&2
  exit 1
fi
echo "$OUTPUT"

BLOCK="$(printf '%s\n' "$OUTPUT" | sed -n '/^---CHATWOOT_ENV---$/,/^---END---$/p')"
if [ -z "$BLOCK" ]; then
  echo "引导脚本没有输出 CHATWOOT_ENV 段 —— 初始化失败" >&2
  exit 1
fi

echo "==> 回填 .env"
while IFS= read -r line; do
  case "$line" in
    ---*|"") continue ;;
    ADMIN_PASSWORD=*) continue ;;   # 密码不进 .env，单独存文件
  esac
  key="${line%%=*}"
  value="${line#*=}"
  if grep -q "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
  echo "    $key 已更新"
done <<< "$BLOCK"

chmod 600 "$ENV_FILE"

# 管理员初始密码只在「新建用户」那一轮有明文 —— 单独存文件，不进 .env
ADMIN_PASSWORD="$(printf '%s\n' "$BLOCK" | sed -n 's/^ADMIN_PASSWORD=//p' | tail -1)"
if [ -n "$ADMIN_PASSWORD" ]; then
  mkdir -p "$ROOT/keys"; chmod 700 "$ROOT/keys"
  umask 077
  printf '%s\n' "$ADMIN_PASSWORD" > "$ROOT/keys/admin-password.txt"
  chmod 600 "$ROOT/keys/admin-password.txt"
  echo "    管理员初始密码已存到 keys/admin-password.txt (600)"
fi

echo "==> 完成。现在可以 docker compose up -d"
