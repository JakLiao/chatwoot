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
  注册表里只存 API Key 的 sha256 摘要。

## 目录结构

```
.
├── docker-compose.yml            # 运维真源：rails / sidekiq / intake
├── .env                          # 真值（600）· 由 scripts/gen-env.py 生成
├── .env.example                  # 占位符模板（可进 git）
├── keys/                         # 各来源系统的接入 Key 明文（700/600）
├── gateway/                      # 采集网关
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app/
│   │   ├── main.py               # FastAPI 主服务 + 字段映射
│   │   ├── chatwoot.py           # Chatwoot Application API 客户端
│   │   ├── config.py             # 环境变量 + 来源系统注册表
│   │   ├── schemas.py            # 接口契约
│   │   └── security.py           # API Key 校验
│   └── config/
│       └── source_systems.yml    # 来源系统注册表（唯一事实源）
├── scripts/
│   ├── gen-env.py                # 生成 / 维护 .env、Key、注册表
│   ├── 00-bootstrap-db.sh        # 共享 PG 上建角色 + 库 + 扩展
│   ├── 10-migrate.sh             # 建表 + 跑迁移
│   ├── 20-bootstrap-chatwoot.sh  # 账户 / 管理员 / 令牌 / 收件箱 / 标签 / 属性
│   ├── bootstrap_chatwoot.rb     # 上面那个脚本的 Rails 实现
│   ├── cleanup_test_data.rb      # 清理自检产生的测试数据
│   ├── 40-reset-admin-password.sh # 重置管理员密码并存档
│   └── 30-verify.sh              # 端到端自检（10 项，跑完自动清理）
├── overrides/
│   └── zz_sidekiq_redis_timeout.rb  # 覆盖 Sidekiq 的 Redis 读超时（见文件头）
└── docs/
    └── intake-api.md             # 接入方看这份
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
cd ~/projects/chatwoot

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
# → 已追加到 gateway/config/source_systems.yml
docker compose restart intake
```

注册表里的 `api_key_sha256` 与 `.env` 里的明文始终保持一致 —— 两个都改了会对不上。

来源系统被停用时把注册表里的 `enabled` 改成 `false` 再重启网关即可，网关会回 403。

## 发布到 GitHub fork

本地 `~/projects/chatwoot` 是**唯一真值源**。`scripts/publish-to-fork.sh` 把它同步进
fork 的 `deploy/` 子目录，剥离密钥后提交推送：

```bash
bash scripts/publish-to-fork.sh --dry-run                 # 只同步 + 审计，不提交不推送
bash scripts/publish-to-fork.sh --branch feat/xxx         # 切/建分支后推送
bash scripts/publish-to-fork.sh --no-push                 # 只提交不推送
```

脚本会剥离 `.env`、`keys/`、注册表里的 `api_key_sha256`，并在提交前做**密钥审计**：
拿本机真值里的每个敏感串到待提交内容里全文搜，命中即中止提交。

fork 仓库：`https://github.com/JakLiao/chatwoot`（部署在 `deploy/` 子目录，上游 Chatwoot
源码原样保留；镜像仍用官方 `chatwoot/chatwoot:v4.19.1`）。

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

- 接入方看：[docs/intake-api.md](docs/intake-api.md)
- Chatwoot 官方配置文档：https://www.chatwoot.com/docs/self-hosted/configuration/environment-variables/
