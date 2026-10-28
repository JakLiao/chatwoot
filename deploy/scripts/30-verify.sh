#!/usr/bin/env bash
# ============================================================================
# 30-verify.sh · 端到端自检
#
# 逐项验证（任何一项失败都会以非零退出码结束）：
#   1. 三个容器都在跑且健康
#   2. Chatwoot /health 可用
#   3. 网关 /healthz 可用
#   4. 端口只绑在 127.0.0.1（没有对外暴露）
#   5. 用真实 API Key 提交一条反馈 → 拿到 conversation id
#   6. 同一个 request_id 再提交一次 → 命中幂等（不新建会话、不重复建联系人）
#   7. Chatwoot 侧能查到这条会话，且来源系统/自定义属性/标签都对
#   9. 共享 PG 里确实落库
#   10. 身份信息（邮箱/电话）确认不落库
#   11. 清理本次产生的测试数据
# ============================================================================

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT"

# shellcheck disable=SC1091
set -a; . "$ROOT/.env"; set +a

KEY_FILE="$ROOT/keys/company_xianxing.key"
API_KEY="$(cat "$KEY_FILE")"
INTAKE_URL="http://127.0.0.1:${INTAKE_PORT:-8080}"
CW_URL="http://127.0.0.1:${CHATWOOT_PORT:-3000}"

FAILED=0
step()  { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()    { printf '    \033[32m✔ %s\033[0m\n' "$1"; }
fail()  { printf '    \033[31m✘ %s\033[0m\n' "$1"; FAILED=1; }

# ---- 1. 容器状态 -------------------------------------------------------
step "1/12 容器状态"
for svc in rails sidekiq intake; do
  state="$(docker compose ps --format '{{.Service}} {{.State}}' "$svc" 2>/dev/null || true)"
  if printf '%s' "$state" | grep -q "running"; then
    ok "$svc 运行中"
  else
    fail "$svc 未运行（$state）"
  fi
done

# ---- 2. Chatwoot 健康 ---------------------------------------------------
step "2/12 Chatwoot 健康"
if curl -fsS --max-time 10 "$CW_URL/health" >/dev/null 2>&1; then
  ok "$CW_URL/health 可用"
else
  fail "$CW_URL/health 不可用"
fi

# ---- 3. 网关健康 --------------------------------------------------------
step "3/12 采集网关健康"
if curl -fsS --max-time 10 "$INTAKE_URL/healthz" >/dev/null 2>&1; then
  ok "$INTAKE_URL/healthz 可用"
else
  fail "$INTAKE_URL/healthz 不可用"
fi

# ---- 4. 端口绑定 --------------------------------------------------------
step "4/12 端口只绑回环"
for container in chatwoot-rails chatwoot-intake; do
  # ⚠️ grep 无匹配会返回 1，配合 set -e/pipefail 会直接中断脚本 —— 必须兜住
  host_ips="$(docker inspect -f '{{range $p, $conf := .NetworkSettings.Ports}}{{range $conf}}{{.HostIp}} {{end}}{{end}}' "$container" 2>/dev/null \
    | tr -s ' ' '\n' | grep -v '^$' | sort -u || true)"
  if [ -z "$host_ips" ]; then
    ok "$container 没有对外发布端口"
  elif printf '%s\n' "$host_ips" | grep -qv '^127\.0\.0\.1$'; then
    fail "$container 的端口绑在非回环地址：$(echo $host_ips | tr '\n' ' ')"
  else
    ok "$container 端口只绑 127.0.0.1"
  fi
done

# sidekiq 不该发布任何端口
sidekiq_ips="$(docker inspect -f '{{range $p, $conf := .NetworkSettings.Ports}}{{range $conf}}{{.HostIp}} {{end}}{{end}}' chatwoot-sidekiq 2>/dev/null \
  | tr -s ' ' '\n' | grep -v '^$' | sort -u || true)"
if [ -z "$sidekiq_ips" ]; then
  ok "chatwoot-sidekiq 没有发布端口"
else
  fail "chatwoot-sidekiq 发布了端口：$(echo $sidekiq_ips | tr '\n' ' ')"
fi

# 宿主机上 3000/8080 必须只监听 127.0.0.1
for port in "${CHATWOOT_PORT:-3000}" "${INTAKE_PORT:-8080}"; do
  listeners="$(ss -ltnH "sport = :$port" 2>/dev/null | awk '{print $4}' | sort -u | tr '\n' ' ' || true)"
  if [ -z "$listeners" ]; then
    ok "宿主机 $port 没有监听"
  elif printf '%s' "$listeners" | grep -qv '127\.0\.0\.1'; then
    fail "宿主机 $port 监听在非回环地址：$listeners"
  else
    ok "宿主机 $port 只监听 $listeners"
  fi
done

# ---- 5. 管理员登录 ------------------------------------------------------
step "5/12 管理员登录"
ADMIN_EMAIL="${CHATWOOT_ADMIN_EMAIL:-voice-admin@chatwoot.local}"
ADMIN_PW_FILE="$ROOT/keys/admin-password.txt"
if [ -f "$ADMIN_PW_FILE" ]; then
  ADMIN_PW="$(cat "$ADMIN_PW_FILE")"
  # ⚠️ Chatwoot v4.19.1 的登录接口读的是**顶层** email/password ——
  #    嵌套在 {"user": {...}} 里会 401
  LOGIN_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -X POST "$CW_URL/auth/sign_in" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"${ADMIN_EMAIL}\",\"password\":\"${ADMIN_PW}\"}")"
  if [ "$LOGIN_CODE" = "200" ]; then
    ok "$ADMIN_EMAIL 可以登录（口令与 keys/admin-password.txt 一致）"
  else
    fail "管理员登录失败 HTTP $LOGIN_CODE —— 跑 scripts/40-reset-admin-password.sh 重置口令"
  fi
else
  fail "缺少 $ADMIN_PW_FILE —— 跑 scripts/40-reset-admin-password.sh 生成"
fi

# ---- 6. 提交一条反馈 ----------------------------------------------------
step "6/12 提交反馈"
REQUEST_ID="verify-$(date +%s)-$$"
PAYLOAD=$(cat <<JSON
{
  "source_system": "company_xianxing",
  "request_id": "${REQUEST_ID}",
  "customer": {
    "identifier": "verify-user-$$",
    "name": "自检用户",
    "company": "自检公司"
  },
  "feedback": {
    "content": "自检提交：导出功能偶尔会卡住，希望优化一下。",
    "title": "导出卡住",
    "feedback_type": "complaint",
    "sentiment": "negative",
    "rating": 2,
    "channel": "app"
  },
  "metadata": {
    "page": "/export",
    "app_version": "3.2.1",
    "nested": { "trace_id": "trace-$$" }
  }
}
JSON
)

RESP="$(curl -fsS --max-time 30 -X POST "$INTAKE_URL/intake/v1/feedback" \
  -H 'Content-Type: application/json' \
  -H "X-Intake-Key: ${API_KEY}" \
  -d "$PAYLOAD")" || { fail "提交失败"; echo "$RESP"; }

CONVERSATION_ID="$(printf '%s' "$RESP" | sed -n 's/.*"conversation_id":\([0-9]*\).*/\1/p')"
CONTACT_ID="$(printf '%s' "$RESP" | sed -n 's/.*"contact_id":\([0-9]*\).*/\1/p')"
STATUS="$(printf '%s' "$RESP" | sed -n 's/.*"status":"\([a-z]*\)".*/\1/p')"
if [ -n "$CONVERSATION_ID" ]; then
  ok "已创建会话 id=$CONVERSATION_ID contact=$CONTACT_ID status=$STATUS"
  echo "    响应：$RESP"
else
  fail "没有拿到 conversation_id"
fi

# ---- 7. 幂等重放 --------------------------------------------------------
step "7/12 幂等重放（同一个 request_id）"
RESP2="$(curl -fsS --max-time 30 -X POST "$INTAKE_URL/intake/v1/feedback" \
  -H 'Content-Type: application/json' \
  -H "X-Intake-Key: ${API_KEY}" \
  -d "$PAYLOAD")" || { fail "重放失败"; }
CONVERSATION_ID2="$(printf '%s' "$RESP2" | sed -n 's/.*"conversation_id":\([0-9]*\).*/\1/p')"
CONTACT_ID2="$(printf '%s' "$RESP2" | sed -n 's/.*"contact_id":\([0-9]*\).*/\1/p')"
STATUS2="$(printf '%s' "$RESP2" | sed -n 's/.*"status":"\([a-z]*\)".*/\1/p')"
if [ "$CONVERSATION_ID2" = "$CONVERSATION_ID" ] && [ "$STATUS2" = "duplicate" ]; then
  ok "幂等生效：复用会话 id=$CONVERSATION_ID2 status=$STATUS2"
else
  fail "幂等没生效：id=$CONVERSATION_ID2 status=$STATUS2（期望同 id + duplicate）"
fi
if [ "$CONTACT_ID2" = "$CONTACT_ID" ]; then
  ok "联系人也是复用的（contact_id=$CONTACT_ID2），没有重复建人"
else
  fail "联系人被重复创建：$CONTACT_ID → $CONTACT_ID2"
fi

# ---- 8. Chatwoot 侧核对 -------------------------------------------------
step "8/12 Chatwoot 侧核对"
CONV="$(curl -fsS --max-time 30 -H "api_access_token: ${CHATWOOT_ACCESS_TOKEN}" \
  "$CW_URL/api/v1/accounts/${CHATWOOT_ACCOUNT_ID}/conversations/${CONVERSATION_ID}")" || true
if printf '%s' "$CONV" | grep -q '"company_xianxing"'; then
  ok "会话里能看到来源系统 company_xianxing"
else
  fail "会话里没找到来源系统标记"
fi
if printf '%s' "$CONV" | grep -q '"meta.page"'; then
  ok "上游 metadata 已拍平进 additional_attributes"
else
  fail "metadata 没进 additional_attributes"
fi
if printf '%s' "$CONV" | grep -q '自检提交'; then
  ok "消息正文已落库"
else
  fail "消息正文没找到"
fi

LABELS="$(curl -fsS --max-time 30 -H "api_access_token: ${CHATWOOT_ACCESS_TOKEN}" \
  "$CW_URL/api/v1/accounts/${CHATWOOT_ACCOUNT_ID}/conversations/${CONVERSATION_ID}/labels" 2>/dev/null || true)"
if printf '%s' "$LABELS" | grep -q '公司显形'; then
  ok "来源标签「公司显形」已挂上"
else
  fail "来源标签没挂上（$LABELS）"
fi

# ---- 9. 共享 PG 落库核对 -----------------------------------------------------
step "9/12 共享 PG 落库核对"
ROW="$(docker exec trial-web-db psql -U trial -d "${POSTGRES_DATABASE}" -tAc \
  "select c.display_id, c.status, m.content from conversations c join messages m on m.conversation_id = c.id where c.id = ${CONVERSATION_ID} limit 1;" 2>/dev/null || true)"
if [ -n "$ROW" ]; then
  ok "PG 里查到了：$ROW"
else
  fail "PG 里查不到这条会话"
fi

# ---- 10. 身份信息不落库 -------------------------------------------------
step "10/12 身份信息由 Casdoor 管理，不在采集侧收集"
CONTACT_ROW="$(docker exec trial-web-db psql -U trial -d "${POSTGRES_DATABASE}" -tAc \
  "select coalesce(email,'') || '|' || coalesce(phone_number,'') from contacts where id = ${CONTACT_ID};" 2>/dev/null || true)"
if [ "$CONTACT_ROW" = "|" ]; then
  ok "Chatwoot 联系人 email/phone_number 均为空"
else
  fail "Chatwoot 联系人竟然存了身份信息：$CONTACT_ROW"
fi

# 传了 email/phone 的请求要被明确拒绝，而不是静默吞掉
REJECT_BODY="$(curl -s --max-time 20 -X POST "$INTAKE_URL/intake/v1/feedback" \
  -H 'Content-Type: application/json' \
  -H "X-Intake-Key: ${API_KEY}" \
  -d '{"source_system":"company_xianxing","customer":{"identifier":"reject-test","email":"a@b.com"},"feedback":{"content":"x"}}')"
if printf '%s' "$REJECT_BODY" | grep -q 'Casdoor'; then
  ok "带 email 的请求被明确拒绝并指向 Casdoor"
else
  fail "带 email 的请求没被正确拒绝：$REJECT_BODY"
fi

# ---- 11. Casdoor 用户回查 ------------------------------------------------
step "11/12 Casdoor 用户回查（补全展示名）"
CASDOOR_USER="trial_test_user"
if [ -n "${CASDOOR_APP_ID:-}" ] && [ -n "${CASDOOR_APP_SECRET:-}" ]; then
  # 直接问 Casdoor 要期望的展示名 —— 不写死，免得对方改了显示名测试就假失败
  CAS_EXPECTED="$(curl -s --max-time 10 -u "${CASDOOR_APP_ID}:${CASDOOR_APP_SECRET}" \
    "${CASDOOR_BASE_URL}/api/get-user?id=${CASDOOR_ORG}/${CASDOOR_USER}" 2>/dev/null \
    | python3 -c "import json,sys
try:
    d = json.load(sys.stdin)
    print((d.get('data') or {}).get('displayName') or '')
except Exception:
    print('')" 2>/dev/null || true)"
  if [ -z "$CAS_EXPECTED" ]; then
    printf '    \033[33m— Casdoor 不可达或用户不存在，跳过本项\033[0m\n'
  else
    CAS_RID="verify-casdoor-$$"
    CAS_RESP="$(curl -s --max-time 30 -X POST "$INTAKE_URL/intake/v1/feedback" \
      -H 'Content-Type: application/json' \
      -H "X-Intake-Key: ${API_KEY}" \
      -d "{\"source_system\":\"company_xianxing\",\"request_id\":\"${CAS_RID}\",\"customer\":{\"identifier\":\"${CASDOOR_USER}\"},\"feedback\":{\"content\":\"Casdoor 回查自检\"}}")"
    CAS_CID="$(printf '%s' "$CAS_RESP" | sed -n 's/.*"contact_id":\([0-9]*\).*/\1/p')"
    CAS_NAME="$(docker exec trial-web-db psql -U trial -d "${POSTGRES_DATABASE}" -tAc \
      "select name from contacts where id=${CAS_CID};" 2>/dev/null || true)"
    if [ "$CAS_NAME" = "$CAS_EXPECTED" ]; then
      ok "Casdoor 回查生效：展示名自动补全为「${CAS_NAME}」"
    else
      fail "Casdoor 回查没生效（拿到「${CAS_NAME}」，期望「${CAS_EXPECTED}」）"
    fi
  fi
else
  printf '    \033[33m— 未配置 Casdoor 凭据，跳过本项\033[0m\n'
fi

# ---- 12. 清理测试数据 ----------------------------------------------------
step "12/12 清理测试数据"
CLEANUP="$(docker compose run --rm -v "$ROOT:/bootstrap:ro" \
  rails bundle exec rails runner /bootstrap/scripts/cleanup_test_data.rb 2>&1 || true)"
printf '%s\n' "$CLEANUP" | grep -E '^\[cleanup\]' | sed 's/^/    /'
if printf '%s\n' "$CLEANUP" | grep -q '^\[cleanup\] 完成'; then
  ok "测试数据已清理"
else
  fail "清理失败（不影响功能，可手工处理）"
fi

printf '\n'
if [ "$FAILED" -eq 0 ]; then
  printf '\033[32m全部通过\033[0m\n'
else
  printf '\033[31m有项目失败\033[0m\n'
fi
exit "$FAILED"
