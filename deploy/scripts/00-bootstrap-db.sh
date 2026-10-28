#!/usr/bin/env bash
# ============================================================================
# 00-bootstrap-db.sh · 在共享 PostgreSQL 上准备 Chatwoot 的库与角色
#
# 幂等：可重复执行。
#
# 复用本机共享实例 ~/postgres-trial（容器 trial-web-db，库端口 5432）：
#   · 角色 chatwoot  —— 只 OWN 自己那个库
#   · 库   chatwoot  —— owner = chatwoot
#   · 扩展 pg_stat_statements / pg_trgm / pgcrypto / vector
#     （与 db/migrate/20230426130150_init_schema.rb 的 enable_extension 对齐）
#
# 权限来源：共享实例的超级用户凭据在 ~/postgres-trial/.env（600，本脚本只读）。
# Chatwoot 自己的口令在项目 .env 里 —— 绝不在本文件硬编码。
# ============================================================================

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
ENV_FILE="$ROOT/.env"
SHARED_ENV="$HOME/postgres-trial/.env"
DB_CONTAINER="trial-web-db"

[ -f "$ENV_FILE" ] || { echo "缺少 $ENV_FILE —— 先跑 scripts/gen-env.py" >&2; exit 1; }
[ -f "$SHARED_ENV" ] || { echo "缺少 $SHARED_ENV —— 共享 PG 项目没装好" >&2; exit 1; }
docker ps --format '{{.Names}}' | grep -qx "$DB_CONTAINER" || {
  echo "共享 PG 容器 $DB_CONTAINER 未运行 —— 先 cd ~/postgres-trial && docker compose up -d" >&2; exit 1; }

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

# ⚠️ 共享实例的 .env 里也有 POSTGRES_PASSWORD（那是超管口令）—— 直接 source 会把
#    本项目 chatwoot 角色的口令覆盖掉。必须读进**不同的变量名**。
SHARED_PG_SUPERUSER=""
SHARED_PG_SUPERPW=""
while IFS='=' read -r key value; do
  key="${key#"${key%%[![:space:]]*}"}"
  value="${value#${value%%[![:space:]]*}}"
  case "$key" in
    POSTGRES_USER)    SHARED_PG_SUPERUSER="$value" ;;
    POSTGRES_PASSWORD) SHARED_PG_SUPERPW="$value" ;;
  esac
done < "$SHARED_ENV"
SUPERUSER="${SHARED_PG_SUPERUSER:-trial}"

: "${POSTGRES_PASSWORD:?项目 .env 里缺 POSTGRES_PASSWORD}"
: "${POSTGRES_DATABASE:?项目 .env 里缺 POSTGRES_DATABASE}"
: "${POSTGRES_USERNAME:?项目 .env 里缺 POSTGRES_USERNAME}"

if [ -n "$SHARED_PG_SUPERPW" ] && [ "$SHARED_PG_SUPERPW" = "$POSTGRES_PASSWORD" ]; then
  echo "⚠️ 本项目口令与共享实例超管口令相同 —— 换一个，别复用" >&2
  exit 1
fi

psql_super() {
  docker exec -i "$DB_CONTAINER" psql -U "$SUPERUSER" -d postgres -v ON_ERROR_STOP=1 "$@"
}

echo "==> 准备角色 $POSTGRES_USERNAME"
psql_super -c "DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$POSTGRES_USERNAME') THEN
    CREATE ROLE $POSTGRES_USERNAME LOGIN PASSWORD '$POSTGRES_PASSWORD';
  ELSE
    ALTER ROLE $POSTGRES_USERNAME LOGIN PASSWORD '$POSTGRES_PASSWORD';
  END IF;
END \$\$;"

echo "==> 准备库 $POSTGRES_DATABASE"
if ! docker exec "$DB_CONTAINER" psql -U "$SUPERUSER" -d postgres -tAc \
      "SELECT 1 FROM pg_database WHERE datname='$POSTGRES_DATABASE'" | grep -q 1; then
  psql_super -c "CREATE DATABASE $POSTGRES_DATABASE OWNER $POSTGRES_USERNAME ENCODING 'UTF8' TEMPLATE template0;"
else
  psql_super -c "ALTER DATABASE $POSTGRES_DATABASE OWNER TO $POSTGRES_USERNAME;"
fi

echo "==> 启用扩展"
docker exec -i "$DB_CONTAINER" psql -U "$SUPERUSER" -d "$POSTGRES_DATABASE" -v ON_ERROR_STOP=1 <<'SQL'
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS plpgsql;
CREATE EXTENSION IF NOT EXISTS vector;
SQL

echo "==> 核对"
docker exec "$DB_CONTAINER" psql -U "$SUPERUSER" -d "$POSTGRES_DATABASE" -tAc \
  "SELECT extname FROM pg_extension ORDER BY 1;" | sed 's/^/    /'
echo "OK: 角色 $POSTGRES_USERNAME · 库 $POSTGRES_DATABASE"
