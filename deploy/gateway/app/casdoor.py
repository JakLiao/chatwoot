"""Casdoor 客户端 —— 用户身份的唯一真值源。

职责边界（重要）：
    * 采集接口**不接收**邮箱/电话，这些字段由 Casdoor 管理。
    * 本客户端只做**只读查询**：拿来源系统传来的客户标识去 Casdoor 查人，
      用来补全展示名（`name`）并记录 Casdoor 用户 id 便于溯源。
    * **绝不**把 Casdoor 的 email / phone 写进 Chatwoot —— 身份信息只在 Casdoor 存一份。

鉴权：Casdoor 的应用凭据（clientId / clientSecret）走 HTTP Basic，
只能读所属组织下的用户，不需要管理员会话。

查不到用户、或 Casdoor 暂时不可用时**一律不阻断**采集 —— 客户声音比用户名重要。
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict, Optional
from urllib.parse import quote

import httpx

log = logging.getLogger("intake.casdoor")


class CasdoorClient:
    def __init__(
        self,
        base_url: str,
        org: str,
        app_id: str,
        app_secret: str,
        timeout: float = 5.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._org = org
        self._auth = base64.b64encode(f"{app_id}:{app_secret}".encode("utf-8")).decode("ascii")
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f"Basic {self._auth}",
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        """按用户名查组织下的用户。查不到 / 出错返回 None（不抛异常）。"""
        if not username:
            return None
        url = f"{self._base_url}/api/get-user"
        params = {"id": f"{self._org}/{quote(username, safe='')}"}
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
            # 用户不存在是最常见情况 —— 记 info 就行，别刷 warning
            log.info(
                "Casdoor 里没有用户 %s/%s（不影响采集）", self._org, username
            )
            return None

        user = payload.get("data")
        return user if isinstance(user, dict) else None


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
