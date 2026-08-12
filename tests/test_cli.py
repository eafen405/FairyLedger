"""T1 建库骨架与建档 — CLI 黑盒测试（spec Testing Decisions 单一 seam）。

只断言 `fairy` 命令的外部行为：stdout 结构化 JSON、退出码 0/1/2、
写入后通过后续命令可观察的状态。schema 核对是同一 seam 的验证手段——
只读打开临时库核对表/列（spec 允许对无 CLI 暴露处做只读核对）。
"""

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


if __name__ == "__main__":
    unittest.main()
