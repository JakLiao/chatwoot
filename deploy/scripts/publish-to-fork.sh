#!/usr/bin/env bash
# ============================================================================
# publish-to-fork.sh · 把本地部署同步到 GitHub fork 并推送
#
# 原则：**以本地为准**（本地 ~/projects/chatwoot 是唯一真值源）。
# 本脚本把本地部署复制进 fork 的 deploy/ 目录，剥离所有密钥，提交并推送。
#
# 剥离规则（硬编码，不依赖 .gitignore 兜底）：
#   · .env                        —— 含 PG 口令 / Valkey 口令 / API 令牌
#   · keys/                       —— 各来源系统接入 Key 明文 + 管理员口令
#   · gateway/config/source_systems.yml 里的 api_key_sha256 —— 改成占位符
#   · 任何其它命中密钥值的文件
#
# 同步前会做一次**密钥审计**：把本地真值里的敏感字符串拿到待提交内容里全文搜，
# 命中就中止提交。
#
# 用法：
#   bash scripts/publish-to-fork.sh                 # 同步 + 提交 + 推送当前分支
#   bash scripts/publish-to-fork.sh --dry-run       # 只同步 + 审计，不提交不推送
#   bash scripts/publish-to-fork.sh --branch feat/x # 先建/切分支再推
#   bash scripts/publish-to-fork.sh --no-push       # 只同步 + 提交，不推送
# ============================================================================

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"

FORK_DIR="${CHATWOOT_FORK_DIR:-$HOME/projects/chatwoot-fork}"
DEPLOY_SUBDIR="deploy"
# 默认分支：长期在 feat/voc-intake-deploy 上提交，不误推 develop
DEFAULT_BRANCH="${CHATWOOT_PUBLISH_BRANCH:-feat/voc-intake-deploy}"
BRANCH=""
DRY_RUN=0
DO_PUSH=1

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --no-push) DO_PUSH=0 ;;
    --branch) BRANCH="$2"; shift ;;
    *) echo "未知参数：$1" >&2; exit 1 ;;
  esac
  shift
done

[ -d "$FORK_DIR/.git" ] || { echo "fork 目录不存在或不是 git 仓库：$FORK_DIR" >&2; exit 1; }

TARGET_DIR="$FORK_DIR/$DEPLOY_SUBDIR"

# --------------------------------------------------------------------------
# 1. 复制（排除密钥）
# --------------------------------------------------------------------------
echo "==> 同步 $ROOT → $TARGET_DIR"
rm -rf "$TARGET_DIR"
mkdir -p "$TARGET_DIR"

# tar 排除：.env、keys/、__pycache__、*.pyc、.git
tar -C "$ROOT" \
    --exclude='./.env' \
    --exclude='./.env.*' \
    --exclude='./keys' \
    --exclude='./__pycache__' \
    --exclude='*.pyc' \
    --exclude='./.git' \
    --exclude='./storage' \
    -cf - . | tar -C "$TARGET_DIR" -xf -

# .env.example 是模板，要保留（上面的 .env.* 把它排除了，补回来）
if [ -f "$ROOT/.env.example" ]; then
  cp "$ROOT/.env.example" "$TARGET_DIR/.env.example"
fi

# --------------------------------------------------------------------------
# 2. 剥离注册表里的 API Key 摘要
# --------------------------------------------------------------------------
REGISTRY="$TARGET_DIR/gateway/config/source_systems.yml"
if [ -f "$REGISTRY" ]; then
  echo "==> 剥离注册表里的 api_key_sha256"
  python3 - "$REGISTRY" <<'PY'
import re, sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
text = re.sub(
    r"^(\s*api_key_sha256:\s*).*$",
    r'\1""   # 本机由 scripts/gen-env.py 填入，勿提交真实摘要',
    text,
    flags=re.M,
)
open(path, "w", encoding="utf-8").write(text)
PY
fi

# --------------------------------------------------------------------------
# 3. 密钥审计：拿本地真值里的敏感串去待提交内容里搜
# --------------------------------------------------------------------------
echo "==> 密钥审计"
SECRETS=()
[ -f "$ROOT/.env" ] && while IFS= read -r line; do
  key="${line%%=*}"
  value="${line#*=}"
  case "$key" in
    POSTGRES_PASSWORD|REDIS_PASSWORD|CHATWOOT_ACCESS_TOKEN|SECRET_KEY_BASE|\
    ACTIVE_RECORD_ENCRYPTION_PRIMARY_KEY|ACTIVE_RECORD_ENCRYPTION_DETERMINISTIC_KEY|\
    ACTIVE_RECORD_ENCRYPTION_KEY_DERIVATION_SALT|CASDOOR_APP_SECRET|INTAKE_KEY_*)
      [ -n "$value" ] && SECRETS+=("$value")
      ;;
  esac
done < "$ROOT/.env"

if [ -d "$ROOT/keys" ]; then
  while IFS= read -r f; do
    v="$(cat "$f")"
    [ -n "$v" ] && SECRETS+=("$v")
  done < <(find "$ROOT/keys" -type f)
fi

# Valkey ACL 口令在 URL 里，单独抽出来
if [ -f "$ROOT/.env" ]; then
  valkey_pw="$(sed -n 's|^REDIS_URL=redis://app:\([^@]*\)@.*|\1|p' "$ROOT/.env")"
  [ -n "$valkey_pw" ] && SECRETS+=("$valkey_pw")
fi

LEAK=0
for s in "${SECRETS[@]:-}"; do
  [ -z "$s" ] && continue
  if grep -rqF -- "$s" "$TARGET_DIR" 2>/dev/null; then
    echo "    ✘ 泄漏：待提交内容里出现了密钥值（${#s} 字符，已打码）" >&2
    LEAK=1
  fi
done
if [ "$LEAK" -ne 0 ]; then
  echo "审计未通过，已中止。" >&2
  exit 1
fi
echo "    ✔ 未发现密钥泄漏（审计了 ${#SECRETS[@]} 个敏感值）"

# 顺带确认没有误带 .env / keys
if [ -e "$TARGET_DIR/.env" ] || [ -d "$TARGET_DIR/keys" ]; then
  echo "    ✘ .env 或 keys/ 被复制进来了" >&2
  exit 1
fi

# --------------------------------------------------------------------------
# 4. 提交
# --------------------------------------------------------------------------
cd "$FORK_DIR"
BRANCH="${BRANCH:-$DEFAULT_BRANCH}"
if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  echo "==> 切到已有分支 $BRANCH"
  git checkout "$BRANCH"
else
  echo "==> 新建分支 $BRANCH"
  git checkout -b "$BRANCH"
fi

git add -A "$DEPLOY_SUBDIR"
if git diff --cached --quiet; then
  echo "==> 没有变更，无需提交"
  exit 0
fi

echo "==> 待提交的文件"
git diff --cached --name-status

if [ "$DRY_RUN" -eq 1 ]; then
  echo "==> dry-run：到此为止，不提交不推送"
  exit 0
fi

# 提交信息：PUBLISH_MESSAGE 优先，否则自动生成带变更清单的
CHANGED_COUNT="$(git diff --cached --name-only | wc -l | tr -d ' ')"
CHANGED_LIST="$(git diff --cached --name-status | sed 's/^/  /')"
if [ -n "${PUBLISH_MESSAGE:-}" ]; then
  COMMIT_SUBJECT="$PUBLISH_MESSAGE"
  COMMIT_BODY="${PUBLISH_BODY:-}

变更清单：
${CHANGED_LIST}"
else
  COMMIT_SUBJECT="chore(deploy): 同步本地部署（${CHANGED_COUNT} 个文件）"
  COMMIT_BODY="变更清单：
${CHANGED_LIST}"
fi

git commit -q -F - <<MSG
${COMMIT_SUBJECT}

${COMMIT_BODY}
MSG

echo "==> 已提交：$(git log -1 --format='%h %s')"

if [ "$DO_PUSH" -eq 1 ]; then
  echo "==> 推送到 origin $(git rev-parse --abbrev-ref HEAD)"
  git push -u origin "$(git rev-parse --abbrev-ref HEAD)"
  echo "==> 推送完成"
else
  echo "==> --no-push：已提交未推送"
fi
