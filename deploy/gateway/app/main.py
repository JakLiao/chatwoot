"""采集网关主服务。

对外只暴露一个业务接口：

    POST /intake/v1/feedback
        Header: X-Intake-Key: <该来源系统的 API Key>
        Body:   {source_system, customer, feedback, metadata, request_id}

网关负责把这份统一载荷翻译成 Chatwoot 的调用，并**统一补齐来源系统等必要信息**：
来源系统、请求 ID、接收时间、反馈类型、情绪、评分、渠道、原始 metadata ——
上游服务不需要知道 Chatwoot 的字段映射规则。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .chatwoot import ChatwootClient, ChatwootError
from .config import Settings, SourceSystem, load_settings
from .schemas import IDENTITY_FIELDS, IntakeRequest, IntakeResponse, SourceInfo
from .security import API_KEY_HEADER, resolve_source

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("intake")

# JSONB 字符串长度上限（Chatwoot 属性限制），留一点余量
_MAX_STR = 1400
_MAX_INT = 9_999_999_999
_KEY_SAFE = re.compile(r"[^0-9A-Za-z_.-]+")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.settings = settings
    app.state.chatwoot = ChatwootClient(settings)
    log.info(
        "采集网关就绪：Chatwoot=%s account=%s inbox=%s，已登记来源：%s",
        settings.base_url,
        settings.account_id,
        settings.inbox_id,
        ", ".join(sorted(settings.sources)) or "（无）",
    )
    try:
        yield
    finally:
        await app.state.chatwoot.aclose()


app = FastAPI(
    title="Chatwoot 客户声音采集网关",
    description="上游业务系统通过内网 API 提交客户声音，网关统一落库到 Chatwoot。",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(ChatwootError)
async def _chatwoot_error_handler(_: Request, exc: ChatwootError) -> JSONResponse:
    log.error("Chatwoot 调用失败：%s", exc)
    return JSONResponse(
        status_code=502,
        content={"error": "下游 Chatwoot 调用失败", "detail": str(exc)},
    )


# --------------------------------------------------------------------- 健康
@app.get("/healthz")
async def healthz() -> Dict[str, Any]:
    settings: Settings = app.state.settings
    return {
        "status": "ok",
        "account_id": settings.account_id,
        "inbox_id": settings.inbox_id,
        "sources": [s.id for s in settings.enabled_sources()],
    }


# --------------------------------------------------------------------- 来源
@app.get("/intake/v1/sources", response_model=List[SourceInfo])
async def list_sources(
    x_intake_key: Optional[str] = Header(None, alias=API_KEY_HEADER),
) -> List[SourceInfo]:
    """列出登记在册的来源系统（需要有效接入凭证）。"""
    settings: Settings = app.state.settings
    source = _authenticate(settings, x_intake_key)
    visible = settings.enabled_sources()
    if not source.allow_any_source:
        visible = [s for s in visible if s.id == source.id]
    return [SourceInfo(**s.public_dict()) for s in visible]


# --------------------------------------------------------------------- 采集
@app.post("/intake/v1/feedback", response_model=IntakeResponse, status_code=201)
async def intake_feedback(
    request: Request,
    payload: IntakeRequest,
    x_intake_key: Optional[str] = Header(None, alias=API_KEY_HEADER),
) -> IntakeResponse:
    """提交一条客户声音。"""
    settings: Settings = app.state.settings
    client: ChatwootClient = app.state.chatwoot

    key_source = _authenticate(settings, x_intake_key)
    source = _resolve_source_for_payload(settings, key_source, payload.source_system)

    started = time.perf_counter()
    request_id = payload.request_id or _derive_request_id(source, payload)

    # ---- 1. 联系人：按 identifier upsert ------------------------------
    contact_identifier = _contact_identifier(source.id, payload.customer.identifier)
    contact = await client.find_contact_by_identifier(contact_identifier)
    contact_reused = contact is not None
    if contact is None:
        contact = await _create_contact(client, source, payload, contact_identifier, request_id)
    else:
        contact = await _refresh_contact(client, contact, source, payload, request_id)

    # ---- 2. 会话：source_id 幂等 ---------------------------------------
    source_id = _conversation_source_id(source, request_id)
    conversation_body = _build_conversation_body(
        source=source,
        payload=payload,
        contact=contact,
        source_id=source_id,
        request_id=request_id,
    )

    status = "created"
    # ⚠️ 幂等必须由网关自己做「先查后建」：
    #    Chatwoot 收到同一个 source_id 只会复用 contact_inbox，**照样会新建一条会话**，
    #    唯一索引拦不住重复会话。所以这里先按 source_id 回查，命中就复用。
    # 同一个 source_id 的并发请求用锁串行化，避免两边都查不到、各建一条。
    async with _lock_for(source_id):
        existing = await client.find_conversation_by_source_id(source_id)
        if existing is not None:
            conversation = existing
            status = "duplicate"
            log.info(
                "幂等命中，复用已有会话 source_id=%s conversation=%s",
                source_id,
                existing.get("id"),
            )
        else:
            try:
                conversation = await client.create_conversation(conversation_body)
            except ChatwootError as exc:
                if exc.status_code != 422:
                    raise
                # 并发下另一个请求刚建好 —— 回查一次
                existing = await client.find_conversation_by_source_id(source_id)
                if existing is None:
                    raise
                conversation = existing
                status = "duplicate"
                log.info("创建冲突但已存在，复用会话 source_id=%s", source_id)

    # ---- 3. 标签：来源系统 + 反馈类型 ----------------------------------
    labels = _labels_for(source, payload)
    for label in labels:
        await client.ensure_label(label)
    await client.attach_labels(conversation["id"], labels)

    elapsed_ms = (time.perf_counter() - started) * 1000
    log.info(
        "采集成功 source=%s request_id=%s conversation=%s contact=%s reused=%s %.0fms",
        source.id,
        request_id,
        conversation.get("display_id"),
        contact.get("id"),
        contact_reused,
        elapsed_ms,
    )

    return IntakeResponse(
        request_id=request_id,
        source_system=source.id,
        status=status,
        # ⚠️ Chatwoot Application API 的会话 id 就是 display_id（路由按 find_by!(display_id:) 解析），
        #    所以这里两个字段是同一个值。uuid 才是全局唯一的。
        conversation_id=conversation["id"],
        conversation_display_id=conversation.get("display_id", conversation["id"]),
        conversation_uuid=conversation.get("uuid"),
        contact_id=contact["id"],
        inbox_id=settings.inbox_id,
        source_id=source_id,
        labels=labels,
    )


# ================================================================== 内部实现
# 同一 source_id 的请求串行化，防止并发重复建会话
_source_locks: dict[str, "asyncio.Lock"] = {}


def _lock_for(source_id: str) -> "asyncio.Lock":
    lock = _source_locks.get(source_id)
    if lock is None:
        # asyncio 单线程，get 与 set 之间没有 await，这里是安全的
        lock = asyncio.Lock()
        _source_locks[source_id] = lock
    return lock


def _authenticate(settings: Settings, api_key: Optional[str]) -> SourceSystem:
    source = resolve_source(settings, api_key)
    if source is None:
        raise HTTPException(status_code=401, detail="无效的接入凭证（X-Intake-Key）")
    if not source.enabled:
        raise HTTPException(status_code=403, detail=f"来源系统 {source.id} 已停用")
    return source


def _resolve_source_for_payload(
    settings: Settings, key_source: SourceSystem, claimed_source: str
) -> SourceSystem:
    """把「凭证绑定的来源」和「载荷里声明的来源」对齐。

    默认严格：一把 Key 只能代表一个来源系统 —— 防止来源被伪造。
    注册表里给该来源加 ``allow_any_source: true`` 才可以提交任意来源。
    """
    if key_source.allow_any_source:
        target = settings.source(claimed_source)
        if target is None:
            raise HTTPException(
                status_code=422, detail=f"未登记的来源系统：{claimed_source}"
            )
        if not target.enabled:
            raise HTTPException(status_code=403, detail=f"来源系统 {target.id} 已停用")
        return target

    if claimed_source and claimed_source != key_source.id:
        if settings.source(claimed_source) is None:
            raise HTTPException(
                status_code=422, detail=f"未登记的来源系统：{claimed_source}"
            )
        raise HTTPException(
            status_code=422,
            detail=f"接入凭证与 source_system 不匹配（凭证属于 {key_source.id}）",
        )
    return key_source


def _contact_identifier(source_id: str, raw_identifier: str) -> str:
    """加来源前缀，避免不同系统的同名客户互相覆盖。"""
    return f"{source_id}:{raw_identifier}"[:200]


def _derive_request_id(source: SourceSystem, payload: IntakeRequest) -> str:
    """没有 request_id 时兜底推导一个。

    优先用「反馈发生时间」—— 同一件事重试仍然会命中同一个键；
    没有时间就退化到 5 分钟时间桶（能挡住网络重试，但挡不住隔很久的重复提交）。
    """
    feedback = payload.feedback
    if feedback.occurred_at:
        bucket = feedback.occurred_at.isoformat()
    else:
        bucket = str(int(time.time() // 300))
    digest = hashlib.sha256(
        "|".join(
            [
                source.id,
                payload.customer.identifier,
                feedback.content,
                bucket,
            ]
        ).encode("utf-8")
    ).hexdigest()
    return f"auto_{digest[:32]}"


def _conversation_source_id(source: SourceSystem, request_id: str) -> str:
    """会话的 source_id —— Chatwoot 上 (inbox_id, source_id) 唯一，天然幂等。"""
    return f"{source.id}:{request_id}"[:200]


def _labels_for(source: SourceSystem, payload: IntakeRequest) -> List[str]:
    labels = [source.label]
    feedback_type = payload.feedback.feedback_type or source.default_feedback_type
    if feedback_type:
        labels.append(_safe_label(feedback_type))
    # 去重但保持顺序
    seen: List[str] = []
    for label in labels:
        if label not in seen:
            seen.append(label)
    return seen


def _safe_label(raw: str) -> str:
    """把任意类型名洗成合法的 Chatwoot 标签标题。

    Chatwoot 的规则：\\A[\\p{L}\\p{N}]+[\\p{L}\\p{N}_-]+\\z（至少 2 字符，不能有空格）。
    """
    candidate = _KEY_SAFE.sub("_", (raw or "").strip().lower())
    candidate = candidate.strip("_")
    if len(candidate) < 2:
        candidate = f"t_{candidate}" if candidate else "t_unknown"
    return candidate[:50]


async def _create_contact(
    client: ChatwootClient,
    source: SourceSystem,
    payload: IntakeRequest,
    identifier: str,
    request_id: str,
) -> Dict[str, Any]:
    body = _contact_body(source, payload, identifier, request_id)
    try:
        return await client.create_contact(body)
    except ChatwootError as exc:
        # identifier 在账户内唯一 —— 撞车时（并发提交同一客户）回查复用
        if exc.status_code != 422:
            raise
        log.warning("联系人创建冲突（identifier=%s），改按 identifier 回查", identifier)
        existing = await client.find_contact_by_identifier(identifier)
        if existing:
            return existing
        raise


async def _refresh_contact(
    client: ChatwootClient,
    contact: Dict[str, Any],
    source: SourceSystem,
    payload: IntakeRequest,
    request_id: str,
) -> Dict[str, Any]:
    """补齐已存在联系人的信息（名字/邮箱/手机号可能被上游补充过）。"""
    updates: Dict[str, Any] = {}
    if payload.customer.name and contact.get("name") != payload.customer.name:
        updates["name"] = payload.customer.name
    # ⚠️ 不更新邮箱/电话 —— 身份信息由 Casdoor 统一管理，Chatwoot 侧不落库

    additional = dict(contact.get("additional_attributes") or {})
    extra_attrs = _sanitize_dict(payload.customer.extra, prefix="customer.", drop_identity=True)
    additional.update(extra_attrs)
    additional["source_system"] = source.id
    additional["intake_last_request_id"] = request_id
    additional["intake_last_seen_at"] = _now_iso()
    if additional != (contact.get("additional_attributes") or {}):
        updates["additional_attributes"] = additional

    if not updates:
        return contact
    try:
        return await client.update_contact(contact["id"], updates)
    except ChatwootError as exc:
        # 补资料失败不该拖垮整条反馈的入库
        log.warning("联系人 %s 更新失败：%s", contact.get("id"), exc)
        return contact


def _contact_body(
    source: SourceSystem,
    payload: IntakeRequest,
    identifier: str,
    request_id: str,
) -> Dict[str, Any]:
    customer = payload.customer
    additional: Dict[str, Any] = {
        "source_system": source.id,
        "intake_request_id": request_id,
        "intake_received_at": _now_iso(),
    }
    if customer.company:
        additional["company_name"] = customer.company
    if customer.city:
        additional["city"] = customer.city
    additional.update(_sanitize_dict(customer.extra, prefix="customer.", drop_identity=True))

    body: Dict[str, Any] = {
        "inbox_id": app.state.settings.inbox_id,
        "identifier": identifier,
        "additional_attributes": additional,
        "custom_attributes": {"source_system": source.id},
    }
    if customer.name:
        body["name"] = customer.name
    # ⚠️ 刻意不写 email / phone_number —— 身份信息由 Casdoor 统一管理
    return body


def _build_conversation_body(
    *,
    source: SourceSystem,
    payload: IntakeRequest,
    contact: Dict[str, Any],
    source_id: str,
    request_id: str,
) -> Dict[str, Any]:
    settings: Settings = app.state.settings
    feedback = payload.feedback
    received_at = _now_iso()

    additional: Dict[str, Any] = {
        "intake_source_system": source.id,
        "intake_request_id": request_id,
        "intake_received_at": received_at,
    }
    if feedback.title:
        additional["intake_title"] = feedback.title
    if feedback.feedback_type:
        additional["intake_feedback_type"] = feedback.feedback_type
    if feedback.sentiment:
        additional["intake_sentiment"] = feedback.sentiment
    if feedback.channel:
        additional["intake_channel"] = feedback.channel
    if feedback.occurred_at:
        additional["intake_occurred_at"] = feedback.occurred_at.isoformat()
    if feedback.rating is not None:
        additional["intake_rating"] = feedback.rating
    # 上游原始 metadata 拍平到 meta.* 下，不覆盖 Chatwoot 自己的键
    additional.update(_sanitize_dict(payload.metadata, prefix="meta."))
    additional.update(_sanitize_dict(feedback.extra, prefix="feedback."))

    custom: Dict[str, Any] = {
        "source_system": source.id,
        "request_id": request_id,
    }
    if feedback.feedback_type:
        custom["feedback_type"] = feedback.feedback_type
    if feedback.sentiment:
        custom["sentiment"] = feedback.sentiment
    if feedback.channel:
        custom["channel"] = feedback.channel
    if feedback.rating is not None:
        custom["rating"] = feedback.rating
    if feedback.occurred_at:
        custom["occurred_at"] = feedback.occurred_at.isoformat()

    message: Dict[str, Any] = {
        "content": feedback.content[: settings.max_content_length],
        "message_type": "incoming",
        "content_type": "text",
    }
    if feedback.occurred_at:
        message["external_created_at"] = feedback.occurred_at.isoformat()

    return {
        "source_id": source_id,
        "inbox_id": settings.inbox_id,
        "contact_id": contact["id"],
        "additional_attributes": additional,
        "custom_attributes": custom,
        "message": message,
    }


def _sanitize_dict(data: Dict[str, Any], prefix: str = "", drop_identity: bool = False) -> Dict[str, Any]:
    """把任意嵌套结构拍平成「字符串键 → 标量值」，并做长度/类型收敛。

    ``drop_identity=True`` 时会把邮箱/电话一类身份字段**丢掉** —— 身份信息归
    Casdoor 管，采集接口不该把它们带进 Chatwoot。
    """
    out: Dict[str, Any] = {}
    for raw_key, value in (data or {}).items():
        key = _KEY_SAFE.sub("_", str(raw_key)).strip("_")[:60]
        if not key:
            continue
        if drop_identity and key.lower() in IDENTITY_FIELDS:
            log.warning("丢弃身份字段 %s%s（身份信息由 Casdoor 统一管理）", prefix, raw_key)
            continue
        full_key = f"{prefix}{key}"[:120]
        out.update(_flatten_value(full_key, value))
    return out


def _flatten_value(key: str, value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for sub_key, sub_value in value.items():
            sub = _KEY_SAFE.sub("_", str(sub_key)).strip("_")[:60]
            if not sub:
                continue
            out.update(_flatten_value(f"{key}.{sub}"[:120], sub_value))
        return out
    if isinstance(value, bool):
        return {key: "true" if value else "false"}
    if isinstance(value, int):
        if abs(value) >= _MAX_INT:
            return {key: str(value)}
        return {key: value}
    if isinstance(value, float):
        return {key: value}
    if isinstance(value, (list, tuple)):
        try:
            return {key: json.dumps(value, ensure_ascii=False)[:_MAX_STR]}
        except (TypeError, ValueError):
            return {key: str(value)[:_MAX_STR]}
    if isinstance(value, datetime):
        return {key: value.isoformat()}
    return {key: str(value)[:_MAX_STR]}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
