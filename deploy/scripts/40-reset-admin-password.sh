#!/usr/bin/env bash
# ============================================================================
# 40-reset-admin-password.sh · 重置 Chatwoot 管理员密码
#
# 引导脚本生成的初始密码只以哈希入库、不留档 —— 万一丢了就登不进 UI。
# 这个脚本生成一个新密码、重置、并存到 keys/admin-password.txt（600）。
#
# 用法：
#   bash scripts/40-reset-admin-password.sh
#
# 账号：默认 voice-admin@chatwoot.local（见 .env 的 CHATWOOT_ADMIN_EMAIL）
# ============================================================================

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT"

ENV_FILE="$ROOT/.env"
[ -f "$ENV_FILE" ] || { echo "缺少 $ENV_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

ADMIN_EMAIL="${CHATWOOT_ADMIN_EMAIL:-voice-admin@chatwoot.local}"
KEY_FILE="$ROOT/keys/admin-password.txt"

mkdir -p "$ROOT/keys"
chmod 700 "$ROOT/keys"

echo "==> 重置 $ADMIN_EMAIL 的密码"
OUTPUT="$(docker compose run --rm -v "$ROOT:/bootstrap:ro" \
  rails bundle exec rails runner /bootstrap/scripts/reset_admin_password.rb 2>&1)" || {
  echo "$OUTPUT"
  echo "重置失败" >&2
  exit 1
}

PASSWORD="$(printf '%s\n' "$OUTPUT" | sed -n 's/^ADMIN_PASSWORD=//p' | tail -1)"
if [ -z "$PASSWORD" ]; then
  echo "$OUTPUT"
  echo "没有拿到新密码" >&2
  exit 1
fi

umask 077
printf '%s\n' "$PASSWORD" > "$KEY_FILE"
chmod 600 "$KEY_FILE"

echo "==> 新密码已存到 $KEY_FILE (600)"
echo "    账号：$ADMIN_EMAIL"
echo "    密码：$PASSWORD"
echo
echo "    登录地址：http://127.0.0.1:${CHATWOOT_PORT:-3000}"
echo "    ⚠️ 首次登录后请在 Chatwoot 里改掉密码，并妥善保管这个文件。"
