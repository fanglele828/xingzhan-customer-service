"""
Agent 可调用的只读工具。

设计原则：
- 只把"模型确有选择需要"的能力封成工具；确定性计算可作为服务函数复用。
- 工具返回结构化数据，不直接生成面向用户的自然语言。
- LLM 拿到工具结果后，再用自己的话组织回答（见 chat.py 的 system prompt）。

【重要】user_id 绑定方式：
- 每个请求调用 make_tools(user_id) 生成一组新的工具实例，通过闭包捕获 user_id。
- 不在全局函数对象上挂 _user_id，避免并发请求互相覆盖（线程安全）。
"""
from __future__ import annotations

from typing import Optional

from langchain_core.tools import tool

from backend.rag.retrieve import search_knowledge_tool
from backend.services.orders import get_order, list_orders, track_shipment, get_ticket
from backend.services.products import list_products, get_product
from backend.services.after_sales import preview_after_sales


def make_tools(user_id: str) -> list:
    """
    为指定用户生成一组工具（每次调用创建新实例，闭包捕获 user_id，线程安全）。

    关键：user_id 从服务端上下文注入，不由 LLM 自行指定。
    """

    @tool
    def search_knowledge(query: str, top_k: int = 4) -> list[dict]:
        """
        检索星栈数码知识库（政策、商品手册、FAQ 等公开文档）。

        用于回答：退货政策、保修期限、商品使用说明、常见问题等知识类问题。
        返回的是带来源页码的结构化证据片段，需要你用自己的话总结后再回答用户。
        """
        return search_knowledge_tool(query, top_k=top_k)

    @tool
    def search_products(category: Optional[str] = None,
                        max_price: Optional[float] = None,
                        features: Optional[list[str]] = None) -> list[dict]:
        """
        按条件筛选在售商品。

        参数：
        - category: 品类，如 "蓝牙耳机"、"机械键盘"、"无线鼠标"、"充电器"、"移动电源"、"扩展坞"
        - max_price: 最高价格（元）
        - features: 必须满足的特性列表，如 ["anc"] 表示需要主动降噪，["热插拔"] 表示键盘需支持热插拔
        返回真实在售、有库存的商品列表。
        """
        return list_products(category=category, max_price=max_price, features=features or [])

    @tool
    def get_product_detail(product_id: str) -> dict | None:
        """按商品 ID、完整型号或品牌加型号查询详细规格。比较已命名型号时分别查询，无需先搜索 ID。"""
        return get_product(product_id)

    @tool
    def query_orders(status: Optional[str] = None) -> list[dict]:
        """
        查询当前用户的订单列表。

        可按状态筛选：pending_payment(待付款)、paid(已付款未发货)、shipped(已发货未签收)、
        delivered(已签收)、returning(退货中)、exchanging(换货中)、cancelled(已取消)、refunded(已退款)。
        返回的订单自动绑定当前登录用户，不可指定其他用户。
        """
        return list_orders(user_id, status=status)

    @tool
    def query_order_detail(order_id: str) -> dict:
        """查询单个订单的详情（含商品列表）。必须是当前用户的订单。"""
        return get_order(order_id, user_id)

    @tool
    def track_shipment_tool(order_id: str) -> dict:
        """查询订单的物流轨迹（按时间排序的事件列表）。必须是当前用户的订单。"""
        return track_shipment(order_id, user_id)

    @tool
    def get_ticket_status(ticket_id: str) -> dict:
        """查询工单状态。必须是当前用户的工单。"""
        return get_ticket(ticket_id, user_id)

    @tool
    def apply_after_sales(order_id: str, issue_type: str = "no_reason", reason: str = "") -> dict:
        """
        发起售后申请预审（退货 / 换货 / 质量问题）。

        当用户明确要求"申请退货"、"我要换货"、"商品有质量问题要退/换"时调用此工具。
        注意：这只是预审，不会直接创建工单；用户确认后才会真正提交。

        参数：
        - order_id: 订单号，如 ORD-1002
        - issue_type: 问题类型
            no_reason  = 无理由退货（默认）
            defective  = 商品质量问题
            wrong_item = 发错货
            missing_item = 漏发/缺件
        - reason: 用户描述的具体原因（可选）

        返回：预审结论（可申请 / 不符合 / 需人工审核）+ 原因说明。
        如果可申请，返回中会带一个 request_id，前端会弹出确认框让用户确认。
        """
        result = preview_after_sales(
            order_id=order_id,
            user_id=user_id,
            issue_type=issue_type,
            reason=reason,
        )
        # 把草稿缓存到售后流程的草稿库，供确认环节使用
        draft = result.get("draft")
        if draft:
            from backend.agent.after_sales_graph import _draft_store
            _draft_store[draft["request_id"]] = draft
        return result

    return [
        search_knowledge,
        search_products,
        get_product_detail,
        query_orders,
        query_order_detail,
        track_shipment_tool,
        get_ticket_status,
        apply_after_sales,
    ]


# 保留向后兼容的别名（旧代码可能引用 bind_user_tools）
def bind_user_tools(user_id: str) -> list:
    """兼容旧接口：等价于 make_tools。"""
    return make_tools(user_id)
