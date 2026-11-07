"""
售后预审服务：确定性规则判断退货资格。

这是"亮点 A"的核心：政策文本经 RAG 检索作为解释依据，
但"能不能退"的判断由 Python 规则函数确定性执行，不让 LLM 自行产生期限或例外。

三态结论：
- eligible_to_apply：符合普通自动申请预审条件（可提交工单，不代表退款批准）
- not_eligible_for_auto_return：不符合普通自动预审
- needs_human_review：质量问题、拆封限制、数据缺失等需人工审核
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from backend.config import BUSINESS_DIR, settings
from backend.services.orders import get_order, has_open_ticket
from backend.storage.sqlite import db_session, fetch_one
from backend.utils import BizError


# 售后工单类型 → 订单状态（贴近真实电商的售后进行中状态）
# - return_request（无理由退货）→ 退货中
# - quality_issue（质量/错发/缺件）→ 换货中
_TICKET_TYPE_TO_ORDER_STATUS = {
    "return_request": "returning",
    "quality_issue": "exchanging",
}

POLICY_RULES_PATH = BUSINESS_DIR / "policy_rules.json"

# 缓存政策规则（文件在运行期不变，避免每次预审都读磁盘）
_policy_rules_cache: dict | None = None


def load_policy_rules() -> dict:
    """加载业务规则配置（带内存缓存）。"""
    global _policy_rules_cache
    if _policy_rules_cache is None:
        _policy_rules_cache = json.loads(POLICY_RULES_PATH.read_text(encoding="utf-8"))
    return _policy_rules_cache


def _days_since_delivery(delivered_at: str, now: datetime) -> int:
    """计算签收日到现在的自然日差（签收日记第 0 天）。"""
    delivered = datetime.fromisoformat(delivered_at)
    if delivered.tzinfo is None:
        delivered = delivered.replace(tzinfo=settings.TIMEZONE)
    delta = now.date() - delivered.date()
    return delta.days


def preview_after_sales(order_id: str, user_id: str,
                        product_id: str | None = None,
                        issue_type: str = "no_reason",
                        reason: str = "") -> dict:
    """
    退货资格预审（只读，不写数据库）。

    返回三态结论 + 原因 + 证据引用 + 草稿（含 request_id，供确认环节幂等写入）。
    """
    rules = load_policy_rules()
    now = settings.now()
    return_policy = rules["return_policy"]

    # 1. 查询订单（自动校验归属）
    order = get_order(order_id, user_id)

    # 2. 检查是否已有未完结同类工单
    existing = has_open_ticket(order_id, user_id, "return_request")
    if existing:
        return {
            "decision": "needs_human_review",
            "reasons": [f"该订单已有未完结工单 {existing['ticket_id']}，请等待处理或联系客服。"],
            "evidence_refs": [],
            "missing_fields": [],
            "draft": None,
        }

    reasons: list[str] = []
    missing: list[str] = []

    # 3. 订单状态必须是 delivered
    if order["status"] != return_policy["requires_status"]:
        reasons.append(f"订单当前状态为「{order['status']}」，普通退货需已签收（delivered）。")
        return {
            "decision": "not_eligible_for_auto_return",
            "reasons": reasons,
            "evidence_refs": [],
            "missing_fields": missing,
            "draft": None,
        }

    # 5. 质量问题 / 错发 / 缺件 → 转人工，不套用普通拆封规则
    if issue_type in return_policy["issue_types_manual_review"]:
        reasons.append(f"问题类型「{issue_type}」需人工核验，不按普通退货自动预审。")
        return {
            "decision": "needs_human_review",
            "reasons": reasons,
            "evidence_refs": [],
            "missing_fields": missing,
            "draft": _make_draft(order_id, user_id, product_id, issue_type, reason, order),
        }

    # 4. 签收期限 <= 7 天
    delivered_at = order.get("delivered_at")
    if not delivered_at:
        missing.append("delivered_at")
        return {
            "decision": "needs_human_review",
            "reasons": ["缺少签收时间，无法判断退货期限。"],
            "evidence_refs": [],
            "missing_fields": missing,
            "draft": None,
        }

    days = _days_since_delivery(delivered_at, now)
    window = return_policy["window_days_from_delivery"]
    if days > window:
        reasons.append(f"已签收 {days} 天，超过普通退货 {window} 天期限。")
        return {
            "decision": "not_eligible_for_auto_return",
            "reasons": reasons,
            "evidence_refs": [],
            "missing_fields": missing,
            "draft": None,
        }

    # 6. 拆封的蓝牙耳机 → 转人工
    if order.get("package_state") == "opened":
        # 查询订单商品品类
        items = order.get("items", [])
        categories = set()
        for it in items:
            # 从 products 表查品类
            prow = fetch_one("SELECT category FROM products WHERE product_id=?", (it["product_id"],))
            if prow:
                categories.add(prow["category"])

        opened_cats = return_policy["opened_categories_manual_review"]
        if any(cat in opened_cats for cat in categories):
            reasons.append("蓝牙耳机已拆封，按规则需人工审核，不自动通过普通退货。")
            return {
                "decision": "needs_human_review",
                "reasons": reasons,
                "evidence_refs": [],
                "missing_fields": missing,
                "draft": _make_draft(order_id, user_id, product_id, issue_type, reason, order),
            }

    # 7. 商品状态必须正常
    if order.get("item_condition") != return_policy["requires_item_condition"]:
        reasons.append(f"商品状态为「{order.get('item_condition')}」，普通退货要求状态正常。")
        return {
            "decision": "needs_human_review",
            "reasons": reasons,
            "evidence_refs": [],
            "missing_fields": missing,
            "draft": _make_draft(order_id, user_id, product_id, issue_type, reason, order),
        }

    # 8. 多商品订单只退单件且无单件状态 → 转人工
    if product_id and len(order.get("items", [])) > 1:
        # 数据中只存订单级包装状态，无单件状态
        reasons.append("多商品订单按单件退货时，缺少单件状态信息，需人工核验。")
        return {
            "decision": "needs_human_review",
            "reasons": reasons,
            "evidence_refs": [],
            "missing_fields": ["per_item_condition"],
            "draft": _make_draft(order_id, user_id, product_id, issue_type, reason, order),
        }

    # 9. 通过预审
    reasons.append(f"订单已签收 {days} 天（在 {window} 天内），商品状态正常，符合普通退货申请条件。")
    return {
        "decision": "eligible_to_apply",
        "reasons": reasons,
        "evidence_refs": [],
        "missing_fields": missing,
        "draft": _make_draft(order_id, user_id, product_id, issue_type, reason, order),
    }


def _make_draft(order_id: str, user_id: str, product_id: str | None,
                issue_type: str, reason: str, order: dict) -> dict:
    """生成工单草稿（包含幂等 request_id，确认时复用）。"""
    request_id = f"as-{order_id}-{uuid.uuid4().hex[:8]}"
    return {
        "request_id": request_id,
        "order_id": order_id,
        "user_id": user_id,
        "product_id": product_id,
        "issue_type": issue_type,
        "reason": reason,
        "ticket_type": "return_request" if issue_type == "no_reason" else "quality_issue",
        "order_status": order["status"],
    }


def confirm_after_sales(request_id: str, user_id: str, confirm: bool,
                        draft: dict) -> dict:
    """
    确认售后申请：用户确认后才写入工单。

    幂等保证：tickets.request_id 有 UNIQUE 约束，重复提交不会创建多条。
    复核：再次校验订单归属与当前状态，防止等待期间状态变化。
    """
    if not confirm:
        return {"confirmed": False, "ticket_id": None, "message": "已取消申请，未创建工单。"}

    order_id = draft["order_id"]
    ticket_type = draft.get("ticket_type", "return_request")
    reason = draft.get("reason", "") or "用户申请售后"
    new_order_status = _TICKET_TYPE_TO_ORDER_STATUS.get(ticket_type, "returning")

    with db_session() as con:
        # 先获取写锁，使重复请求在前一笔提交之后检查工单和订单状态。
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT user_id, order_id, ticket_id FROM tickets WHERE request_id=?", (request_id,)
        ).fetchone()
        if existing:
            if (existing["user_id"], existing["order_id"]) != (user_id, order_id):
                raise BizError("该申请无法确认，请重新发起预审。", status_code=409)
            return {
                "confirmed": True,
                "ticket_id": existing["ticket_id"],
                "message": f"工单已存在（重复提交），工单号：{existing['ticket_id']}。",
            }

        order = con.execute(
            "SELECT status FROM orders WHERE order_id=? AND user_id=?", (order_id, user_id)
        ).fetchone()
        if not order:
            raise BizError("订单归属校验失败，无法创建工单", status_code=403)
        if order["status"] in ("cancelled", "refunded", "returned", "returning", "exchanging"):
            raise BizError("订单状态已变化，无法再次提交售后申请。", status_code=409)

        ticket_id = f"TKT-{uuid.uuid4().hex[:4].upper()}"
        con.execute(
            "INSERT INTO tickets (ticket_id, request_id, user_id, order_id, type, status, description, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
            (ticket_id, request_id, user_id, order_id, ticket_type, reason, settings.now().isoformat()),
        )
        con.execute(
            "UPDATE orders SET status=? WHERE order_id=? AND user_id=?",
            (new_order_status, order_id, user_id),
        )

    status_label = "退货中" if new_order_status == "returning" else "换货中"
    return {
        "confirmed": True,
        "ticket_id": ticket_id,
        "order_status": new_order_status,
        "message": f"售后工单已创建，工单号：{ticket_id}，订单状态已更新为「{status_label}」。请留意后续处理进展。",
    }
