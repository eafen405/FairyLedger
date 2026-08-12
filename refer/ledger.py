#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
柴油机配件进销存记账本 (ledger.py)
用法：
  python3 ledger.py init                          # 初始化数据库
  python3 ledger.py sell ...                      # 记售出
  python3 ledger.py buy ...                       # 记进货
  python3 ledger.py daily [--date YYYY-MM-DD]     # 日报
  python3 ledger.py monthly [YYYY-MM]             # 月报
  python3 ledger.py stock                         # 当前库存
  python3 ledger.py profit [YYYY-MM]              # 毛利汇总
  python3 ledger.py add-product ...               # 添加商品
  python3 ledger.py add-customer ...              # 添加客户/供应商
  python3 ledger.py list-products                 # 商品列表
  python3 ledger.py list-customers                # 往来单位列表
  python3 ledger.py export [--out file.csv]       # 导出流水 CSV
  python3 ledger.py backup                        # 备份数据库
"""
import argparse
import csv
import datetime
import os
import shutil
import sqlite3
import sys

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger.db")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS products (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,                -- 名称：缸盖总成
    model      TEXT DEFAULT '',              -- 型号：210 / 300R.14.14
    unit       TEXT DEFAULT '件',            -- 单位：件/个
    cost_price REAL,                         -- 参考进价
    sale_price REAL,                         -- 参考售价
    UNIQUE(name, model)
);

CREATE TABLE IF NOT EXISTS counterparties (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name   TEXT NOT NULL UNIQUE,             -- 徐丽 / 河南1881222334
    region TEXT DEFAULT '',                  -- 地区：湖南/河南/烟台
    phone  TEXT DEFAULT '',                  -- 电话
    ctype  TEXT DEFAULT 'customer'           -- customer=客户 supplier=供应商
);

CREATE TABLE IF NOT EXISTS transactions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    biz_date       TEXT NOT NULL,            -- 业务日期 YYYY-MM-DD
    biz_type       TEXT NOT NULL,            -- sale=售出 purchase=进货
    product_id     INTEGER NOT NULL REFERENCES products(id),
    qty            REAL NOT NULL,            -- 数量
    price          REAL NOT NULL,            -- 单价
    amount         REAL NOT NULL,            -- 金额（默认 qty*price，可手填）
    counterparty_id INTEGER REFERENCES counterparties(id),
    note           TEXT DEFAULT '',          -- 备注
    created_at     TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(biz_date);
CREATE INDEX IF NOT EXISTS idx_tx_type ON transactions(biz_type);
"""


def fmt_price(v):
    """格式化价格：None 显示 '-',数字用 :g。"""
    if v is None:
        return "-"
    return f"{v:g}"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def fmt_date(s):
    """把 8/6、2026-8-6 等格式规范成 YYYY-MM-DD，默认今天。"""
    if not s:
        return datetime.date.today().isoformat()
    s = s.strip()
    for sep in ("/", "-", "."):
        if sep in s:
            parts = [p for p in s.split(sep) if p]
            if len(parts) == 2:
                y = datetime.date.today().year
                m, d = int(parts[0]), int(parts[1])
            elif len(parts) == 3:
                y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
            else:
                raise ValueError(f"无法解析日期: {s}")
            return f"{y:04d}-{m:02d}-{d:02d}"
    raise ValueError(f"无法解析日期: {s}")


def find_or_create_product(conn, name, model="", unit="件", cost=None, sale=None):
    """按名称+型号查找商品；不存在则创建。返回 product_id。"""
    row = conn.execute(
        "SELECT id FROM products WHERE name=? AND model=?", (name, model)
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO products(name, model, unit, cost_price, sale_price) VALUES(?,?,?,?,?)",
        (name, model, unit, cost, sale),
    )
    return cur.lastrowid


def find_or_create_counterparty(conn, name, region="", phone="", ctype="customer"):
    """按名称查找往来单位；不存在则创建。返回 id 或 None（未提供名称）。"""
    if not name:
        return None
    row = conn.execute("SELECT id FROM counterparties WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO counterparties(name, region, phone, ctype) VALUES(?,?,?,?)",
        (name, region, phone, ctype),
    )
    return cur.lastrowid


def cmd_init(args):
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    print(f"数据库已初始化: {DB_PATH}")


def cmd_add_product(args):
    conn = get_conn()
    try:
        pid = find_or_create_product(
            conn, args.name, args.model, args.unit, args.cost, args.sale
        )
        conn.commit()
        print(f"商品已保存 id={pid}: {args.name} {args.model} ({args.unit})")
    except sqlite3.IntegrityError as e:
        print(f"添加失败: {e}", file=sys.stderr)


def cmd_add_customer(args):
    conn = get_conn()
    try:
        cid = find_or_create_counterparty(
            conn, args.name, args.region, args.phone, args.ctype
        )
        conn.commit()
        print(f"往来单位已保存 id={cid}: {args.name} [{args.ctype}]")
    except sqlite3.IntegrityError as e:
        print(f"添加失败: {e}", file=sys.stderr)


def _record(conn, args, biz_type):
    """通用：记一笔售出/进货。"""
    biz_date = fmt_date(args.date)
    name = args.name.strip()
    if not name:
        print("错误：--name 不能为空", file=sys.stderr)
        sys.exit(1)
    model = (args.model or "").strip()
    unit = (args.unit or "件").strip()

    # 商品匹配：精确(name+model)优先，未命中再模糊(name)，单结果复用，多结果让用户选
    product = None
    if args.product_id:
        product = conn.execute(
            "SELECT * FROM products WHERE id=?", (args.product_id,)
        ).fetchone()
    if product is None:
        exact = conn.execute(
            "SELECT * FROM products WHERE name=? AND model=?", (name, model)
        ).fetchone()
        if exact:
            product = exact
            print(f"  匹配到商品: {product['name']} {product['model']} ({product['unit']})")
        else:
            rows = conn.execute(
                "SELECT * FROM products WHERE name LIKE ? ORDER BY id LIMIT 5",
                (f"%{name}%",),
            ).fetchall()
            if len(rows) == 1 and (not model or rows[0]["model"] == model):
                product = rows[0]
                print(f"  匹配到商品: {product['name']} {product['model']} ({product['unit']})")
            elif len(rows) > 1 and not model:
                # 未提供型号但名称匹配多个，让用户选
                print("  匹配到多个商品，请用 --product-id 指定：")
                for r in rows:
                    print(f"    id={r['id']}: {r['name']} {r['model']} ({r['unit']})")
                sys.exit(2)

    if product is None:
        pid = find_or_create_product(
            conn, name, model, unit, args.cost, args.sale
        )
        print(f"  新商品已创建 id={pid}: {name} {model} ({unit})")
    else:
        pid = product["id"]
        if args.cost and not product["cost_price"]:
            conn.execute(
                "UPDATE products SET cost_price=? WHERE id=?", (args.cost, pid)
            )
        if args.sale and not product["sale_price"]:
            conn.execute(
                "UPDATE products SET sale_price=? WHERE id=?", (args.sale, pid)
            )

    ctype = "supplier" if biz_type == "purchase" else "customer"
    cid = find_or_create_counterparty(
        conn, (args.counterparty or "").strip(),
        (args.region or "").strip(), (args.phone or "").strip(), ctype,
    )

    qty = float(args.qty)
    price = float(args.price)
    amount = float(args.amount) if args.amount is not None else round(qty * price, 2)

    cur = conn.execute(
        """INSERT INTO transactions
           (biz_date, biz_type, product_id, qty, price, amount, counterparty_id, note)
           VALUES(?,?,?,?,?,?,?,?)""",
        (biz_date, biz_type, pid, qty, price, amount, cid, (args.note or "").strip()),
    )
    conn.commit()
    label = "售出" if biz_type == "sale" else "进货"
    cp = conn.execute(
        "SELECT name FROM counterparties WHERE id=?", (cid,)
    ).fetchone() if cid else None
    print(
        f"✓ 已记录{label} id={cur.lastrowid}: {biz_date} "
        f"{name} {model} x{qty:g} @ {price:g} = {amount:g}"
        + (f"  → {cp['name']}" if cp else "")
    )


def cmd_sell(args):
    conn = get_conn()
    _record(conn, args, "sale")


def cmd_buy(args):
    conn = get_conn()
    _record(conn, args, "purchase")


def _sum_rows(rows, label):
    total = sum(r["amount"] for r in rows)
    print(f"\n{label}: {len(rows)} 笔，合计 {total:,.2f} 元")
    return total


def cmd_daily(args):
    conn = get_conn()
    d = fmt_date(args.date)
    rows = conn.execute(
        """SELECT t.*, p.name, p.model, p.unit, c.name AS cp_name
           FROM transactions t
           JOIN products p ON t.product_id = p.id
           LEFT JOIN counterparties c ON t.counterparty_id = c.id
           WHERE t.biz_date=? ORDER BY t.biz_type, t.id""",
        (d,),
    ).fetchall()
    print(f"===== {d} 流水 =====")
    if not rows:
        print("无记录")
        return
    for r in rows:
        kind = "进" if r["biz_type"] == "purchase" else "售"
        cp = f" → {r['cp_name']}" if r["cp_name"] else ""
        print(
            f"{kind} {r['name']} {r['model']} {r['qty']:g}{r['unit']} "
            f"@{r['price']:g} = {r['amount']:g}{cp}"
        )
    sales = [r for r in rows if r["biz_type"] == "sale"]
    buys = [r for r in rows if r["biz_type"] == "purchase"]
    _sum_rows(sales, "当日售出")
    _sum_rows(buys, "当日进货")


def cmd_monthly(args):
    conn = get_conn()
    month = args.month or args.month_pos or datetime.date.today().strftime("%Y-%m")
    rows = conn.execute(
        """SELECT t.*, p.name, p.model, p.unit, c.name AS cp_name
           FROM transactions t
           JOIN products p ON t.product_id = p.id
           LEFT JOIN counterparties c ON t.counterparty_id = c.id
           WHERE t.biz_date LIKE ? ORDER BY t.biz_date, t.id""",
        (month + "%",),
    ).fetchall()
    print(f"===== {month} 月报 =====")
    if not rows:
        print("无记录")
        return
    by_day = {}
    for r in rows:
        by_day.setdefault(r["biz_date"], []).append(r)
    for d in sorted(by_day):
        day_sales = [r for r in by_day[d] if r["biz_type"] == "sale"]
        day_buys = [r for r in by_day[d] if r["biz_type"] == "purchase"]
        s = sum(r["amount"] for r in day_sales)
        b = sum(r["amount"] for r in day_buys)
        print(f"{d}: 售出 {len(day_sales)}笔 {s:,.2f} | 进货 {len(day_buys)}笔 {b:,.2f}")
    sales = [r for r in rows if r["biz_type"] == "sale"]
    buys = [r for r in rows if r["biz_type"] == "purchase"]
    _sum_rows(sales, "本月售出")
    _sum_rows(buys, "本月进货")


def cmd_stock(args):
    conn = get_conn()
    rows = conn.execute(
        """SELECT p.id, p.name, p.model, p.unit, p.cost_price, p.sale_price,
                  SUM(CASE WHEN t.biz_type='purchase' THEN t.qty
                           WHEN t.biz_type='sale' THEN -t.qty ELSE 0 END) AS stock
           FROM products p
           LEFT JOIN transactions t ON t.product_id = p.id
           GROUP BY p.id
           HAVING stock != 0 OR p.cost_price IS NOT NULL OR p.sale_price IS NOT NULL
           ORDER BY p.name, p.model""",
    ).fetchall()
    print("===== 当前库存 =====")
    if not rows:
        print("无记录")
        return
    total_val = 0.0
    for r in rows:
        stock = r["stock"] or 0
        val = stock * (r["cost_price"] or 0)
        total_val += val
        flag = ""
        if stock < 0:
            flag = "  ⚠️ 库存为负（售出多于进货）"
        print(
            f"{r['name']} {r['model']} ({r['unit']}): {stock:g}"
            f"  进价{fmt_price(r['cost_price'])} 售价{fmt_price(r['sale_price'])}{flag}"
        )
    print(f"\n库存按进价估算总值: {total_val:,.2f} 元")


def cmd_profit(args):
    conn = get_conn()
    month = args.month or args.month_pos or datetime.date.today().strftime("%Y-%m")
    rows = conn.execute(
        """SELECT t.*, p.name, p.model, p.cost_price, c.name AS cp_name
           FROM transactions t
           JOIN products p ON t.product_id = p.id
           LEFT JOIN counterparties c ON t.counterparty_id = c.id
           WHERE t.biz_type='sale' AND t.biz_date LIKE ? ORDER BY t.biz_date, t.id""",
        (month + "%",),
    ).fetchall()
    print(f"===== {month} 毛利（售价-参考进价） =====")
    if not rows:
        print("无记录")
        return
    total_profit = 0.0
    for r in rows:
        cost = r["cost_price"]
        if cost is None:
            print(f"{r['biz_date']} {r['name']} {r['model']}: 无进价，无法算毛利")
            continue
        profit = round((r["price"] - cost) * r["qty"], 2)
        total_profit += profit
        cp = f" → {r['cp_name']}" if r["cp_name"] else ""
        print(
            f"{r['biz_date']} {r['name']} {r['model']} x{r['qty']:g}: "
            f"毛利 {profit:,.2f}（售价{r['price']:g} - 进价{cost:g}）{cp}"
        )
    print(f"\n本月毛利合计: {total_profit:,.2f} 元")


def cmd_list_products(args):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM products ORDER BY name, model"
    ).fetchall()
    print("===== 商品列表 =====")
    if not rows:
        print("无记录")
        return
    for r in rows:
        print(
            f"id={r['id']}: {r['name']} {r['model']} ({r['unit']}) "
            f"进价{r['cost_price'] or '-'} 售价{r['sale_price'] or '-'}"
        )


def cmd_list_customers(args):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM counterparties ORDER BY ctype, name"
    ).fetchall()
    print("===== 往来单位 =====")
    if not rows:
        print("无记录")
        return
    for r in rows:
        print(
            f"id={r['id']} [{r['ctype']}] {r['name']} {r['region']} {r['phone']}"
        )


def cmd_export(args):
    conn = get_conn()
    out = args.out or os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f"流水导出_{datetime.date.today().isoformat()}.csv",
    )
    rows = conn.execute(
        """SELECT t.biz_date, t.biz_type, p.name, p.model, p.unit, t.qty,
                  t.price, t.amount, c.name AS cp_name, c.region, c.phone, t.note
           FROM transactions t
           JOIN products p ON t.product_id = p.id
           LEFT JOIN counterparties c ON t.counterparty_id = c.id
           ORDER BY t.biz_date, t.id""",
    ).fetchall()
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(
            ["日期", "类型", "名称", "型号", "单位", "数量", "单价", "金额",
             "往来单位", "地区", "电话", "备注"]
        )
        for r in rows:
            kind = "进货" if r["biz_type"] == "purchase" else "售出"
            w.writerow(
                [r["biz_date"], kind, r["name"], r["model"], r["unit"], r["qty"],
                 r["price"], r["amount"], r["cp_name"], r["region"], r["phone"],
                 r["note"]]
            )
    print(f"已导出 {len(rows)} 条到 {out}")


def cmd_backup(args):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), f"backup_{ts}.db"
    )
    if os.path.exists(DB_PATH):
        shutil.copy2(DB_PATH, dst)
        print(f"已备份到 {dst}")
    else:
        print("数据库不存在，无需备份", file=sys.stderr)


def build_parser():
    p = argparse.ArgumentParser(description="柴油机配件进销存记账本")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("init", help="初始化数据库")

    ap = sub.add_parser("add-product", help="添加商品")
    ap.add_argument("--name", required=True)
    ap.add_argument("--model", default="")
    ap.add_argument("--unit", default="件")
    ap.add_argument("--cost", type=float, default=None)
    ap.add_argument("--sale", type=float, default=None)

    ac = sub.add_parser("add-customer", help="添加客户/供应商")
    ac.add_argument("--name", required=True)
    ac.add_argument("--region", default="")
    ac.add_argument("--phone", default="")
    ac.add_argument("--ctype", default="customer", choices=["customer", "supplier"])

    def add_tx_args(sp):
        sp.add_argument("--date", default="", help="日期，如 8/6 或 2026-08-06，默认今天")
        sp.add_argument("--name", required=True, help="名称")
        sp.add_argument("--model", default="", help="型号")
        sp.add_argument("--unit", default="件")
        sp.add_argument("--qty", type=float, required=True)
        sp.add_argument("--price", type=float, required=True)
        sp.add_argument("--amount", type=float, default=None, help="金额，默认=数量×单价")
        sp.add_argument("--counterparty", default="", help="客户/供应商名称")
        sp.add_argument("--region", default="")
        sp.add_argument("--phone", default="")
        sp.add_argument("--note", default="")
        sp.add_argument("--product-id", type=int, default=None)
        sp.add_argument("--cost", type=float, default=None, help="顺带记录进价")
        sp.add_argument("--sale", type=float, default=None, help="顺带记录售价")

    ss = sub.add_parser("sell", help="记售出")
    add_tx_args(ss)
    sb = sub.add_parser("buy", help="记进货")
    add_tx_args(sb)

    sd = sub.add_parser("daily", help="日报")
    sd.add_argument("--date", default="")
    sm = sub.add_parser("monthly", help="月报")
    sm.add_argument("--month", default="")
    sm.add_argument("month_pos", nargs="?", default="", help=argparse.SUPPRESS)
    sp_ = sub.add_parser("profit", help="毛利汇总")
    sp_.add_argument("--month", default="")
    sp_.add_argument("month_pos", nargs="?", default="", help=argparse.SUPPRESS)
    sub.add_parser("stock", help="当前库存")
    sub.add_parser("list-products", help="商品列表")
    sub.add_parser("list-customers", help="往来单位列表")
    se = sub.add_parser("export", help="导出 CSV")
    se.add_argument("--out", default="")
    sub.add_parser("backup", help="备份数据库")
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        sys.exit(0)
    cmds = {
        "init": cmd_init,
        "add-product": cmd_add_product,
        "add-customer": cmd_add_customer,
        "sell": cmd_sell,
        "buy": cmd_buy,
        "daily": cmd_daily,
        "monthly": cmd_monthly,
        "stock": cmd_stock,
        "profit": cmd_profit,
        "list-products": cmd_list_products,
        "list-customers": cmd_list_customers,
        "export": cmd_export,
        "backup": cmd_backup,
    }
    if not os.path.exists(DB_PATH) and args.cmd != "init":
        print("数据库不存在，请先运行: python3 ledger.py init", file=sys.stderr)
        sys.exit(1)
    cmds[args.cmd](args)


if __name__ == "__main__":
    main()
