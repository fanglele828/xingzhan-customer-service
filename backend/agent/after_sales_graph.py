"""
受控售后工作流。

设计说明：
- 退货申请会修改业务数据（创建工单），不能让 Agent 自由调用写工具。
- 流程分两步：预审（只读）→ 用户确认 → 幂等写工单。
- 最终防重靠 tickets.request_id 的 UNIQUE 约束，不依赖内存。

为了代码清晰和可测试，这里把"预审"和"确认写工单"拆成两个显式函数，
中间用 request_id 关联。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import StateGraph, END
from typing_extensions import TypedDict

from backend.config import CHECKPOINT_DB_PATH
from backend.services.after_sales import preview_after_sales, confirm_after_sales


class AfterSalesState(TypedDict):
    user_id: str
    order_id: str
    product_id: str | None
    issue_type: str
    reason: str
    preview: dict | None
    draft: dict | None


def _preview_node(state: AfterSalesState) -> AfterSalesState:
    """节点：确定性预审，生成草稿（不写库）。"""
    preview = preview_after_sales(
        order_id=state["order_id"],
        user_id=state["user_id"],
        product_id=state["product_id"],
        issue_type=state["issue_type"],
        reason=state["reason"],
    )
    state["preview"] = preview
    state["draft"] = preview.get("draft")
    return state


def build_graph():
    """构建售后预审流程图（带 Checkpointer）。"""
    builder = StateGraph(AfterSalesState)
    builder.add_node("preview", _preview_node)
    builder.set_entry_point("preview")
    builder.add_edge("preview", END)

    conn = sqlite3.connect(str(CHECKPOINT_DB_PATH), check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    return builder.compile(checkpointer=checkpointer)


_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


# 草稿缓存：request_id -> draft（内存缓存，重启后丢失但工单库有 UNIQUE 防重）
_draft_store: dict[str, dict] = {}


def start_after_sales(user_id: str, order_id: str, product_id: str | None,
                      issue_type: str, reason: str, thread_id: str) -> dict:
    """
    启动售后预审，返回三态结论 + 草稿（含 request_id）。

    不写工单。前端拿到 request_id 后展示确认按钮。
    """
    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id}}

    state: AfterSalesState = {
        "user_id": user_id,
        "order_id": order_id,
        "product_id": product_id,
        "issue_type": issue_type,
        "reason": reason,
        "preview": None,
        "draft": None,
    }

    result = graph.invoke(state, config=config)
    preview = result.get("preview", {})
    draft = result.get("draft")

    # 缓存草稿，供确认环节使用
    if draft:
        _draft_store[draft["request_id"]] = draft

    response: dict[str, Any] = {
        "decision": preview.get("decision"),
        "reasons": preview.get("reasons", []),
        "missing_fields": preview.get("missing_fields", []),
    }

    if draft:
        response["draft"] = draft
        response["action_required"] = {
            "action_type": "after_sales_confirm",
            "request_id": draft["request_id"],
            "payload": {"order_id": order_id},
        }

    return response


def confirm_after_sales_request(request_id: str, user_id: str, confirm: bool,
                                thread_id: str) -> dict:
    """
    用户确认后，创建工单（幂等）。

    - confirm=False：取消，不写工单。
    - confirm=True：复核后幂等写入，重复 request_id 只产生一条工单。
    """
    draft = _draft_store.get(request_id)
    if not draft:
        # 草稿不在内存中（如服务重启），尝试从数据库查已有工单。
        # 必须带 user_id 过滤，防止越权枚举 request_id。
        from backend.storage.sqlite import fetch_one
        row = fetch_one(
            "SELECT * FROM tickets WHERE request_id=? AND user_id=?",
            (request_id, user_id),
        )
        if row:
            return {
                "confirmed": True,
                "ticket_id": row["ticket_id"],
                "message": f"工单已存在，工单号：{row['ticket_id']}。",
            }
        return {"confirmed": False, "message": "找不到对应的申请草稿，请重新发起。"}

    # 校验草稿归属
    if draft["user_id"] != user_id:
        from backend.utils import BizError
        raise BizError("无权确认此申请", status_code=403)

    if not confirm:
        return {"confirmed": False, "message": "已取消申请，未创建工单。"}

    # 直接调用幂等写工单服务
    result = confirm_after_sales(
        request_id=request_id,
        user_id=user_id,
        confirm=True,
        draft=draft,
    )
    return result
