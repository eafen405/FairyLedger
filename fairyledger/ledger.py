"""流水与库存成本：进货/期初录入写入、当前库存与移动加权均价现算（spec D2 + ADR 0004）。

- 写入命令（purchase/opening）单事务原子：商品查证、往来单位解析、流水落库一体，
  中途失败不留半截数据。
- 派生值一律现算（ADR 0001）：当前数量 = 期初合计 + 流水净量，当前加权均价 =
  结存金额 ÷ 结存数量，均不落列。
- 加权平均公式：单位成本 =（进货前结存金额 + 本次进货金额）÷（进货前结存数量 +
  本次进货数量）；只有进货触发重算，售出按当时均价快照成本出库、本身不改变均价；
  期初为首笔结存基数、无期初退化为本次进货单价；运费不进分子分母（T7 费用化）。
"""

import datetime
import re
import sqlite3
from typing import Any

from .errors import BusinessError

# 业务日期：YYYY-MM-DD（Fairy 语音转写后传入；校验防脏数据破坏流水排序）
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 库存报表派生列：qty/unit_cost/amount 输出前四舍五入到 4 位小数，去掉浮点噪声
ROUND = 4


def _require_drawing_no(drawing_no: str | None) -> str:
    """图号必填：去空白，缺失 → 业务错误。"""
    drawing_no = (drawing_no or "").strip()
    if not drawing_no:
        raise BusinessError("参数缺失: --drawing-no")
    return drawing_no


def _require_positive_qty(qty: float | None) -> float:
    """数量必填且为正数（spec D1：qty 正数，流水核心事实）。"""
    if qty is None:
        raise BusinessError("参数缺失: --qty")
    if qty <= 0:
        raise BusinessError("数量必须为正数")
    return qty


def _require_product(
    conn: sqlite3.Connection, drawing_no: str
) -> sqlite3.Row:
    """按图号精确查找商品（大小写不敏感）；未知图号 → 业务错误「无此商品」。"""
    row = conn.execute(
        "SELECT id, drawing_no, name, unit FROM products"
        " WHERE drawing_no = ? COLLATE NOCASE",
        (drawing_no,),
    ).fetchone()
    if row is None:
        raise BusinessError(f"无此商品: {drawing_no}")
    return row


def _resolve_supplier(conn: sqlite3.Connection, name: str) -> int:
    """供应商按名称 create-or-resolve：同名复用并打供应商标记，不存在自动建档。"""
    row = conn.execute(
        "SELECT id FROM counterparties WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE counterparties SET is_supplier = 1 WHERE id = ?", (row["id"],)
        )
        return row["id"]
    cur = conn.execute(
        "INSERT INTO counterparties (name, is_supplier) VALUES (?, 1)", (name,)
    )
    return cur.lastrowid


def record_purchase(
    conn: sqlite3.Connection,
    drawing_no: str | None,
    qty: float | None,
    price: float | None,
    freight: float | None,
    supplier: str | None,
    date: str | None,
    note: str | None,
) -> dict[str, Any]:
    """录一笔进货流水（biz_type=purchase）。

    数量/进价必填；运费可空默认 0；供应商可空、按名称 create-or-resolve；
    日期缺省今天；运费不参与加权平均（T7 费用化）。
    """
    drawing_no = _require_drawing_no(drawing_no)
    qty = _require_positive_qty(qty)
    if price is None:
        raise BusinessError("参数缺失: --price")
    if price < 0:
        raise BusinessError("进价不能为负")
    freight = 0.0 if freight is None else freight
    if freight < 0:
        raise BusinessError("运费不能为负")
    biz_date = (
        datetime.date.today().isoformat() if date is None else date.strip()
    )
    if not DATE_RE.match(biz_date):
        raise BusinessError("日期格式应为 YYYY-MM-DD")
    supplier = (supplier or "").strip()
    with conn:
        product = _require_product(conn, drawing_no)
        cp_id = _resolve_supplier(conn, supplier) if supplier else None
        cur = conn.execute(
            "INSERT INTO transactions"
            " (biz_date, biz_type, product_id, qty, price, freight,"
            "  counterparty_id, note)"
            " VALUES (?, 'purchase', ?, ?, ?, ?, ?, ?)",
            (biz_date, product["id"], qty, price, freight, cp_id, note),
        )
        tx_id = cur.lastrowid
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    return {
        **dict(row),
        "drawing_no": product["drawing_no"],
        "name": product["name"],
        "unit": product["unit"],
        "supplier": supplier or None,
    }


def record_opening(
    conn: sqlite3.Connection,
    drawing_no: str | None,
    qty: float | None,
    cost: float | None,
) -> dict[str, Any]:
    """录一笔期初库存（追加语义：同商品可多次，数量累加；成本缺省 0）。

    期初是库存与加权均价的起始基数；补录型（新商品出货归零）与启用型并存，
    不区分、同为 opening_stock 行。
    """
    drawing_no = _require_drawing_no(drawing_no)
    qty = _require_positive_qty(qty)
    cost = 0.0 if cost is None else cost
    if cost < 0:
        raise BusinessError("期初成本不能为负")
    with conn:
        product = _require_product(conn, drawing_no)
        cur = conn.execute(
            "INSERT INTO opening_stock (product_id, qty, cost) VALUES (?, ?, ?)",
            (product["id"], qty, cost),
        )
        opening_id = cur.lastrowid
    row = conn.execute(
        "SELECT * FROM opening_stock WHERE id = ?", (opening_id,)
    ).fetchone()
    return {
        **dict(row),
        "drawing_no": product["drawing_no"],
        "name": product["name"],
        "unit": product["unit"],
    }


def _stock_state(conn: sqlite3.Connection, product_id: int) -> tuple[float, float, float]:
    """重放期初 + 流水，现算某商品的 (当前数量, 当前加权均价, 结存金额)（ADR 0001/0004）。

    期初行按录入序聚合为基数（数量 Σqty、金额 Σqty×cost）；流水按
    biz_date + id（同日按录入序）逐笔推进，只有进货改变结存金额与数量并重算均价。
    售出/红冲分支（sale/purchase_return/sale_return）镜像 T1 数量 SQL 的符号方向，
    待 T3/T5 开放对应写入命令后自然生效；当前均价 = 结存金额 ÷ 结存数量。
    结存金额保持全精度，供金额列直接取用，避免 qty×avg 重算引入舍入差。
    """
    qty = 0.0
    amount = 0.0
    for o in conn.execute(
        "SELECT qty, cost FROM opening_stock WHERE product_id = ? ORDER BY id",
        (product_id,),
    ):
        qty += o["qty"]
        amount += o["qty"] * (o["cost"] or 0.0)
    for t in conn.execute(
        "SELECT biz_type, qty, price, cost FROM transactions"
        " WHERE product_id = ? ORDER BY biz_date, id",
        (product_id,),
    ):
        bt = t["biz_type"]
        q, price, cost = t["qty"], t["price"], t["cost"]
        if bt == "purchase":
            amount += q * price
            qty += q
        elif bt == "purchase_return":
            amount -= q * price  # 按原进货价红字冲减，均价随之重算
            qty -= q
        elif bt == "sale":
            amount -= q * (amount / qty if qty else 0.0)  # 按当时均价出库，均价不变
            qty -= q
        elif bt == "sale_return":
            amount += q * (cost or 0.0)  # 按原售出成本快照回库
            qty += q
    avg = amount / qty if qty else 0.0
    return qty, avg, amount


def query_stock(
    conn: sqlite3.Connection, drawing_no: str | None = None
) -> list[dict[str, Any]]:
    """当前库存报表：图号/名称/数量+单位/当前加权均价/金额（全现算），默认图号序。

    列 = 图号/名称/数量+单位（期初+流水净量现算）/单位成本（当前加权均价）/
    金额（qty×均价）；可 --drawing-no 过滤（spec D6）。
    """
    sql = "SELECT id, drawing_no, name, unit FROM products"
    params: list[Any] = []
    if drawing_no:
        sql += " WHERE drawing_no = ? COLLATE NOCASE"
        params.append(drawing_no)
    sql += " ORDER BY drawing_no"
    rows = []
    for p in conn.execute(sql, params).fetchall():
        qty, avg, amount = _stock_state(conn, p["id"])
        rows.append(
            {
                "id": p["id"],
                "drawing_no": p["drawing_no"],
                "name": p["name"],
                "unit": p["unit"],
                "qty": round(qty, ROUND),
                "unit_cost": round(avg, ROUND),
                "amount": round(amount, ROUND),
            }
        )
    return rows
