"""
请求/响应数据结构（Pydantic）。

把前端和后端之间的契约显式化：
- ChatReply 是聊天接口的统一返回，包含自然语言回答 + 结构化证据。
- 重要结构（商品卡、来源卡、预审结果、工单状态）独立成模型，
  避免前端直接从大模型自由文本中解析授权信息。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ======================= 聊天相关 ======================= #

class ChatRequest(BaseModel):
    """聊天请求。"""

    message: str = Field(..., description="用户输入的问题")
    user_id: str = Field(..., description="演示用户 ID，如 U001")
    thread_id: str | None = Field(None, description="会话线程 ID，用于恢复上下文")


class Citation(BaseModel):
    """知识来源卡片：只展示文档名与页码，不把内部字段暴露给用户。"""

    doc_id: str
    title: str
    page_number: int | None = None
    section_title: str | None = None
    content_type: str | None = None  # text / table / figure_description


class ProductCard(BaseModel):
    """商品推荐卡片。"""

    product_id: str
    name: str
    price_cny: float
    stock: int
    category: str
    features: str
    warranty_months: int


class OrderSummary(BaseModel):
    """订单摘要（仅当前用户可见）。"""

    order_id: str
    status: str
    placed_at: str
    amount_cny: float
    item_count: int


class ActionRequired(BaseModel):
    """需要用户进一步操作的结构化提示（如确认售后申请）。"""

    action_type: str  # after_sales_confirm / order_cancel_confirm / ...
    request_id: str
    payload: dict[str, Any]


class ChatReply(BaseModel):
    """聊天统一返回。"""

    answer: str = Field(..., description="面向用户的自然语言回答")
    citations: list[Citation] = Field(default_factory=list)
    products: list[ProductCard] = Field(default_factory=list)
    orders: list[OrderSummary] = Field(default_factory=list)
    action_required: ActionRequired | None = None
    tool_traces: list[dict] = Field(default_factory=list, description="工具调用摘要，调试用")


# ======================= 售后预审相关 ======================= #

class AfterSalesDecision(str, Enum):
    eligible = "eligible_to_apply"          # 符合普通自动申请预审条件
    not_eligible = "not_eligible_for_auto_return"  # 不符合普通自动预审
    needs_review = "needs_human_review"     # 需要人工审核


class AfterSalesPreviewRequest(BaseModel):
    user_id: str
    order_id: str
    product_id: str | None = None
    issue_type: str = "no_reason"  # no_reason / defective / wrong_item / missing_item
    reason: str = ""


class AfterSalesPreview(BaseModel):
    """售后预审结果（不写数据库）。"""

    decision: AfterSalesDecision
    reasons: list[str]
    evidence_refs: list[Citation] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    draft: dict | None = None  # 工单草稿，包含 request_id


class AfterSalesConfirmRequest(BaseModel):
    user_id: str
    request_id: str
    confirm: bool  # True=确认提交工单，False=取消


class TicketStatus(BaseModel):
    ticket_id: str
    order_id: str
    type: str
    status: str
    description: str
    created_at: str
