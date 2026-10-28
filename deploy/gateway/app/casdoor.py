"""Casdoor 客户端 —— 用户身份的唯一真值源。

职责边界（重要）：
    * 采集接口**不接收**邮箱/电话，这些字段由 Casdoor 管理。
    * 本客户端只做**只读查询**：拿来源系统传来的客户标识去 Casdoor 查人，
      用来补全展示名（`name`）并记录 Casdoor 用户 id 便于溯源。
    * **绝不**把 Casdoor 的 email / phone 写进 Chatwoot —— 身份信息只在 Casdoor 存一份。

两种标识都能查
--------------
上游（显形等）手里通常有两个标识：

    * ``sub`` / user_id —— Casdoor 用户主键（UUID），**全局唯一且稳定**
    * ``name`` / username —— Casdoor 用户名，**可能变**（访客是 ``guest_<uuid>``，
      升级注册后会变成真实用户名）

对应到本客户端：

    * UUID  → 走「组织用户列表」按 id 匹配（Casdoor 的 ``get-user`` 只认
      ``组织/名称`` 形式，没有按 id 查的接口）→ 带 TTL 缓存，避免每次都全量扫
    * 用户名 → 直接 ``get-user?id=组织/名称``，O(1)

**建议上游传 UUID（sub）**：用户名会变，用它当身份键会把同一个人的反馈拆成
多个 Chatwoot 联系人。

鉴权：Casdoor 的应用凭据（clientId / clientSecret）走 HTTP Basic，
只能读所属组织下的用户，不需要管理员会话。

查不到用户、或 Casdoor 暂时不可用时**一律不阻断**采集 —— 客户声音比用户名重要。
"""

from __future__ import annotations

import base64
import logging
import re
import time
from typing import Any, Dict, Optional
from urllib.parse import quote

import httpx

log = logging.getLogger("intake.casdoor")

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def looks_like_uuid(value: str) -> bool:
    return bool(_UUID_RE.match((value or "").strip()))


class CasdoorClient:
    def __init__(
        self,
        base_url: str,
        org: str,
        app_id: str,
        app_secret: str,
        timeout: float = 5.0,
        user_cache_ttl: float = 300.0,
        page_size: int = 200,
        max_pages: int = 50,
    ):
        self._base_url = base_url.rstrip("/")
        self._org = org
        self._auth = base64.b64encode(f"{app_id}:{app_secret}".encode("utf-8")).decode("ascii")
        self._timeout = timeout
        self._page_size = page_size
        self._max_pages = max_pages
        self._cache_ttl = user_cache_ttl
        self._user_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_expire_at: float = 0.0
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f"Basic {self._auth}",
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_user(self, identifier: str) -> Optional[Dict[str, Any]]:
        """按标识查组织下的用户。查不到 / 出错返回 None（不抛异常）。"""
        identifier = (identifier or "").strip()
        if not identifier:
            return None

        # 快路径：Casdoor 的 get-user 只认「组织/名称」
        user = await self._get_user_by_name(identifier)
        if user:
            return user

        # UUID（Casdoor sub）→ 走组织用户列表匹配
        if looks_like_uuid(identifier):
            return await self._get_user_by_id(identifier)

        log.info("Casdoor 里没有用户 %s/%s（不影响采集）", self._org, identifier)
        return None

    # ------------------------------------------------------------------ 内部
    async def _get_user_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        url = f"{self._base_url}/api/get-user"
        params = {"id": f"{self._org}/{quote(name, safe='')}"}
        try:
            response = await self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            log.warning("Casdoor 查询失败（不影响采集）：%s", exc)
            return None

        if response.status_code != 200:
            log.warning("Casdoor 返回 HTTP %s（不影响采集）", response.status_code)
            return None

        try:
            payload = response.json()
        except ValueError:
            log.warning("Casdoor 返回的不是 JSON（不影响采集）")
            return None

        if not isinstance(payload, dict) or payload.get("status") != "ok":
            return None

        user = payload.get("data")
        # status=ok 但 data 为 null —— 说明没有这个用户
        return user if isinstance(user, dict) else None

    async def _get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        users = await self._org_users()
        return users.get(user_id)

    async def _org_users(self) -> Dict[str, Dict[str, Any]]:
        """拉取组织下的用户（带 TTL 缓存），返回 {用户 id: 用户记录}。

        Casdoor 没有「按 id 查单个用户」的接口，只能分页扫全量 —— 所以必须缓存，
        否则每次提交反馈都要扫一遍组织。
        """
        now = time.monotonic()
        if now < self._cache_expire_at:
            return self._user_cache

        url = f"{self._base_url}/api/get-users"
        collected: Dict[str, Dict[str, Any]] = {}
        page = 1
        started = time.monotonic()
        while page <= self._max_pages:
            params = {"owner": self._org, "p": page, "pageSize": self._page_size}
            try:
                response = await self._client.get(url, params=params)
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("Casdoor 用户列表拉取失败（不影响采集）：%s", exc)
                break

            if not isinstance(payload, dict) or payload.get("status") != "ok":
                break
            batch = payload.get("data") or []
            if not isinstance(batch, list) or not batch:
                break

            for user in batch:
                if isinstance(user, dict) and user.get("id"):
                    collected[str(user["id"])] = user

            if len(batch) < self._page_size:
                break
            page += 1

        self._user_cache = collected
        self._cache_expire_at = now + self._cache_ttl
        log.info(
            "Casdoor 组织 %s 用户列表已刷新：%d 人（耗时 %.1fs，缓存 %ds）",
            self._org,
            len(collected),
            time.monotonic() - started,
            self._cache_ttl,
        )
        return collected


def display_name_of(user: Dict[str, Any]) -> Optional[str]:
    """从 Casdoor 用户记录里取展示名（只用非身份字段）。"""
    for key in ("displayName", "name", "nickname", "preferredUsername"):
        value = user.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def user_id_of(user: Dict[str, Any]) -> Optional[str]:
    """Casdoor 用户的全局唯一 id（UUID）—— 只用于溯源，不是身份信息。"""
    value = user.get("id")
    return value if isinstance(value, str) and value.strip() else None
