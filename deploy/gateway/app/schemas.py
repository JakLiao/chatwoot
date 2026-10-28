"""采集接口的请求/响应模型。

契约要点
--------
* 上游只需要知道 **一个** 入口、**一套** 字段；Chatwoot 的联系人 upsert、
  source_id 幂等、标签、自定义属性全部由网关处理。
* ``source_system`` 必填且必须在注册表里登记（除非调用方拿的是「全局 Key」）。
* ``customer.identifier`` 是**来源系统内部**的客户唯一标识 —— 网关会加上
  来源前缀再写进 Chatwoot，避免不同系统的同名客户互相覆盖。
* ``request_id`` 是推荐的幂等键：同一个 request_id 重复提交只会产生一条会话。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Customer(BaseModel):
    """客户基本信息。"""

    model_config = ConfigDict(extra="forbid")

    identifier: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="来源系统内部的客户唯一标识（如 user_id / 手机号）",
    )
    name: Optional[str] = Field(None, max_length=200, description="客户昵称/姓名")
    email: Optional[str] = Field(None, max_length=200, description="邮箱")
    phone_number: Optional[str] = Field(None, max_length=60, description="手机号")
    company: Optional[str] = Field(None, max_length=200, description="公司/组织")
    city: Optional[str] = Field(None, max_length=100, description="城市")
    extra: Dict[str, Any] = Field(default_factory=dict, description="其余客户属性，原样带进 additional_attributes")

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        # 极简校验：真正的合法性交给 Chatwoot，但明显非法的先丢掉，免得整条反馈被拒
        if "@" not in value or value.startswith("@") or value.endswith("@") or " " in value:
            return None
        return value.lower()

    @field_validator("name", "phone_number", "company", "city")
    @classmethod
    def _strip(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value or None


class Feedback(BaseModel):
    """客户声音本体。"""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(..., min_length=1, description="客户原话/反馈正文")
    title: Optional[str] = Field(None, max_length=200, description="一句话标题（可空）")
    feedback_type: Optional[str] = Field(
        None, max_length=64, description="反馈类型：complaint / suggestion / praise / question ..."
    )
    sentiment: Optional[str] = Field(
        None, max_length=32, description="情绪：positive / neutral / negative"
    )
    rating: Optional[float] = Field(None, description="评分（星级或 NPS）")
    channel: Optional[str] = Field(
        None, max_length=64, description="客户表达渠道：app / web / phone / wechat ..."
    )
    occurred_at: Optional[datetime] = Field(
        None, description="反馈发生时间（ISO8601）；不传则用收到时间"
    )
    extra: Dict[str, Any] = Field(default_factory=dict, description="其余反馈属性")

    @field_validator("content")
    @classmethod
    def _strip_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("content 不能为空")
        return value


class IntakeRequest(BaseModel):
    """一次客户声音提交的完整载荷。"""

    model_config = ConfigDict(extra="forbid")

    source_system: str = Field(..., min_length=1, max_length=64, description="来源系统 id")
    customer: Customer
    feedback: Feedback
    metadata: Dict[str, Any] = Field(default_factory=dict, description="来源系统自定义元数据")
    request_id: Optional[str] = Field(
        None,
        max_length=128,
        description="上游请求唯一 ID —— 推荐的幂等键；重复提交同一个 request_id 不会产生第二条会话",
    )

    @field_validator("source_system", "request_id")
    @classmethod
    def _strip(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value or None


class IntakeResponse(BaseModel):
    """采集接口的响应。"""

    request_id: str
    source_system: str
    status: str = Field(..., description="created（新建）/ duplicate（幂等命中，已存在）")
    conversation_id: int
    conversation_display_id: int
    conversation_uuid: Optional[str] = None
    contact_id: int
    inbox_id: int
    source_id: str
    labels: List[str] = Field(default_factory=list)


class SourceInfo(BaseModel):
    id: str
    display_name: str
    label: str
    enabled: bool
    description: str
    default_feedback_type: str


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[Any] = None
