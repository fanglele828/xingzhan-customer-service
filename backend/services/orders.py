"""
订单 / 物流 / 工单查询服务。

核心安全原则：
- 订单、物流、工单是用户私有数据，查询必须带 (order_id, user_id) 联合条件。
- "订单不存在" 和 "不是你的订单" 统一返回无权访问，不泄露其他用户信息。
- LLM 不能自定义 auth_user_id，身份从服务端上下文取得。
"""
from __future__ import annotations

from backend.storage.sqlite import fetch_all, fetch_one
from backend.utils import BizError


def _row_to_dict(row) -> dict:
    return dict(row) if row else None


# ======================= 订单 ======================= #

def get_order(order_id: str, user_id: str) -> dict:
    """
    查询单个订单，必须校验归属。

    越权或不存在统一抛出 BizError，不区分具体原因（防止信息泄露）。
    返回的 items 附带商品名称（product_name），便于向用户说明买了什么。
    """
    row = fetch_one(
        "SELECT * FROM orders WHERE order_id=? AND user_id=?",
        (order_id, user_id),
    )
    if not row:
        raise BizError(f"无权访问订单 {order_id} 或订单不存在", status_code=403)
    order = _row_to_dict(row)
    # 附带订单商品，并 JOIN products 拿到商品名称（用户关心的是买了啥，不是 product_id）
    items = fetch_all(
        "SELECT oi.*, p.name AS product_name, p.category AS product_category, p.brand AS product_brand "
        "FROM order_items oi JOIN products p ON oi.product_id=p.product_id "
        "WHERE oi.order_id=?",
        (order_id,),
    )
    order["items"] = [_row_to_dict(i) for i in items]
    return order


def list_orders(user_id: str, status: str | None = None) -> list[dict]:
    """列出当前用户的订单。"""
    sql = "SELECT * FROM orders WHERE user_id=?"
    params: list = [user_id]
    if status:
        sql += " AND status=?"
        params.append(status)
    rows = fetch_all(sql, tuple(params))
    return [_row_to_dict(r) for r in rows]


# ======================= 物流 ======================= #

def track_shipment(order_id: str, user_id: str) -> dict:
    """
    查询订单物流轨迹，必须校验归属。

    返回物流主信息 + 按时间排序的事件列表。
    """
    # 先校验订单归属
    get_order(order_id, user_id)

    shipment = fetch_one(
        "SELECT * FROM shipments WHERE order_id=?", (order_id,)
    )
    if not shipment:
        return {"shipment": None, "events": []}

    events = fetch_all(
        "SELECT * FROM shipment_events WHERE shipment_id=? ORDER BY timestamp ASC",
        (shipment["shipment_id"],),
    )
    return {
        "shipment": _row_to_dict(shipment),
        "events": [_row_to_dict(e) for e in events],
    }


# ======================= 工单 ======================= #

def get_ticket(ticket_id: str, user_id: str) -> dict:
    """查询工单状态，必须校验归属。"""
    row = fetch_one(
        "SELECT * FROM tickets WHERE ticket_id=? AND user_id=?",
        (ticket_id, user_id),
    )
    if not row:
        raise BizError(f"无权访问工单 {ticket_id} 或工单不存在", status_code=403)
    return _row_to_dict(row)


def list_tickets(user_id: str) -> list[dict]:
    """列出当前用户的工单。"""
    rows = fetch_all("SELECT * FROM tickets WHERE user_id=?", (user_id,))
    return [_row_to_dict(r) for r in rows]


def has_open_ticket(order_id: str, user_id: str, ticket_type: str | None = None) -> dict | None:
    """
    检查某订单是否已有未完结工单（避免重复申请）。

    未完结状态：open / in_review。
    """
    sql = ("SELECT * FROM tickets WHERE order_id=? AND user_id=? "
           "AND status IN ('open','in_review')")
    params: list = [order_id, user_id]
    if ticket_type:
        sql += " AND type=?"
        params.append(ticket_type)
    row = fetch_one(sql, tuple(params))
    return _row_to_dict(row) if row else None


# 工单类型 → 完结后订单状态（贴近真实电商售后闭环）
# - return_request 完结 → 已退货（退货流程结束）
# - quality_issue 完结  → 已签收（换货完成，用户收到新商品）
_TICKET_TYPE_TO_RESOLVED_STATUS = {
    "return_request": "returned",
    "quality_issue": "delivered",
}


def resolve_ticket(ticket_id: str, user_id: str) -> dict:
    """
    完结工单（模拟商家审核通过），同步更新订单状态。

    这是售后状态机的闭环：工单创建时订单进入 退货中/换货中，
    工单完结时订单回到 已退货/已签收，保证 tickets 与 orders 状态一致。

    真实系统中此操作由客服后台触发，此处提供接口用于演示完整流程。
    """
    from backend.storage.sqlite import db_session

    ticket = get_ticket(ticket_id, user_id)  # 校验归属
    if ticket["status"] == "resolved":
        return {"ticket_id": ticket_id, "status": "resolved", "message": "工单已完结，无需重复操作。"}

    order_id = ticket["order_id"]
    ticket_type = ticket["type"]
    new_order_status = _TICKET_TYPE_TO_RESOLVED_STATUS.get(ticket_type, "returned")

    with db_session() as con:
        con.execute(
            "UPDATE tickets SET status='resolved' WHERE ticket_id=? AND user_id=?",
            (ticket_id, user_id),
        )
        con.execute(
            "UPDATE orders SET status=? WHERE order_id=? AND user_id=?",
            (new_order_status, order_id, user_id),
        )

    return {
        "ticket_id": ticket_id,
        "status": "resolved",
        "order_status": new_order_status,
        "message": f"工单 {ticket_id} 已完结，订单状态已更新。",
    }
