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


if __name__ == "__main__":
    unittest.main()
