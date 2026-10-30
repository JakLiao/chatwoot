# 客户声音采集接口

上游业务系统只跟这一个接口打交道。

- 基地址：`http://<内网地址>:8080`（本机是 `http://127.0.0.1:8080`）
- 鉴权：请求头 `X-Intake-Key: <该来源系统的 API Key>`
- 内容类型：`application/json; charset=utf-8`

> 网关只监听回环。上游服务请走内网地址，**不要**把端口暴露到公网。

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/intake/v1/feedback` | 提交一条客户声音 |
| `GET` | `/intake/v1/sources` | 列出你有权提交的来源系统 |
| `GET` | `/healthz` | 健康检查（不需要鉴权） |

## 提交客户声音

### 请求

```http
POST /intake/v1/feedback
Content-Type: application/json
X-Intake-Key: ci_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

```json
{
  "source_system": "company_xianxing",
  "request_id": "fb-20261028-0001",
  "customer": {
    "identifier": "user-10086",
    "name": "张三",
    "company": "某某公司",
    "city": "杭州",
    "extra": { "vip_level": "gold" }
  },
  "feedback": {
    "content": "导出报表经常卡住，麻烦优化一下。",
    "title": "导出卡住",
    "feedback_type": "complaint",
    "sentiment": "negative",
    "rating": 2,
    "channel": "app",
    "occurred_at": "2026-10-28T10:12:00+08:00",
    "extra": { "reproduce_steps": "选了 30 天区间就卡" }
  },
  "metadata": {
    "page": "/report/export",
    "app_version": "3.2.1",
    "trace": { "id": "abc-123" }
  }
}
```

### 不采集邮箱与电话

**客户身份信息（邮箱、手机号等）统一由 Casdoor 管理，采集接口一律不收，Chatwoot 侧也不落库。**

- `customer` 里**没有** `email` / `phone_number` 字段。
- 传了会被**明确拒绝**（422，错误信息里会指向 Casdoor），而不是被静默忽略 —— 免得上游
  以为「提交了就已经收下了」。
- `customer.extra` 里若藏了 `email` / `phone` / `mobile` 之类的键，会被**丢弃**并打一条
  告警日志。
- 需要联系客户、去查用户资料，请去 Casdoor。

> `customer.identifier` 建议填 **Casdoor 里的用户 id（或用户名）**，这样两个系统能对上，
> 客服在 Chatwoot 里看到反馈时，直接拿 identifier 去 Casdoor 查人即可。

### 字段说明

| 字段 | 必填 | 说明 |
|---|---|---|
| `source_system` | ✅ | 来源系统 id，必须在网关注册表里登记，且与 API Key 绑定的来源一致 |
| `request_id` | 推荐 | 上游请求唯一 ID，**幂等键**。同一个 `request_id` 重复提交只会产生一条会话。**建议用「内容指纹」**（见下方「幂等规则」） |
| `customer.identifier` | ✅ | 客户唯一标识。**建议填 Casdoor 里的用户 id（`sub`，UUID）**（`yujingsi` 组织下），网关会用它回查 Casdoor。填用户名也能查，但用户名会变，不适合当身份键。网关会自动加来源前缀再写进 Chatwoot，所以不同系统的同名客户不会互相覆盖 |
| `customer.name` | | 客户昵称/姓名（**不是**身份凭证，只是展示用）。不传时网关会从 Casdoor 取展示名补上 |
| `customer.company` / `city` | | 公司/组织、城市 |
| `customer.extra` | | 其余客户属性，拍平后带进联系人 `additional_attributes`（身份字段会被丢弃） |
| `feedback.content` | ✅ | 客户原话。会作为一条 `incoming` 消息的正文 |
| `feedback.title` | | 一句话标题，带进会话 `additional_attributes` |
| `feedback.feedback_type` | | `complaint` / `suggestion` / `praise` / `question` … 会同时成为会话标签 |
| `feedback.sentiment` | | `positive` / `neutral` / `negative` |
| `feedback.rating` | | 评分（星级或 NPS） |
| `feedback.channel` | | 客户表达渠道：`app` / `web` / `phone` / `wechat` … |
| `feedback.occurred_at` | | 反馈发生时间（ISO8601）。不传就用收到时间 |
| `feedback.extra` | | 其余反馈属性，拍平后带进 `additional_attributes` |
| `metadata` | | 来源系统自定义元数据，拍平后带进会话 `additional_attributes` 的 `meta.*` 下 |

未列出的字段会被拒绝（`extra="forbid"`），多传字段请放进 `extra` / `metadata`。

### 响应

`201 Created`：

```json
{
  "request_id": "fb-20261028-0001",
  "source_system": "company_xianxing",
  "status": "created",
  "conversation_id": 12,
  "conversation_display_id": 12,
  "conversation_uuid": "9f1c...",
  "contact_id": 5,
  "inbox_id": 1,
  "source_id": "company_xianxing:fb-20261028-0001",
  "labels": ["公司显形", "complaint"]
}
```

> `conversation_id` 与 `conversation_display_id` 是同一个值 —— Chatwoot Application API
> 里的会话 id 就是账户内的 `display_id`（路由按 `find_by!(display_id:)` 解析）。
> 真正全局唯一的是 `conversation_uuid`。

`status` 为 `duplicate` 表示这次是**幂等重放**，复用了已存在的会话（HTTP 状态仍是 201，
因为第一次提交也是 201；如果想在重放时区分开，看 `status` 字段即可）。

### 错误

| HTTP | 含义 | 处理建议 |
|---|---|---|
| `401` | API Key 不认识 | 检查 `X-Intake-Key` |
| `403` | 来源系统已停用 | 联系管理员 |
| `422` | 载荷不合法 / 来源未登记 / Key 与 `source_system` 不匹配 / 传了邮箱电话 | 看 `detail` |
| `502` | 下游 Chatwoot 调用失败 | 重试，带上同样的 `request_id`（不会重复入库） |

## 幂等规则

`request_id` → 会话的 `source_id`（`company_xianxing:fb-20261028-0001`）。

**幂等是网关保证的，不是 Chatwoot 保证的。** 原因：Chatwoot 收到重复的 `source_id`
只会复用那个 contact_inbox，**照样会新建一条会话** —— 唯一索引只挡重复 contact_inbox，
挡不住重复会话。所以网关在创建之前先按 `source_id` 回查：

- 命中 → 直接复用已有会话，响应 `status = "duplicate"`
- 没命中 → 才创建
- 同一 `source_id` 的并发请求在网关内串行化，不会各建一条

同一个 `request_id` 重复提交的结果：

- 不会产生第二条会话
- 响应里 `status` 是 `duplicate`，`conversation_id` 指向原来那条
- **网络超时后重试请务必带上原样的 `request_id`**

没传 `request_id` 时网关会兜底推导：优先用 `occurred_at`，没有就用 5 分钟时间桶 ——
能挡住网络重试，但挡不住隔很久的重复提交。**正式接入请务必传 `request_id`。**

### 建议：`request_id` 用「内容指纹」

网关只认「同一个 `request_id` = 同一条会话」这条规则，**键里放什么由接入方决定**。
但键的**语义**会直接影响 Chatwoot 里的会话形态 —— 选错会出现「显形库里是最新答案、
Chatwoot 里停在第一次」的分叉。

| 键的内容 | 同内容重试 | 改答案 | 风险 |
|---|---|---|---|
| 内容指纹（**推荐**） | 幂等命中，不重复建会话 | **新建会话**（改主意也是信号） | 无 |
| 业务行 id | 幂等命中 | **被幂等吞掉** → 会话停在旧内容 | ⚠️ 两边分叉 |
| 时间戳 / 随机数 | **重复建会话** | 新建会话 | ⚠️ 重试即重复 |

> 指纹示例：`fb-<sha256(user_id|kind|scope|target_type|target_id|score|tier|content).slice(0,24)>`
> —— 含用户与内容，**不含时间戳与随机数**。

**为什么「行 id」是坑**：接入方常把反馈落库成 upsert（一人·一目标一行，改答案=覆盖），
行 id 在改答案时**不变** → 拿它当键 → 网关认为「重复」→ 复用旧会话 → 新答案永远进不去。
显形侧踩过这个坑，详见 [`feedback-flow.md`](./feedback-flow.md) §4.3 落差 ③。

## Chatwoot 侧映射

| 网关字段 | Chatwoot 位置 |
|---|---|
| `source_system` | 会话标签 + 会话属性 `source_system` + 联系人属性 `source_system` + 联系人 `additional_attributes.source_system` |
| `request_id` | 会话属性 `request_id` + `additional_attributes.intake_request_id` |
| `feedback.content` | 消息正文（`message_type=incoming`） |
| `feedback.title` / `feedback_type` / `sentiment` / `rating` / `channel` / `occurred_at` | 会话属性 + `additional_attributes.intake_*` |
| `metadata.*` | 会话 `additional_attributes.meta.*`（嵌套会拍平成 `meta.a.b`） |
| `customer.name` / `company` / `city` | 联系人姓名 + `additional_attributes`（公司、城市等）。`name` 缺失时用 Casdoor 的展示名 |
| `customer.identifier` | 联系人 `identifier`（实际写入 `<来源系统>:<原始标识>`） |
| Casdoor 查到的用户 | 联系人 `additional_attributes.casdoor_user_id` / `casdoor_org`（仅溯源用） |
| 接收时间 | `additional_attributes.intake_received_at` |
| **邮箱 / 电话** | **不采集、不落库** —— 身份以 Casdoor 为唯一源 |

会话建在专用的 **API 频道收件箱**（默认名「客户声音采集」）里 —— Chatwoot 只允许
API 频道收 `incoming` 消息，客户反馈必须走这个频道。

## curl 示例

```bash
curl -X POST http://127.0.0.1:8080/intake/v1/feedback \
  -H 'Content-Type: application/json' \
  -H "X-Intake-Key: $(cat keys/company_xianxing.key)" \
  -d '{
    "source_system": "company_xianxing",
    "request_id": "fb-20261028-0001",
    "customer": { "identifier": "user-10086", "name": "张三" },
    "feedback": {
      "content": "导出报表经常卡住，麻烦优化一下。",
      "feedback_type": "complaint",
      "sentiment": "negative",
      "occurred_at": "2026-10-28T10:12:00+08:00"
    },
    "metadata": { "page": "/report/export", "app_version": "3.2.1" }
  }'
```

## 上游接入清单

1. 找管理员要一把 Key（每个来源系统一把，`keys/<source_id>.key`）
2. 确认 `source_system` 与 Key 绑定的来源一致
3. `customer.identifier` 填 **Casdoor 的用户 id（`sub`，UUID）** —— 不要填邮箱/电话，也不要填会变的用户名
4. 每条反馈生成一个唯一 `request_id`
5. POST 到内网地址的 `/intake/v1/feedback`
6. 失败时带**原样的 `request_id`** 重试

## 与 Casdoor 的关系

| 职责 | 归属 |
|---|---|
| 用户身份（邮箱、电话、账号） | **Casdoor**（`http://casdoor.test:9201`，组织 `yujingsi`）—— 唯一真值源 |
| 客户声音内容与来源 | 本系统（Chatwoot） |
| 上游服务如何拿到当前用户 | 从 Casdoor 会话取，再把 `identifier` 传给采集接口 |

采集接口不碰身份数据；Chatwoot 侧也不冗余存一份。要看用户资料，去 Casdoor。

### 网关会拿 identifier 回查一次 Casdoor

每次提交，网关会用 `customer.identifier` 去 Casdoor 的 `yujingsi` 组织里查这个用户：

- **查到** → 用 Casdoor 的展示名补 `customer.name`（上游自己传了名就以上游为准），
  并把 `casdoor_user_id` 记进联系人的 `additional_attributes` 便于溯源。
- **查不到 / Casdoor 不可用** → 照常入库，只记一条日志。**客户声音不会因为查不到
  用户而被丢掉。**

回查只读取**展示名**和**用户 id**。Casdoor 里的邮箱、电话**永远不会被写进 Chatwoot**。

> `identifier` 填 Casdoor 的**用户名**（如 `trial_test_user`）或**用户 id（UUID）**都能查到。
> 推荐用 **UUID（`sub`）** —— 用户名会变（访客 `guest_<uuid>` → 升级注册后变真实名），
> 用它当身份键会把同一个人的反馈拆成多个 Chatwoot 联系人。
> 填其它系统的 id 查不到 —— 那时只是少了展示名补全，反馈照常入库。

完整端到端流程（含显形侧的字段映射与接入清单）见 **[feedback-flow.md](feedback-flow.md)**。
