"""接入鉴权：每个来源系统一把独立 API Key。

* Key 明文只存在于项目根 ``.env`` / ``keys/``（600）；
* 注册表里只存 sha256 摘要，容器内也读不到明文。
* 用 ``hmac.compare_digest`` 做常数时间比较，避免计时侧信道。
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Optional

from .config import Settings, SourceSystem

API_KEY_HEADER = "x-intake-key"


def sha256_hex(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def resolve_source(settings: Settings, api_key: Optional[str]) -> Optional[SourceSystem]:
    """用 API Key 找到对应的来源系统。

    返回 ``None`` 表示 Key 不认识 —— 调用方应回 401。
    """
    if not api_key:
        return None

    digest = sha256_hex(api_key.strip())
    for source in settings.sources.values():
        if not source.api_key_sha256:
            continue
        if hmac.compare_digest(source.api_key_sha256, digest):
            return source
    return None
