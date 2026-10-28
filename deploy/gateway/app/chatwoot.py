"""Chatwoot Application API 客户端。

只用到「客户声音收集」真正需要的 6 个接口：

* ``POST /api/v1/accounts/{id}/contacts/filter``  —— 按 identifier 查联系人
* ``POST /api/v1/accounts/{id}/contacts``            —— 建联系人
* ``POST /api/v1/accounts/{id}/conversations``       —— 建会话（带 source_id 幂等）
* ``GET  /api/v1/accounts/{id}/conversations``       —— 按 source_id 回查会话
* ``POST /api/v1/accounts/{id}/labels``              —— 确保标签存在
* ``POST /api/v1/accounts/{id}/conversations/{cid}/labels`` —— 给会话打标签

鉴权头是 ``api_access_token``（社区版即可用，不需要企业版 license）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from .config import Settings

log = logging.getLogger("intake.chatwoot")


class ChatwootError(RuntimeError):
    """Chatwoot 返回了非 2xx，或者响应体不符合预期。"""

    def __init__(self, message: str, status_code: Optional[int] = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class ChatwootClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=settings.request_timeout,
            headers={
                "api_access_token": settings.access_token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------------- 内部
    async def _request(
        self, method: str, path: str, *, params: Optional[dict] = None, json: Optional[dict] = None
    ) -> Any:
        url = f"/api/v1/accounts/{self._settings.account_id}{path}"
        try:
            response = await self._client.request(method, url, params=params, json=json)
        except httpx.HTTPError as exc:
            raise ChatwootError(f"调用 Chatwoot 失败：{exc}") from exc

        if response.status_code >= 400:
            body = _safe_json(response)
            raise ChatwootError(
                f"Chatwoot 返回 {response.status_code}：{_short(body)}",
                status_code=response.status_code,
                payload=body,
            )

        if response.status_code == 204 or not response.content:
            return None
        return _safe_json(response)

    # -------------------------------------------------------------- 联系人
    async def find_contact_by_identifier(self, identifier: str) -> Optional[Dict[str, Any]]:
        """按 identifier 精确查联系人（identifier 在账户内唯一）。"""
        body = {
            "payload": [
                {
                    "attribute_key": "identifier",
                    "filter_operator": "equal_to",
                    "values": [identifier],
                    "query_operator": None,
                }
            ]
        }
        data = await self._request("POST", "/contacts/filter", json=body)
        contacts = _extract_list(data, ("payload",), ("contacts",), ("data", "payload"))
        return contacts[0] if contacts else None

    async def find_contact_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        body = {
            "payload": [
                {
                    "attribute_key": "email",
                    "filter_operator": "equal_to",
                    "values": [email],
                    "query_operator": None,
                }
            ]
        }
        data = await self._request("POST", "/contacts/filter", json=body)
        contacts = _extract_list(data, ("payload",), ("contacts",), ("data", "payload"))
        return contacts[0] if contacts else None

    async def create_contact(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = await self._request("POST", "/contacts", json=payload)
        if isinstance(data, dict):
            contact = data.get("payload", {}).get("contact") if isinstance(data.get("payload"), dict) else None
            if isinstance(contact, dict):
                return contact
            # 兜底：直接就是联系人对象
            if "id" in data:
                return data
        raise ChatwootError(f"创建联系人返回结构异常：{_short(data)}")

    async def update_contact(self, contact_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = await self._request("PUT", f"/contacts/{contact_id}", json=payload)
        if isinstance(data, dict):
            contact = data.get("payload", {}).get("contact") if isinstance(data.get("payload"), dict) else None
            if isinstance(contact, dict):
                return contact
            if "id" in data:
                return data
        raise ChatwootError(f"更新联系人返回结构异常：{_short(data)}")

    # ---------------------------------------------------------------- 会话
    async def create_conversation(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = await self._request("POST", "/conversations", json=payload)
        if isinstance(data, dict) and "id" in data:
            return data
        raise ChatwootError(f"创建会话返回结构异常：{_short(data)}")

    async def find_conversation_by_source_id(self, source_id: str) -> Optional[Dict[str, Any]]:
        """按 source_id 回查会话 —— source_id 在 (inbox_id, source_id) 上唯一。

        ⚠️ Chatwoot 只会复用 contact_inbox，**每次调用仍然会新建一条会话**，
        所以同一个 source_id 可能对应多条会话。这里取 id 最小（=最早）的那条，
        即首次提交产生的那条。

        注意 API 返回的 ``id`` 就是 display_id（路由按 find_by!(display_id:) 解析），
        它随创建顺序单调递增，用它排序是对的。
        """
        params = {
            "inbox_id": self._settings.inbox_id,
            "status": "all",
            "source_id": source_id,
        }
        data = await self._request("GET", "/conversations", params=params)
        conversations = _extract_list(data, ("data", "payload"), ("payload",), ("conversations",))
        if not conversations:
            return None
        return min(conversations, key=lambda item: item.get("id") or 0)

    # ---------------------------------------------------------------- 标签
    async def ensure_label(self, title: str) -> None:
        """确保标签存在。

        Chatwoot 的标签挂在 acts_as_taggable_on 的 tag 上，但 UI / 报表用的是
        ``labels`` 表 —— 所以必须先建 Label 再挂到会话上。
        """
        try:
            await self._request("POST", "/labels", json={"label": {"title": title}})
        except ChatwootError as exc:
            # 已存在（422）是可以接受的
            if exc.status_code == 422:
                log.debug("标签已存在：%s", title)
                return
            raise

    async def attach_labels(self, conversation_id: int, labels: List[str]) -> None:
        if not labels:
            return
        await self._request(
            "POST", f"/conversations/{conversation_id}/labels", json={"labels": labels}
        )


# --------------------------------------------------------------------- 工具
def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text[:500]}


def _extract_list(data: Any, *paths: tuple) -> List[Dict[str, Any]]:
    """从各种可能的响应结构里挖出列表。

    每个 path 是一串**逐层**的键，例如 ``("data", "payload")``；
    终点必须是一个 list。
    """
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for path in paths:
        cursor: Any = data
        matched = True
        for key in path:
            if isinstance(cursor, dict) and key in cursor:
                cursor = cursor[key]
            else:
                matched = False
                break
        if matched and isinstance(cursor, list):
            return [item for item in cursor if isinstance(item, dict)]
    return []


def _short(data: Any, limit: int = 400) -> str:
    text = data if isinstance(data, str) else str(data)
    return text[:limit]
