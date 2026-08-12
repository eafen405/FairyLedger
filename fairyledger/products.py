"""商品主数据：建档、当前库存、商品检索（T1 范围）。"""

import re
import sqlite3
from typing import Any

from .errors import BusinessError

# 自编图号：ZC-<递增数字>，与厂家号共用 drawing_no 列（spec D1）。
ZC_RE = re.compile(r"^ZC-(\d+)$", re.IGNORECASE)
# 检索词段拆分：按「数字/字母（含 . _ - 等图号常见符号）」与「中文等其余字符」的边界切开。
# 例："170活塞" → ["170", "活塞"]；"300.14.14缸盖" → ["300.14.14", "缸盖"]。
SEGMENT_RE = re.compile(r"[A-Za-z0-9._\-]+|[^\sA-Za-z0-9._\-]+")


def next_zc_number(conn: sqlite3.Connection) -> str:
    """下一个自编图号：现有 ZC-<n> 的最大值 +1；无则从 ZC-1 起。"""
    max_n = 0
    for (drawing_no,) in conn.execute("SELECT drawing_no FROM products").fetchall():
        m = ZC_RE.match(drawing_no)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"ZC-{max_n + 1}"


def add_product(
    conn: sqlite3.Connection,
    drawing_no: str | None,
    name: str | None,
    unit: str | None,
    aliases: list[str] | None,
) -> dict[str, Any]:
    """建档：有图号按厂家号、缺图号自编 ZC-；别名 0..N 去重；单位纯文本默认 '件'。

    单事务原子写入（商品 + 别名），图号唯一冲突视为可预期业务错误。
    """
    if not name or not name.strip():
        raise BusinessError("参数缺失: --name")
    drawing_no = (drawing_no or "").strip() or next_zc_number(conn)
    unit = (unit or "").strip() or "件"
    alias_list = list(dict.fromkeys(a.strip() for a in (aliases or []) if a.strip()))
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO products (drawing_no, name, unit) VALUES (?, ?, ?)",
                (drawing_no, name, unit),
            )
            pid = cur.lastrowid
            for alias in alias_list:
                conn.execute(
                    "INSERT INTO aliases (product_id, alias) VALUES (?, ?)",
                    (pid, alias),
                )
    except sqlite3.IntegrityError as exc:
        raise BusinessError(f"图号已存在: {drawing_no}") from exc
    row = conn.execute(
        "SELECT id, drawing_no, name, unit, created_at FROM products WHERE id = ?",
        (pid,),
    ).fetchone()
    return {**dict(row), "aliases": alias_list}


def query_stock(
    conn: sqlite3.Connection, drawing_no: str | None = None
) -> list[dict[str, Any]]:
    """当前库存：期初合计 + 流水净量现算（ADR 0001），默认图号序。

    流水方向按 biz_type 定符号：purchase/sale_return 加、sale/purchase_return 减。
    """
    sql = """
        SELECT p.id, p.drawing_no, p.name, p.unit,
               COALESCE((SELECT SUM(o.qty) FROM opening_stock o
                          WHERE o.product_id = p.id), 0)
             + COALESCE((SELECT SUM(CASE t.biz_type
                             WHEN 'purchase' THEN t.qty
                             WHEN 'sale_return' THEN t.qty
                             WHEN 'sale' THEN -t.qty
                             WHEN 'purchase_return' THEN -t.qty END)
                          FROM transactions t WHERE t.product_id = p.id), 0) AS qty
          FROM products p
    """
    params: list[Any] = []
    if drawing_no:
        sql += " WHERE p.drawing_no = ? COLLATE NOCASE"
        params.append(drawing_no)
    sql += " ORDER BY p.drawing_no"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _aliases(conn: sqlite3.Connection, product_id: int) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT alias FROM aliases WHERE product_id = ? ORDER BY id",
            (product_id,),
        ).fetchall()
    ]


def _product_with_aliases(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    return {**dict(row), "aliases": _aliases(conn, row["id"])}


def search_products(conn: sqlite3.Connection, query: str) -> dict[str, Any]:
    """商品检索（T1 验收）：图号精确命中优先；否则词段在 图号/名称/别名 间 AND 包含匹配。

    返回 {"match": "exact"|"multiple"|"none", "products": [...]}——
    唯一命中返回 exact（Fairy 直接确认回显图号），多命中返回候选数组（Fairy 列序号）。
    """
    q = query.strip()
    if not q:
        raise BusinessError("检索词为空")
    # 1) 图号精确匹配优先（大小写不敏感，口述友好）
    row = conn.execute(
        "SELECT * FROM products WHERE drawing_no = ? COLLATE NOCASE", (q,)
    ).fetchone()
    if row is not None:
        return {"match": "exact", "products": [_product_with_aliases(conn, row)]}
    # 2) 词段 AND 匹配：每个词段命中 图号/名称/别名 任一字段才算该商品命中
    segments = [s for s in SEGMENT_RE.findall(q)]
    hits: list[sqlite3.Row] = []
    for p in conn.execute("SELECT * FROM products ORDER BY drawing_no").fetchall():
        fields = [p["drawing_no"], p["name"]] + _aliases(conn, p["id"])
        blob = " ".join(fields).lower()
        if all(s.lower() in blob for s in segments):
            hits.append(p)
    if not hits:
        return {"match": "none", "products": []}
    if len(hits) == 1:
        return {"match": "exact", "products": [_product_with_aliases(conn, hits[0])]}
    return {
        "match": "multiple",
        "products": [_product_with_aliases(conn, p) for p in hits],
    }
