#!/usr/bin/env python3
# ============================================================================
# gen-env.py · 生成 / 维护 Chatwoot 部署的真值文件
#
#   .env                        真实密钥（600，不进 git）
#   .env.example                占位符模板（可进 git）
#   keys/<source_id>.key        每个来源系统的接入 API Key 明文（600）
#   gateway/config/source_systems.yml   来源系统注册表（只存 API Key 的 sha256）
#
# 用法：
#   python3 scripts/gen-env.py                       # 首次生成（.env 已存在则跳过）
#   python3 scripts/gen-env.py --force               # 重建 .env（会换掉所有密钥！）
#   python3 scripts/gen-env.py --add-source <id> [--display-name 名称] [--type 类型]
#                                                   # 新增一个来源系统
#
# 设计要点：
#   · 真实密钥只落在 .env 与 keys/（600）；注册表里只存 sha256 摘要
#   · PostgreSQL / Valkey 都复用本机共享实例，口令从各自项目读取，不在此硬编码
#   · .env 与注册表始终保持一致（同一个 Key 两处都能对上）
# ============================================================================

import argparse
import hashlib
import json
import os
import pathlib
import re
import secrets
import string
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
EXAMPLE_PATH = ROOT / ".env.example"
KEYS_DIR = ROOT / "keys"
REGISTRY_PATH = ROOT / "gateway" / "config" / "source_systems.yml"
# 摘要覆盖文件（git 外 · 见 upsert_source 的说明）
LOCAL_REGISTRY_PATH = ROOT / "gateway" / "config" / "source_systems.local.yml"

# 本机共享实例（只读引用其真值，不复制口令）
VALKEY_ENV = pathlib.Path.home() / "valkey" / ".env"
CASDOOR_INIT = pathlib.Path.home() / "casdoor" / "init_data.json"

# Casdoor：贵司的组织名与采集用的应用（应用凭据只能读本组织下的用户）
CASDOOR_ORG = os.environ.get("CASDOOR_ORG", "yujingsi")
CASDOOR_APP = os.environ.get("CASDOOR_APP_NAME", "deepprep-trial-test")
CASDOOR_BASE_URL = os.environ.get("CASDOOR_BASE_URL", "http://casdoor.test:9201")

# 首批预置的来源系统
DEFAULT_SOURCE = {
    "id": "company_xianxing",
    "display_name": "公司显形",
    "description": "公司显形侧的客户反馈入口",
    "default_feedback_type": "app",
}

REGISTRY_HEADER = """# ============================================================================
# source_systems.yml · 客户声音采集来源系统注册表
#
# 网关只认这里登记的来源：不在表里的 source_system 会被直接拒绝（422）。
#
# 字段：
#   id                  来源系统唯一标识（snake_case）
#   display_name        显示名
#   label               挂到 Chatwoot 会话上的标签标题
#                       ⚠️ 必须满足 \\A[\\p{L}\\p{N}]+[\\p{L}\\p{N}_-]+\\z
#                          （至少 2 字符，不能有空格，不能以 _ 或 - 开头）
#   enabled             false = 停用该来源（网关回 403）
#   api_key_sha256      该来源接入 Key 的 sha256 十六进制摘要（**不要写明文**）
#   description         备注
#   default_feedback_type   上游没给 feedback_type 时用的默认值
#   allow_any_source    true = 这把 Key 可以代表任意已登记来源提交（默认 false，
#                        即「一把 Key 只能代表一个来源」，防止来源被伪造）
#
# 新增来源：python3 scripts/gen-env.py --add-source <id>
# ============================================================================
"""

EXAMPLE = """# ============================================================================
# .env.example · Chatwoot 客户声音收集系统 · 模板（可进 git）
#
# 复制成 .env 后填入真实值：
#     cp .env.example .env && chmod 600 .env
#
# 或者直接用脚本生成（会一次性生成全部随机密钥）：
#     python3 scripts/gen-env.py
#
# ⚠️ .env 里的值是真值（PostgreSQL 口令、Valkey ACL 口令、Chatwoot 访问令牌），
#    任何时候都不要提交 .env 本身。
# ============================================================================

# ---- Compose ----
COMPOSE_PROJECT_NAME=chatwoot
TZ=Asia/Shanghai

# ---- Chatwoot 应用 ----
SECRET_KEY_BASE=replace_with_128_char_alnum
ACTIVE_RECORD_ENCRYPTION_PRIMARY_KEY=replace_with_32_char_alnum
ACTIVE_RECORD_ENCRYPTION_DETERMINISTIC_KEY=replace_with_32_char_alnum
ACTIVE_RECORD_ENCRYPTION_KEY_DERIVATION_SALT=replace_with_32_char_alnum
FRONTEND_URL=http://127.0.0.1:3000
CHATWOOT_PORT=3000
RAILS_ENV=production
NODE_ENV=production
INSTALLATION_ENV=docker
RAILS_LOG_TO_STDOUT=true
LOG_LEVEL=info
RAILS_MAX_THREADS=5
RAILS_MIN_THREADS=3
WEB_CONCURRENCY=0
SIDEKIQ_CONCURRENCY=3
ENABLE_ACCOUNT_SIGNUP=false
FORCE_SSL=false
ACTIVE_STORAGE_SERVICE=local
MAILER_SENDER_EMAIL="Chatwoot Voice <voice@localhost>"
SMTP_ADDRESS=127.0.0.1
SMTP_PORT=2525
SMTP_DOMAIN=localhost
SMTP_AUTHENTICATION=
ENABLE_PUSH_RELAY_SERVER=false
ENABLE_RACK_ATTACK=false
BUNDLE_FROZEN=true
BUNDLE_TIMEOUT=15
BUNDLE_RETRY=1

# ---- PostgreSQL · 复用共享实例 ~/postgres-trial ----
POSTGRES_HOST=db
POSTGRES_PORT=5432
POSTGRES_DATABASE=chatwoot
POSTGRES_USERNAME=chatwoot
POSTGRES_PASSWORD=replace_with_pg_password

# ---- Valkey · 复用共享实例 ~/valkey ----
# db1 与 trial-web 的 db0 隔离；口令取自 ~/valkey/.env 的 VALKEY_APP_PASSWORD
REDIS_URL=redis://app:replace_with_valkey_app_password@valkey:6379/1
REDIS_PASSWORD=replace_with_valkey_app_password
REDIS_ALFRED_SIZE=5
REDIS_VELMA_SIZE=5

# ---- Casdoor · 用户身份的唯一真值源（只读查询）----
# 采集接口不收邮箱/电话；网关用 customer.identifier 去 Casdoor 查用户，只补展示名。
# 凭据取自 ~/casdoor/init_data.json 里对应应用的 clientId/clientSecret。
CASDOOR_ENABLED=true
CASDOOR_BASE_URL=http://casdoor.test:9201
CASDOOR_ORG=yujingsi
CASDOOR_APP_ID=replace_with_casdoor_client_id
CASDOOR_APP_SECRET=replace_with_casdoor_client_secret
CASDOOR_TIMEOUT=5

# ---- 采集网关 ----
INTAKE_PORT=8080
CHATWOOT_BASE_URL=http://rails:3000
CHATWOOT_ACCOUNT_ID=1
CHATWOOT_INBOX_ID=1
CHATWOOT_ACCESS_TOKEN=replace_with_chatwoot_access_token

# ---- 各来源系统的接入 API Key ----
INTAKE_KEY_COMPANY_XIANXING=replace_with_source_api_key
"""


# --------------------------------------------------------------------- 工具
def read_valkey_app_password() -> str:
    """从 ~/valkey/.env 里取 app 用户的 ACL 口令（共享 Valkey 的接入方凭据）。"""
    if not VALKEY_ENV.exists():
        sys.exit(f"找不到 {VALKEY_ENV} —— 本机 Valkey 项目没装好？")
    for line in VALKEY_ENV.read_text().splitlines():
        if line.strip().startswith("VALKEY_APP_PASSWORD="):
            return line.split("=", 1)[1].strip()
    sys.exit(f"{VALKEY_ENV} 里没有 VALKEY_APP_PASSWORD")


def rand_hex(n_bytes: int) -> str:
    return secrets.token_hex(n_bytes)

def rand_alnum(n: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def read_casdoor_app(org: str, app_name: str) -> tuple[str, str]:
    """从 ~/casdoor/init_data.json 里取应用的 clientId / clientSecret。

    只读引用属主项目的真值，不在这里硬编码。
    """
    if not CASDOOR_INIT.exists():
        return "", ""
    try:
        data = json.loads(CASDOOR_INIT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    for app in data.get("applications", []):
        if app.get("name") == app_name and app.get("organization") == org:
            return str(app.get("clientId") or ""), str(app.get("clientSecret") or "")
    return "", ""


def new_api_key() -> str:
    return "ci_" + rand_alnum(40)


def sha256_hex(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalise_label(raw: str, fallback: str) -> str:
    """和 gateway/app/config.py 保持一致：洗成合法的 Chatwoot 标签标题。"""
    candidate = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]+", "_", (raw or "").strip().lower()).strip("_")
    if len(candidate) < 2:
        candidate = re.sub(r"[^0-9A-Za-z_]+", "_", fallback.lower()).strip("_")
    if len(candidate) < 2:
        candidate = f"src_{fallback.lower()}"
    return candidate


# ----------------------------------------------------------------- 注册表
def load_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {"sources": []}
    data = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8")) or {}
    data.setdefault("sources", [])
    return data


def save_registry(data: dict) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=False, width=120
    )
    REGISTRY_PATH.write_text(REGISTRY_HEADER + "\n" + body, encoding="utf-8")
    # 注册表里没有明文密钥，644 足够（容器内以 nobody 运行）
    os.chmod(REGISTRY_PATH, 0o644)


def load_local_digests() -> dict:
    """读摘要覆盖文件（git 外 · 只有 id → api_key_sha256）。"""
    if not LOCAL_REGISTRY_PATH.exists():
        return {"sources": []}
    data = yaml.safe_load(LOCAL_REGISTRY_PATH.read_text(encoding="utf-8")) or {}
    data.setdefault("sources", [])
    return data


def save_local_digests(data: dict) -> None:
    LOCAL_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=False, width=120
    )
    header = (
        "# ============================================================================\n"
        "# source_systems.local.yml · 接入 Key 摘要覆盖文件（**不进 git**）\n"
        "#\n"
        "# 真源进了 git → 主注册表 source_systems.yml 不能带真实摘要\n"
        "# → 摘要写在这里，网关启动时叠加到主表上（见 gateway/app/config.py）\n"
        "# → 由 scripts/gen-env.py 自动维护，不要手改\n"
        "# ============================================================================\n"
    )
    LOCAL_REGISTRY_PATH.write_text(header + "\n" + body, encoding="utf-8")
    # 摘要是「验证预言机」：有它就能拿候选 Key 对答案 → 按密钥口径收权限
    os.chmod(LOCAL_REGISTRY_PATH, 0o600)


def upsert_source(entry: dict, api_key: str) -> None:
    """把来源写进注册表。

    摘要分流（2026-10-30 真源反转）：
      部署真源进了 git（chatwoot 仓的 deploy/ 子目录）→ 主注册表
      **也在 git 里** → 真实摘要不能跟着进 git，否则一次 `git add .`
      就把摘要提上去了。于是拆成两份：

        source_systems.yml         在 git   · 非敏感配置（id / 显示名 / 标签 / 开关）
        source_systems.local.yml   在 git 外 · 只有 api_key_sha256

      网关启动时先读主表，再把覆盖文件里的摘要盖上去（config.apply_local_digests）。
      运行时行为与「摘要写主表」完全一致。
    """
    digest = sha256_hex(api_key)

    # ---- 主注册表（git 内）：摘要留空占位 ----
    data = load_registry()
    sources = data["sources"]
    record = {
        "id": entry["id"],
        "display_name": entry.get("display_name") or entry["id"],
        "label": normalise_label(
            entry.get("label") or entry.get("display_name") or entry["id"], entry["id"]
        ),
        "enabled": bool(entry.get("enabled", True)),
        # 占位：真实摘要在 source_systems.local.yml（git 外）
        "api_key_sha256": "",
        "description": entry.get("description", ""),
        "default_feedback_type": entry.get("default_feedback_type", ""),
    }
    for index, existing in enumerate(sources):
        if isinstance(existing, dict) and existing.get("id") == entry["id"]:
            merged = dict(existing)
            merged.update(record)
            sources[index] = merged
            break
    else:
        sources.append(record)
    save_registry(data)

    # ---- 摘要覆盖文件（git 外）：只存 id → 摘要 ----
    local = load_local_digests()
    local_sources = [
        s for s in local["sources"] if not (isinstance(s, dict) and s.get("id") == entry["id"])
    ]
    local_sources.append({"id": entry["id"], "api_key_sha256": digest})
    local["sources"] = local_sources
    save_local_digests(local)


def env_key_name(source_id: str) -> str:
    return "INTAKE_KEY_" + re.sub(r"[^0-9A-Za-z]+", "_", source_id).upper()


def write_env_key(source_id: str, api_key: str) -> None:
    """把 Key 写进 .env（同名则替换）。"""
    if not ENV_PATH.exists():
        sys.exit(f"{ENV_PATH} 不存在 —— 先跑 python3 scripts/gen-env.py")
    lines = ENV_PATH.read_text().splitlines()
    name = env_key_name(source_id)
    replaced = False
    for index, line in enumerate(lines):
        if line.startswith(f"{name}="):
            lines[index] = f"{name}={api_key}"
            replaced = True
            break
    if not replaced:
        lines += ["", "# ---- 来源系统接入 Key ----", f"{name}={api_key}"]
    ENV_PATH.write_text("\n".join(lines) + "\n")
    os.chmod(ENV_PATH, 0o600)


def casdoor_env_values() -> dict:
    casdoor_id, casdoor_secret = read_casdoor_app(CASDOOR_ORG, CASDOOR_APP)
    return {
        "CASDOOR_ENABLED": "true" if (casdoor_id and casdoor_secret) else "false",
        "CASDOOR_BASE_URL": CASDOOR_BASE_URL,
        "CASDOOR_ORG": CASDOOR_ORG,
        "CASDOOR_APP_ID": casdoor_id,
        "CASDOOR_APP_SECRET": casdoor_secret,
        "CASDOOR_TIMEOUT": "5",
    }


def sync_casdoor_env() -> bool:
    """把 Casdoor 配置同步进**已存在**的 .env —— 只动 Casdoor 段，不碰其它值。

    `--force` 会轮换全部密钥（PG 口令、SECRET_KEY_BASE、访问令牌、各来源 Key），
    会打断正在运行的部署；只想补 Casdoor 配置时走这条路径。
    """
    if not ENV_PATH.exists():
        return False
    values = casdoor_env_values()
    if not values["CASDOOR_APP_ID"]:
        print("⚠ 没在 ~/casdoor/init_data.json 里找到应用凭据，跳过 Casdoor 同步")
        return False

    lines = ENV_PATH.read_text().splitlines()
    for key, value in values.items():
        for index, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[index] = f"{key}={value}"
                break
        else:
            lines += ["", "# ---- Casdoor · 用户身份的唯一真值源（只读查询）----", f"{key}={value}"]
    ENV_PATH.write_text("\n".join(lines) + "\n")
    os.chmod(ENV_PATH, 0o600)
    return True


# --------------------------------------------------------------------- .env
def build_env(source_keys: dict) -> str:
    pg_password = rand_hex(24)
    valkey_password = read_valkey_app_password()
    casdoor_id, casdoor_secret = read_casdoor_app(CASDOOR_ORG, CASDOOR_APP)

    lines = [
        "# ==========================================================================",
        "# .env · Chatwoot 客户声音收集系统 · 本机真值（600 · 不进 git）",
        "#",
        "# 由 scripts/gen-env.py 生成。任何改动请同步回脚本默认值，",
        "# 否则下次 --force 重建会丢掉手工调整。",
        "#",
        "# ⚠️ 本文件含明文口令：权限必须 600，不要提交到 git，不要贴进聊天记录。",
        "# ==========================================================================",
        "",
        "# ---- Compose ----",
        "COMPOSE_PROJECT_NAME=chatwoot",
        "TZ=Asia/Shanghai",
        "",
        "# ---- Chatwoot 应用 ----",
        "# 签名 Cookie 完整性密钥（纯字母数字，避免特殊字符）",
        f"SECRET_KEY_BASE={rand_alnum(128)}",
        "# Active Record 加密密钥（各 32 字节，缺了 User 模型里的 encrypts 字段会炸）",
        f"ACTIVE_RECORD_ENCRYPTION_PRIMARY_KEY={rand_alnum(32)}",
        f"ACTIVE_RECORD_ENCRYPTION_DETERMINISTIC_KEY={rand_alnum(32)}",
        f"ACTIVE_RECORD_ENCRYPTION_KEY_DERIVATION_SALT={rand_alnum(32)}",
        "# 前端访问地址（内网直连，不要填公网域名）",
        "FRONTEND_URL=http://127.0.0.1:3000",
        "CHATWOOT_PORT=3000",
        "RAILS_ENV=production",
        "NODE_ENV=production",
        "INSTALLATION_ENV=docker",
        "RAILS_LOG_TO_STDOUT=true",
        "LOG_LEVEL=info",
        "# 内存吃紧（宿主仅剩 ~2.5Gi 可用）：Puma 单进程 5 线程 + Sidekiq 3 并发",
        "RAILS_MAX_THREADS=5",
        "RAILS_MIN_THREADS=3",
        "WEB_CONCURRENCY=0",
        "SIDEKIQ_CONCURRENCY=3",
        "ENABLE_ACCOUNT_SIGNUP=false",
        "FORCE_SSL=false",
        "ACTIVE_STORAGE_SERVICE=local",
        "# 站内不发信：SMTP 指向一个没人监听的端口，邮件任务会快速失败而不是拖住队列",
        'MAILER_SENDER_EMAIL="Chatwoot Voice <voice@localhost>"',
        "SMTP_ADDRESS=127.0.0.1",
        "SMTP_PORT=2525",
        "SMTP_DOMAIN=localhost",
        "SMTP_AUTHENTICATION=",
        "ENABLE_PUSH_RELAY_SERVER=false",
        "# 采集网关是内网高频调用，关掉 Rack Attack 限流",
        "ENABLE_RACK_ATTACK=false",
        "# gem 已预装在镜像里；frozen + 超时上限 = 启动时 bundle install 不会联网卡住",
        "BUNDLE_FROZEN=true",
        "BUNDLE_TIMEOUT=15",
        "BUNDLE_RETRY=1",
        "",
        "# ---- PostgreSQL · 复用共享实例 ~/postgres-trial（容器内恒为 5432）----",
        "POSTGRES_HOST=db",
        "POSTGRES_PORT=5432",
        "POSTGRES_DATABASE=chatwoot",
        "POSTGRES_USERNAME=chatwoot",
        f"POSTGRES_PASSWORD={pg_password}",
        "",
        "# ---- Valkey · 复用共享实例 ~/valkey ----",
        "# ⚠️ ACL 用户 app 的口令必须同时出现在 URL 和 REDIS_PASSWORD：",
        "#    redis-rb / redis-client 两边都读，显式 password 会覆盖 URL 里的，填成同一个值最稳。",
        "# ⚠️ 用 db1 隔离：db0 已被 trial-web 占用。",
        f"REDIS_URL=redis://app:{valkey_password}@valkey:6379/1",
        f"REDIS_PASSWORD={valkey_password}",
        "REDIS_ALFRED_SIZE=5",
        "REDIS_VELMA_SIZE=5",
        "",
        "# ---- Casdoor · 用户身份的唯一真值源（只读查询）----",
        "# 采集接口不收邮箱/电话；网关用 customer.identifier 去这里查人，只补展示名。",
        f"CASDOOR_ENABLED={'true' if casdoor_id and casdoor_secret else 'false'}",
        f"CASDOOR_BASE_URL={CASDOOR_BASE_URL}",
        f"CASDOOR_ORG={CASDOOR_ORG}",
        f"CASDOOR_APP_ID={casdoor_id}",
        f"CASDOOR_APP_SECRET={casdoor_secret}",
        "CASDOOR_TIMEOUT=5",
        "",
        "# ---- 采集网关（intake gateway）----",
        "INTAKE_PORT=8080",
        "# 网关在 compose 内网里走服务名 rails",
        "CHATWOOT_BASE_URL=http://rails:3000",
        "# ↓↓ 由 scripts/20-bootstrap-chatwoot.sh 回填 ↓↓",
        "CHATWOOT_ACCOUNT_ID=1",
        "CHATWOOT_INBOX_ID=1",
        "CHATWOOT_ACCESS_TOKEN=__BOOTSTRAP_PENDING__",
        "",
        "# ---- 各来源系统的接入 API Key ----",
        "# ⚠️ 明文只在这里出现一次，发给对应上游系统后从本文件删除。",
    ]
    for source_id, key in source_keys.items():
        lines.append(f"{env_key_name(source_id)}={key}")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------- main
def cmd_generate(force: bool) -> None:
    if ENV_PATH.exists() and not force:
        print(f"{ENV_PATH} 已存在，跳过（要重建请加 --force）")
        if sync_casdoor_env():
            print("已同步 Casdoor 配置进 .env（其余值未动）")
    else:
        KEYS_DIR.mkdir(exist_ok=True)
        os.chmod(KEYS_DIR, 0o700)

        api_key = new_api_key()
        source_keys = {DEFAULT_SOURCE["id"]: api_key}
        (KEYS_DIR / f"{DEFAULT_SOURCE['id']}.key").write_text(api_key + "\n")
        os.chmod(KEYS_DIR / f"{DEFAULT_SOURCE['id']}.key", 0o600)

        ENV_PATH.write_text(build_env(source_keys))
        os.chmod(ENV_PATH, 0o600)
        upsert_source(DEFAULT_SOURCE, api_key)

        print(f"已生成 {ENV_PATH} (600)")
        print(f"已生成 {KEYS_DIR}/{DEFAULT_SOURCE['id']}.key (600)")
        print(f"已登记来源系统：{DEFAULT_SOURCE['id']}")

    EXAMPLE_PATH.write_text(EXAMPLE)
    print(f"已刷新 {EXAMPLE_PATH}")


def cmd_add_source(source_id: str, display_name: str, feedback_type: str, description: str) -> None:
    if not ENV_PATH.exists():
        sys.exit(f"{ENV_PATH} 不存在 —— 先跑 python3 scripts/gen-env.py")

    KEYS_DIR.mkdir(exist_ok=True)
    os.chmod(KEYS_DIR, 0o700)

    api_key = new_api_key()
    key_file = KEYS_DIR / f"{source_id}.key"
    key_file.write_text(api_key + "\n")
    os.chmod(key_file, 0o600)

    entry = {
        "id": source_id,
        "display_name": display_name or source_id,
        "description": description,
        "default_feedback_type": feedback_type,
    }
    upsert_source(entry, api_key)
    write_env_key(source_id, api_key)

    print(f"已登记来源系统：{source_id}")
    print(f"接入 Key（只此一次，请交给对应上游）：{api_key}")
    print(f"  → 已存 {key_file} (600)")
    print("  → 改完记得重启网关：docker compose restart intake")


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 Chatwoot 部署真值文件")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的 .env")
    parser.add_argument("--add-source", metavar="SOURCE_ID", help="新增一个来源系统")
    parser.add_argument("--display-name", default="", help="来源系统显示名")
    parser.add_argument("--type", default="", help="默认反馈类型（app / web ...）")
    parser.add_argument("--description", default="", help="来源系统备注")
    args = parser.parse_args()

    if args.add_source:
        cmd_add_source(
            args.add_source, args.display_name, args.type, args.description
        )
    else:
        cmd_generate(args.force)


if __name__ == "__main__":
    main()
