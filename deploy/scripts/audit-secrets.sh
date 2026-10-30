#!/usr/bin/env bash
# ============================================================================
# audit-secrets.sh · 提交前密钥审计（deploy/ 目录）
#
# 存在理由（2026-10-30 真源反转后）：
#   此前部署真源在仓外的 ~/projects/chatwoot，改完要用 publish-to-fork.sh
#   单向发布进 fork —— 那时「密钥不进 git」靠的是发布脚本剥离 + 审计。
#   现在真源**就在 git 里**（chatwoot 仓的 deploy/ 子目录），
#   改动直接手改、直接提交 → 审计必须变成**提交前的常驻自检**，
#   否则一次 `git add .` 就能把 .env / keys/ 里的真值提上去。
#
# 本脚本做两件事：
#   ① 从 .env 与 keys/ 取出所有敏感值，逐个到**待提交内容**里全文搜
#   ② 确认 .env / keys/ 没有被 git 跟踪（.gitignore 生效）
#
# 命中即**非零退出** —— 提交流程应把它当门禁（pre-commit hook / CI）。
#
# 用法：
#   bash scripts/audit-secrets.sh                # 审计工作区 + 暂存区
#   bash scripts/audit-secrets.sh --staged       # 只审计暂存区（提交前用）
#
# 退出码：0 = 干净 · 1 = 发现泄漏
# ============================================================================

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"

STAGED_ONLY=0
[ "${1:-}" = "--staged" ] && STAGED_ONLY=1

cd "$ROOT"

# --------------------------------------------------------------------------
# 1. 收集敏感值
# --------------------------------------------------------------------------
SECRETS=()
if [ -f ".env" ]; then
  while IFS= read -r line; do
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      POSTGRES_PASSWORD|REDIS_PASSWORD|CHATWOOT_ACCESS_TOKEN|SECRET_KEY_BASE|\
      ACTIVE_RECORD_ENCRYPTION_PRIMARY_KEY|ACTIVE_RECORD_ENCRYPTION_DETERMINISTIC_KEY|\
      ACTIVE_RECORD_ENCRYPTION_KEY_DERIVATION_SALT|CASDOOR_APP_SECRET|INTAKE_KEY_*)
        [ -n "$value" ] && SECRETS+=("$value")
        ;;
    esac
  done < ".env"

  # Valkey ACL 口令在 URL 里，单独抽出来
  valkey_pw="$(sed -n 's|^REDIS_URL=redis://app:\([^@]*\)@.*|\1|p' ".env" || true)"
  [ -n "$valkey_pw" ] && SECRETS+=("$valkey_pw")
fi

if [ -d "keys" ]; then
  while IFS= read -r f; do
    v="$(cat "$f")"
    [ -n "$v" ] && SECRETS+=("$v")
  done < <(find "keys" -type f)
fi

# --------------------------------------------------------------------------
# 2. 取待审计的内容
# --------------------------------------------------------------------------
# 审计对象 = git 跟踪的文件（排除 .env / keys/ 本身 —— 它们的真值就在那里）
if [ "$STAGED_ONLY" -eq 1 ]; then
  mapfile -t FILES < <(git diff --cached --name-only --diff-filter=ACMR | grep -v '^\.env$' | grep -v '^keys/' || true)
else
  mapfile -t FILES < <(git ls-files | grep -v '^\.env$' | grep -v '^keys/' || true)
fi

if [ "${#FILES[@]}" -eq 0 ]; then
  echo "✔ 没有待审计的文件"
else
  LEAK=0
  for s in "${SECRETS[@]:-}"; do
    [ -z "$s" ] && continue
    # 只在待审计文件里搜，不扫整个工作区（工作区里有 .env 本身是正常的）
    for f in "${FILES[@]}"; do
      [ -f "$f" ] || continue
      if grep -qF -- "$s" "$f" 2>/dev/null; then
        echo "    ✘ 泄漏：$f 里出现了密钥值（${#s} 字符，已打码）" >&2
        LEAK=1
      fi
    done
  done
  if [ "$LEAK" -ne 0 ]; then
    echo "审计未通过，已中止。" >&2
    exit 1
  fi
  echo "    ✔ 未发现密钥泄漏（审计了 ${#SECRETS[@]} 个敏感值 · ${#FILES[@]} 个文件）"
fi

# --------------------------------------------------------------------------
# 3. 真值文件不能被 git 跟踪
# --------------------------------------------------------------------------
TRACKED="$(git ls-files | grep -E '^\.env$|^keys/' || true)"
if [ -n "$TRACKED" ]; then
  echo "✘ 真值文件被 git 跟踪了：" >&2
  echo "$TRACKED" >&2
  exit 1
fi
echo "    ✔ .env / keys/ 未被 git 跟踪"
