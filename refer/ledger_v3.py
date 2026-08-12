#!/usr/bin/env python3
"""柴油机配件进销存台账 v3（共识版）。
SQLite 单文件 + 标准库 CLI。Fairy 飞书代录调用。

核心模型：商品(图号+名称) / 往来单位(挂账标记) / 流水(进/出/红冲) / 收付款(年底销账)
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import date, datetime

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger_v3.db")


def connect():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY,
        drawing_no TEXT UNIQUE NOT NULL,   -- 图号（唯一查询凭据）
        name TEXT NOT NULL,                -- 名称（俗称）
        unit TEXT DEFAULT '件',
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS counterparties(
        id INTEGER PRIMARY KEY,
        ctype TEXT NOT NULL,               -- customer / supplier
        name TEXT NOT NULL,
        region TEXT,
        phone TEXT,
        is_credit INTEGER DEFAULT 0,       -- 挂账标记
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS transactions(
        id INTEGER PRIMARY KEY,
        biz_date TEXT NOT NULL,            -- YYYY-MM-DD
        biz_type TEXT NOT NULL,            -- purchase/sale/purchase_return/sale_return
        product_id INTEGER NOT NULL,
        qty REAL NOT NULL,
        price REAL NOT NULL,
        amount REAL NOT NULL,              -- qty*price 自动算
        freight REAL DEFAULT 0,            -- 运费（仅售出单使用，进货为0）
        counterparty_id INTEGER,
        ref_id INTEGER,                    -- 红冲关联的原单 id
        note TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY(product_id) REFERENCES products(id)
    );
    CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY,
        pay_date TEXT NOT NULL,
        pay_type TEXT NOT NULL,            -- receive 收客户款 / pay 付供应商款
        counterparty_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        note TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    """)
    # 旧库迁移：给已存在的 transactions 表补 freight 列（幂等）
    cols = [r[1] for r in con.execute("PRAGMA table_info(transactions)").fetchall()]
    if "freight" not in cols:
        con.execute("ALTER TABLE transactions ADD COLUMN freight REAL DEFAULT 0")
    con.commit()
    return con


# ---------- 工具 ----------
def get_or_create_product(con, drawing_no, name, unit="件", note=""):
    row = con.execute("SELECT * FROM products WHERE drawing_no=?", (drawing_no,)).fetchone()
    if row:
        return row["id"], False
    cur = con.execute("INSERT INTO products(drawing_no,name,unit,note) VALUES(?,?,?,?)",
                      (drawing_no, name, unit, note))
    return cur.lastrowid, True


def get_or_create_cp(con, ctype, name, region="", phone="", is_credit=0):
    row = con.execute("SELECT * FROM counterparties WHERE ctype=? AND name=?",
                      (ctype, name)).fetchone()
    if row:
        return row["id"], False, row
    cur = con.execute(
        "INSERT INTO counterparties(ctype,name,region,phone,is_credit) VALUES(?,?,?,?,?)",
        (ctype, name, region, phone, is_credit))
    return cur.lastrowid, True, None


def find_product(con, keyword):
    """图号精确优先，其次名称模糊。"""
    row = con.execute("SELECT * FROM products WHERE drawing_no=?", (keyword,)).fetchone()
    if row:
        return row
    rows = con.execute("SELECT * FROM products WHERE name LIKE ? LIMIT 5",
                       (f"%{keyword}%",)).fetchall()
    return rows


def stock_qty(con, product_id, asof=None):
    """库存 = 进 + 退进调整 − 出 − 退出调整（红冲自动反号）。"""
    cond = "biz_date<=?" if asof else "1=1"
    args = (asof,) if asof else ()
    row = con.execute(
        f"""SELECT COALESCE(SUM(CASE WHEN biz_type='purchase' THEN qty
                             WHEN biz_type='sale' THEN -qty
                             WHEN biz_type='sale_return' THEN qty
                             WHEN biz_type='purchase_return' THEN -qty
                             ELSE 0 END),0) AS q
            FROM transactions WHERE product_id=? AND {cond}""",
        (product_id,) + args).fetchone()
    return row["q"]


# ---------- 业务命令 ----------
def cmd_product_add(con, a):
    pid, created = get_or_create_product(con, a.drawing_no, a.name, a.unit, a.note)
    con.commit()
    print(f"{'新建' if created else '已存在'}商品: {a.drawing_no} {a.name} ({a.unit}) id={pid}")
    return 0


def cmd_sell(con, a):
    prods = find_product(con, a.product)
    if not prods:
        print(f"错误: 商品 '{a.product}' 不存在。请先 product-add（图号必填）")
        return 1
    p = prods[0] if isinstance(prods, list) else prods
    if isinstance(prods, list) and len(prods) > 1:
        print("匹配到多个商品，请用图号精确指定：")
        for r in prods:
            print(f"  {r['drawing_no']}  {r['name']}")
        return 1
    cid, created, _ = get_or_create_cp(con, "customer", a.customer or "散客",
                                       a.region or "", a.phone or "", int(a.credit))
    amount = round(a.qty * a.price, 2)
    freight = round(a.freight, 2)
    cur = con.execute(
        "INSERT INTO transactions(biz_date,biz_type,product_id,qty,price,amount,freight,counterparty_id,note)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (a.date or date.today().isoformat(), "sale", p["id"], a.qty, a.price, amount, freight, cid, a.note))
    tid = cur.lastrowid
    # 现结客户：自动记为已收款（交易即清）；挂账客户：挂应收
    cp_row = con.execute("SELECT is_credit FROM counterparties WHERE id=?", (cid,)).fetchone()
    if cp_row and cp_row["is_credit"]:
        tag = "（挂账）"
    else:
        con.execute(
            "INSERT INTO payments(pay_date,pay_type,counterparty_id,amount,note) VALUES(?,?,?,?,?)",
            (a.date or date.today().isoformat(), "receive", cid, amount, f"现结自动#{tid}"))
        tag = "（现结已收）"
    con.commit()
    f_s = f" +运费 {freight}" if freight else ""
    print(f"售出 {a.date or '今天'}: {p['name']}({p['drawing_no']}) ×{a.qty} @{a.price} = {amount} 元{f_s} → {a.customer or '散客'}{tag} [单号#{tid}]")
    return 0


def cmd_buy(con, a):
    p = find_product(con, a.product)
    if isinstance(p, list):
        if len(p) != 1:
            print("匹配到多个商品，请用图号精确指定")
            return 1
        p = p[0]
    if not p:
        print(f"错误: 商品 '{a.product}' 不存在")
        return 1
    cid, created, _ = get_or_create_cp(con, "supplier", a.supplier or "厂家",
                                       a.region or "", a.phone or "", int(a.credit))
    amount = round(a.qty * a.price, 2)
    cur = con.execute(
        "INSERT INTO transactions(biz_date,biz_type,product_id,qty,price,amount,counterparty_id,note)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (a.date or date.today().isoformat(), "purchase", p["id"], a.qty, a.price, amount, cid, a.note))
    tid = cur.lastrowid
    cp_row = con.execute("SELECT is_credit FROM counterparties WHERE id=?", (cid,)).fetchone()
    if cp_row and cp_row["is_credit"]:
        tag = "（挂账）"
    else:
        con.execute(
            "INSERT INTO payments(pay_date,pay_type,counterparty_id,amount,note) VALUES(?,?,?,?,?)",
            (a.date or date.today().isoformat(), "pay", cid, amount, f"现结自动#{tid}"))
        tag = "（现结已付）"
    con.commit()
    print(f"进货 {a.date or '今天'}: {p['name']}({p['drawing_no']}) ×{a.qty} @{a.price} = {amount} 元 ← {a.supplier or '厂家'}{tag} [单号#{tid}]")
    return 0


def cmd_return(con, a):
    """红冲：关联原单，类型反向，数量/金额自动取反。"""
    orig = con.execute("SELECT * FROM transactions WHERE id=?", (a.id,)).fetchone()
    if not orig:
        print(f"错误: 原单 #{a.id} 不存在")
        return 1
    if orig["ref_id"]:
        print(f"错误: #{a.id} 已是红冲单，不能再次红冲")
        return 1
    rtype = {"purchase": "purchase_return", "sale": "sale_return",
             "purchase_return": "purchase", "sale_return": "sale"}[orig["biz_type"]]
    qty = a.qty or orig["qty"]
    cur = con.execute(
        "INSERT INTO transactions(biz_date,biz_type,product_id,qty,price,amount,freight,counterparty_id,ref_id,note)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (a.date or date.today().isoformat(), rtype, orig["product_id"], qty, orig["price"],
         round(qty * orig["price"], 2), orig["freight"] or 0, orig["counterparty_id"], orig["id"],
         a.note or f"红冲#{orig['id']}"))
    con.commit()
    print(f"红冲成功: #{a.id} → #{cur.lastrowid}（{rtype}，数量 {qty}，库存已回补）")
    return 0


def cmd_stock(con, a):
    print(f"{'图号':<16}{'名称':<14}{'单位':<4}{'库存':>8}")
    rows = con.execute("SELECT * FROM products ORDER BY drawing_no").fetchall()
    for p in rows:
        q = stock_qty(con, p["id"], a.date)
        if a.all or q != 0:
            flag = " ⚠️负库存" if q < 0 else ""
            print(f"{p['drawing_no']:<16}{p['name']:<14}{p['unit']:<4}{q:>8.1f}{flag}")
    return 0


def _debt_summary(con):
    """应收（客户欠我）与应付（我欠供应商）汇总。"""
    rows = con.execute("""
      SELECT c.id, c.name, c.ctype,
        COALESCE(SUM(CASE WHEN t.biz_type='sale' THEN t.amount
                          WHEN t.biz_type='sale_return' THEN -t.amount ELSE 0 END),0) AS 应收,
        COALESCE(SUM(CASE WHEN t.biz_type='purchase' THEN t.amount
                          WHEN t.biz_type='purchase_return' THEN -t.amount ELSE 0 END),0) AS 应付,
        COALESCE((SELECT SUM(amount) FROM payments WHERE counterparty_id=c.id AND pay_type='receive'),0) AS 已收,
        COALESCE((SELECT SUM(amount) FROM payments WHERE counterparty_id=c.id AND pay_type='pay'),0) AS 已付
      FROM counterparties c LEFT JOIN transactions t ON t.counterparty_id=c.id
      GROUP BY c.id ORDER BY c.ctype, c.name
    """).fetchall()
    return rows


def cmd_debt(con, a):
    rows = _debt_summary(con)
    if not rows:
        print("暂无往来单位")
        return 0
    print("=== 应收（客户欠我）===")
    for r in rows:
        if r["ctype"] != "customer":
            continue
        bal = r["应收"] - r["已收"]
        if r["应收"] or a.all:
            print(f"  {r['name']:<12} 应收 {r['应收']:>10.2f}  已收 {r['已收']:>10.2f}  欠款 {bal:>10.2f}")
    print("=== 应付（我欠供应商）===")
    for r in rows:
        if r["ctype"] != "supplier":
            continue
        bal = r["应付"] - r["已付"]
        if r["应付"] or a.all:
            print(f"  {r['name']:<12} 应付 {r['应付']:>10.2f}  已付 {r['已付']:>10.2f}  欠款 {bal:>10.2f}")
    return 0


def cmd_pay(con, a):
    """收/付款销账：pay receive 客户名 金额 / pay pay 供应商名 金额"""
    ctype = "customer" if a.pay_type == "receive" else "supplier"
    row = con.execute("SELECT * FROM counterparties WHERE ctype=? AND name=?",
                      (ctype, a.name)).fetchone()
    if not row:
        print(f"错误: {'客户' if ctype=='customer' else '供应商'} '{a.name}' 不存在")
        return 1
    cur = con.execute(
        "INSERT INTO payments(pay_date,pay_type,counterparty_id,amount,note) VALUES(?,?,?,?,?)",
        (a.date or date.today().isoformat(), a.pay_type, row["id"], a.amount, a.note))
    con.commit()
    verb = "收款" if a.pay_type == "receive" else "付款"
    print(f"{verb}: {a.name} {a.amount} 元（{a.date or '今天'}）[凭证#{cur.lastrowid}]")
    return 0


def cmd_daily(con, a):
    d = a.date or date.today().isoformat()
    rows = con.execute("SELECT * FROM transactions WHERE biz_date=? ORDER BY id", (d,)).fetchall()
    total_sale = sum(r["amount"] for r in rows if r["biz_type"] == "sale")
    total_buy = sum(r["amount"] for r in rows if r["biz_type"] == "purchase")
    total_freight = sum(r["freight"] or 0 for r in rows if r["biz_type"] == "sale")
    print(f"=== 日报 {d} ===")
    if not rows:
        print("无记录")
        return 0
    for r in rows:
        p = con.execute("SELECT name, note FROM products WHERE id=?", (r["product_id"],)).fetchone()
        name = p["name"]
        fac = (p["note"] or "").strip()
        rtype = {"purchase": "进", "sale": "出", "purchase_return": "退进", "sale_return": "退出"}[r["biz_type"]]
        qty_s = str(int(r["qty"])) if float(r["qty"]).is_integer() else str(r["qty"])
        suffix = f",{fac}" if fac else ""
        f_s = f" 运费{r['freight']}" if r["freight"] else ""
        print(f"  #{r['id']:<4} [{rtype}] {name} ×{qty_s} @{r['price']} = {r['amount']}{f_s}{suffix}")
    print(f"--- 合计: 售出 {total_sale} 元（含运费 {total_freight}） / 进货 {total_buy} 元 / 共 {len(rows)} 笔 ---")
    return 0


def cmd_export(con, a):
    import csv
    path = a.out or os.path.join(os.path.dirname(DB), f"流水导出_{date.today().isoformat()}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["日期", "类型", "图号", "名称", "数量", "单价", "金额", "运费", "往来单位", "备注"])
        for r in con.execute("SELECT * FROM transactions ORDER BY biz_date, id").fetchall():
            p = con.execute("SELECT * FROM products WHERE id=?", (r["product_id"],)).fetchone()
            cp = con.execute("SELECT name FROM counterparties WHERE id=?", (r["counterparty_id"],)).fetchone()
            w.writerow([r["biz_date"], r["biz_type"], p["drawing_no"], p["name"],
                        r["qty"], r["price"], r["amount"], r["freight"] or 0,
                        cp["name"] if cp else "", r["note"] or ""])
    print(f"已导出: {path}")
    return 0


def cmd_backup(con, a):
    dst = os.path.join(os.path.dirname(DB), f"backup_{date.today().isoformat()}.db")
    shutil.copy2(DB, dst)
    print(f"已备份: {dst}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="柴油机配件进销存 v3")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("product-add", help="新建商品")
    p.add_argument("drawing_no"); p.add_argument("name"); p.add_argument("--unit", default="件")
    p.add_argument("--note", default="", help="商品备注")
    p.set_defaults(fn=cmd_product_add)

    p = sub.add_parser("sell", help="售出")
    p.add_argument("product", help="图号或名称"); p.add_argument("qty", type=float)
    p.add_argument("price", type=float); p.add_argument("--customer", default="")
    p.add_argument("--region", default=""); p.add_argument("--phone", default="")
    p.add_argument("--freight", type=float, default=0, help="运费（默认0）")
    p.add_argument("--credit", action="store_true", help="本次挂账")
    p.add_argument("--date", default=""); p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_sell)

    p = sub.add_parser("buy", help="进货")
    p.add_argument("product"); p.add_argument("qty", type=float); p.add_argument("price", type=float)
    p.add_argument("--supplier", default=""); p.add_argument("--region", default="")
    p.add_argument("--phone", default="")
    p.add_argument("--credit", action="store_true"); p.add_argument("--date", default=""); p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_buy)

    p = sub.add_parser("return", help="红冲退货")
    p.add_argument("id", type=int); p.add_argument("--qty", type=float, default=0)
    p.add_argument("--date", default=""); p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_return)

    p = sub.add_parser("stock", help="库存")
    p.add_argument("--date", default=""); p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_stock)

    p = sub.add_parser("debt", help="应收应付")
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_debt)

    p = sub.add_parser("pay", help="收/付款销账: pay receive|pay 名称 金额")
    p.add_argument("pay_type", choices=["receive", "pay"]); p.add_argument("name")
    p.add_argument("amount", type=float); p.add_argument("--date", default=""); p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_pay)

    p = sub.add_parser("daily", help="日报")
    p.add_argument("--date", default="")
    p.set_defaults(fn=cmd_daily)

    p = sub.add_parser("export", help="导出 CSV")
    p.add_argument("--out", default="")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("backup", help="备份")
    p.set_defaults(fn=cmd_backup)

    a = ap.parse_args()
    con = connect()
    sys.exit(a.fn(con, a))


if __name__ == "__main__":
    main()
