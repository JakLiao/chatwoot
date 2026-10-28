"""采集网关配置：环境变量 + 来源系统注册表。

设计要点
--------
* 来源系统注册表（``source_systems.yml``）是**唯一**的事实源：来源 id、显示名、
  Chatwoot 标签、是否启用、API Key 的 sha256 摘要都在里面。
* 注册表里只存 API Key 的 **sha256 摘要**，不存明文 —— 明文只存在于项目根的
  ``.env`` 与 ``keys/`` 目录（600）。
* 新增来源系统不需要改代码：改注册表 → 重启网关。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

# Chatwoot 的 Label 标题正则（lib/regex_helper.rb）：
#   \A[\p{L}\p{N}]+[\p{L}\p{N}_-]+\z
#   → 至少 2 个字符，不能以 _ 或 - 开头，不能有空格
_LABEL_SAFE = re.compile(r"^[^\W_][\w一-鿿-]*$", re.UNICODE)


class ConfigError(RuntimeError):
    """配置不合法（注册表写坏了 / 必填环境变量缺失）。"""


@dataclass(frozen=True)
class SourceSystem:
    """一个上游来源系统。"""

    id: str
    display_name: str
    label: str
    enabled: bool = True
    api_key_sha256: str = ""
    description: str = ""
    default_feedback_type: str = ""
    allow_any_source: bool = False
    extra: Dict[str, str] = field(default_factory=dict)

    def public_dict(self) -> dict:
        """对外暴露的字段 —— 绝不含摘要、密钥。"""
        return {
            "id": self.id,
            "display_name": self.display_name,
            "label": self.label,
            "enabled": self.enabled,
            "description": self.description,
            "default_feedback_type": self.default_feedback_type,
        }


@dataclass
class Settings:
    base_url: str
    account_id: int
    inbox_id: int
    access_token: str
    registry_path: Path
    request_timeout: float
    max_content_length: int
    sources: Dict[str, SourceSystem]
    # Casdoor —— 用户身份的唯一真值源（只读查询；查不到也不阻断采集）
    casdoor_enabled: bool = False
    casdoor_base_url: str = ""
    casdoor_org: str = ""
    casdoor_app_id: str = ""
    casdoor_app_secret: str = ""
    casdoor_timeout: float = 5.0
    casdoor_user_cache_ttl: float = 300.0

    def source(self, source_id: str) -> Optional[SourceSystem]:
        return self.sources.get(source_id)

    def enabled_sources(self) -> List[SourceSystem]:
        return [s for s in self.sources.values() if s.enabled]

    @property
    def casdoor_configured(self) -> bool:
        """Casdoor 是否可用（开了开关且四个参数齐了）。"""
        return bool(
            self.casdoor_enabled
            and self.casdoor_base_url
            and self.casdoor_org
            and self.casdoor_app_id
            and self.casdoor_app_secret
        )


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw if raw else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是整数，当前值 {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是数字，当前值 {raw!r}") from exc


def _normalise_label(raw: str, fallback: str) -> str:
    """把显示名洗成一个合法的 Chatwoot 标签标题。"""
    candidate = (raw or "").strip()
    # Chatwoot 会把标签 downcase；这里保持一致，免得回查时对不上
    candidate = candidate.lower()
    if len(candidate) < 2 or not _LABEL_SAFE.match(candidate):
        candidate = fallback.lower()
    if len(candidate) < 2:
        # 兜底：实在没法用 id 就退化成固定前缀
        candidate = f"src_{fallback.lower()}"
    return candidate


def load_registry(path: Path) -> Dict[str, SourceSystem]:
    """读取来源系统注册表。"""
    if not path.exists():
        raise ConfigError(f"来源系统注册表不存在：{path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("sources") or []
    if not isinstance(entries, list):
        raise ConfigError(f"{path} 里的 sources 必须是一个列表")

    sources: Dict[str, SourceSystem] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigError(f"{path} 里 sources 的元素必须是字典")

        source_id = str(entry.get("id") or "").strip()
        if not source_id:
            raise ConfigError(f"{path} 里有一条来源缺少 id")
        if source_id in sources:
            raise ConfigError(f"{path} 里来源 id 重复：{source_id}")

        display_name = str(entry.get("display_name") or source_id).strip()
        label = _normalise_label(str(entry.get("label") or display_name), source_id)
        digest = str(entry.get("api_key_sha256") or "").strip().lower()

        if digest and not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ConfigError(f"{path} 里来源 {source_id} 的 api_key_sha256 不是合法的 sha256 十六进制")

        known = {
            "id",
            "display_name",
            "label",
            "enabled",
            "api_key_sha256",
            "description",
            "default_feedback_type",
            "allow_any_source",
        }
        extra = {k: str(v) for k, v in entry.items() if k not in known}

        sources[source_id] = SourceSystem(
            id=source_id,
            display_name=display_name,
            label=label,
            enabled=bool(entry.get("enabled", True)),
            api_key_sha256=digest,
            description=str(entry.get("description") or "").strip(),
            default_feedback_type=str(entry.get("default_feedback_type") or "").strip(),
            allow_any_source=bool(entry.get("allow_any_source", False)),
            extra=extra,
        )
    return sources


def load_settings() -> Settings:
    """从环境变量 + 注册表装配全局配置。"""
    registry_path = Path(_env("INTAKE_REGISTRY_PATH", "/app/config/source_systems.yml"))

    base_url = (_env("CHATWOOT_BASE_URL", "http://rails:3000") or "").rstrip("/")
    access_token = _env("CHATWOOT_ACCESS_TOKEN", "") or ""

    if not base_url:
        raise ConfigError("缺少 CHATWOOT_BASE_URL")
    if not access_token or access_token == "__BOOTSTRAP_PENDING__":
        raise ConfigError(
            "缺少 CHATWOOT_ACCESS_TOKEN —— 先跑 scripts/20-bootstrap-chatwoot.sh 生成"
        )

    return Settings(
        base_url=base_url,
        account_id=_env_int("CHATWOOT_ACCOUNT_ID", 1),
        inbox_id=_env_int("CHATWOOT_INBOX_ID", 1),
        access_token=access_token,
        registry_path=registry_path,
        request_timeout=_env_float("INTAKE_HTTP_TIMEOUT", 20.0),
        max_content_length=_env_int("INTAKE_MAX_CONTENT_LENGTH", 20000),
        sources=load_registry(registry_path),
        casdoor_enabled=bool(_env("CASDOOR_ENABLED", "false").lower() in ("1", "true", "yes")),
        casdoor_base_url=(_env("CASDOOR_BASE_URL", "") or "").rstrip("/"),
        casdoor_org=_env("CASDOOR_ORG", "") or "",
        casdoor_app_id=_env("CASDOOR_APP_ID", "") or "",
        casdoor_app_secret=_env("CASDOOR_APP_SECRET", "") or "",
        casdoor_timeout=_env_float("CASDOOR_TIMEOUT", 5.0),
        casdoor_user_cache_ttl=_env_float("CASDOOR_USER_CACHE_TTL", 300.0),
    )
