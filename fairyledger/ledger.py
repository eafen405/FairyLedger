"""流水、库存成本与挂账对账：进货/售出/期初录入/收付款/红冲/修改写入、
当前库存与移动加权均价、挂账欠款现算、单产品流水、日报与期间毛利
（spec D2/D5/D6 + ADR 0001/0004）。

- 写入命令（purchase/sale/opening/receive/pay/reverse/edit）单事务原子：
  商品查证、往来单位解析、流水落库、审计日志一体，中途失败不留半截数据。
- 红冲（reverse）：必须关联原单（ref_id）、按原单成本冲、退货价 = 原单价格——
  进货→purchase_return（数量金额按原进货价红字冲减、均价重算、不直接冲当期毛利），
  售出→sale_return（按原售出成本快照回库、收入成本同步冲减、毛利按该笔减少）；
  原单保留不动，退货流水与审计日志同事务。
- 修改（edit）：按 transaction id 定位、部分更新（传了哪些改哪些）；不允许改商品
  （图号）——换商品走红冲 + 新单；改售出 qty/price 不改成本快照；每次修改自动写
  审计日志（整行 before/after 快照，可完整还原，audit_log 无 CLI 暴露）。
- 派生值一律现算（ADR 0001）：当前数量 = 期初合计 + 流水净量，当前加权均价 =
  结存金额 ÷ 结存数量，往来欠款 = 挂账交易累计 − 收付款累计，均不落列。
- 加权平均公式：单位成本 =（进货前结存金额 + 本次进货金额）÷（进货前结存数量 +
  本次进货数量）；只有进货触发重算，售出按当时均价快照成本出库、本身不改变均价；
  期初为首笔结存基数、无期初退化为本次进货单价；运费不进分子分母（T7 费用化）。
- 挂账判定 = 往来单位档案 is_credit 标记（查询时现算）：档案挂账后该单位进货/售出
  自动累计欠款，收/付款按累计制一笔冲减总额（spec D6）。
"""

import datetime
import json
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


def _require_tx(conn: sqlite3.Connection, tx_id: int) -> sqlite3.Row:
    """按 id 定位流水；不存在 → 业务错误「无此流水」（红冲/修改引用原单的入口）。"""
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    if row is None:
        raise BusinessError(f"无此流水: {tx_id}")
    return row


def _counterparty_name(conn: sqlite3.Connection, cp_id: int | None) -> str | None:
    """往来单位名称；无则 None。"""
    if cp_id is None:
        return None
    row = conn.execute("SELECT name FROM counterparties WHERE id = ?", (cp_id,)).fetchone()
    return row["name"] if row is not None else None


def _write_audit(
    conn: sqlite3.Connection,
    table_name: str,
    record_id: int,
    action: str,
    before: sqlite3.Row | dict | None,
    after: sqlite3.Row | dict | None,
    note: str | None = None,
) -> int:
    """写一行审计日志（spec D1）：改/删前整行 + 后整行 JSON 快照，可完整还原。

    audit_log 无 CLI 暴露（spec Testing Decisions 允许测试只读核对）；
    action 取值 update（edit）/ reverse（红冲），before/after 均为整行快照。
    必须在调用方事务内执行，随业务写入同事务原子。
    """
    def _dump(row: sqlite3.Row | dict | None) -> str | None:
        return json.dumps(dict(row), ensure_ascii=False) if row is not None else None

    cur = conn.execute(
        "INSERT INTO audit_log (table_name, record_id, action, before_json, after_json, note)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (table_name, record_id, action, _dump(before), _dump(after), note),
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


def record_reverse(
    conn: sqlite3.Connection,
    tx_id: int | None,
    date: str | None,
    note: str | None,
) -> dict[str, Any]:
    """红冲原单（spec D2/D5）：必须关联原单、按原单成本冲、退货价 = 原单价格。

    - 进货原单 → purchase_return：数量与金额按原进货单价红字冲减，结存均价随之
      重算（均价现算、随查随得），不直接冲减当期毛利（只减少后续可售成本）。
    - 售出原单 → sale_return：按原售出成本快照（cost = 原单快照）把货退回库存，
      收入与成本同步冲减，毛利按该笔减少。
    - 原单保留不动，新退货流水 ref_id 关联原单；退货 freight 为 0（运费费用化，
      退货不产生运费，金额 = qty×price 现算，spec D2 未定义退货运费）。
    - 红冲致记录为负不硬拒（业务纪律非系统闸门）。
    - 一单一冲：原单已被红冲（存在 ref_id 指向它的退货行）再红冲 → 业务错误
      （T5 定案：防止重复红冲导致货重复回库）。
    - 单事务原子：退货流水 + 审计日志（action=reverse，before=原单整行、
      after=退货整行）同事务，中途失败不留半截。
    """
    if tx_id is None:
        raise BusinessError("参数缺失: --tx")
    biz_date = _normalize_date(date)
    with conn:
        orig = _require_tx(conn, tx_id)
        if orig["biz_type"] not in ("purchase", "sale"):
            raise BusinessError(
                f"仅支持红冲进货/售出原单，该单类型: {orig['biz_type']}"
            )
        existing = conn.execute(
            "SELECT id FROM transactions WHERE ref_id = ? LIMIT 1", (tx_id,)
        ).fetchone()
        if existing is not None:
            raise BusinessError(
                f"该原单已红冲: #{tx_id} → #{existing['id']}"
            )
        ret_type = (
            "purchase_return" if orig["biz_type"] == "purchase" else "sale_return"
        )
        # 售出退货需携带原单成本快照供回库（_stock_state sale_return 分支按 cost 恢复）；
        # 进货退货按原进货价冲减，无需成本快照列
        cost = orig["cost"] if ret_type == "sale_return" else None
        cur = conn.execute(
            "INSERT INTO transactions"
            " (biz_date, biz_type, product_id, qty, price, freight, cost,"
            "  counterparty_id, ref_id, note)"
            " VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
            (biz_date, ret_type, orig["product_id"], orig["qty"], orig["price"],
             cost, orig["counterparty_id"], orig["id"], note),
        )
        ret_id = cur.lastrowid
        ret = conn.execute(
            "SELECT * FROM transactions WHERE id = ?", (ret_id,)
        ).fetchone()
        audit_id = _write_audit(
            conn, "transactions", orig["id"], "reverse", orig, ret,
            f"reverse transactions #{orig['id']} → #{ret_id} ({ret_type})",
        )
    product = conn.execute(
        "SELECT drawing_no, name, unit FROM products WHERE id = ?",
        (ret["product_id"],),
    ).fetchone()
    return _tx_with_product(
        conn, ret_id, product,
        counterparty=_counterparty_name(conn, ret["counterparty_id"]),
        audit_id=audit_id,
        original={
            "id": orig["id"],
            "biz_date": orig["biz_date"],
            "biz_type": orig["biz_type"],
            "qty": orig["qty"],
            "price": orig["price"],
            "cost": orig["cost"],
        },
    )


def edit_transaction(
    conn: sqlite3.Connection,
    tx_id: int | None,
    qty: float | None,
    price: float | None,
    customer: str | None,
    date: str | None,
    note: str | None,
) -> dict[str, Any]:
    """修改流水（spec D5 修改语义 + 审计）。

    - 部分更新：传了哪些改哪些（可改 qty/price/customer/date/note）。
    - 不允许改商品（图号）：CLI 无 --drawing-no 参数（结构性禁止），换商品 =
      红冲 + 新单（换货语义）。
    - 改售出单 qty/price 不改成本快照（cost 是售出时快照，改价只影响收入侧、
      毛利随之变）；进货改 qty/price 是事实修改，均价随重放现算自然更新。
    - 每次修改自动写审计日志（action=update，before/after 整行 JSON 快照，
      可完整还原）；校验失败不写审计、不改行（单事务原子）。
    - 无修改字段（只传 --tx）→ 业务错误，不写审计。
    """
    # 传了哪些改哪些：None = 未传；customer 空串视为未传（不支持清空往来单位）。
    # changed 只在字段值实际变化时才记（同名往来单位/同值字段不产生 UPDATE，
    # 也不写审计——audit 只记真实修改，避免 before==after 的永久无意义行）。
    if tx_id is None:
        raise BusinessError("参数缺失: --tx")
    if qty is not None and qty <= 0:
        raise BusinessError("数量必须为正数")
    if price is not None and price < 0:
        raise BusinessError("单价不能为负")
    if date is not None and not DATE_RE.match(date.strip()):
        raise BusinessError("日期格式应为 YYYY-MM-DD")
    customer = (customer or "").strip() or None
    biz_date = date.strip() if date is not None else None
    if (
        qty is None and price is None and customer is None
        and biz_date is None and note is None
    ):
        raise BusinessError(
            "参数缺失: 无修改字段（可改 --qty/--price/--customer/--date/--note）"
        )
    with conn:
        orig = _require_tx(conn, tx_id)
        cp_id = orig["counterparty_id"]
        if customer is not None:
            role_sale = orig["biz_type"] in ("sale", "sale_return")
            cp_id = _resolve_counterparty(
                conn, customer, customer=role_sale, supplier=not role_sale,
            )
        sets: list[str] = []
        params: list[Any] = []
        changed: list[str] = []
        if qty is not None and qty != orig["qty"]:
            sets.append("qty = ?")
            params.append(qty)
            changed.append("qty")
        if price is not None and price != orig["price"]:
            sets.append("price = ?")
            params.append(price)
            changed.append("price")
        if cp_id != orig["counterparty_id"]:
            sets.append("counterparty_id = ?")
            params.append(cp_id)
            changed.append("customer")
        if biz_date is not None and biz_date != orig["biz_date"]:
            sets.append("biz_date = ?")
            params.append(biz_date)
            changed.append("date")
        if note is not None and note != orig["note"]:
            sets.append("note = ?")
            params.append(note)
            changed.append("note")
        if not sets:
            raise BusinessError("无实际修改字段（提供的值与当前相同），未写入审计")
        params.append(tx_id)
        conn.execute(f"UPDATE transactions SET {', '.join(sets)} WHERE id = ?", params)
        updated = conn.execute(
            "SELECT * FROM transactions WHERE id = ?", (tx_id,)
        ).fetchone()
        audit_id = _write_audit(
            conn, "transactions", tx_id, "update", orig, updated,
            f"edit transactions #{tx_id}: {', '.join(changed)}",
        )
    product = conn.execute(
        "SELECT drawing_no, name, unit FROM products WHERE id = ?",
        (updated["product_id"],),
    ).fetchone()
    # 回显键随单类型：售出/售出退货 → customer，进货/进货退货 → supplier（与写入命令一致）
    role_key = "customer" if updated["biz_type"] in ("sale", "sale_return") else "supplier"
    extra = {role_key: _counterparty_name(conn, updated["counterparty_id"])}
    return _tx_with_product(
        conn, tx_id, product, audit_id=audit_id, changed=changed, **extra,
    )


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


def _credit_balances(
    conn: sqlite3.Connection, name: str | None = None
) -> list[dict[str, Any]]:
    """全部挂账单位期末欠款（= 挂账交易累计 − 收付款累计，全历史现算，spec D6）。

    退货（purchase_return/sale_return）按负金额计入（T5 定案）；未挂账单位不进
    总览；欠款降序。query credit 总览与日报挂账块余额共用同一现算逻辑。
    """
    bal_sql = (
        "SELECT cp.name AS counterparty,"
        " COALESCE((SELECT SUM(CASE WHEN t.biz_type IN ('purchase_return', 'sale_return')"
        "                          THEN -t.qty * t.price ELSE t.qty * t.price END)"
        "           FROM transactions t WHERE t.counterparty_id = cp.id), 0)"
        " - COALESCE((SELECT SUM(p.amount) FROM payments p"
        "             WHERE p.counterparty_id = cp.id), 0) AS balance"
        " FROM counterparties cp WHERE cp.is_credit = 1"
    )
    params: list[Any] = []
    if name:
        bal_sql += " AND cp.name = ? COLLATE NOCASE"
        params.append(name)
    balances = [
        {"counterparty": r["counterparty"], "balance": round(r["balance"], ROUND)}
        for r in conn.execute(bal_sql, params).fetchall()
    ]
    balances.sort(key=lambda b: (-b["balance"], b["counterparty"]))
    return balances


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
    - 退货（purchase_return/sale_return）按负金额计入：挂账单位退货冲减欠款
      （T5 定案：红冲后退货进对账单与余额，欠款相应减少）。
    - 日期筛选只收窄 records——欠款余额是当前状态（现算），不受期间过滤影响。
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
        "         t.biz_type AS doc_type,"
        "         CASE WHEN t.biz_type IN ('purchase_return', 'sale_return')"
        "              THEN -t.qty * t.price ELSE t.qty * t.price END AS amount,"
        "         t.note AS note, 0 AS sort_group, t.id AS seq"
        "  FROM transactions t JOIN counterparties cp ON cp.id = t.counterparty_id"
        "  WHERE cp.is_credit = 1"
        "    AND t.biz_type IN ('purchase', 'sale', 'purchase_return', 'sale_return')"
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

    return {"records": records, "balances": _credit_balances(conn, name)}


def query_history(
    conn: sqlite3.Connection,
    drawing_no: str | None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict[str, Any]]:
    """单产品全部流水（spec D5：进货/售出/红冲逐笔），日期降序。

    任意条件组合过滤（产品必填 + 日期区间）；列 = 日期/类型/图号/名称/数量+单位/
    单价/金额/运费/成本/往来单位/备注/关联原单；退货行（purchase_return/sale_return）
    带「红冲」关联原单标记（ref = 原单 id/日期/类型，普通行 ref 为 None）。
    """
    drawing_no = _require_drawing_no(drawing_no)
    _validate_date_filters(from_date, to_date)
    sql = (
        "SELECT t.id, t.biz_date, t.biz_type, t.qty, t.price, t.freight, t.cost,"
        "       t.note, t.ref_id,"
        "       p.drawing_no, p.name, p.unit, cp.name AS counterparty,"
        "       r.biz_date AS ref_biz_date, r.biz_type AS ref_biz_type"
        " FROM transactions t"
        " JOIN products p ON p.id = t.product_id"
        " LEFT JOIN counterparties cp ON cp.id = t.counterparty_id"
        " LEFT JOIN transactions r ON r.id = t.ref_id"
        " WHERE p.drawing_no = ? COLLATE NOCASE"
    )
    params: list[Any] = [drawing_no]
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
                "biz_type": r["biz_type"],
                "drawing_no": r["drawing_no"],
                "name": r["name"],
                "unit": r["unit"],
                "qty": round(r["qty"], ROUND),
                "price": r["price"],
                "amount": round(r["qty"] * r["price"], ROUND),
                "freight": round(r["freight"] or 0.0, ROUND),
                "cost": round(r["cost"], ROUND) if r["cost"] is not None else None,
                "counterparty": r["counterparty"],
                "note": r["note"],
                "ref_id": r["ref_id"],
                "ref": _ref_dict(r),
            }
        )
    return rows


def _ref_dict(r: sqlite3.Row) -> dict[str, Any] | None:
    """红冲原单子对象（须已 SELECT ref_id/ref_biz_date/ref_biz_date 列）；原单为空返回 None。

    日报/周期汇总明细、export 流水、query history 共用同一形状（spec D6「红冲」标记）。
    """
    if r["ref_id"] is None:
        return None
    return {
        "id": r["ref_id"],
        "biz_date": r["ref_biz_date"],
        "biz_type": r["ref_biz_type"],
    }


def _detail_dict(r: sqlite3.Row) -> dict[str, Any]:
    """流水明细行（日报/周期汇总明细共用，spec D6 日报明细结构）。

    列 = 日期/类型/图号/名称/数量+单位/单价/金额/往来单位 + 退货行「红冲」ref 原单标记。
    """
    return {
        "id": r["id"],
        "biz_date": r["biz_date"],
        "biz_type": r["biz_type"],
        "drawing_no": r["drawing_no"],
        "name": r["name"],
        "unit": r["unit"],
        "qty": round(r["qty"], ROUND),
        "price": r["price"],
        "amount": round(r["qty"] * r["price"], ROUND),
        "counterparty": r["counterparty"],
        "ref": _ref_dict(r),
    }


def report_daily(
    conn: sqlite3.Connection, date: str | None = None
) -> dict[str, Any]:
    """日报（spec D6 四块，结构化 JSON，Fairy 以文字消息发四块，不产文件）。

    默认昨天；无业务日照发（汇总为零、异常区为空）；查询/报告类天然可重跑、
    无写入副作用。
    1. 当日汇总：进货/售出（笔数/总金额，当日退货净额冲减）、运费合计（全部流水
       freight 求和，退货 freight=0）、毛利合计 = 售出净额 − 成本快照净额
       （售出退货冲减当期毛利，D2；运费不进毛利）。
    2. 挂账变动（口径已确认）：新增挂账额 = 当日挂账单位交易净额（进货/售出正金额、
       退货负金额，与对账单 records 同口径）；收款/付款合计只算挂账单位的收付款；
       余额 = 全部挂账单位全历史现算（复用 _credit_balances）。
    3. 流水明细：当日进货+售出+退货逐笔（录入序），退货行带「红冲」ref 原单标记。
    4. 异常提醒：仅金额异常——当日售出单价偏离该商品上次成交价（商品全局、
       严格更早的最近一笔成交价，口径已确认）±30% 的流水列出供核对；
       无上次成交价或基准价为 0（无有效基准）时不判定；不设负库存提醒。
    """
    if date is None:
        biz_date = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    else:
        biz_date = date.strip()
        if not DATE_RE.match(biz_date):
            raise BusinessError("日期格式应为 YYYY-MM-DD")

    tx_rows = conn.execute(
        "SELECT biz_type, qty, price, freight, cost FROM transactions"
        " WHERE biz_date = ? AND biz_type IN"
        " ('purchase', 'sale', 'purchase_return', 'sale_return')",
        (biz_date,),
    ).fetchall()

    def _net(kind: str, ret_kind: str) -> tuple[int, float, float]:
        count = 0
        amount = 0.0
        cost = 0.0
        for t in tx_rows:
            if t["biz_type"] == kind:
                sign = 1
            elif t["biz_type"] == ret_kind:
                sign = -1
            else:
                continue
            count += sign
            amount += sign * t["qty"] * t["price"]
            cost += sign * t["qty"] * (t["cost"] or 0.0)
        return count, amount, cost

    p_count, p_amount, _ = _net("purchase", "purchase_return")
    s_count, s_amount, s_cost = _net("sale", "sale_return")
    freight = round(sum((t["freight"] or 0.0) for t in tx_rows), ROUND)

    new_credit = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN t.biz_type IN ('purchase_return', 'sale_return')"
        "                        THEN -t.qty * t.price ELSE t.qty * t.price END), 0)"
        " FROM transactions t JOIN counterparties cp ON cp.id = t.counterparty_id"
        " WHERE cp.is_credit = 1 AND t.biz_date = ?"
        "   AND t.biz_type IN ('purchase', 'sale', 'purchase_return', 'sale_return')",
        (biz_date,),
    ).fetchone()[0]
    received = conn.execute(
        "SELECT COALESCE(SUM(p.amount), 0) FROM payments p"
        " JOIN counterparties cp ON cp.id = p.counterparty_id"
        " WHERE cp.is_credit = 1 AND p.pay_type = 'receive' AND p.pay_date = ?",
        (biz_date,),
    ).fetchone()[0]
    paid = conn.execute(
        "SELECT COALESCE(SUM(p.amount), 0) FROM payments p"
        " JOIN counterparties cp ON cp.id = p.counterparty_id"
        " WHERE cp.is_credit = 1 AND p.pay_type = 'pay' AND p.pay_date = ?",
        (biz_date,),
    ).fetchone()[0]
    balance = round(sum(b["balance"] for b in _credit_balances(conn)), ROUND)

    details = []
    for r in conn.execute(
        "SELECT t.id, t.biz_date, t.biz_type, t.qty, t.price, t.ref_id,"
        "       p.drawing_no, p.name, p.unit, cp.name AS counterparty,"
        "       o.biz_date AS ref_biz_date, o.biz_type AS ref_biz_type"
        " FROM transactions t"
        " JOIN products p ON p.id = t.product_id"
        " LEFT JOIN counterparties cp ON cp.id = t.counterparty_id"
        " LEFT JOIN transactions o ON o.id = t.ref_id"
        " WHERE t.biz_date = ? AND t.biz_type IN"
        " ('purchase', 'sale', 'purchase_return', 'sale_return')"
        " ORDER BY t.id",
        (biz_date,),
    ).fetchall():
        details.append(_detail_dict(r))

    alerts = []
    for r in conn.execute(
        "SELECT t.id, t.product_id, t.qty, t.price, t.biz_date,"
        "       p.drawing_no, p.name"
        " FROM transactions t JOIN products p ON p.id = t.product_id"
        " WHERE t.biz_date = ? AND t.biz_type = 'sale' ORDER BY t.id",
        (biz_date,),
    ).fetchall():
        last = conn.execute(
            "SELECT price FROM transactions"
            " WHERE biz_type = 'sale' AND product_id = ?"
            "   AND (biz_date < ? OR (biz_date = ? AND id < ?))"
            " ORDER BY biz_date DESC, id DESC LIMIT 1",
            (r["product_id"], r["biz_date"], r["biz_date"], r["id"]),
        ).fetchone()
        if last is None or last["price"] == 0:
            continue
        deviation = (r["price"] - last["price"]) / last["price"]
        if abs(deviation) >= 0.3:
            alerts.append(
                {
                    "id": r["id"],
                    "drawing_no": r["drawing_no"],
                    "name": r["name"],
                    "qty": round(r["qty"], ROUND),
                    "price": r["price"],
                    "last_price": last["price"],
                    "deviation": round(deviation, ROUND),
                }
            )

    return {
        "date": biz_date,
        "summary": {
            "purchase": {"count": p_count, "amount": round(p_amount, ROUND)},
            "sale": {"count": s_count, "amount": round(s_amount, ROUND)},
            "freight": freight,
            "margin": round(s_amount - s_cost, ROUND),
        },
        "credit": {
            "new_credit": round(new_credit, ROUND),
            "received": round(received, ROUND),
            "paid": round(paid, ROUND),
            "balance": balance,
        },
        "details": details,
        "alerts": alerts,
    }


def _parse_period(from_date: str | None, to_date: str | None) -> tuple[str, str]:
    """期间参数解析（query margin / report period 共用）：缺省默认本月；
    --from/--to 须成对给出；YYYY-MM-DD 格式；起≤止。返回 (from, to)。"""
    today = datetime.date.today()
    if from_date is None and to_date is None:
        return f"{today.year:04d}-{today.month:02d}-01", today.isoformat()
    if from_date is None or to_date is None:
        raise BusinessError("参数缺失: --from 与 --to 须成对给出（缺省默认本月）")
    from_date, to_date = from_date.strip(), to_date.strip()
    if not (DATE_RE.match(from_date) and DATE_RE.match(to_date)):
        raise BusinessError("日期格式应为 YYYY-MM-DD")
    if from_date > to_date:
        raise BusinessError("起始日期不能晚于截止日期")
    return from_date, to_date


def _margin_grouped(
    conn: sqlite3.Connection,
    cond: str,
    params: list[Any],
    *,
    with_qty: bool = False,
) -> tuple[float, float, list[dict[str, Any]], list[dict[str, Any]]]:
    """期间毛利核心（query margin / report period / export 毛利 sheet 共用，口径单一来源）。

    cond/params 已含全部过滤条件（如 `t.biz_date BETWEEN ? AND ?`、产品/客户过滤）；
    口径：sale 正、sale_return 负（售出退货按原单冲减当期）；毛利 = 金额 − 成本快照
    合计；毛利率 = 毛利 ÷ 金额（无售出时 null）。返回 (售出净额, 成本净额,
    按产品分组, 按客户分组)，分组均毛利降序。with_qty=True 时产品分组多
    「售出数量」列（period 报表用），query margin 不带该列。
    """
    amount = 0.0
    cost = 0.0
    for t in conn.execute(
        f"SELECT biz_type, qty, price, cost FROM transactions t"
        f" WHERE t.biz_type IN ('sale', 'sale_return') AND {cond}",
        params,
    ).fetchall():
        sign = -1 if t["biz_type"] == "sale_return" else 1
        amount += sign * t["qty"] * t["price"]
        cost += sign * t["qty"] * (t["cost"] or 0.0)

    amount_sql = (
        "SUM(CASE WHEN t.biz_type = 'sale' THEN t.qty * t.price"
        "         ELSE -t.qty * t.price END)"
    )
    cost_sql = (
        "SUM(CASE WHEN t.biz_type = 'sale' THEN t.qty * COALESCE(t.cost, 0)"
        "         ELSE -t.qty * COALESCE(t.cost, 0) END)"
    )
    qty_sql = "SUM(CASE WHEN t.biz_type = 'sale' THEN t.qty ELSE -t.qty END)"

    def _margin_rows(sql: str, params: list[Any]) -> list[dict[str, Any]]:
        rows = []
        for r in conn.execute(sql, params).fetchall():
            g_amount = r["amount"]
            g_cost = r["cost"]
            g_margin = g_amount - g_cost
            row = dict(r)
            row.pop("amount", None)
            row.pop("cost", None)
            row["amount"] = round(g_amount, ROUND)
            row["cost"] = round(g_cost, ROUND)
            row["margin"] = round(g_margin, ROUND)
            row["margin_rate"] = round(g_margin / g_amount, ROUND) if g_amount else None
            rows.append(row)
        return rows

    product_cols = "p.drawing_no, p.name"
    if with_qty:
        product_cols += f", {qty_sql} AS sale_qty"
    by_product = _margin_rows(
        f"SELECT {product_cols}, {amount_sql} AS amount, {cost_sql} AS cost"
        f" FROM transactions t JOIN products p ON p.id = t.product_id"
        f" WHERE t.biz_type IN ('sale', 'sale_return') AND {cond}"
        f" GROUP BY t.product_id",
        params,
    )
    by_product.sort(key=lambda g: (-g["margin"], g["drawing_no"]))
    by_customer = _margin_rows(
        f"SELECT cp.name AS customer, {amount_sql} AS amount, {cost_sql} AS cost"
        f" FROM transactions t LEFT JOIN counterparties cp ON cp.id = t.counterparty_id"
        f" WHERE t.biz_type IN ('sale', 'sale_return') AND {cond}"
        f" GROUP BY cp.name",
        params,
    )
    by_customer.sort(key=lambda g: (-g["margin"], g["customer"] or ""))
    return amount, cost, by_product, by_customer


def query_margin(
    conn: sqlite3.Connection,
    from_date: str | None = None,
    to_date: str | None = None,
    product: str | None = None,
    customer: str | None = None,
) -> dict[str, Any]:
    """期间毛利报表（spec D5/D6）：金额/成本/毛利/毛利率 + 按产品/客户分组。

    - 期间：--from/--to 成对给出、缺省默认本月（1 号至今天）；格式校验 + 起≤止。
    - 口径：期间售出金额/成本 = sale 正、sale_return 负（售出退货按原单冲减当期，
      sale_return 行在期间内即计入，负方向）；毛利 = 金额 − 成本（快照合计）；
      毛利率 = 毛利 ÷ 售出金额（无售出时 null）。
    - 分组：按产品（图号/名称）与按客户（现结归入 customer=null 组），均为
      金额/成本/毛利/毛利率、毛利降序；--product/--customer 过滤作用于全报表。
    - 分组计算复用 _margin_grouped（与 report period / export 毛利 sheet 同口径）。
    """
    from_date, to_date = _parse_period(from_date, to_date)

    product_id = None
    if product:
        product_id = _require_product(conn, (product or "").strip())["id"]
    cp_id: int | None = None
    if customer:
        customer = customer.strip()
        row = conn.execute(
            "SELECT id FROM counterparties WHERE name = ? COLLATE NOCASE", (customer,)
        ).fetchone()
        cp_id = row["id"] if row is not None else -1  # 无此客户 → 过滤后空结果

    cond = "t.biz_date BETWEEN ? AND ?"
    params: list[Any] = [from_date, to_date]
    if product_id is not None:
        cond += " AND t.product_id = ?"
        params.append(product_id)
    if cp_id is not None:
        cond += " AND t.counterparty_id = ?"
        params.append(cp_id)

    amount, cost, by_product, by_customer = _margin_grouped(conn, cond, params)
    margin = amount - cost
    return {
        "from": from_date,
        "to": to_date,
        "amount": round(amount, ROUND),
        "cost": round(cost, ROUND),
        "margin": round(margin, ROUND),
        "margin_rate": round(margin / amount, ROUND) if amount else None,
        "by_product": by_product,
        "by_customer": by_customer,
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


def _stock_state(
    conn: sqlite3.Connection,
    product_id: int,
    *,
    before_date: str | None = None,
    until_date: str | None = None,
) -> tuple[float, float, float]:
    """重放期初 + 流水，现算某商品的 (数量, 当前加权均价, 结存金额)（ADR 0001/0004）。

    期初行按录入序聚合为基数（数量 Σqty、金额 Σqty×cost）；流水按
    biz_date + id（同日按录入序）逐笔推进，只有进货改变结存金额与数量并重算均价。
    before_date 时只推进 biz_date < before_date 的流水（周期汇总期初口径）、
    until_date 时只推进 biz_date <= until_date 的流水（周期汇总期末口径）；
    两者缺省 = 全历史（当前状态）。
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
    tx_cond = "product_id = ?"
    tx_params: list[Any] = [product_id]
    if before_date is not None:
        tx_cond += " AND biz_date < ?"
        tx_params.append(before_date)
    if until_date is not None:
        tx_cond += " AND biz_date <= ?"
        tx_params.append(until_date)
    for t in conn.execute(
        f"SELECT biz_type, qty, price, cost FROM transactions"
        f" WHERE {tx_cond} ORDER BY biz_date, id",
        tx_params,
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


def export_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    """整库快照数据（spec D6 导出 5-Sheet，导出时点现算，口径已确认）。

    - transactions: 全部业务流水（biz_date/id 序），退货带红冲原单信息（ref）。
    - products: 图号/名称/单位/别名（、拼接，别名按录入序）。
    - counterparties: 名称/类型标记/挂账标记/联系方式（地区+电话拼接）。
    - stock: 当前库存（复用 query_stock 现算）。
    - margin: 按产品分组的全历史毛利汇总（整库快照语义，毛利降序）。
    """
    flow = []
    for r in conn.execute(
        "SELECT t.id, t.biz_date, t.biz_type, t.qty, t.price, t.freight, t.cost,"
        "       t.note, t.ref_id, p.drawing_no, p.name, p.unit,"
        "       cp.name AS counterparty,"
        "       o.biz_date AS ref_biz_date, o.biz_type AS ref_biz_type"
        " FROM transactions t"
        " JOIN products p ON p.id = t.product_id"
        " LEFT JOIN counterparties cp ON cp.id = t.counterparty_id"
        " LEFT JOIN transactions o ON o.id = t.ref_id"
        " ORDER BY t.biz_date, t.id"
    ).fetchall():
        flow.append(
            {
                "id": r["id"],
                "biz_date": r["biz_date"],
                "biz_type": r["biz_type"],
                "drawing_no": r["drawing_no"],
                "name": r["name"],
                "unit": r["unit"],
                "qty": round(r["qty"], ROUND),
                "price": r["price"],
                "amount": round(r["qty"] * r["price"], ROUND),
                "freight": round(r["freight"] or 0.0, ROUND),
                "cost": round(r["cost"], ROUND) if r["cost"] is not None else None,
                "counterparty": r["counterparty"],
                "note": r["note"],
                "ref": _ref_dict(r),
            }
        )

    products_rows = []
    for r in conn.execute(
        "SELECT p.drawing_no, p.name, p.unit,"
        " (SELECT GROUP_CONCAT(alias, '、') FROM (SELECT alias FROM aliases"
        "   WHERE product_id = p.id ORDER BY id)) AS aliases"
        " FROM products p ORDER BY p.drawing_no"
    ).fetchall():
        products_rows.append(
            {
                "drawing_no": r["drawing_no"],
                "name": r["name"],
                "unit": r["unit"],
                "aliases": r["aliases"] or "",
            }
        )

    cp_rows = []
    for r in conn.execute(
        "SELECT name, is_customer, is_supplier, is_credit, region, phone"
        " FROM counterparties ORDER BY name"
    ).fetchall():
        roles = []
        if r["is_customer"]:
            roles.append("客户")
        if r["is_supplier"]:
            roles.append("供应商")
        cp_rows.append(
            {
                "name": r["name"],
                "roles": "+".join(roles),
                "is_credit": r["is_credit"],
                "contact": f"{r['region'] or ''} {r['phone'] or ''}".strip(),
            }
        )

    # 毛利 sheet：按产品分组的全历史毛利汇总（口径已确认：整库快照语义）
    _, _, margin_rows, _ = _margin_grouped(conn, "1 = 1", [])
    return {
        "transactions": flow,
        "products": products_rows,
        "counterparties": cp_rows,
        "stock": query_stock(conn),
        "margin": margin_rows,
    }


def period_report(
    conn: sqlite3.Connection,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    """周期汇总数据（spec D6 周期汇总四块，周/月/年同一套结构、仅区间不同）。

    - 期间：--from/--to 成对给出、缺省默认本月（与 query margin 同规则）；起≤止。
    - 汇总：进货/售出笔数与金额按期间退货净额冲减（与日报同口径）；运费按类型单列
      求和（进货只加 purchase 行、售出只加 sale 行，退货 freight=0）；毛利 =
      售出净额 − 成本快照净额（复用 _margin_grouped，运费不进毛利）。
    - 期初/期末库存金额（口径已确认）：期初 = 截止 from 前一日（biz_date < from）
      重放现算、期末 = 截止 to（biz_date <= to）重放现算，按全部商品合计；
      期初录入恒为基数。期末 − 期初 = 期间净业务变动，与期间汇总自洽。
    - 分组：按产品（含售出数量列）/按客户，毛利降序（TOP 10 由写入侧截取）。
    - 明细：期间全部流水逐笔（biz_date/id 序，形状同日报明细，退货带红冲原单标记）。
    """
    from_date, to_date = _parse_period(from_date, to_date)

    tx_rows = conn.execute(
        "SELECT biz_type, qty, price, freight, cost FROM transactions"
        " WHERE biz_date BETWEEN ? AND ?"
        " AND biz_type IN ('purchase', 'sale', 'purchase_return', 'sale_return')",
        (from_date, to_date),
    ).fetchall()

    def _net(kind: str, ret_kind: str) -> tuple[int, float, float]:
        count = 0
        amount = 0.0
        freight = 0.0
        for t in tx_rows:
            if t["biz_type"] == kind:
                sign = 1
                freight += t["freight"] or 0.0
            elif t["biz_type"] == ret_kind:
                sign = -1
            else:
                continue
            count += sign
            amount += sign * t["qty"] * t["price"]
        return count, amount, freight

    p_count, p_amount, p_freight = _net("purchase", "purchase_return")
    s_count, s_amount, s_freight = _net("sale", "sale_return")

    cond = "t.biz_date BETWEEN ? AND ?"
    params: list[Any] = [from_date, to_date]
    amount, cost, by_product, by_customer = _margin_grouped(
        conn, cond, params, with_qty=True
    )

    opening_inventory = 0.0
    closing_inventory = 0.0
    for p in conn.execute("SELECT id FROM products").fetchall():
        opening_inventory += _stock_state(conn, p["id"], before_date=from_date)[2]
        closing_inventory += _stock_state(conn, p["id"], until_date=to_date)[2]

    details = []
    for r in conn.execute(
        "SELECT t.id, t.biz_date, t.biz_type, t.qty, t.price, t.ref_id,"
        "       p.drawing_no, p.name, p.unit, cp.name AS counterparty,"
        "       o.biz_date AS ref_biz_date, o.biz_type AS ref_biz_type"
        " FROM transactions t"
        " JOIN products p ON p.id = t.product_id"
        " LEFT JOIN counterparties cp ON cp.id = t.counterparty_id"
        " LEFT JOIN transactions o ON o.id = t.ref_id"
        " WHERE t.biz_date BETWEEN ? AND ?"
        " AND t.biz_type IN ('purchase', 'sale', 'purchase_return', 'sale_return')"
        " ORDER BY t.biz_date, t.id",
        (from_date, to_date),
    ).fetchall():
        details.append(_detail_dict(r))

    return {
        "from": from_date,
        "to": to_date,
        "summary": {
            "purchase": {"count": p_count, "amount": round(p_amount, ROUND),
                         "freight": round(p_freight, ROUND)},
            "sale": {"count": s_count, "amount": round(s_amount, ROUND),
                     "freight": round(s_freight, ROUND)},
            "margin": round(amount - cost, ROUND),
            "opening_inventory": round(opening_inventory, ROUND),
            "closing_inventory": round(closing_inventory, ROUND),
        },
        "by_product": by_product,
        "by_customer": by_customer,
        "details": details,
    }
