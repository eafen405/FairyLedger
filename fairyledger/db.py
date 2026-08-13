"""库文件定位、连接与 schema 初始化（spec D1 定稿 DDL）。

表结构逐字对齐 spec D1：products/aliases/counterparties/transactions/
payments/opening_stock/audit_log 共 7 张表 + transactions 3 个索引。
"""

import os
import sqlite3
from pathlib import Path

DDL = """
CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY,
    drawing_no  TEXT NOT NULL UNIQUE,  -- 图号：厂家号或自编号(ZC-前缀)，唯一查询凭据
    name        TEXT NOT NULL,         -- 正式名称
    unit        TEXT DEFAULT '件',     -- 件/副/对，纯文本，无换算
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS aliases (
    id          INTEGER PRIMARY KEY,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    alias       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS counterparties (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    is_customer INTEGER DEFAULT 0,  -- 客户标记位
    is_supplier INTEGER DEFAULT 0,  -- 供应商标记位
    is_credit   INTEGER DEFAULT 0,  -- 挂账标记（布尔，无额度上限）
    region      TEXT,
    phone       TEXT,
    note        TEXT,
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY,
    biz_date        TEXT NOT NULL,   -- YYYY-MM-DD
    biz_type        TEXT NOT NULL,   -- purchase/sale/purchase_return/sale_return
    product_id      INTEGER NOT NULL REFERENCES products(id),
    qty             REAL NOT NULL,   -- 正数，退货用类型区分
    price           REAL NOT NULL,   -- 成交单价
    freight         REAL DEFAULT 0,  -- 运费，进货/售出都可，可空默认0
    cost            REAL,            -- 成本快照：售出记当时加权成本
    counterparty_id INTEGER REFERENCES counterparties(id),
    ref_id          INTEGER,         -- 红冲关联的原单 id
    note            TEXT,
    created_at      TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_tx_date    ON transactions(biz_date);
CREATE INDEX IF NOT EXISTS idx_tx_product ON transactions(product_id);
CREATE INDEX IF NOT EXISTS idx_tx_cp      ON transactions(counterparty_id);

CREATE TABLE IF NOT EXISTS payments (
    id              INTEGER PRIMARY KEY,
    pay_date        TEXT NOT NULL,
    pay_type        TEXT NOT NULL,   -- receive收客户款 / pay付供应商款
    counterparty_id INTEGER NOT NULL REFERENCES counterparties(id),
    amount          REAL NOT NULL,
    note            TEXT,
    created_at      TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS opening_stock (
    id          INTEGER PRIMARY KEY,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    qty         REAL NOT NULL,
    cost        REAL,            -- 期初成本，供加权起点
    recorded_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY,
    table_name  TEXT NOT NULL,
    record_id   INTEGER NOT NULL,
    action      TEXT NOT NULL,   -- insert/update/reverse
    before_json TEXT,
    after_json  TEXT,
    note        TEXT,
    changed_at  TEXT DEFAULT (datetime('now','localtime'))
);
"""

DEFAULT_DB_PATH = Path.home() / ".fairyledger" / "ledger.db"


def resolve_db_path(db_arg: str | None) -> Path:
    """库文件路径：--db 参数 > FAIRY_DB 环境变量 > 默认 ~/.fairyledger/ledger.db。"""
    if db_arg:
        return Path(db_arg)
    env = os.environ.get("FAIRY_DB")
    if env:
        return Path(env)
    return DEFAULT_DB_PATH


def connect(db_path: Path) -> sqlite3.Connection:
    """打开连接（自动建父目录、开外键约束），行以 sqlite3.Row 返回。"""
    db_path = Path(db_path)
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """幂等初始化：空库建表建索引，已有库无副作用。"""
    conn.executescript(DDL)
