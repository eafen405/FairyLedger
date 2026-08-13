"""T1 建库骨架与建档 — CLI 黑盒测试（spec Testing Decisions 单一 seam）。

只断言 `fairy` 命令的外部行为：stdout 结构化 JSON、退出码 0/1/2、
写入后通过后续命令可观察的状态。schema 核对是同一 seam 的验证手段——
只读打开临时库核对表/列（spec 允许对无 CLI 暴露处做只读核对）。
"""

import datetime
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAIRY = ROOT / "fairy"

EXPECTED_TABLES = {
    "products", "aliases", "counterparties", "transactions",
    "payments", "opening_stock", "audit_log",
}
EXPECTED_INDEXES = {"idx_tx_date", "idx_tx_product", "idx_tx_cp"}
TRANSACTION_COLUMNS = {
    "id", "biz_date", "biz_type", "product_id", "qty", "price",
    "freight", "cost", "counterparty_id", "ref_id", "note", "created_at",
}


def run_fairy(db: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(FAIRY), "--db", str(db), *args],
        capture_output=True, text=True, encoding="utf-8",
    )


def table_set(db: Path) -> set[str]:
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def index_set(db: Path) -> set[str]:
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type='index' AND name LIKE 'idx_%'"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def column_map(db: Path, table: str) -> dict[str, tuple]:
    conn = sqlite3.connect(str(db))
    try:
        # PRAGMA table_info 行: (cid, name, type, notnull, dflt_value, pk)
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r[1]: r for r in rows}
    finally:
        conn.close()


class T1Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "test.db"

    def tearDown(self):
        self._tmp.cleanup()

    def fairy(self, *args: str) -> subprocess.CompletedProcess:
        return run_fairy(self.db, *args)

    def add_product(self, *args: str) -> dict:
        cp = self.fairy("product", "add", *args)
        self.assertEqual(cp.returncode, 0, f"stderr: {cp.stderr}")
        return json.loads(cp.stdout)

    def purchase(self, *args: str) -> dict:
        cp = self.fairy("purchase", *args)
        self.assertEqual(cp.returncode, 0, f"stderr: {cp.stderr}")
        return json.loads(cp.stdout)

    def opening(self, *args: str) -> dict:
        cp = self.fairy("opening", *args)
        self.assertEqual(cp.returncode, 0, f"stderr: {cp.stderr}")
        return json.loads(cp.stdout)

    def query_db(self, sql: str, *params) -> list[tuple]:
        """只读打开临时库核对无 CLI 暴露处的行级事实（spec Testing Decisions 允许）。"""
        conn = sqlite3.connect(str(self.db))
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def stock_rows(self) -> list[dict]:
        cp = self.fairy("query", "stock")
        self.assertEqual(cp.returncode, 0, f"stderr: {cp.stderr}")
        return json.loads(cp.stdout)

    def stock_row(self, drawing_no: str) -> dict:
        rows = [r for r in self.stock_rows() if r["drawing_no"] == drawing_no]
        self.assertEqual(len(rows), 1, f"应恰好一行: {rows}")
        return rows[0]

    def sale(self, *args: str) -> dict:
        cp = self.fairy("sale", *args)
        self.assertEqual(cp.returncode, 0, f"stderr: {cp.stderr}")
        return json.loads(cp.stdout)

    def price_rows(self, *args: str) -> list[dict]:
        cp = self.fairy("query", "price", *args)
        self.assertEqual(cp.returncode, 0, f"stderr: {cp.stderr}")
        return json.loads(cp.stdout)

    def receive(self, *args: str) -> dict:
        cp = self.fairy("receive", *args)
        self.assertEqual(cp.returncode, 0, f"stderr: {cp.stderr}")
        return json.loads(cp.stdout)

    def pay(self, *args: str) -> dict:
        cp = self.fairy("pay", *args)
        self.assertEqual(cp.returncode, 0, f"stderr: {cp.stderr}")
        return json.loads(cp.stdout)

    def credit_rows(self, *args: str) -> dict:
        cp = self.fairy("query", "credit", *args)
        self.assertEqual(cp.returncode, 0, f"stderr: {cp.stderr}")
        return json.loads(cp.stdout)

    def reverse(self, *args: str) -> dict:
        cp = self.fairy("reverse", *args)
        self.assertEqual(cp.returncode, 0, f"stderr: {cp.stderr}")
        return json.loads(cp.stdout)

    def edit(self, *args: str) -> dict:
        cp = self.fairy("edit", *args)
        self.assertEqual(cp.returncode, 0, f"stderr: {cp.stderr}")
        return json.loads(cp.stdout)

    def history_rows(self, *args: str) -> list[dict]:
        cp = self.fairy("query", "history", *args)
        self.assertEqual(cp.returncode, 0, f"stderr: {cp.stderr}")
        return json.loads(cp.stdout)

    def audit_rows(self) -> list[tuple]:
        """只读核对 audit_log（spec Testing Decisions：无 CLI 暴露处允许只读打开核对）。"""
        return self.query_db(
            "SELECT id, table_name, record_id, action, before_json, after_json, note"
            " FROM audit_log ORDER BY id"
        )


class TestSchemaInit(T1Base):
    def test_empty_db_initializes_all_tables_and_indexes(self):
        cp = self.fairy("query", "stock")
        self.assertEqual(cp.returncode, 0, f"stderr: {cp.stderr}")
        self.assertEqual(table_set(self.db), EXPECTED_TABLES)
        self.assertEqual(index_set(self.db), EXPECTED_INDEXES)

    def test_schema_matches_spec_ddl(self):
        self.fairy("query", "stock")
        products = column_map(self.db, "products")
        self.assertTrue(products["drawing_no"][3], "drawing_no 应 NOT NULL")
        self.assertTrue(products["name"][3], "name 应 NOT NULL")
        self.assertEqual(products["unit"][4], "'件'", "unit 默认 '件'")
        aliases = column_map(self.db, "aliases")
        self.assertEqual(set(aliases), {"id", "product_id", "alias"})
        tx = column_map(self.db, "transactions")
        self.assertEqual(set(tx), TRANSACTION_COLUMNS, "transactions 列应对齐 DDL")
        self.assertNotIn("amount", tx, "金额不落列，一律 qty×price 现算")
        payments = column_map(self.db, "payments")
        self.assertIn("amount", payments)

    def test_init_is_idempotent(self):
        self.assertEqual(self.fairy("query", "stock").returncode, 0)
        self.assertEqual(self.fairy("query", "stock").returncode, 0)


class TestProductAdd(T1Base):
    def test_add_with_drawing_no(self):
        out = self.add_product("--drawing-no", "300.14.14", "--name", "缸盖", "--unit", "件", "--alias", "缸盖总成")
        self.assertEqual(out["drawing_no"], "300.14.14")
        self.assertEqual(out["name"], "缸盖")
        self.assertEqual(out["unit"], "件")
        self.assertEqual(out["aliases"], ["缸盖总成"])
        self.assertIn("id", out)
        self.assertIn("created_at", out)

    def test_add_auto_zc_increment_and_unique(self):
        first = self.add_product("--name", "活塞")
        self.assertEqual(first["drawing_no"], "ZC-1")
        second = self.add_product("--name", "活塞环")
        self.assertEqual(second["drawing_no"], "ZC-2")
        self.assertNotEqual(first["id"], second["id"])
        # 厂家号存在时自编号不冲突
        third = self.add_product("--name", "气门")
        self.assertEqual(third["drawing_no"], "ZC-3")

    def test_add_without_name_exit1_and_nothing_written(self):
        cp = self.fairy("product", "add")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("参数缺失", cp.stderr)
        self.assertEqual(cp.stdout, "")
        cp = self.fairy("query", "stock")
        self.assertEqual(json.loads(cp.stdout), [])

    def test_add_duplicate_drawing_no_exit1(self):
        self.add_product("--drawing-no", "170", "--name", "活塞")
        cp = self.fairy("product", "add", "--drawing-no", "170", "--name", "另一活塞")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("170", cp.stderr)
        cp = self.fairy("query", "stock")
        self.assertEqual(len(json.loads(cp.stdout)), 1, "重复建档不应产生第二行")

    def test_multiple_aliases_and_same_alias_across_products(self):
        a = self.add_product("--drawing-no", "170", "--name", "活塞", "--alias", "活塞A", "--alias", "活塞总成")
        b = self.add_product("--drawing-no", "171", "--name", "活塞环", "--alias", "活塞A")
        self.assertEqual(a["aliases"], ["活塞A", "活塞总成"])
        self.assertEqual(b["aliases"], ["活塞A"])
        # 同别名去重
        c = self.add_product("--drawing-no", "172", "--name", "缸盖", "--alias", "缸盖", "--alias", "缸盖")
        self.assertEqual(c["aliases"], ["缸盖"])

    def test_unit_default_and_pure_text(self):
        default = self.add_product("--drawing-no", "170", "--name", "活塞")
        self.assertEqual(default["unit"], "件")
        custom = self.add_product("--drawing-no", "171", "--name", "副活塞", "--unit", "副")
        self.assertEqual(custom["unit"], "副")


class TestQueryStock(T1Base):
    def test_stock_empty_on_fresh_db(self):
        cp = self.fairy("query", "stock")
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(json.loads(cp.stdout), [])

    def test_stock_lists_all_products_with_zero_qty_by_drawing_no(self):
        for dn, name in [("300.14.14", "缸盖"), ("170", "活塞"), ("YC-2", "气门")]:
            self.add_product("--drawing-no", dn, "--name", name)
        cp = self.fairy("query", "stock")
        self.assertEqual(cp.returncode, 0)
        rows = json.loads(cp.stdout)
        self.assertEqual([r["drawing_no"] for r in rows], ["170", "300.14.14", "YC-2"])
        for row in rows:
            self.assertEqual(row["qty"], 0, "无期初无流水，数量现算为 0")
            self.assertIn("name", row)
            self.assertIn("unit", row)

    def test_stock_filter_by_drawing_no(self):
        self.add_product("--drawing-no", "170", "--name", "活塞")
        self.add_product("--drawing-no", "171", "--name", "活塞环")
        cp = self.fairy("query", "stock", "--drawing-no", "170")
        rows = json.loads(cp.stdout)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["drawing_no"], "170")


class TestSearch(T1Base):
    def test_exact_drawing_no_priority(self):
        self.add_product("--drawing-no", "170", "--name", "活塞")
        self.add_product("--drawing-no", "XYZ", "--name", "170活塞套件")
        cp = self.fairy("query", "product", "--q", "170")
        self.assertEqual(cp.returncode, 0)
        out = json.loads(cp.stdout)
        self.assertEqual(out["match"], "exact")
        self.assertEqual(len(out["products"]), 1)
        self.assertEqual(out["products"][0]["drawing_no"], "170", "图号精确命中应优先于名称包含")

    def test_exact_drawing_no_case_insensitive(self):
        self.add_product("--drawing-no", "YC-3", "--name", "气门")
        cp = self.fairy("query", "product", "--q", "yc-3")
        out = json.loads(cp.stdout)
        self.assertEqual(out["match"], "exact")
        self.assertEqual(out["products"][0]["drawing_no"], "YC-3")

    def test_name_contains_hit(self):
        self.add_product("--drawing-no", "170", "--name", "活塞环")
        cp = self.fairy("query", "product", "--q", "活塞")
        out = json.loads(cp.stdout)
        self.assertEqual(out["match"], "exact")
        self.assertEqual(out["products"][0]["name"], "活塞环")

    def test_alias_contains_hit(self):
        self.add_product("--drawing-no", "YC-3", "--name", "气门", "--alias", "进气门")
        cp = self.fairy("query", "product", "--q", "进气")
        out = json.loads(cp.stdout)
        self.assertEqual(out["match"], "exact")
        self.assertEqual(out["products"][0]["drawing_no"], "YC-3")

    def test_mixed_segments_and_match(self):
        self.add_product("--drawing-no", "170", "--name", "活塞")
        self.add_product("--drawing-no", "300.14.14", "--name", "缸盖")
        cp = self.fairy("query", "product", "--q", "170活塞")
        out = json.loads(cp.stdout)
        self.assertEqual(out["match"], "exact", "170 命中图号、活塞 命中名称，AND 后唯一")
        self.assertEqual(out["products"][0]["drawing_no"], "170")

    def test_mixed_segments_multiple_candidates(self):
        self.add_product("--drawing-no", "170", "--name", "活塞")
        self.add_product("--drawing-no", "170A", "--name", "活塞环")
        cp = self.fairy("query", "product", "--q", "170活塞")
        out = json.loads(cp.stdout)
        self.assertEqual(out["match"], "multiple")
        self.assertEqual([p["drawing_no"] for p in out["products"]], ["170", "170A"])

    def test_segment_must_match_some_field(self):
        self.add_product("--drawing-no", "170", "--name", "缸盖")
        cp = self.fairy("query", "product", "--q", "170活塞")
        out = json.loads(cp.stdout)
        self.assertEqual(out["match"], "none", "活塞 段无字段命中，整词不命中")

    def test_no_match(self):
        self.add_product("--drawing-no", "170", "--name", "活塞")
        cp = self.fairy("query", "product", "--q", "不存在的商品")
        out = json.loads(cp.stdout)
        self.assertEqual(out["match"], "none")
        self.assertEqual(out["products"], [])


class TestErrorContract(T1Base):
    def test_missing_q_exit1_stderr(self):
        cp = self.fairy("query", "product")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("参数缺失", cp.stderr)
        self.assertEqual(cp.stdout, "")

    def test_unknown_command_exit1(self):
        cp = self.fairy("nosuchcommand")
        self.assertEqual(cp.returncode, 1)
        self.assertNotEqual(cp.stderr, "")

    def test_system_error_exit2(self):
        # --db 的父路径被普通文件占住 → mkdir 失败 = 系统错误，退出码 2
        blocker = Path(self._tmp.name) / "blocker"
        blocker.write_text("x", encoding="utf-8")
        cp = run_fairy(blocker / "nested" / "t.db", "query", "stock")
        self.assertEqual(cp.returncode, 2)
        self.assertIn("系统错误", cp.stderr)

    def test_errors_go_to_stderr_not_stdout(self):
        cp = self.fairy("product", "add")
        self.assertEqual(cp.returncode, 1)
        self.assertEqual(cp.stdout, "", "错误消息不应出现在 stdout")


class TestPurchase(T1Base):
    """T2 进货：必填校验、无此商品、供应商 create-or-resolve、默认值、原子性。"""

    def test_qty_and_price_required(self):
        self.add_product("--drawing-no", "170", "--name", "活塞")
        for args in (
            ("--drawing-no", "170", "--price", "10"),
            ("--drawing-no", "170", "--qty", "3"),
        ):
            cp = self.fairy("purchase", *args)
            self.assertEqual(cp.returncode, 1, f"stderr: {cp.stderr}")
            self.assertIn("参数缺失", cp.stderr)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM transactions")[0][0], 0,
                         "参数缺失不应落任何流水")

    def test_drawing_no_required(self):
        cp = self.fairy("purchase", "--qty", "3", "--price", "10")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("参数缺失", cp.stderr)

    def test_unknown_product_exit1(self):
        cp = self.fairy("purchase", "--drawing-no", "NOPE", "--qty", "3", "--price", "10")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("无此商品", cp.stderr)

    def test_basic_purchase_writes_tx_and_stock(self):
        self.add_product("--drawing-no", "170", "--name", "活塞", "--unit", "件")
        out = self.purchase("--drawing-no", "170", "--qty", "5", "--price", "10")
        self.assertEqual(out["biz_type"], "purchase")
        self.assertEqual(out["qty"], 5.0)
        self.assertEqual(out["price"], 10.0)
        self.assertEqual(out["freight"], 0.0, "运费可空默认 0")
        self.assertEqual(out["biz_date"], datetime.date.today().isoformat(), "日期缺省今天")
        self.assertIsNone(out["note"])
        self.assertIsNone(out["supplier"], "供应商可空")
        self.assertEqual(out["drawing_no"], "170")
        self.assertEqual(out["name"], "活塞")
        self.assertIn("id", out)
        row = self.stock_row("170")
        self.assertEqual(row["qty"], 5.0)
        self.assertEqual(row["unit_cost"], 10.0, "无期初时均价退化为进货单价")
        self.assertEqual(row["amount"], 50.0)

    def test_freight_date_note_filled(self):
        self.add_product("--drawing-no", "170", "--name", "活塞")
        out = self.purchase(
            "--drawing-no", "170", "--qty", "3", "--price", "50",
            "--freight", "20", "--date", "2026-08-01", "--note", "加急",
        )
        self.assertEqual(out["freight"], 20.0)
        self.assertEqual(out["biz_date"], "2026-08-01")
        self.assertEqual(out["note"], "加急")
        row = self.stock_row("170")
        self.assertEqual(row["unit_cost"], 50.0, "运费不进加权公式")
        self.assertEqual(row["amount"], 150.0)

    def test_supplier_create_or_resolve(self):
        self.add_product("--drawing-no", "170", "--name", "活塞")
        out = self.purchase("--drawing-no", "170", "--qty", "1", "--price", "10", "--supplier", "甲厂")
        self.assertEqual(out["supplier"], "甲厂")
        self.assertEqual(self.query_db("SELECT name, is_supplier FROM counterparties"),
                         [("甲厂", 1)], "新供应商自动建档并打供应商标记")
        self.purchase("--drawing-no", "170", "--qty", "1", "--price", "10", "--supplier", "甲厂")
        self.assertEqual(len(self.query_db("SELECT id FROM counterparties")), 1,
                         "同名供应商复用，不重复建档")

    def test_negative_qty_and_price_rejected(self):
        self.add_product("--drawing-no", "170", "--name", "活塞")
        cp = self.fairy("purchase", "--drawing-no", "170", "--qty", "-3", "--price", "10")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("正数", cp.stderr)
        cp = self.fairy("purchase", "--drawing-no", "170", "--qty", "3", "--price", "-5")
        self.assertEqual(cp.returncode, 1)
        self.assertNotEqual(cp.stderr, "")
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM transactions")[0][0], 0)

    def test_atomic_failure_leaves_no_partial_data(self):
        # 商品不存在时，即使供应商解析先发生也不留半截数据（单事务原子）
        cp = self.fairy("purchase", "--drawing-no", "NOPE", "--qty", "2",
                        "--price", "10", "--supplier", "甲厂")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("无此商品", cp.stderr)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM transactions")[0][0], 0)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM counterparties")[0][0], 0,
                         "中途失败不应留下供应商档案")


class TestOpening(T1Base):
    """T2 期初库存：追加语义、成本缺省 0、无此商品、校验。"""

    def test_append_semantics(self):
        self.add_product("--drawing-no", "170", "--name", "活塞")
        out = self.opening("--drawing-no", "170", "--qty", "10", "--cost", "4")
        self.assertEqual(out["qty"], 10.0)
        self.assertEqual(out["cost"], 4.0)
        self.assertEqual(out["drawing_no"], "170")
        self.assertEqual(out["name"], "活塞")
        self.opening("--drawing-no", "170", "--qty", "5", "--cost", "4")
        row = self.stock_row("170")
        self.assertEqual(row["qty"], 15.0, "同商品多次期初累加")

    def test_cost_default_zero(self):
        self.add_product("--drawing-no", "170", "--name", "活塞")
        out = self.opening("--drawing-no", "170", "--qty", "5")
        self.assertEqual(out["cost"], 0.0, "期初成本缺省 0")
        row = self.stock_row("170")
        self.assertEqual(row["qty"], 5.0)
        self.assertEqual(row["unit_cost"], 0.0)
        self.assertEqual(row["amount"], 0.0)

    def test_unknown_product_exit1(self):
        cp = self.fairy("opening", "--drawing-no", "NOPE", "--qty", "5")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("无此商品", cp.stderr)

    def test_qty_must_be_positive(self):
        self.add_product("--drawing-no", "170", "--name", "活塞")
        cp = self.fairy("opening", "--drawing-no", "170", "--qty", "0")
        self.assertEqual(cp.returncode, 1)
        self.assertNotEqual(cp.stderr, "")
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM opening_stock")[0][0], 0)

    def test_negative_cost_rejected(self):
        self.add_product("--drawing-no", "170", "--name", "活塞")
        cp = self.fairy("opening", "--drawing-no", "170", "--qty", "5", "--cost", "-1")
        self.assertEqual(cp.returncode, 1)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM opening_stock")[0][0], 0)


class TestStockCost(T1Base):
    """T2 库存报表：数量/当前加权均价/金额全现算，默认图号序。"""

    def test_zero_state(self):
        self.add_product("--drawing-no", "170", "--name", "活塞")
        row = self.stock_row("170")
        self.assertEqual(row["qty"], 0.0)
        self.assertEqual(row["unit_cost"], 0.0)
        self.assertEqual(row["amount"], 0.0)

    def test_opening_as_weighted_base(self):
        self.add_product("--drawing-no", "170", "--name", "活塞")
        self.opening("--drawing-no", "170", "--qty", "10", "--cost", "4")
        self.purchase("--drawing-no", "170", "--qty", "5", "--price", "10")
        row = self.stock_row("170")
        self.assertEqual(row["qty"], 15.0)
        self.assertEqual(row["unit_cost"], 6.0, "有期初时以期为基数重算 (40+50)/15")
        self.assertEqual(row["amount"], 90.0)

    def test_moving_average_recomputes_per_purchase(self):
        self.add_product("--drawing-no", "170", "--name", "活塞")
        self.opening("--drawing-no", "170", "--qty", "10", "--cost", "4")
        self.purchase("--drawing-no", "170", "--qty", "5", "--price", "10")   # 均价 → 6
        self.purchase("--drawing-no", "170", "--qty", "5", "--price", "14")   # 均价 → 8
        row = self.stock_row("170")
        self.assertEqual(row["qty"], 20.0)
        self.assertEqual(row["unit_cost"], 8.0, "(90+70)/20")
        self.assertEqual(row["amount"], 160.0)

    def test_freight_excluded_from_avg(self):
        self.add_product("--drawing-no", "170", "--name", "活塞")
        self.opening("--drawing-no", "170", "--qty", "10", "--cost", "4")
        self.purchase("--drawing-no", "170", "--qty", "5", "--price", "10", "--freight", "20")
        row = self.stock_row("170")
        self.assertEqual(row["unit_cost"], 6.0, "运费不进公式分子分母")
        self.assertEqual(row["amount"], 90.0)

    def test_multiple_opening_rows_aggregate_base(self):
        self.add_product("--drawing-no", "170", "--name", "活塞")
        self.opening("--drawing-no", "170", "--qty", "5", "--cost", "20")
        self.opening("--drawing-no", "170", "--qty", "3", "--cost", "30")
        self.purchase("--drawing-no", "170", "--qty", "2", "--price", "10")
        row = self.stock_row("170")
        self.assertEqual(row["qty"], 10.0)
        self.assertEqual(row["unit_cost"], 21.0, "(100+90+20)/10")
        self.assertEqual(row["amount"], 210.0)

    def test_default_order_by_drawing_no(self):
        for dn, name in [("300.14.14", "缸盖"), ("170", "活塞")]:
            self.add_product("--drawing-no", dn, "--name", name)
        self.opening("--drawing-no", "170", "--qty", "2", "--cost", "5")
        rows = self.stock_rows()
        self.assertEqual([r["drawing_no"] for r in rows], ["170", "300.14.14"])
        self.assertEqual(rows[0]["qty"], 2.0)
        self.assertEqual(rows[1]["qty"], 0.0)

    def test_stock_row_keys(self):
        self.add_product("--drawing-no", "170", "--name", "活塞", "--unit", "件")
        self.opening("--drawing-no", "170", "--qty", "2", "--cost", "5")
        row = self.stock_row("170")
        self.assertEqual(set(row), {"id", "drawing_no", "name", "unit", "qty", "unit_cost", "amount"})

    def test_filtered_stock_keeps_cost_columns(self):
        self.add_product("--drawing-no", "170", "--name", "活塞")
        self.add_product("--drawing-no", "171", "--name", "活塞环")
        self.opening("--drawing-no", "170", "--qty", "2", "--cost", "5")
        cp = self.fairy("query", "stock", "--drawing-no", "170")
        self.assertEqual(cp.returncode, 0, f"stderr: {cp.stderr}")
        rows = json.loads(cp.stdout)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["drawing_no"], "170")
        self.assertEqual(rows[0]["qty"], 2.0)
        self.assertEqual(rows[0]["unit_cost"], 5.0)
        self.assertEqual(rows[0]["amount"], 10.0)


class TestSale(T1Base):
    """T3 售出：成本快照、现结缺省、挂账标记、缺单价带出上次成交价。"""

    def _seed(self):
        self.add_product("--drawing-no", "170", "--name", "活塞", "--unit", "件")
        self.opening("--drawing-no", "170", "--qty", "10", "--cost", "4")

    def test_qty_and_drawing_no_required(self):
        self._seed()
        for args in (
            ("--drawing-no", "170", "--price", "10"),
            ("--qty", "3", "--price", "10"),
            ("--qty", "3", "--drawing-no", "170"),
        ):
            cp = self.fairy("sale", *args)
            self.assertEqual(cp.returncode, 1, f"stderr: {cp.stderr}")
            self.assertIn("参数缺失", cp.stderr)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM transactions")[0][0], 0,
                         "参数缺失不应落任何流水")

    def test_unknown_product_exit1_atomic(self):
        cp = self.fairy("sale", "--drawing-no", "NOPE", "--qty", "3",
                        "--price", "10", "--customer", "乙店")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("无此商品", cp.stderr)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM transactions")[0][0], 0)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM counterparties")[0][0], 0,
                         "中途失败不应留下客户档案")

    def test_basic_sale_cash_default_and_cost_snapshot(self):
        self._seed()
        out = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15")
        self.assertEqual(out["biz_type"], "sale")
        self.assertEqual(out["qty"], 3.0)
        self.assertEqual(out["price"], 15.0)
        self.assertEqual(out["cost"], 4.0, "成本列 = 售出时加权均价快照")
        self.assertEqual(out["freight"], 0.0, "运费可空默认 0")
        self.assertEqual(out["biz_date"], datetime.date.today().isoformat(), "日期缺省今天")
        self.assertIsNone(out["customer"], "缺客户默认现结不挂账")
        self.assertIs(out["credit"], False)
        self.assertNotIn("price_auto", out, "显式给价不标自动带出")
        row = self.stock_row("170")
        self.assertEqual(row["qty"], 7.0, "售出后库存数量减少")
        self.assertEqual(row["unit_cost"], 4.0, "售出不触发均价重算")
        self.assertEqual(row["amount"], 28.0)

    def test_sale_snapshot_uses_current_weighted_avg(self):
        self._seed()
        self.purchase("--drawing-no", "170", "--qty", "5", "--price", "10")  # 均价 → 6
        out = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15")
        self.assertEqual(out["cost"], 6.0, "快照取当时加权均价 (40+50)/15")
        row = self.stock_row("170")
        self.assertEqual(row["qty"], 12.0)
        self.assertEqual(row["unit_cost"], 6.0)
        self.assertEqual(row["amount"], 72.0)

    def test_sale_does_not_recompute_avg_frozen_snapshot(self):
        self._seed()
        out = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15")
        self.assertEqual(out["cost"], 4.0)
        self.purchase("--drawing-no", "170", "--qty", "5", "--price", "10")
        row = self.stock_row("170")
        self.assertEqual(row["qty"], 12.0)
        self.assertEqual(row["unit_cost"], 6.5, "后续进货按 (28+50)/12 重算")
        self.assertEqual(row["amount"], 78.0)
        rows = self.price_rows("--drawing-no", "170")
        self.assertEqual(rows[0]["cost"], 4.0, "已快照成本不受后续进货影响")

    def test_customer_create_or_resolve(self):
        self._seed()
        out = self.sale("--drawing-no", "170", "--qty", "1", "--price", "10",
                        "--customer", "乙店")
        self.assertEqual(out["customer"], "乙店")
        self.assertIs(out["credit"], False)
        self.assertEqual(self.query_db(
            "SELECT name, is_customer, is_supplier, is_credit FROM counterparties"),
            [("乙店", 1, 0, 0)], "新客户自动建档并打客户标记，现结不挂账")
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "10", "--customer", "乙店")
        self.assertEqual(len(self.query_db("SELECT id FROM counterparties")), 1,
                         "同名客户复用，不重复建档")

    def test_credit_sets_is_credit(self):
        self._seed()
        out = self.sale("--drawing-no", "170", "--qty", "2", "--price", "10",
                        "--customer", "乙店", "--credit")
        self.assertIs(out["credit"], True)
        self.assertEqual(self.query_db(
            "SELECT is_customer, is_credit FROM counterparties WHERE name = '乙店'"),
            [(1, 1)], "--credit 同时置往来单位挂账标记")

    def test_credit_requires_customer(self):
        self._seed()
        cp = self.fairy("sale", "--drawing-no", "170", "--qty", "2",
                        "--price", "10", "--credit")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("客户", cp.stderr)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM transactions")[0][0], 0)

    def test_price_auto_fill_last_trade_and_echo(self):
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "50", "--customer", "乙店")
        out = self.sale("--drawing-no", "170", "--qty", "1", "--customer", "乙店")
        self.assertEqual(out["price"], 50.0, "缺单价带出该客户该商品上次成交价直接写入")
        self.assertIs(out["price_auto"], True)
        self.assertEqual(self.query_db(
            "SELECT price FROM transactions WHERE biz_type='sale' ORDER BY id DESC LIMIT 1"),
            [(50.0,)], "带出的价格确实写入库中")

    def test_price_auto_fill_scoped_by_customer_and_product(self):
        self._seed()
        self.add_product("--drawing-no", "171", "--name", "活塞环")
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "50", "--customer", "乙店")
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "70", "--customer", "丙店")
        self.sale("--drawing-no", "171", "--qty", "1", "--price", "100", "--customer", "乙店")
        out = self.sale("--drawing-no", "170", "--qty", "1", "--customer", "丙店")
        self.assertEqual(out["price"], 70.0, "按客户过滤取该客户该商品上次成交价")
        out = self.sale("--drawing-no", "170", "--qty", "1", "--customer", "乙店")
        self.assertEqual(out["price"], 50.0, "不被其他产品的成交价干扰")

    def test_price_missing_no_history_must_ask(self):
        self._seed()
        for args in (
            ("--drawing-no", "170", "--qty", "3"),
            ("--drawing-no", "170", "--qty", "3", "--customer", "乙店"),
        ):
            cp = self.fairy("sale", *args)
            self.assertEqual(cp.returncode, 1, f"stderr: {cp.stderr}")
            self.assertIn("参数缺失", cp.stderr)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM transactions")[0][0], 0,
                         "无历史必问，不落流水")
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM counterparties")[0][0], 0,
                         "无历史失败时新建客户一并回滚")

    def test_negative_price_rejected(self):
        self._seed()
        cp = self.fairy("sale", "--drawing-no", "170", "--qty", "2", "--price", "-5")
        self.assertEqual(cp.returncode, 1)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM transactions")[0][0], 0)

    def test_freight_date_note_filled(self):
        self._seed()
        out = self.sale("--drawing-no", "170", "--qty", "2", "--price", "10",
                        "--freight", "20", "--date", "2026-08-01", "--note", "加急")
        self.assertEqual(out["freight"], 20.0)
        self.assertEqual(out["biz_date"], "2026-08-01")
        self.assertEqual(out["note"], "加急")
        self.assertEqual(out["cost"], 4.0, "售出运费不影响成本快照")

    def test_invalid_date_rejected(self):
        self._seed()
        cp = self.fairy("sale", "--drawing-no", "170", "--qty", "2",
                        "--price", "10", "--date", "2026/08/01")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("日期格式", cp.stderr)


class TestQueryPrice(T1Base):
    """T3 报价参考：历史成交价 + 成本快照列，产品/客户/日期组合筛选、日期降序。"""

    def _seed_sales(self):
        self.add_product("--drawing-no", "170", "--name", "活塞", "--unit", "件")
        self.opening("--drawing-no", "170", "--qty", "20", "--cost", "4")
        self.sale("--drawing-no", "170", "--qty", "2", "--price", "10",
                  "--date", "2026-08-01", "--customer", "乙店", "--note", "首批")
        self.sale("--drawing-no", "170", "--qty", "3", "--price", "12",
                  "--date", "2026-08-05")
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "15",
                  "--date", "2026-08-10", "--customer", "乙店")

    def test_requires_drawing_no(self):
        self._seed_sales()
        for args in ((), ("--from", "2026-08-01")):
            cp = self.fairy("query", "price", *args)
            self.assertEqual(cp.returncode, 1, f"stderr: {cp.stderr}")
            self.assertIn("参数缺失", cp.stderr)

    def test_sales_desc_with_cost_snapshot_columns(self):
        self._seed_sales()
        rows = self.price_rows("--drawing-no", "170")
        self.assertEqual([r["biz_date"] for r in rows],
                         ["2026-08-10", "2026-08-05", "2026-08-01"], "日期降序")
        first = rows[0]
        self.assertEqual(set(first),
                         {"id", "biz_date", "drawing_no", "name", "unit",
                          "qty", "price", "amount", "cost", "customer", "note"})
        self.assertEqual(first["drawing_no"], "170")
        self.assertEqual(first["name"], "活塞")
        self.assertEqual(first["unit"], "件")
        self.assertEqual(first["qty"], 1.0)
        self.assertEqual(first["price"], 15.0)
        self.assertEqual(first["amount"], 15.0, "金额 qty×price 现算")
        self.assertEqual(first["cost"], 4.0, "含该笔成交的成本快照列")
        self.assertEqual(first["customer"], "乙店")
        self.assertIsNone(rows[1]["customer"], "现结售出客户为空")
        self.assertIsNone(rows[1]["note"])
        self.assertEqual(rows[2]["note"], "首批")

    def test_price_excludes_purchases(self):
        self._seed_sales()
        self.purchase("--drawing-no", "170", "--qty", "5", "--price", "9",
                      "--date", "2026-08-12")
        rows = self.price_rows("--drawing-no", "170")
        self.assertEqual(len(rows), 3, "进货不是成交，不进入报价参考")
        self.assertNotIn("2026-08-12", [r["biz_date"] for r in rows])

    def test_price_filter_by_customer(self):
        self._seed_sales()
        rows = self.price_rows("--drawing-no", "170", "--customer", "乙店")
        self.assertEqual([r["biz_date"] for r in rows], ["2026-08-10", "2026-08-01"])
        self.assertTrue(all(r["customer"] == "乙店" for r in rows))

    def test_price_filter_by_date_range(self):
        self._seed_sales()
        rows = self.price_rows("--drawing-no", "170",
                               "--from", "2026-08-05", "--to", "2026-08-10")
        self.assertEqual([r["biz_date"] for r in rows], ["2026-08-10", "2026-08-05"])
        rows = self.price_rows("--drawing-no", "170",
                               "--from", "2026-08-10", "--to", "2026-08-10")
        self.assertEqual([r["biz_date"] for r in rows], ["2026-08-10"], "区间含边界")

    def test_price_empty_for_unknown_filters(self):
        self._seed_sales()
        self.assertEqual(self.price_rows("--drawing-no", "NOPE"), [])
        self.assertEqual(self.price_rows("--drawing-no", "170", "--customer", "没买过"), [])

    def test_price_invalid_date_rejected(self):
        self._seed_sales()
        cp = self.fairy("query", "price", "--drawing-no", "170", "--from", "2026/08/01")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("日期格式", cp.stderr)


class TestReceivePay(T1Base):
    """T4 收款/付款：必填校验、create-or-resolve、默认值、落库（累计制一笔冲减）。"""

    def test_counterparty_and_amount_required(self):
        for args in (("--counterparty", "乙店"), ("--amount", "100")):
            cp = self.fairy("receive", *args)
            self.assertEqual(cp.returncode, 1, f"stderr: {cp.stderr}")
            self.assertIn("参数缺失", cp.stderr)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM payments")[0][0], 0,
                         "参数缺失不应落任何收付款")

    def test_basic_receive_writes_payment_and_marks_customer(self):
        out = self.receive("--counterparty", "乙店", "--amount", "100")
        self.assertEqual(out["pay_type"], "receive")
        self.assertEqual(out["amount"], 100.0)
        self.assertEqual(out["counterparty"], "乙店")
        self.assertEqual(out["pay_date"], datetime.date.today().isoformat(), "日期缺省今天")
        self.assertIsNone(out["note"])
        self.assertIn("id", out)
        self.assertEqual(self.query_db(
            "SELECT name, is_customer, is_supplier, is_credit FROM counterparties"),
            [("乙店", 1, 0, 0)], "新往来单位自动建档并打客户标记")

    def test_pay_writes_payment_and_marks_supplier(self):
        out = self.pay("--counterparty", "甲厂", "--amount", "50",
                       "--date", "2026-08-01", "--note", "结货款")
        self.assertEqual(out["pay_type"], "pay")
        self.assertEqual(out["amount"], 50.0)
        self.assertEqual(out["pay_date"], "2026-08-01")
        self.assertEqual(out["note"], "结货款")
        self.assertEqual(self.query_db(
            "SELECT name, is_customer, is_supplier FROM counterparties"),
            [("甲厂", 0, 1)], "付款打供应商标记")

    def test_same_counterparty_reused(self):
        self.receive("--counterparty", "乙店", "--amount", "10")
        self.receive("--counterparty", "乙店", "--amount", "20")
        self.assertEqual(len(self.query_db("SELECT id FROM counterparties")), 1,
                         "同名往来单位复用，不重复建档")
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM payments")[0][0], 2)

    def test_negative_or_zero_amount_rejected(self):
        for amt in ("-5", "0"):
            cp = self.fairy("pay", "--counterparty", "甲厂", "--amount", amt)
            self.assertEqual(cp.returncode, 1, f"stderr: {cp.stderr}")
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM payments")[0][0], 0)

    def test_invalid_date_rejected(self):
        cp = self.fairy("receive", "--counterparty", "乙店", "--amount", "10",
                        "--date", "2026/08/01")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("日期格式", cp.stderr)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM payments")[0][0], 0)


class TestQueryCredit(T1Base):
    """T4 挂账对账单（spec D6）：欠款现算、一维表、总览、筛选。

    挂账判定 = 往来单位档案 is_credit 标记（查询时现算）：档案挂账后该单位
    交易自动累计欠款——现结售给已挂账单位同样计挂账（T4 口径）。
    """

    def _seed(self):
        self.add_product("--drawing-no", "170", "--name", "活塞", "--unit", "件")
        self.opening("--drawing-no", "170", "--qty", "100", "--cost", "4")

    def test_empty_on_fresh_db(self):
        out = self.credit_rows()
        self.assertEqual(out, {"records": [], "balances": []})

    def test_credit_sale_accumulates_debt(self):
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "3", "--price", "10",
                  "--customer", "乙店", "--credit", "--date", "2026-08-01")
        out = self.credit_rows()
        self.assertEqual(len(out["records"]), 1)
        rec = out["records"][0]
        self.assertEqual(rec, {"date": "2026-08-01", "counterparty": "乙店",
                               "doc_type": "sale", "amount": 30.0, "note": None},
                         "对账单列 = 日期/往来单位/单据/金额/备注")
        self.assertEqual(out["balances"], [{"counterparty": "乙店", "balance": 30.0}],
                         "期末欠款 = 挂账交易累计 − 收付款累计")

    def test_cash_sale_to_credit_flagged_unit_counts(self):
        # 现结售给 is_credit=1 单位同样累计欠款（挂账由档案标记推导，不看单笔 --credit）
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "2", "--price", "10",
                  "--customer", "乙店", "--credit", "--date", "2026-08-01")
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "15",
                  "--customer", "乙店", "--date", "2026-08-02")
        out = self.credit_rows()
        self.assertEqual(out["balances"], [{"counterparty": "乙店", "balance": 35.0}])
        self.assertEqual(len(out["records"]), 2)

    def test_non_credit_unit_excluded(self):
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "10",
                  "--customer", "丙店", "--date", "2026-08-01")  # 现结不挂账
        self.receive("--counterparty", "丙店", "--amount", "5")  # 收款照录但单位未挂账
        out = self.credit_rows()
        self.assertEqual(out, {"records": [], "balances": []}, "未挂账单位交易不计入欠款")

    def test_receive_reduces_balance(self):
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "3", "--price", "10",
                  "--customer", "乙店", "--credit", "--date", "2026-08-01")
        self.receive("--counterparty", "乙店", "--amount", "10", "--date", "2026-08-05")
        out = self.credit_rows()
        self.assertEqual(out["balances"], [{"counterparty": "乙店", "balance": 20.0}],
                         "收款一笔冲减总额，余额现算")
        self.assertEqual([r["doc_type"] for r in out["records"]], ["sale", "receive"],
                         "挂账流水 + 收付款同列一维表")

    def test_balance_recomputed_live_from_facts(self):
        # 收款发生在挂账标记之前：查询时按当前档案标记现算（ADR 0001，状态由事实推导）
        self._seed()
        self.receive("--counterparty", "乙店", "--amount", "20", "--date", "2026-08-01")
        self.sale("--drawing-no", "170", "--qty", "3", "--price", "10",
                  "--customer", "乙店", "--credit", "--date", "2026-08-05")
        out = self.credit_rows()
        self.assertEqual(out["balances"], [{"counterparty": "乙店", "balance": 10.0}])

    def test_purchase_to_credit_unit_and_pay_reduce(self):
        # 同一往来单位兼客户/供应商：进货也累计欠款，付款一笔冲减
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "10",
                  "--customer", "乙店", "--credit", "--date", "2026-08-01")
        self.purchase("--drawing-no", "170", "--qty", "2", "--price", "20",
                      "--supplier", "乙店", "--date", "2026-08-02")
        self.pay("--counterparty", "乙店", "--amount", "15", "--date", "2026-08-03")
        out = self.credit_rows()
        self.assertEqual(out["balances"], [{"counterparty": "乙店", "balance": 35.0}])
        self.assertEqual([(r["date"], r["doc_type"], r["amount"]) for r in out["records"]],
                         [("2026-08-01", "sale", 10.0),
                          ("2026-08-02", "purchase", 40.0),
                          ("2026-08-03", "pay", 15.0)], "默认日期升序")

    def test_same_date_order_tx_before_payment(self):
        # 同日：流水先于收付款、再按录入序（确定性排序）
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "10",
                  "--customer", "乙店", "--credit", "--date", "2026-08-01")
        self.receive("--counterparty", "乙店", "--amount", "10", "--date", "2026-08-01")
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "20",
                  "--customer", "乙店", "--credit", "--date", "2026-08-02")
        out = self.credit_rows()
        self.assertEqual([(r["date"], r["doc_type"]) for r in out["records"]],
                         [("2026-08-01", "sale"), ("2026-08-01", "receive"),
                          ("2026-08-02", "sale")])

    def test_filter_by_counterparty(self):
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "2", "--price", "10",
                  "--customer", "乙店", "--credit", "--date", "2026-08-01")
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "50",
                  "--customer", "丙店", "--credit", "--date", "2026-08-02")
        out = self.credit_rows("--counterparty", "丙店")
        self.assertEqual([r["counterparty"] for r in out["records"]], ["丙店"])
        self.assertEqual(out["balances"], [{"counterparty": "丙店", "balance": 50.0}],
                         "按单位筛选后总览只列该单位")

    def test_filter_by_date_range_narrows_records_only(self):
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "10",
                  "--customer", "乙店", "--credit", "--date", "2026-08-01")
        self.receive("--counterparty", "乙店", "--amount", "6", "--date", "2026-08-05")
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "20",
                  "--customer", "乙店", "--credit", "--date", "2026-08-10")
        out = self.credit_rows("--from", "2026-08-05", "--to", "2026-08-05")
        self.assertEqual([(r["date"], r["doc_type"]) for r in out["records"]],
                         [("2026-08-05", "receive")], "期间过滤收窄一维表（含边界）")
        self.assertEqual(out["balances"], [{"counterparty": "乙店", "balance": 24.0}],
                         "期末欠款是当前状态现算，不受期间过滤影响")

    def test_balances_overview_sorted_desc_with_zero_and_non_credit_excluded(self):
        self._seed()
        self.add_product("--drawing-no", "171", "--name", "活塞环")
        self.opening("--drawing-no", "171", "--qty", "50", "--cost", "4")
        self.sale("--drawing-no", "170", "--qty", "2", "--price", "10",
                  "--customer", "乙店", "--credit", "--date", "2026-08-01")
        self.sale("--drawing-no", "171", "--qty", "1", "--price", "60",
                  "--customer", "丁店", "--credit", "--date", "2026-08-02")
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "10",
                  "--customer", "戊店", "--credit", "--date", "2026-08-03")  # 挂账但无后续
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "10",
                  "--customer", "己店", "--date", "2026-08-04")  # 现结，非挂账
        out = self.credit_rows()
        self.assertEqual([(b["counterparty"], b["balance"]) for b in out["balances"]],
                         [("丁店", 60.0), ("乙店", 20.0), ("戊店", 10.0)], "欠款降序")
        self.assertNotIn("己店", [b["counterparty"] for b in out["balances"]],
                         "未挂账单位不进总览")

    def test_credit_unit_with_zero_balance_in_overview(self):
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "10",
                  "--customer", "乙店", "--credit", "--date", "2026-08-01")
        self.receive("--counterparty", "乙店", "--amount", "10", "--date", "2026-08-02")
        out = self.credit_rows()
        self.assertEqual(out["balances"], [{"counterparty": "乙店", "balance": 0.0}],
                         "挂账单位欠款清零后仍在总览（余额 0）")

    def test_sale_return_reduces_credit_balance(self):
        # T5 定案：挂账客户售出退货按负金额冲减欠款（余额与对账单同口径）
        self._seed()
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "10",
                      "--customer", "乙店", "--credit", "--date", "2026-08-01")
        self.reverse("--tx", str(s["id"]), "--date", "2026-08-02")
        out = self.credit_rows()
        self.assertEqual(out["balances"], [{"counterparty": "乙店", "balance": 0.0}],
                         "售出退货后欠款冲减回零")
        self.assertEqual([(r["date"], r["doc_type"], r["amount"]) for r in out["records"]],
                         [("2026-08-01", "sale", 30.0),
                          ("2026-08-02", "sale_return", -30.0)],
                         "对账单退货行金额为负")

    def test_purchase_return_reduces_credit_balance(self):
        # 挂账供应商进货后退货，欠供应商款相应减少
        self._seed()
        p = self.purchase("--drawing-no", "170", "--qty", "2", "--price", "20",
                          "--supplier", "甲厂", "--date", "2026-08-01")
        # 甲厂挂账：进货打供应商标记后，再收款侧无法置挂账；用 --credit 售出给甲厂打通挂账标记
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "5",
                  "--customer", "甲厂", "--credit", "--date", "2026-08-02")
        self.reverse("--tx", str(p["id"]), "--date", "2026-08-03")
        out = self.credit_rows()
        self.assertEqual(out["balances"], [{"counterparty": "甲厂", "balance": 5.0}],
                         "进货 40 − 退货 40 + 售出 5 = 5")
        self.assertEqual([(r["date"], r["doc_type"], r["amount"]) for r in out["records"]],
                         [("2026-08-01", "purchase", 40.0),
                          ("2026-08-02", "sale", 5.0),
                          ("2026-08-03", "purchase_return", -40.0)])

    def test_invalid_date_rejected(self):
        self._seed()
        cp = self.fairy("query", "credit", "--from", "2026/08/01")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("日期格式", cp.stderr)


class TestReverse(T1Base):
    """T5 红冲（spec D2/D5）：进货→purchase_return 按原进货价冲减、均价重算；
    售出→sale_return 按原成本快照回库、收入成本同冲；原单保留、ref_id 关联；
    单事务原子；每次 reverse 落审计日志（整行 before/after）。"""

    def _seed_product(self):
        self.add_product("--drawing-no", "170", "--name", "活塞", "--unit", "件")

    def _seed_stock(self):
        self._seed_product()
        self.opening("--drawing-no", "170", "--qty", "10", "--cost", "4")

    def test_reverse_requires_tx(self):
        self._seed_product()
        for args in ((), ("--date", "2026-08-01")):
            cp = self.fairy("reverse", *args)
            self.assertEqual(cp.returncode, 1, f"stderr: {cp.stderr}")
            self.assertIn("参数缺失", cp.stderr)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM transactions")[0][0], 0)

    def test_reverse_unknown_tx_exit1_atomic(self):
        self._seed_product()
        cp = self.fairy("reverse", "--tx", "999")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("无此流水", cp.stderr)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM transactions")[0][0], 0)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM audit_log")[0][0], 0,
                         "红冲失败不应留审计")

    def test_purchase_return_basic(self):
        self._seed_product()
        p = self.purchase("--drawing-no", "170", "--qty", "5", "--price", "10")
        out = self.reverse("--tx", str(p["id"]))
        self.assertEqual(out["biz_type"], "purchase_return", "进货退货类型")
        self.assertEqual(out["qty"], 5.0)
        self.assertEqual(out["price"], 10.0, "退货价 = 原单价格")
        self.assertEqual(out["ref_id"], p["id"], "红冲必须关联原单")
        self.assertEqual(out["biz_date"], datetime.date.today().isoformat(), "日期缺省今天")
        self.assertIsNone(out["cost"], "进货退货无成本快照")
        self.assertEqual(out["drawing_no"], "170")
        self.assertIn("audit_id", out)
        self.assertEqual(self.query_db(
            "SELECT biz_type, qty, price FROM transactions WHERE id = ?", p["id"]),
            [("purchase", 5.0, 10.0)], "原单保留不动")
        row = self.stock_row("170")
        self.assertEqual(row["qty"], 0.0, "数量按原进货量红字冲减")
        self.assertEqual(row["amount"], 0.0)

    def test_purchase_return_recomputes_average(self):
        self._seed_stock()
        p = self.purchase("--drawing-no", "170", "--qty", "5", "--price", "10")  # 均价 → 6
        self.reverse("--tx", str(p["id"]))
        row = self.stock_row("170")
        self.assertEqual(row["qty"], 10.0)
        self.assertEqual(row["unit_cost"], 4.0, "进货退货按原进货价冲减、均价重算回期初")
        self.assertEqual(row["amount"], 40.0)

    def test_sale_return_restores_stock_at_cost_snapshot(self):
        self._seed_stock()
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15")  # 快照 cost 4
        out = self.reverse("--tx", str(s["id"]))
        self.assertEqual(out["biz_type"], "sale_return")
        self.assertEqual(out["qty"], 3.0)
        self.assertEqual(out["price"], 15.0, "退货价 = 原售出价")
        self.assertEqual(out["cost"], 4.0, "按原售出单成本快照回库")
        self.assertEqual(out["ref_id"], s["id"])
        row = self.stock_row("170")
        self.assertEqual(row["qty"], 10.0, "货退回库存")
        self.assertEqual(row["unit_cost"], 4.0)
        self.assertEqual(row["amount"], 40.0)

    def test_sale_return_uses_original_snapshot_not_current_avg(self):
        self._seed_stock()
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15")  # cost 4
        self.purchase("--drawing-no", "170", "--qty", "5", "--price", "10")   # 均价 → 6.5
        out = self.reverse("--tx", str(s["id"]))
        self.assertEqual(out["cost"], 4.0, "回库按原单成本快照，不是当前均价 6.5")
        row = self.stock_row("170")
        self.assertEqual(row["qty"], 15.0, "10-3+5+3")
        self.assertEqual(row["amount"], 90.0, "40-12+50+12")
        self.assertEqual(row["unit_cost"], 6.0)

    def test_reverse_with_date_and_note(self):
        self._seed_product()
        p = self.purchase("--drawing-no", "170", "--qty", "2", "--price", "50")
        out = self.reverse("--tx", str(p["id"]), "--date", "2026-08-05", "--note", "厂家召回")
        self.assertEqual(out["biz_date"], "2026-08-05")
        self.assertEqual(out["note"], "厂家召回")

    def test_reverse_invalid_date_rejected_atomic(self):
        self._seed_product()
        p = self.purchase("--drawing-no", "170", "--qty", "2", "--price", "50")
        cp = self.fairy("reverse", "--tx", str(p["id"]), "--date", "2026/08/05")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("日期格式", cp.stderr)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM transactions")[0][0], 1,
                         "失败不应新增退货流水")
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM audit_log")[0][0], 0)

    def test_reverse_only_original_purchase_or_sale(self):
        self._seed_product()
        p = self.purchase("--drawing-no", "170", "--qty", "2", "--price", "50")
        r = self.reverse("--tx", str(p["id"]))
        self.assertEqual(r["biz_type"], "purchase_return")
        cp = self.fairy("reverse", "--tx", str(r["id"]))
        self.assertEqual(cp.returncode, 1, "不能红冲退货单本身")
        self.assertIn("仅支持红冲进货/售出原单", cp.stderr)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM transactions")[0][0], 2,
                         "失败不新增任何流水")
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM audit_log")[0][0], 1,
                         "失败不新增审计")

    def test_reverse_twice_rejected(self):
        # T5 定案：一单一冲——原单已有退货行再红冲 → 业务错误，防货重复回库
        self._seed_stock()
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15")
        self.reverse("--tx", str(s["id"]))
        cp = self.fairy("reverse", "--tx", str(s["id"]))
        self.assertEqual(cp.returncode, 1, f"stderr: {cp.stderr}")
        self.assertIn("已红冲", cp.stderr)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM transactions")[0][0], 2,
                         "失败不新增第二次退货流水")
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM audit_log")[0][0], 1,
                         "失败不新增审计")
        row = self.stock_row("170")
        self.assertEqual(row["qty"], 10.0, "货只回库一次")

    def test_reverse_output_carries_original(self):
        self._seed_stock()
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15")
        out = self.reverse("--tx", str(s["id"]))
        self.assertEqual(out["original"], {
            "id": s["id"], "biz_date": s["biz_date"], "biz_type": "sale",
            "qty": 3.0, "price": 15.0, "cost": 4.0,
        }, "输出带原单信息供 Fairy 回显")

    def test_reverse_writes_audit_before_after(self):
        self._seed_stock()
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15")
        out = self.reverse("--tx", str(s["id"]))
        rows = self.audit_rows()
        self.assertEqual(len(rows), 1)
        audit_id, table, record_id, action, before, after, note = rows[0]
        self.assertEqual((table, record_id, action), ("transactions", s["id"], "reverse"))
        self.assertIn("reverse", note)
        self.assertEqual(out["audit_id"], audit_id)
        before_d, after_d = json.loads(before), json.loads(after)
        self.assertEqual(set(before_d), TRANSACTION_COLUMNS, "审计含整行 before 快照")
        self.assertEqual(set(after_d), TRANSACTION_COLUMNS, "审计含整行 after 快照")
        self.assertEqual(before_d["id"], s["id"])
        self.assertEqual(before_d["biz_type"], "sale")
        self.assertEqual(before_d["qty"], 3.0)
        self.assertEqual(after_d["id"], out["id"])
        self.assertEqual(after_d["biz_type"], "sale_return")
        self.assertEqual(after_d["ref_id"], s["id"])
        self.assertEqual(after_d["cost"], 4.0)


class TestEdit(T1Base):
    """T5 修改（spec D5）：部分更新、不允许改图号（CLI 无该参数，结构性禁止）、
    改售出 qty/price 不改成本快照；每次修改写审计（整行 before/after，可完整还原）。"""

    def _seed_stock(self):
        self.add_product("--drawing-no", "170", "--name", "活塞", "--unit", "件")
        self.opening("--drawing-no", "170", "--qty", "10", "--cost", "4")

    def test_edit_requires_tx(self):
        self._seed_stock()
        cp = self.fairy("edit", "--qty", "2")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("参数缺失", cp.stderr)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM audit_log")[0][0], 0)

    def test_edit_unknown_tx_exit1_atomic(self):
        self._seed_stock()
        cp = self.fairy("edit", "--tx", "999", "--qty", "2")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("无此流水", cp.stderr)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM audit_log")[0][0], 0)

    def test_edit_no_fields_exit1(self):
        self._seed_stock()
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15")
        cp = self.fairy("edit", "--tx", str(s["id"]))
        self.assertEqual(cp.returncode, 1)
        self.assertIn("参数缺失", cp.stderr)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM audit_log")[0][0], 0,
                         "空修改不应写审计")

    def test_edit_partial_update_qty_price_keeps_cost(self):
        self._seed_stock()
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15")
        out = self.edit("--tx", str(s["id"]), "--qty", "2", "--price", "20")
        self.assertEqual(out["qty"], 2.0)
        self.assertEqual(out["price"], 20.0)
        self.assertEqual(out["cost"], 4.0, "改售出 qty/price 不改成本快照")
        self.assertEqual(out["changed"], ["qty", "price"])
        self.assertIn("audit_id", out)
        self.assertEqual(out["drawing_no"], "170", "不允许改商品，图号保持原值")
        row = self.stock_row("170")
        self.assertEqual(row["qty"], 8.0, "改数量后库存现算跟随")
        self.assertEqual(row["unit_cost"], 4.0)

    def test_edit_price_only_other_fields_unchanged(self):
        self._seed_stock()
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15",
                      "--customer", "乙店", "--date", "2026-08-01", "--note", "原单")
        out = self.edit("--tx", str(s["id"]), "--price", "12")
        self.assertEqual(out["price"], 12.0)
        self.assertEqual(out["qty"], 3.0, "只改传了的字段")
        self.assertEqual(out["customer"], "乙店")
        self.assertEqual(out["biz_date"], "2026-08-01")
        self.assertEqual(out["note"], "原单")

    def test_edit_customer_create_or_resolve(self):
        self._seed_stock()
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15")
        out = self.edit("--tx", str(s["id"]), "--customer", "乙店")
        self.assertEqual(out["customer"], "乙店")
        self.assertEqual(self.query_db(
            "SELECT name, is_customer, is_supplier, is_credit FROM counterparties"),
            [("乙店", 1, 0, 0)], "新客户自动建档并打客户标记")

    def test_edit_purchase_customer_marks_supplier(self):
        self._seed_stock()
        p = self.purchase("--drawing-no", "170", "--qty", "2", "--price", "50")
        out = self.edit("--tx", str(p["id"]), "--customer", "甲厂")
        self.assertEqual(out["supplier"], "甲厂", "进货单修改往来单位打供应商标记")
        self.assertEqual(self.query_db(
            "SELECT name, is_customer, is_supplier FROM counterparties"),
            [("甲厂", 0, 1)])

    def test_edit_date_and_invalid_date(self):
        self._seed_stock()
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15")
        out = self.edit("--tx", str(s["id"]), "--date", "2026-08-01")
        self.assertEqual(out["biz_date"], "2026-08-01")
        cp = self.fairy("edit", "--tx", str(s["id"]), "--date", "2026/08/01")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("日期格式", cp.stderr)
        self.assertEqual(self.query_db(
            "SELECT biz_date FROM transactions WHERE id = ?", s["id"])[0][0],
            "2026-08-01", "失败不改动原行")

    def test_edit_note_only(self):
        self._seed_stock()
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15")
        out = self.edit("--tx", str(s["id"]), "--note", "加急")
        self.assertEqual(out["note"], "加急")
        self.assertEqual(out["qty"], 3.0)
        self.assertEqual(out["price"], 15.0)
        self.assertEqual(out["changed"], ["note"])

    def test_edit_qty_must_be_positive(self):
        self._seed_stock()
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15")
        for q in ("0", "-2"):
            cp = self.fairy("edit", "--tx", str(s["id"]), "--qty", q)
            self.assertEqual(cp.returncode, 1, f"stderr: {cp.stderr}")
        self.assertEqual(self.query_db(
            "SELECT qty FROM transactions WHERE id = ?", s["id"])[0][0], 3.0,
            "失败不改动原行")
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM audit_log")[0][0], 0,
                         "校验失败不写审计")

    def test_edit_negative_price_rejected(self):
        self._seed_stock()
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15")
        cp = self.fairy("edit", "--tx", str(s["id"]), "--price", "-5")
        self.assertEqual(cp.returncode, 1)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM audit_log")[0][0], 0)

    def test_edit_rejects_drawing_no_flag(self):
        self._seed_stock()
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15")
        cp = self.fairy("edit", "--tx", str(s["id"]), "--drawing-no", "171")
        self.assertEqual(cp.returncode, 1, "edit 无图号参数：换商品走红冲+新单")
        self.assertEqual(self.query_db(
            "SELECT biz_type FROM transactions WHERE id = ?", s["id"])[0][0], "sale")

    def test_edit_same_customer_noop_no_crash(self):
        # 传的值与当前相同 → 无实际修改：业务错误（退出码 1，非系统错误 2），不写审计
        self._seed_stock()
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15",
                      "--customer", "乙店")
        cp = self.fairy("edit", "--tx", str(s["id"]), "--customer", "乙店")
        self.assertEqual(cp.returncode, 1, f"stderr: {cp.stderr}")
        self.assertIn("无实际修改", cp.stderr)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM audit_log")[0][0], 0)
        cp_id = self.query_db("SELECT id FROM counterparties WHERE name = '乙店'")[0][0]
        self.assertEqual(self.query_db(
            "SELECT counterparty_id FROM transactions WHERE id = ?", s["id"])[0][0],
            cp_id, "往来单位保持原值")

    def test_edit_same_value_qty_noop_no_audit(self):
        # 传的值与当前相同（qty）→ 无实际修改：业务错误，不写 before==after 的审计噪音
        self._seed_stock()
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15")
        cp = self.fairy("edit", "--tx", str(s["id"]), "--qty", "3")
        self.assertEqual(cp.returncode, 1, f"stderr: {cp.stderr}")
        self.assertIn("无实际修改", cp.stderr)
        self.assertEqual(self.query_db("SELECT COUNT(*) FROM audit_log")[0][0], 0)


    def test_edit_writes_audit_before_after(self):
        self._seed_stock()
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15",
                      "--note", "原备注")
        out = self.edit("--tx", str(s["id"]), "--qty", "2", "--note", "改后")
        rows = self.audit_rows()
        self.assertEqual(len(rows), 1)
        audit_id, table, record_id, action, before, after, note = rows[0]
        self.assertEqual((table, record_id, action), ("transactions", s["id"], "update"))
        self.assertIn("edit", note)
        self.assertEqual(out["audit_id"], audit_id)
        before_d, after_d = json.loads(before), json.loads(after)
        self.assertEqual(set(before_d), TRANSACTION_COLUMNS, "before 为整行快照")
        self.assertEqual(set(after_d), TRANSACTION_COLUMNS, "after 为整行快照")
        self.assertEqual(before_d["qty"], 3.0)
        self.assertEqual(before_d["note"], "原备注")
        self.assertEqual(after_d["qty"], 2.0)
        self.assertEqual(after_d["note"], "改后")
        self.assertEqual(after_d["id"], s["id"])
        self.assertEqual(after_d["cost"], 4.0, "成本快照不改")


class TestHistory(T1Base):
    """T5 单产品全部流水（spec D5/D6）：进货/售出/红冲逐笔、日期降序、
    退货行带「红冲」关联原单标记（ref 原单信息）。"""

    def _seed(self):
        self.add_product("--drawing-no", "170", "--name", "活塞", "--unit", "件")
        self.opening("--drawing-no", "170", "--qty", "100", "--cost", "4")

    def test_history_requires_drawing_no(self):
        self._seed()
        cp = self.fairy("query", "history")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("参数缺失", cp.stderr)

    def test_history_empty_for_unknown_product(self):
        self._seed()
        self.assertEqual(self.history_rows("--drawing-no", "NOPE"), [])

    def test_history_lists_all_types_with_ref_marker(self):
        self._seed()
        p = self.purchase("--drawing-no", "170", "--qty", "5", "--price", "10",
                          "--date", "2026-08-01")
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15",
                      "--date", "2026-08-05")
        sr = self.reverse("--tx", str(s["id"]), "--date", "2026-08-06")
        pr = self.reverse("--tx", str(p["id"]), "--date", "2026-08-07")
        rows = self.history_rows("--drawing-no", "170")
        self.assertEqual([r["biz_type"] for r in rows],
                         ["purchase_return", "sale_return", "sale", "purchase"], "日期降序")
        by_id = {r["id"]: r for r in rows}
        ret = by_id[sr["id"]]
        self.assertEqual(ret["ref_id"], s["id"])
        self.assertEqual(ret["ref"],
                         {"id": s["id"], "biz_date": "2026-08-05", "biz_type": "sale"},
                         "退货行带「红冲」关联原单标记")
        normal = by_id[s["id"]]
        self.assertIsNone(normal["ref"], "普通行无红冲标记")
        self.assertIsNone(normal["ref_id"])
        self.assertEqual(by_id[pr["id"]]["ref"],
                         {"id": p["id"], "biz_date": "2026-08-01", "biz_type": "purchase"})

    def test_history_columns(self):
        self._seed()
        self.purchase("--drawing-no", "170", "--qty", "5", "--price", "10")
        rows = self.history_rows("--drawing-no", "170")
        self.assertEqual(set(rows[0]),
                         {"id", "biz_date", "biz_type", "drawing_no", "name", "unit",
                          "qty", "price", "amount", "freight", "cost", "counterparty",
                          "note", "ref_id", "ref"})

    def test_history_date_filter(self):
        self._seed()
        self.purchase("--drawing-no", "170", "--qty", "5", "--price", "10",
                      "--date", "2026-08-01")
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "15",
                  "--date", "2026-08-10")
        rows = self.history_rows("--drawing-no", "170", "--from", "2026-08-05")
        self.assertEqual([r["biz_type"] for r in rows], ["sale"])
        rows = self.history_rows("--drawing-no", "170", "--to", "2026-08-05")
        self.assertEqual([r["biz_type"] for r in rows], ["purchase"])
        rows = self.history_rows("--drawing-no", "170",
                                 "--from", "2026-08-10", "--to", "2026-08-10")
        self.assertEqual([r["biz_type"] for r in rows], ["sale"], "区间含边界")

    def test_history_date_desc_order(self):
        self._seed()
        self.purchase("--drawing-no", "170", "--qty", "1", "--price", "10",
                      "--date", "2026-08-01")
        self.purchase("--drawing-no", "170", "--qty", "1", "--price", "30",
                      "--date", "2026-08-02")
        self.purchase("--drawing-no", "170", "--qty", "1", "--price", "20",
                      "--date", "2026-08-03")
        rows = self.history_rows("--drawing-no", "170")
        self.assertEqual([r["biz_date"] for r in rows],
                         ["2026-08-03", "2026-08-02", "2026-08-01"], "日期降序")

    def test_history_invalid_date_rejected(self):
        self._seed()
        cp = self.fairy("query", "history", "--drawing-no", "170", "--from", "2026/08/01")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("日期格式", cp.stderr)

    def test_history_amount_cost_columns(self):
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "3", "--price", "15")
        rows = self.history_rows("--drawing-no", "170")
        sale_row = rows[0]
        self.assertEqual(sale_row["amount"], 45.0, "金额 qty×price 现算")
        self.assertEqual(sale_row["cost"], 4.0, "售出行含成本快照")
        self.assertIsNone(sale_row["counterparty"])


class TestReportDaily(T1Base):
    """T6 日报（spec D6 四块）：默认昨天；无业务日汇总为零、异常区为空；
    毛利 = 售出净额 − 成本净额、运费单列不进毛利；挂账块按已确认口径
    （新增挂账额 = 挂账单位当日交易净额、收款/付款合计只算挂账单位、
    余额全历史现算）；明细当日逐笔（进货/售出/退货）、退货带「红冲」ref 标记；
    异常提醒 = 当日售出单价偏离该商品上次成交价（商品全局、严格更早）±30%。"""

    def _seed(self):
        self.add_product("--drawing-no", "170", "--name", "活塞", "--unit", "件")
        self.add_product("--drawing-no", "171", "--name", "活塞环", "--unit", "件")
        self.opening("--drawing-no", "170", "--qty", "100", "--cost", "4")
        self.opening("--drawing-no", "171", "--qty", "100", "--cost", "6")

    def daily(self, *args: str) -> dict:
        cp = self.fairy("report", "daily", *args)
        self.assertEqual(cp.returncode, 0, f"stderr: {cp.stderr}")
        return json.loads(cp.stdout)

    def test_daily_defaults_to_yesterday(self):
        y = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        self._seed()
        self.purchase("--drawing-no", "170", "--qty", "10", "--price", "5", "--date", y)
        out = self.daily()
        self.assertEqual(out["date"], y)
        self.assertEqual(out["summary"]["purchase"], {"count": 1, "amount": 50.0})

    def test_daily_empty_day_all_zero(self):
        self._seed()
        out = self.daily("--date", "2026-08-01")
        self.assertEqual(out, {
            "date": "2026-08-01",
            "summary": {"purchase": {"count": 0, "amount": 0.0},
                        "sale": {"count": 0, "amount": 0.0},
                        "freight": 0.0, "margin": 0.0},
            "credit": {"new_credit": 0.0, "received": 0.0, "paid": 0.0,
                       "balance": 0.0},
            "details": [], "alerts": [],
        }, "无业务日汇总为零、异常区为空")

    def test_daily_summary_basic(self):
        self._seed()
        self.purchase("--drawing-no", "170", "--qty", "10", "--price", "5",
                      "--freight", "10", "--date", "2026-08-10")
        self.sale("--drawing-no", "171", "--qty", "3", "--price", "15",
                  "--freight", "5", "--date", "2026-08-10")
        out = self.daily("--date", "2026-08-10")
        s = out["summary"]
        self.assertEqual(s["purchase"], {"count": 1, "amount": 50.0})
        self.assertEqual(s["sale"], {"count": 1, "amount": 45.0})
        self.assertEqual(s["freight"], 15.0, "运费 = 进货+售出 freight 求和")
        self.assertEqual(s["margin"], 27.0, "毛利 = 售出金额 − 成本快照合计 (45−3×6)")

    def test_daily_summary_nets_returns(self):
        self._seed()
        p = self.purchase("--drawing-no", "170", "--qty", "10", "--price", "5",
                          "--freight", "10", "--date", "2026-08-10")
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15",
                      "--date", "2026-08-10")
        self.reverse("--tx", str(s["id"]), "--date", "2026-08-10")
        self.reverse("--tx", str(p["id"]), "--date", "2026-08-10")
        out = self.daily("--date", "2026-08-10")
        s = out["summary"]
        self.assertEqual(s["purchase"], {"count": 0, "amount": 0.0}, "进货退货净额冲减")
        self.assertEqual(s["sale"], {"count": 0, "amount": 0.0}, "售出退货净额冲减")
        self.assertEqual(s["freight"], 10.0, "退货 freight=0，只留原单运费")
        self.assertEqual(s["margin"], 0.0)

    def test_daily_summary_partial_returns(self):
        self._seed()
        s1 = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15",
                       "--date", "2026-08-10")
        self.sale("--drawing-no", "170", "--qty", "2", "--price", "10",
                  "--date", "2026-08-10")
        self.reverse("--tx", str(s1["id"]), "--date", "2026-08-10")
        out = self.daily("--date", "2026-08-10")
        s = out["summary"]
        self.assertEqual(s["sale"], {"count": 1, "amount": 20.0})
        self.assertEqual(s["margin"], 12.0, "净额: 45+20−45 收入、12+8−12 成本")

    def test_daily_credit_block(self):
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "3", "--price", "10",
                  "--customer", "乙店", "--credit", "--date", "2026-08-10")
        self.receive("--counterparty", "乙店", "--amount", "10", "--date", "2026-08-10")
        out = self.daily("--date", "2026-08-10")
        self.assertEqual(out["credit"],
                         {"new_credit": 30.0, "received": 10.0, "paid": 0.0,
                          "balance": 20.0})

    def test_daily_new_credit_nets_return(self):
        self._seed()
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "10",
                      "--customer", "乙店", "--credit", "--date", "2026-08-10")
        self.reverse("--tx", str(s["id"]), "--date", "2026-08-10")
        out = self.daily("--date", "2026-08-10")
        self.assertEqual(out["credit"]["new_credit"], 0.0, "挂账退货负金额冲减新增挂账额")
        self.assertEqual(out["credit"]["balance"], 0.0)

    def test_daily_new_credit_includes_credit_purchase(self):
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "3", "--price", "10",
                  "--customer", "乙店", "--credit", "--date", "2026-08-10")
        self.purchase("--drawing-no", "171", "--qty", "2", "--price", "20",
                      "--supplier", "乙店", "--date", "2026-08-10")
        out = self.daily("--date", "2026-08-10")
        self.assertEqual(out["credit"]["new_credit"], 70.0, "挂账单位进货也计新增挂账额")

    def test_daily_received_paid_only_credit_units(self):
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "3", "--price", "10",
                  "--customer", "乙店", "--credit", "--date", "2026-08-10")
        self.receive("--counterparty", "乙店", "--amount", "10", "--date", "2026-08-10")
        self.receive("--counterparty", "丙店", "--amount", "5", "--date", "2026-08-10")
        self.pay("--counterparty", "丁厂", "--amount", "7", "--date", "2026-08-10")
        out = self.daily("--date", "2026-08-10")
        self.assertEqual(out["credit"]["received"], 10.0, "非挂账单位收款不计入")
        self.assertEqual(out["credit"]["paid"], 0.0, "非挂账单位付款不计入")
        self.assertEqual(out["credit"]["balance"], 20.0)

    def test_daily_credit_balance_full_history(self):
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "3", "--price", "10",
                  "--customer", "乙店", "--credit", "--date", "2026-08-01")
        self.receive("--counterparty", "乙店", "--amount", "10", "--date", "2026-08-01")
        self.sale("--drawing-no", "170", "--qty", "2", "--price", "10",
                  "--customer", "乙店", "--credit", "--date", "2026-08-10")
        out = self.daily("--date", "2026-08-10")
        self.assertEqual(out["credit"]["new_credit"], 20.0)
        self.assertEqual(out["credit"]["received"], 0.0, "收款在 8/1，不在日报日")
        self.assertEqual(out["credit"]["balance"], 40.0, "余额全历史现算 30−10+20")

    def test_daily_details_columns_and_ref_marker(self):
        self._seed()
        p = self.purchase("--drawing-no", "170", "--qty", "5", "--price", "10",
                          "--supplier", "甲厂", "--date", "2026-08-10")
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15",
                      "--customer", "乙店", "--date", "2026-08-10")
        sr = self.reverse("--tx", str(s["id"]), "--date", "2026-08-10")
        out = self.daily("--date", "2026-08-10")
        self.assertEqual([r["id"] for r in out["details"]],
                         [p["id"], s["id"], sr["id"]], "当日逐笔、按录入序")
        by_id = {r["id"]: r for r in out["details"]}
        self.assertEqual(set(by_id[s["id"]]),
                         {"id", "biz_date", "biz_type", "drawing_no", "name", "unit",
                          "qty", "price", "amount", "counterparty", "ref"})
        pur = by_id[p["id"]]
        self.assertEqual(pur["biz_type"], "purchase")
        self.assertEqual(pur["drawing_no"], "170")
        self.assertEqual(pur["name"], "活塞")
        self.assertEqual(pur["unit"], "件")
        self.assertEqual(pur["qty"], 5.0)
        self.assertEqual(pur["price"], 10.0)
        self.assertEqual(pur["amount"], 50.0)
        self.assertEqual(pur["counterparty"], "甲厂")
        self.assertIsNone(pur["ref"])
        ret = by_id[sr["id"]]
        self.assertEqual(ret["biz_type"], "sale_return")
        self.assertEqual(ret["amount"], 45.0, "明细金额 = qty×price 原值（方向由类型+ref 标记表达）")
        self.assertEqual(ret["ref"],
                         {"id": s["id"], "biz_date": "2026-08-10", "biz_type": "sale"},
                         "退货行带「红冲」关联原单标记")
        self.assertIsNone(by_id[s["id"]]["ref"], "普通行无红冲标记")

    def test_daily_excludes_other_days(self):
        self._seed()
        self.purchase("--drawing-no", "170", "--qty", "5", "--price", "10",
                      "--date", "2026-08-09")
        self.sale("--drawing-no", "170", "--qty", "3", "--price", "15",
                  "--date", "2026-08-11")
        out = self.daily("--date", "2026-08-10")
        self.assertEqual(out["summary"]["purchase"]["count"], 0)
        self.assertEqual(out["summary"]["sale"]["count"], 0)
        self.assertEqual(out["details"], [])

    def test_daily_alerts_flags_30_percent_deviation(self):
        self._seed()
        self.add_product("--drawing-no", "172", "--name", "缸盖", "--unit", "件")
        self.opening("--drawing-no", "172", "--qty", "10", "--cost", "8")
        # 基线（上次成交价）
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "100",
                  "--date", "2026-08-05")
        self.sale("--drawing-no", "171", "--qty", "1", "--price", "50",
                  "--date", "2026-08-05")
        self.sale("--drawing-no", "172", "--qty", "1", "--price", "20",
                  "--date", "2026-08-05")
        # 当日：170 −40% → 报；171 +20% → 不报；172 +50% → 报
        s170 = self.sale("--drawing-no", "170", "--qty", "1", "--price", "60",
                         "--date", "2026-08-10")
        self.sale("--drawing-no", "171", "--qty", "1", "--price", "60",
                  "--date", "2026-08-10")
        s172 = self.sale("--drawing-no", "172", "--qty", "1", "--price", "30",
                         "--date", "2026-08-10")
        out = self.daily("--date", "2026-08-10")
        self.assertEqual(out["alerts"],
                         [{"id": s170["id"], "drawing_no": "170", "name": "活塞",
                           "qty": 1.0, "price": 60.0, "last_price": 100.0,
                           "deviation": -0.4},
                          {"id": s172["id"], "drawing_no": "172", "name": "缸盖",
                           "qty": 1.0, "price": 30.0, "last_price": 20.0,
                           "deviation": 0.5}])

    def test_daily_alerts_no_prior_sale_skipped(self):
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "50",
                  "--date", "2026-08-10")
        out = self.daily("--date", "2026-08-10")
        self.assertEqual(out["alerts"], [], "首笔成交无上次成交价，不判定")
        self.assertEqual(out["summary"]["sale"]["count"], 1)

    def test_daily_alerts_global_price_not_customer_scoped(self):
        # 口径定案：上次成交价 = 商品全局最近价（不限客户）
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "100",
                  "--customer", "乙店", "--date", "2026-08-05")
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "50",
                  "--customer", "丙店", "--date", "2026-08-10")
        out = self.daily("--date", "2026-08-10")
        self.assertEqual(len(out["alerts"]), 1, "按商品全局价判定，丙店无同客户历史也报")
        self.assertEqual(out["alerts"][0]["price"], 50.0)
        self.assertEqual(out["alerts"][0]["last_price"], 100.0)

    def test_daily_alerts_zero_last_price_no_crash(self):
        # 售出价允许 0（只拒负）；上次成交价为 0 时无有效基准，跳过判定、不崩溃
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "0",
                  "--date", "2026-08-05")
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "50",
                  "--date", "2026-08-10")
        out = self.daily("--date", "2026-08-10")
        self.assertEqual(out["alerts"], [])
        self.assertEqual(out["summary"]["sale"]["count"], 1)

    def test_daily_alerts_ignore_purchases_and_returns(self):
        self._seed()
        s1 = self.sale("--drawing-no", "170", "--qty", "1", "--price", "50",
                       "--date", "2026-08-01")
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "100",
                  "--date", "2026-08-05")
        self.purchase("--drawing-no", "170", "--qty", "5", "--price", "500",
                      "--date", "2026-08-10")
        self.reverse("--tx", str(s1["id"]), "--date", "2026-08-10")
        out = self.daily("--date", "2026-08-10")
        self.assertEqual(out["alerts"], [],
                         "异常只查当日售出成交价：进货价与退货行不参与")

    def test_daily_invalid_date_rejected(self):
        self._seed()
        cp = self.fairy("report", "daily", "--date", "2026/08/10")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("日期格式", cp.stderr)


class TestQueryMargin(T1Base):
    """T6 期间毛利报表（spec D5/D6）：期间必选默认本月、可按产品/客户过滤；
    金额/成本/毛利/毛利率；按产品、按客户分组（毛利降序）；
    售出退货按原单冲减当期（期间内 sale_return 负方向计入）。"""

    def _seed(self):
        self.add_product("--drawing-no", "170", "--name", "活塞", "--unit", "件")
        self.add_product("--drawing-no", "171", "--name", "活塞环", "--unit", "件")
        self.opening("--drawing-no", "170", "--qty", "100", "--cost", "4")
        self.opening("--drawing-no", "171", "--qty", "100", "--cost", "6")

    def margin(self, *args: str) -> dict:
        cp = self.fairy("query", "margin", *args)
        self.assertEqual(cp.returncode, 0, f"stderr: {cp.stderr}")
        return json.loads(cp.stdout)

    def test_margin_defaults_to_current_month(self):
        today = datetime.date.today()
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "2", "--price", "10",
                  "--date", today.isoformat())
        out = self.margin()
        self.assertEqual(out["from"], f"{today.year:04d}-{today.month:02d}-01")
        self.assertEqual(out["to"], today.isoformat())
        self.assertEqual(out["amount"], 20.0)

    def test_margin_half_open_range_rejected(self):
        self._seed()
        for args in (("--from", "2026-08-01"), ("--to", "2026-08-10")):
            cp = self.fairy("query", "margin", *args)
            self.assertEqual(cp.returncode, 1, f"stderr: {cp.stderr}")
            self.assertIn("参数缺失", cp.stderr)

    def test_margin_invalid_date_rejected(self):
        self._seed()
        cp = self.fairy("query", "margin", "--from", "2026/08/01", "--to", "2026-08-10")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("日期格式", cp.stderr)

    def test_margin_from_after_to_rejected(self):
        self._seed()
        cp = self.fairy("query", "margin", "--from", "2026-08-10", "--to", "2026-08-01")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("晚于", cp.stderr)

    def test_margin_totals(self):
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "3", "--price", "15",
                  "--date", "2026-08-05")
        self.sale("--drawing-no", "171", "--qty", "2", "--price", "30",
                  "--date", "2026-08-06")
        self.sale("--drawing-no", "170", "--qty", "1", "--price", "10",
                  "--date", "2026-08-07")
        out = self.margin("--from", "2026-08-01", "--to", "2026-08-10")
        self.assertEqual(out["amount"], 115.0)
        self.assertEqual(out["cost"], 28.0)
        self.assertEqual(out["margin"], 87.0)
        self.assertEqual(out["margin_rate"], round(87 / 115, 4), "毛利率 = 毛利÷售出金额")

    def test_margin_empty_period(self):
        self._seed()
        out = self.margin("--from", "2026-08-01", "--to", "2026-08-10")
        self.assertEqual(out["amount"], 0.0)
        self.assertEqual(out["cost"], 0.0)
        self.assertEqual(out["margin"], 0.0)
        self.assertIsNone(out["margin_rate"], "无售出时毛利率为空")
        self.assertEqual(out["by_product"], [])
        self.assertEqual(out["by_customer"], [])

    def test_margin_sale_return_nets_in_period(self):
        self._seed()
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15",
                      "--date", "2026-08-05")
        self.sale("--drawing-no", "170", "--qty", "2", "--price", "10",
                  "--date", "2026-08-06")
        self.reverse("--tx", str(s["id"]), "--date", "2026-08-07")
        out = self.margin("--from", "2026-08-01", "--to", "2026-08-10")
        self.assertEqual(out["amount"], 20.0, "售出退货在期间内按原单冲减收入")
        self.assertEqual(out["cost"], 8.0, "成本同步冲减")
        self.assertEqual(out["margin"], 12.0)

    def test_margin_return_outside_period_ignored(self):
        self._seed()
        s = self.sale("--drawing-no", "170", "--qty", "3", "--price", "15",
                      "--date", "2026-08-05")
        self.sale("--drawing-no", "170", "--qty", "2", "--price", "10",
                  "--date", "2026-08-06")
        self.reverse("--tx", str(s["id"]), "--date", "2026-08-20")
        out = self.margin("--from", "2026-08-01", "--to", "2026-08-10")
        self.assertEqual(out["amount"], 65.0)
        self.assertEqual(out["cost"], 20.0)
        self.assertEqual(out["margin"], 45.0)

    def test_margin_groups_by_product_margin_desc(self):
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "3", "--price", "15",
                  "--date", "2026-08-05")
        self.sale("--drawing-no", "171", "--qty", "2", "--price", "30",
                  "--date", "2026-08-06")
        out = self.margin("--from", "2026-08-01", "--to", "2026-08-10")
        self.assertEqual([r["drawing_no"] for r in out["by_product"]], ["171", "170"],
                         "按毛利降序")
        p170, p171 = out["by_product"][1], out["by_product"][0]
        self.assertEqual(p170, {"drawing_no": "170", "name": "活塞", "amount": 45.0,
                                "cost": 12.0, "margin": 33.0,
                                "margin_rate": round(33 / 45, 4)})
        self.assertEqual(p171, {"drawing_no": "171", "name": "活塞环", "amount": 60.0,
                                "cost": 12.0, "margin": 48.0, "margin_rate": 0.8})

    def test_margin_groups_by_customer_including_cash(self):
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "2", "--price", "10",
                  "--customer", "乙店", "--date", "2026-08-05")
        self.sale("--drawing-no", "170", "--qty", "3", "--price", "20",
                  "--customer", "乙店", "--date", "2026-08-06")
        self.sale("--drawing-no", "171", "--qty", "1", "--price", "15",
                  "--date", "2026-08-07")
        out = self.margin("--from", "2026-08-01", "--to", "2026-08-10")
        self.assertEqual([r["customer"] for r in out["by_customer"]], ["乙店", None],
                         "现结售出归入 customer=null 组，毛利降序")
        yi, cash = out["by_customer"][0], out["by_customer"][1]
        self.assertEqual(yi, {"customer": "乙店", "amount": 80.0, "cost": 20.0,
                              "margin": 60.0, "margin_rate": 0.75})
        self.assertEqual(cash, {"customer": None, "amount": 15.0, "cost": 6.0,
                                "margin": 9.0, "margin_rate": 0.6})

    def test_margin_filter_by_product(self):
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "3", "--price", "15",
                  "--customer", "乙店", "--date", "2026-08-05")
        self.sale("--drawing-no", "171", "--qty", "2", "--price", "30",
                  "--date", "2026-08-06")
        out = self.margin("--from", "2026-08-01", "--to", "2026-08-10",
                          "--product", "170")
        self.assertEqual(out["amount"], 45.0)
        self.assertEqual(out["cost"], 12.0)
        self.assertEqual([r["drawing_no"] for r in out["by_product"]], ["170"])
        self.assertEqual([r["customer"] for r in out["by_customer"]], ["乙店"])

    def test_margin_filter_by_customer(self):
        self._seed()
        self.sale("--drawing-no", "170", "--qty", "3", "--price", "15",
                  "--customer", "乙店", "--date", "2026-08-05")
        self.sale("--drawing-no", "171", "--qty", "2", "--price", "30",
                  "--customer", "丙店", "--date", "2026-08-06")
        out = self.margin("--from", "2026-08-01", "--to", "2026-08-10",
                          "--customer", "乙店")
        self.assertEqual(out["amount"], 45.0)
        self.assertEqual([r["drawing_no"] for r in out["by_product"]], ["170"])
        self.assertEqual([r["customer"] for r in out["by_customer"]], ["乙店"])

    def test_margin_unknown_product_exit1(self):
        self._seed()
        cp = self.fairy("query", "margin", "--from", "2026-08-01", "--to", "2026-08-10",
                        "--product", "NOPE")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("无此商品", cp.stderr)

    def test_margin_unknown_customer_empty(self):
        self._seed()
        out = self.margin("--from", "2026-08-01", "--to", "2026-08-10",
                          "--customer", "没买过")
        self.assertEqual(out["amount"], 0.0)
        self.assertEqual(out["by_customer"], [])


if __name__ == "__main__":
    unittest.main()
