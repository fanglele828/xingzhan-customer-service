"""
商品服务：从 SQLite 查询商品，硬条件筛选。

为什么硬条件必须用 SQL 而不是让 LLM 从向量库猜：
- 价格、库存、是否在售、ANC 等是"事实"，不能靠语义相似度判断。
- LLM 可能会推荐库存为 0 的商品，或把"通话降噪"误当成 ANC。
"""
from __future__ import annotations

import json

from backend.storage.sqlite import fetch_all, fetch_one


def _row_to_product(row) -> dict:
    """把 SQLite 行转成商品字典，解析 specs_json。"""
    p = dict(row)
    p["specs"] = json.loads(p["specs_json"])
    p["price_cny"] = float(p["price_cny"])
    return p


def list_products(category: str | None = None, max_price: float | None = None,
                  min_price: float | None = None, in_stock_only: bool = True,
                  features: list[str] | None = None) -> list[dict]:
    """
    按条件筛选商品。

    - category: 品类，如 "蓝牙耳机"
    - max_price / min_price: 价格区间（人民币元）
    - in_stock_only: 只返回有库存的商品（默认 True）
    - features: 必须包含的特性关键词列表（在 specs_json 或 features 字段中匹配）
    """
    sql = "SELECT * FROM products WHERE status='active'"
    params: list = []

    if category:
        sql += " AND category = ?"
        params.append(category)
    if max_price is not None:
        sql += " AND price_cny <= ?"
        params.append(max_price)
    if min_price is not None:
        sql += " AND price_cny >= ?"
        params.append(min_price)
    if in_stock_only:
        sql += " AND stock > 0"

    rows = fetch_all(sql, tuple(params))
    products = [_row_to_product(r) for r in rows]

    # 特性硬过滤：优先匹配结构化 specs 中的字段值，再回退到 features 文本
    if features:
        filtered = []
        for p in products:
            ok = True
            for f in features:
                # 处理布尔型规格，如 "anc" → specs["anc"] 必须为 True
                if f in p["specs"] and isinstance(p["specs"][f], bool):
                    if not p["specs"][f]:
                        ok = False
                        break
                # 处理数值型规格，如 "battery_hours>=8"
                elif ">=" in f:
                    key, val = f.split(">=", 1)
                    key = key.strip()
                    value = p["specs"].get(key)
                    try:
                        meets_minimum = not isinstance(value, bool) and float(value) >= float(val)
                    except (ValueError, TypeError):
                        meets_minimum = False
                    if not meets_minimum:
                        ok = False
                        break
                # 回退：文本包含匹配
                else:
                    text = p["features"] + " " + json.dumps(p["specs"], ensure_ascii=False)
                    if f.lower() not in text.lower():
                        ok = False
                        break
            if ok:
                filtered.append(p)
        products = filtered

    return products


def get_product(product_id: str) -> dict | None:
    """按 ID、完整型号或品牌加型号查询。重名时留给用户进一步确认。"""
    row = fetch_one("SELECT * FROM products WHERE product_id=?", (product_id,))
    if row:
        return _row_to_product(row)
    normalized = "".join(product_id.split()).lower()
    matches = fetch_all(
        """SELECT * FROM products WHERE
           lower(replace(model, ' ', '')) = ? OR
           lower(replace(brand || model, ' ', '')) = ? OR
           lower(replace(name, ' ', '')) = ? LIMIT 2""",
        (normalized, normalized, normalized),
    )
    return _row_to_product(matches[0]) if len(matches) == 1 else None


def compare_products(product_ids: list[str]) -> list[dict]:
    """对比多个商品（用于"前两款哪个续航更久"这类追问）。"""
    if not product_ids:
        return []
    placeholders = ",".join("?" * len(product_ids))
    rows = fetch_all(f"SELECT * FROM products WHERE product_id IN ({placeholders})",
                     tuple(product_ids))
    return [_row_to_product(r) for r in rows]
