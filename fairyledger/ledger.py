"""流水、库存成本与挂账对账：进货/售出/期初录入/收付款写入、当前库存与移动加权均价、
挂账欠款现算（spec D2/D6 + ADR 0001/0004）。

- 写入命令（purchase/sale/opening/receive/pay）单事务原子：商品查证、往来单位解析、
  流水落库一体，中途失败不留半截数据。
- 派生值一律现算（ADR 0001）：当前数量 = 期初合计 + 流水净量，当前加权均价 =
  结存金额 ÷ 结存数量，往来欠款 = 挂账交易累计 − 收付款累计，均不落列。
- 加权平均公式：单位成本 =（进货前结存金额 + 本次进货金额）÷（进货前结存数量 +
  本次进货数量）；只有进货触发重算，售出按当时均价快照成本出库、本身不改变均价；
  期初为首笔结存基数、无期初退化为本次进货单价；运费不进分子分母（T7 费用化）。
- 挂账判定 = 往来单位档案 is_credit 标记（查询时现算）：档案挂账后该单位进货/售出
  自动累计欠款，收/付款按累计制一笔冲减总额（spec D6）。
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


def _require_positive(value: float | None, arg: str, label: str) -> float:
    """正数必填：缺失 → 参数缺失；非正 → 必须为正数（流水/收付款核心事实）。"""
    if value is None:
        raise BusinessError(f"参数缺失: --{arg}")
    if value <= 0:
        raise BusinessError(f"{label}必须为正数")
    return value


def _require_positive_qty(qty: float | None) -> float:
    """数量必填且为正数（spec D1：qty 正数，流水核心事实）。"""
    return _require_positive(qty, "qty", "数量")


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


def _resolve_counterparty(
    conn: sqlite3.Connection,
    name: str,
    *,
    customer: bool = False,
    supplier: bool = False,
    credit: bool = False,
) -> int:
    """往来单位按名称 create-or-resolve（CONTEXT：单表，客户/供应商可同时勾选）。

    同名复用并补打标记（不清除已有标记：现结售给挂账客户不改变其档案状态）；
    不存在自动建档。credit 只置位、从不清除。
    """
    row = conn.execute(
        "SELECT id FROM counterparties WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE counterparties SET"
            " is_customer = CASE WHEN ? THEN 1 ELSE is_customer END,"
            " is_supplier = CASE WHEN ? THEN 1 ELSE is_supplier END,"
            " is_credit = CASE WHEN ? THEN 1 ELSE is_credit END"
            " WHERE id = ?",
            (1 if customer else 0, 1 if supplier else 0, 1 if credit else 0, row["id"]),
        )
        return row["id"]
    cur = conn.execute(
        "INSERT INTO counterparties (name, is_customer, is_supplier, is_credit)"
        " VALUES (?, ?, ?, ?)",
        (name, 1 if customer else 0, 1 if supplier else 0, 1 if credit else 0),
    )
    return cur.lastrowid


def _normalize_freight(freight: float | None) -> float:
    """运费：可空默认 0；不能为负。"""
    freight = 0.0 if freight is None else freight
    if freight < 0:
        raise BusinessError("运费不能为负")
    return freight


def _normalize_date(date: str | None) -> str:
    """业务日期：缺省今天；格式必须 YYYY-MM-DD（防脏数据破坏流水排序）。"""
    biz_date = datetime.date.today().isoformat() if date is None else date.strip()
    if not DATE_RE.match(biz_date):
        raise BusinessError("日期格式应为 YYYY-MM-DD")
    return biz_date


def _require_counterparty(name: str | None) -> str:
    """往来单位必填：去空白，缺失 → 业务错误（spec D5：receive/pay 均 --counterparty）。"""
    name = (name or "").strip()
    if not name:
        raise BusinessError("参数缺失: --counterparty")
    return name


def _validate_date_filters(from_date: str | None, to_date: str | None) -> None:
    """查询日期筛选校验（可空；非空必须 YYYY-MM-DD，防脏数据破坏区间比较）。"""
    for d in (from_date, to_date):
        if d is not None and not DATE_RE.match(d.strip()):
            raise BusinessError("日期格式应为 YYYY-MM-DD")


def _tx_with_product(
    conn: sqlite3.Connection, tx_id: int, product: sqlite3.Row, **extra: Any
) -> dict[str, Any]:
    """流水完整行 + 商品要素富化，供写入命令回显（spec D5 输出契约）。"""
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    return {
        **dict(row),
        "drawing_no": product["drawing_no"],
        "name": product["name"],
        "unit": product["unit"],
        **extra,
    }


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
    freight = _normalize_freight(freight)
    biz_date = _normalize_date(date)
    supplier = (supplier or "").strip()
    with conn:
        product = _require_product(conn, drawing_no)
        cp_id = _resolve_counterparty(conn, supplier, supplier=True) if supplier else None
        cur = conn.execute(
            "INSERT INTO transactions"
            " (biz_date, biz_type, product_id, qty, price, freight,"
            "  counterparty_id, note)"
            " VALUES (?, 'purchase', ?, ?, ?, ?, ?, ?)",
            (biz_date, product["id"], qty, price, freight, cp_id, note),
        )
        tx_id = cur.lastrowid
    return _tx_with_product(conn, tx_id, product, supplier=supplier or None)


def _last_sale_price(
    conn: sqlite3.Connection, product_id: int, counterparty_id: int | None
) -> float | None:
    """该商品（可限定客户）最近一笔成交价：日期降序取第一条（报价参考语义，spec D6）。"""
    sql = "SELECT price FROM transactions WHERE biz_type = 'sale' AND product_id = ?"
    params: list[Any] = [product_id]
    if counterparty_id is not None:
        sql += " AND counterparty_id = ?"
        params.append(counterparty_id)
    sql += " ORDER BY biz_date DESC, id DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return row["price"] if row is not None else None


def record_sale(
    conn: sqlite3.Connection,
    drawing_no: str | None,
    qty: float | None,
    price: float | None,
    customer: str | None,
    credit: bool,
    freight: float | None,
    date: str | None,
    note: str | None,
) -> dict[str, Any]:
    """录一笔售出流水（biz_type=sale）。

    数量必填；客户缺省现结不挂账（挂账需明说 --credit，且须有客户）；缺单价自动带出
    该客户该商品上次成交价直接写入（price_auto 标记供 Fairy 回显），无历史则必问；
    成本列 = 售出当时加权均价快照，售出不触发均价重算（spec D2/D3 + ADR 0004）。
    """
    drawing_no = _require_drawing_no(drawing_no)
    qty = _require_positive_qty(qty)
    credit = bool(credit)
    customer = (customer or "").strip()
    if credit and not customer:
        raise BusinessError("挂账需指定客户: --credit 需要 --customer")
    freight = _normalize_freight(freight)
    biz_date = _normalize_date(date)
    with conn:
        product = _require_product(conn, drawing_no)
        cp_id = (
            _resolve_counterparty(conn, customer, customer=True, credit=credit)
            if customer else None
        )
        price_auto = False
        if price is None:
            last = _last_sale_price(conn, product["id"], cp_id)
            if last is None:
                raise BusinessError(
                    "参数缺失: --price（该客户该商品无历史成交价可带出，需询问售价）"
                )
            price = last
            price_auto = True
        if price < 0:
            raise BusinessError("售价不能为负")
        # 成本快照 = 售出时加权均价；无期初无进货时均价未定义、取 0（D2：快照一律取当时均价）
        _, avg, _ = _stock_state(conn, product["id"])
        cur = conn.execute(
            "INSERT INTO transactions"
            " (biz_date, biz_type, product_id, qty, price, freight, cost,"
            "  counterparty_id, note)"
            " VALUES (?, 'sale', ?, ?, ?, ?, ?, ?, ?)",
            (biz_date, product["id"], qty, price, freight, avg, cp_id, note),
        )
        tx_id = cur.lastrowid
    return _tx_with_product(
        conn, tx_id, product,
        customer=customer or None,
        credit=credit,
        **({"price_auto": True} if price_auto else {}),
    )


def record_payment(
    conn: sqlite3.Connection,
    pay_type: str,
    counterparty: str | None,
    amount: float | None,
    date: str | None,
    note: str | None,
) -> dict[str, Any]:
    """录一笔收款(receive)/付款(pay)流水（spec D5：累计制，一笔冲减欠款总额）。

    往来单位必填、按名称 create-or-resolve（receive 打客户标记、pay 打供应商标记）；
    金额必填为正数（方向在 pay_type）；日期缺省今天。挂账判定在查询侧（query credit）
    由档案 is_credit 推导——写入侧不校验是否挂账单位，现结单位收付款照录、不进对账单。
    """
    counterparty = _require_counterparty(counterparty)
    amount = _require_positive(amount, "amount", "金额")
    pay_date = _normalize_date(date)
    with conn:
        cp_id = _resolve_counterparty(
            conn, counterparty,
            customer=(pay_type == "receive"), supplier=(pay_type == "pay"),
        )
        cur = conn.execute(
            "INSERT INTO payments (pay_date, pay_type, counterparty_id, amount, note)"
            " VALUES (?, ?, ?, ?, ?)",
            (pay_date, pay_type, cp_id, amount, note),
        )
        pay_id = cur.lastrowid
    row = conn.execute("SELECT * FROM payments WHERE id = ?", (pay_id,)).fetchone()
    return {**dict(row), "counterparty": counterparty}


def query_price(
    conn: sqlite3.Connection,
    drawing_no: str | None,
    customer: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict[str, Any]]:
    """报价参考：历史成交价列表（sale 流水），含每笔成交的成本快照，日期降序。

    列 = 日期/图号/名称/数量+单位/单价/金额/成本快照/客户/备注；筛选 = 产品/客户/日期
    任意组合；日期降序第一条即上次成交价（spec D6）。
    """
    drawing_no = _require_drawing_no(drawing_no)
    _validate_date_filters(from_date, to_date)
    sql = (
        "SELECT t.id, t.biz_date, t.qty, t.price, t.cost, t.note,"
        " p.drawing_no, p.name, p.unit, cp.name AS customer"
        " FROM transactions t"
        " JOIN products p ON p.id = t.product_id"
        " LEFT JOIN counterparties cp ON cp.id = t.counterparty_id"
        " WHERE t.biz_type = 'sale' AND p.drawing_no = ? COLLATE NOCASE"
    )
    params: list[Any] = [drawing_no]
    customer = (customer or "").strip()
    if customer:
        sql += " AND cp.name = ? COLLATE NOCASE"
        params.append(customer)
    if from_date is not None:
        sql += " AND t.biz_date >= ?"
        params.append(from_date.strip())
    if to_date is not None:
        sql += " AND t.biz_date <= ?"
        params.append(to_date.strip())
    sql += " ORDER BY t.biz_date DESC, t.id DESC"
    rows = []
    for r in conn.execute(sql, params).fetchall():
        rows.append(
            {
                "id": r["id"],
                "biz_date": r["biz_date"],
                "drawing_no": r["drawing_no"],
                "name": r["name"],
                "unit": r["unit"],
                "qty": round(r["qty"], ROUND),
                "price": r["price"],
                "amount": round(r["qty"] * r["price"], ROUND),
                "cost": round(r["cost"], ROUND) if r["cost"] is not None else None,
                "customer": r["customer"],
                "note": r["note"],
            }
        )
    return rows


def query_credit(
    conn: sqlite3.Connection,
    counterparty: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    """挂账对账单（spec D6）：一维表（挂账流水 + 收付款）+ 挂账单位欠款总览。

    - records: 带挂账的记录按时间列出（日期/往来单位/单据/金额/备注），默认日期升序、
      同日流水先于收付款、再按录入序；筛选 = 单位/日期任意组合。
    - balances: 全部挂账单位期末欠款（= 挂账交易累计 − 收付款累计，全历史现算），
      欠款降序；--counterparty 时只列该单位。
    - 挂账判定 = 往来单位档案 is_credit 标记（查询时现算）：档案挂账后该单位进货/售出
      自动累计欠款（现结售给已挂账单位同样计挂账）；未挂账单位交易不计入欠款。
      日期筛选只收窄 records——欠款余额是当前状态（现算），不受期间过滤影响。
    """
    _validate_date_filters(from_date, to_date)
    name = (counterparty or "").strip()

    where, params = " WHERE 1 = 1", []
    if name:
        where += " AND counterparty = ? COLLATE NOCASE"
        params.append(name)
    if from_date is not None:
        where += " AND doc_date >= ?"
        params.append(from_date.strip())
    if to_date is not None:
        where += " AND doc_date <= ?"
        params.append(to_date.strip())
    sql = (
        "SELECT doc_date, counterparty, doc_type, amount, note FROM ("
        "  SELECT t.biz_date AS doc_date, cp.name AS counterparty,"
        "         t.biz_type AS doc_type, t.qty * t.price AS amount,"
        "         t.note AS note, 0 AS sort_group, t.id AS seq"
        "  FROM transactions t JOIN counterparties cp ON cp.id = t.counterparty_id"
        "  WHERE cp.is_credit = 1 AND t.biz_type IN ('purchase', 'sale')"
        "  UNION ALL"
        "  SELECT p.pay_date AS doc_date, cp.name AS counterparty,"
        "         p.pay_type AS doc_type, p.amount AS amount,"
        "         p.note AS note, 1 AS sort_group, p.id AS seq"
        "  FROM payments p JOIN counterparties cp ON cp.id = p.counterparty_id"
        "  WHERE cp.is_credit = 1"
        f"){where} ORDER BY doc_date ASC, sort_group ASC, seq ASC"
    )
    records = [
        {
            "date": r["doc_date"],
            "counterparty": r["counterparty"],
            "doc_type": r["doc_type"],
            "amount": round(r["amount"], ROUND),
            "note": r["note"],
        }
        for r in conn.execute(sql, params).fetchall()
    ]

    bal_sql = (
        "SELECT cp.name AS counterparty,"
        " COALESCE((SELECT SUM(t.qty * t.price) FROM transactions t"
        "           WHERE t.counterparty_id = cp.id"
        "             AND t.biz_type IN ('purchase', 'sale')), 0)"
        " - COALESCE((SELECT SUM(p.amount) FROM payments p"
        "             WHERE p.counterparty_id = cp.id), 0) AS balance"
        " FROM counterparties cp WHERE cp.is_credit = 1"
    )
    bal_params: list[Any] = []
    if name:
        bal_sql += " AND cp.name = ? COLLATE NOCASE"
        bal_params.append(name)
    balances = [
        {"counterparty": r["counterparty"], "balance": round(r["balance"], ROUND)}
        for r in conn.execute(bal_sql, bal_params).fetchall()
    ]
    balances.sort(key=lambda b: (-b["balance"], b["counterparty"]))
    return {"records": records, "balances": balances}


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
