"""
FastAPI 应用入口。

路由设计：
- GET  /health          健康检查（DB + Milvus 状态）
- POST /chat            聊天接口（主 Agent，只读咨询/选品/查询）
- POST /after-sales/preview   售后预审（不写库）
- POST /after-sales/confirm   确认售后申请（受控写工单）
- GET  /tickets/{ticket_id}   工单状态查询（带归属校验）
- GET  /products        商品列表（可选筛选）

中间件：
- 全局异常处理：BizError → 4xx，其他 → 500（不暴露堆栈）
- CORS：允许前端跨域
"""
from __future__ import annotations

import traceback

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.agent.after_sales_graph import start_after_sales, confirm_after_sales_request
from backend.agent.chat import run_agent
from backend.config import settings
from backend.rag.ingest import get_collection_stats
from backend.schemas import (
    AfterSalesConfirmRequest,
    AfterSalesPreviewRequest,
    ChatReply,
    ChatRequest,
    TicketStatus,
)
from backend.services.orders import get_ticket, resolve_ticket
from backend.services.products import list_products
from backend.storage.sqlite import fetch_one
from backend.utils import BizError, fail, ok

app = FastAPI(title="星栈数码智能客服 API", version="1.0.0")

# CORS：允许前端开发服务器跨域访问
# 注意：本项目不使用 Cookie/凭证，因此 allow_credentials=False，allow_origins 可用 "*"。
# 若未来启用凭证登录，必须把 allow_origins 改为具体域名列表。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(BizError)
async def biz_error_handler(request: Request, exc: BizError):
    """业务错误：返回 4xx + 友好提示。"""
    return JSONResponse(status_code=exc.status_code, content=fail(exc.message))


@app.exception_handler(Exception)
async def global_error_handler(request: Request, exc: Exception):
    """全局兜底：返回 500，不暴露堆栈和密钥。"""
    print(f"[ERROR] {exc}\n{traceback.format_exc()}")
    return JSONResponse(status_code=500, content=fail("服务器内部错误，请稍后重试。"))


@app.get("/health")
def health():
    """健康检查：返回 DB 和 Milvus 连接状态。"""
    db_ok = False
    try:
        fetch_one("SELECT 1")
        db_ok = True
    except Exception:
        pass

    milvus = get_collection_stats()
    return ok({
        "db": "ok" if db_ok else "error",
        "milvus": milvus,
        "policy_version": settings.POLICY_VERSION,
    })


@app.post("/chat")
def chat(req: ChatRequest) -> dict:
    """
    聊天接口：主 Agent 处理只读咨询、选品、订单/物流/工单查询。

    售后申请走 /after-sales/preview 和 /after-sales/confirm，不在此直接写库。
    """
    if not req.user_id:
        raise BizError("请选择演示用户身份", status_code=400)
    if not req.message.strip():
        raise BizError("消息不能为空", status_code=400)

    result = run_agent(req.message, req.user_id, req.thread_id)
    return ok(result)


@app.post("/after-sales/preview")
def after_sales_preview(req: AfterSalesPreviewRequest) -> dict:
    """售后预审：返回三态结论 + 草稿，不写数据库。"""
    thread_id = f"as-{req.order_id}-{req.user_id}"
    result = start_after_sales(
        user_id=req.user_id,
        order_id=req.order_id,
        product_id=req.product_id,
        issue_type=req.issue_type,
        reason=req.reason,
        thread_id=thread_id,
    )
    return ok(result)


@app.post("/after-sales/confirm")
def after_sales_confirm(req: AfterSalesConfirmRequest) -> dict:
    """确认售后申请：幂等创建工单。"""
    thread_id = f"confirm-{req.request_id}"
    result = confirm_after_sales_request(
        request_id=req.request_id,
        user_id=req.user_id,
        confirm=req.confirm,
        thread_id=thread_id,
    )
    return ok(result)


@app.get("/tickets/{ticket_id}")
def get_ticket_status(ticket_id: str, user_id: str = "U001") -> dict:
    """查询工单状态（带归属校验）。"""
    ticket = get_ticket(ticket_id, user_id)
    return ok(TicketStatus(**ticket))


@app.post("/tickets/{ticket_id}/resolve")
def resolve_ticket_endpoint(ticket_id: str, user_id: str = "U001") -> dict:
    """
    完结工单（模拟商家审核通过），同步更新订单状态。

    售后闭环：退货中 → 已退货；换货中 → 已签收。
    真实系统中由客服后台触发，此处用于演示完整售后生命周期。
    """
    result = resolve_ticket(ticket_id, user_id)
    return ok(result)


@app.get("/orders")
def list_user_orders(user_id: str = "U001", status: str | None = None) -> dict:
    """列出当前用户的订单（侧边栏展示，支持按状态筛选）。"""
    from backend.services.orders import list_orders
    from backend.storage.sqlite import fetch_all
    orders = list_orders(user_id, status=status)
    # 一次查询拿到所有订单的商品数 + 商品名称，避免 N+1
    if orders:
        ids = [o["order_id"] for o in orders]
        placeholders = ",".join("?" * len(ids))
        rows = fetch_all(
            f"SELECT oi.order_id, "
            f"COUNT(*) as cnt, "
            f"GROUP_CONCAT(p.name, '、') as product_names "
            f"FROM order_items oi JOIN products p ON oi.product_id=p.product_id "
            f"WHERE oi.order_id IN ({placeholders}) GROUP BY oi.order_id",
            tuple(ids),
        )
        # sqlite3.Row 支持按键取值，但没有 .get() 方法，统一转成 dict
        info_map = {r["order_id"]: dict(r) for r in rows}
        for o in orders:
            info = info_map.get(o["order_id"], {})
            o["item_count"] = info.get("cnt", 0)
            o["product_names"] = info.get("product_names", "")
    return ok(orders)


@app.get("/products")
def products(category: str | None = None, max_price: float | None = None,
             features: str | None = None) -> dict:
    """商品列表（可选筛选）。features 用逗号分隔。"""
    feat_list = features.split(",") if features else None
    products = list_products(category=category, max_price=max_price, features=feat_list)
    return ok(products)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8001, reload=True)
