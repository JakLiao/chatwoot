# Chatwoot 客户声音收集系统

统一的客户声音（VOC）收集底座。上游业务系统（公司显形等）通过**内网 API** 把客户
反馈提交进来，网关统一补齐「来源系统」等必要信息后落库到 Chatwoot；客服/产品在
Chatwoot 里按来源、类型、情绪做筛选和跟进。

```
上游业务系统                     本项目                        共享基础设施
─────────────            ────────────────────         ──────────────────────
公司显形  ─┐
官网/APP  ─┼─HTTP(内网)─▶  intake 网关 :8080 ─┐
其它服务  ─┘               (来源系统注册表)     ├─▶  rails    :3000 ─┐
                                                │                  ├─▶ PostgreSQL
                                                │                  │   (~/postgres-trial)
                                                ├─▶ sidekiq ───────┘
                                                │                    └─▶ Valkey
                                                │                       (~/valkey)
                                                └─▶ Casdoor（只读查用户）
                                                    (~/casdoor · yujingsi 组织)
```

> **2026-10-30 真源反转**：本目录（chatwoot 仓的 `deploy/` 子目录）就是**部署真源**，
> 直接在 git 里改。运行时的真值（`.env` / `keys/`）也放在本目录内，由 `.gitignore` 排除。
> 详见 [真源与提交](#真源与提交)。

## 边界

- **不对公网暴露**：所有发布端口只绑 `127.0.0.1`；不改 cloudflared 隧道、不加 ufw 放行。
  上游服务通过**内网地址**访问网关。
- **不自带 PG / Redis**：复用本机共享实例，遵循 `~/postgres-trial`、`~/valkey`、
  `~/casdoor` 的「周边组件独立托管、业务项目只接入」范式。
- **不采集身份信息**：客户邮箱、电话等统一由 **Casdoor** 管理 —— 采集接口不收这些字段，
  Chatwoot 侧也不落库。上游只需传 `customer.identifier`（建议用 Casdoor 的用户名）。
  网关会用它回查 Casdoor 补全展示名，并把 `casdoor_user_id` 记进联系人属性便于溯源；
  **查不到或 Casdoor 不可用都不会阻断采集**。
- **密钥不进 git**：真值在 `.env`（600）与 `keys/`（700 目录 / 600 文件）；
  接入 Key 的 sha256 摘要在 `gateway/config/source_systems.local.yml`（600）。
  **提交前跑 `scripts/audit-secrets.sh`** —— 改动直接进 git 之后，这道门禁是唯一的兜底。

## 目录结构

```
.
├── docker-compose.yml            # 运维真源：rails / sidekiq / intake
├── .env                          # 真值（600）· 由 scripts/gen-env.py 生成 · 不进 git
├── .env.example                  # 占位符模板（可进 git）
├── keys/                         # 各来源系统的接入 Key 明文（700/600）· 不进 git
├── gateway/                      # 采集网关
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app/
│   │   ├── main.py               # FastAPI 主服务 + 字段映射
│   │   ├── chatwoot.py           # Chatwoot Application API 客户端
│   │   ├── config.py             # 环境变量 + 来源系统注册表（含摘要 overlay）
│   │   ├── schemas.py            # 接口契约
│   │   └── security.py           # API Key 校验
│   └── config/
│       ├── source_systems.yml    # 来源系统注册表（git 内 · 摘要留占位符）
│       └── source_systems.local.yml  # 接入 Key 摘要（git 外 · 网关启动时叠加）
├── scripts/
│   ├── gen-env.py                # 生成 / 维护 .env、Key、注册表与摘要
│   ├── 00-bootstrap-db.sh        # 共享 PG 上建角色 + 库 + 扩展
│   ├── 10-migrate.sh             # 建表 + 跑迁移
│   ├── 20-bootstrap-chatwoot.sh  # 账户 / 管理员 / 令牌 / 收件箱 / 标签 / 属性
│   ├── bootstrap_chatwoot.rb     # 上面那个脚本的 Rails 实现
│   ├── cleanup_test_data.rb      # 清理自检产生的测试数据
│   ├── 40-reset-admin-password.sh # 重置管理员密码并存档
│   ├── 30-verify.sh              # 端到端自检（10 项，跑完自动清理）
│   └── audit-secrets.sh          # 提交前密钥审计（改动直接进 git 后的唯一兜底）
├── overrides/
│   └── zz_sidekiq_redis_timeout.rb  # 覆盖 Sidekiq 的 Redis 读超时（见文件头）
└── docs/
    ├── intake-api.md             # 接入方看这份（接口契约）
    └── feedback-flow.md          # 端到端流程（显形侧字段映射 + Casdoor 回查）
```

## 管理后台

客服/产品在 Chatwoot 里按来源、类型、情绪筛选反馈。

- 地址：`http://127.0.0.1:3000`（只绑回环，本机访问）
- 账号：`voice-admin@chatwoot.local`
- 密码：`keys/admin-password.txt`（600）

口令丢了或要换人，跑：

```bash
bash scripts/40-reset-admin-password.sh
```

> ⚠️ 登录接口的参数格式和 devise_token_auth 的默认不一样 —— Chatwoot v4.19.1 的
> `DeviseOverrides::SessionsController` 读的是**顶层** `email` / `password`。
> 嵌套成 `{"user": {...}}` 会返回 401：
>
> ```bash
> # ✅ 正确
> curl -X POST http://127.0.0.1:3000/auth/sign_in \
>   -H 'Content-Type: application/json' \
>   -d '{"email":"voice-admin@chatwoot.local","password":"..."}'
>
> # ❌ 401 Invalid login credentials
> -d '{"user":{"email":"...","password":"..."}}'
> ```

## 首次部署

```bash
cd ~/projects/chatwoot-fork/deploy

# 1. 生成密钥与来源注册表（会建 .env / keys/company_xianxing.key）
python3 scripts/gen-env.py

# 2. 在共享 PostgreSQL 上建库建角色
bash scripts/00-bootstrap-db.sh

# 3. 起 rails + sidekiq，跑迁移
docker compose up -d rails sidekiq
bash scripts/10-migrate.sh

# 4. 建账户 / 管理员 / 访问令牌 / API 收件箱 / 标签 / 自定义属性
bash scripts/20-bootstrap-chatwoot.sh

# 5. 起采集网关
docker compose up -d --build intake

# 6. 端到端自检
bash scripts/30-verify.sh
```

## 日常运维

```bash
docker compose ps                      # 状态
docker compose logs -f intake          # 网关日志
docker compose logs --tail=100 rails   # 主服务日志
docker compose restart rails           # 重启
docker compose down                    # 停（保留数据卷）
```

## 新增一个来源系统

```bash
python3 scripts/gen-env.py --add-source website --display-name 官网 --type web
# → 打印接入 Key（只此一次），已写入 keys/website.key 与 .env
# → 已追加到 gateway/config/source_systems.yml（摘要留占位符）
# → 真实摘要写入 gateway/config/source_systems.local.yml（git 外）
docker compose restart intake
```

注册表里的来源 id 与标签等是**配置**（在 git 里），而
`source_systems.local.yml` 里的 `api_key_sha256` 是**真值**（不在 git 里）——
网关启动时把后者盖到前者上，两边始终一致（`gen-env.py` 一次写两个文件）。

来源系统被停用时把注册表里的 `enabled` 改成 `false` 再重启网关即可，网关会回 403。

## 真源与提交

**本目录（chatwoot 仓的 `deploy/` 子目录）就是部署真源，直接在 git 里改。**

> 2026-10-30 之前，真源在仓外的 `~/projects/chatwoot`（不在 git），
> 改完用 `publish-to-fork.sh` **单向发布**进本目录 —— 那条路已经退役。
> 单向发布意味着「fork 里手改会被下次发布覆盖」「本机改了不进版本控制直到 publish」，
> 两个方向都会漂。**现在只有这一个真源。**

### 改完之后

```bash
cd ~/projects/chatwoot-fork/deploy

# 1. 密钥审计（必跑 —— 唯一兜底）
bash scripts/audit-secrets.sh

# 2. 正常提交（git commit / push 由主理人执行）
git status
```

### 真值放哪

| 文件 | 内容 | 进 git？ |
|---|---|---|
| `.env` | PG / Valkey / Chatwoot 令牌 / Casdoor 应用凭据 | ❌（`.gitignore`） |
| `keys/*.key` | 各来源系统接入 Key 明文 | ❌ |
| `keys/admin-password.txt` | Chatwoot 管理员口令 | ❌ |
| `gateway/config/source_systems.yml` | 来源系统配置（摘要留占位符） | ✅ |
| `gateway/config/source_systems.local.yml` | 接入 Key 的 sha256 摘要 | ❌ |

> ⚠️ **为什么摘要也要拆出去**：摘要不是明文，但它是「验证预言机」——
> 拿到它就能拿候选 Key 对答案。所以它跟 `.env` 同一档：**不进 git**。
> 网关启动时先读主注册表，再把 overlay 里的摘要盖上去（`config.apply_local_digests`），
> 运行时行为与「摘要写主表」完全一致。

### 换机器 / 换目录

compose 项目名固定为 `chatwoot`（compose 文件里 `name: chatwoot`），换目录不换项目。
但 **bind mount 的宿主路径会变** → 换过目录后要重建一次容器：

```bash
cd <新的真源目录>
docker compose up -d --build intake
docker compose up -d rails sidekiq
```

## 资源约束

宿主 14Gi 内存只剩约 2.5Gi 可用、swap 已用满。所以：

- Puma 单进程 5 线程（`WEB_CONCURRENCY=0` / `RAILS_MAX_THREADS=5`）
- Sidekiq 3 并发（`SIDEKIQ_CONCURRENCY=3`）
- 每个服务都设了 `mem_limit`，某个进程泄漏时被 cgroup 掐掉，而不是把整机拖垮

## 已知问题与说明

**Sidekiq 的 Redis 超时被覆盖了。** Chatwoot 的 `lib/redis/config.rb` 把 Redis 读超时
硬编码成 1 秒，比 Sidekiq 7 阻塞取任务等待的 2 秒还短 —— 队列空闲时每隔几秒就会刷一条
`ERROR Error fetching job: Waited 3 seconds`。功能不受影响（任务照常处理、队列不积压），
但红色日志会淹没真正的错误。`overrides/zz_sidekiq_redis_timeout.rb` 通过挂载到
`/app/config/initializers/` 把它放宽到 10 秒。这个改法改不到 `.env` 里
（redis-client 不解析 URL 的 query 参数），只能挂初始化器。去掉该挂载即可回到上游默认。

**pip 源用了阿里云镜像。** 实测 `files.pythonhosted.org` 只有 ~23KB/s，阿里云 ~530KB/s，
差 20 多倍。构建时可用 `--build-arg PIP_INDEX_URL=https://pypi.org/simple/` 覆盖。

## 与共享实例的关系

| 组件 | 归属 | 本项目怎么用 |
|---|---|---|
| PostgreSQL 16 + pgvector | `~/postgres-trial`（容器 `trial-web-db`） | 库 `chatwoot` + 角色 `chatwoot`，网络 `postgres-trial-net`，主机名 `db`，容器内端口恒为 5432 |
| Valkey 7.2 | `~/valkey`（容器 `valkey`） | ACL 用户 `app`，**db1**（db0 已被 trial-web 占用），网络 `valkey-net`，主机名 `valkey` |
| Casdoor | `~/casdoor`（容器 `casdoor`） | 组织 `yujingsi`，网络 `casdoor-net`，主机名 `casdoor.test:9201`，**只读**查用户 |
| Chatwoot | 本项目 | 库 `chatwoot`，API 收件箱 id=1，账户 id=1 |

### Casdoor 凭据

网关用 Casdoor 的**应用凭据**（clientId / clientSecret）只读查用户。当前复用
`~/casdoor/init_data.json` 里 `deepprep-trial-test` 应用的凭据 —— 它本来就在
`yujingsi` 组织下，能读到该组织的用户。

⚠️ 这意味着采集网关和 trial-web 共用同一个应用密钥：将来若要独立轮换，在 Casdoor 里
新建一个同组织的应用，然后把 `CASDOOR_APP_ID` / `CASDOOR_APP_SECRET` 改成新值即可，
代码不用动。凭据由 `scripts/gen-env.py` 从 `~/casdoor/init_data.json` 读取后写入
`.env`（不进 git）。

## 关联

- 接入方看：[docs/intake-api.md](docs/intake-api.md)（接口契约）
- 想知道「第三方 App 提交什么、怎么从 Casdoor 查到人」：[docs/feedback-flow.md](docs/feedback-flow.md)
- Chatwoot 官方配置文档：https://www.chatwoot.com/docs/self-hosted/configuration/environment-variables/
