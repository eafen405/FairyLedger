"""fairy 单入口：argparse 分发，输出结构化 JSON，退出码 0/1/2（spec D5）。

- 0 成功 / 1 业务失败（参数缺失、图号重复、无此商品）/ 2 系统错误（DB 异常等）。
- 所有命令 stdout 输出结构化 JSON；错误消息一律走 stderr。
"""

import argparse
import json
import sqlite3
import sys
from typing import Any, Sequence

from . import db, products
from .errors import BusinessError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fairy",
        description="FairyLedger v4 进销存 CLI",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        help="SQLite 库文件路径（默认 ~/.fairyledger/ledger.db，可用 FAIRY_DB 环境变量覆盖）",
    )
    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    p_product = sub.add_parser("product", help="商品主数据")
    p_product_sub = p_product.add_subparsers(dest="subcommand", metavar="<子命令>")
    p_add = p_product_sub.add_parser("add", help="商品建档")
    p_add.add_argument("--drawing-no", help="图号；缺省自动编 ZC- 递增编号")
    p_add.add_argument("--name", help="正式名称（必填）")
    p_add.add_argument("--unit", help="单位，纯文本；缺省 '件'")
    p_add.add_argument("--alias", action="append", dest="aliases", help="别名，可多次")

    p_query = sub.add_parser("query", help="查询")
    p_query_sub = p_query.add_subparsers(dest="subcommand", metavar="<子命令>")
    p_stock = p_query_sub.add_parser("stock", help="当前库存")
    p_stock.add_argument("--drawing-no", help="按图号过滤")
    p_search = p_query_sub.add_parser("product", help="商品检索（歧义消解）")
    p_search.add_argument("--q", help="检索词：图号精确优先，否则词段 AND 匹配")
    return parser


def _emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def main(argv: Sequence[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse 用法错误默认退出码 2 → 按规约「参数缺失」归为业务错误 1
        raise SystemExit(1 if exc.code == 2 else exc.code)

    if not args.command:
        parser.print_usage(sys.stderr)
        print("fairy: error: 缺少命令", file=sys.stderr)
        return 1

    try:
        conn = db.connect(db.resolve_db_path(args.db))
        try:
            db.ensure_schema(conn)
            if args.command == "product" and args.subcommand == "add":
                _emit(
                    products.add_product(
                        conn, args.drawing_no, args.name, args.unit, args.aliases
                    )
                )
            elif args.command == "query" and args.subcommand == "stock":
                _emit(products.query_stock(conn, args.drawing_no))
            elif args.command == "query" and args.subcommand == "product":
                q = (args.q or "").strip()
                if not q:
                    raise BusinessError("参数缺失: --q")
                _emit(products.search_products(conn, q))
            else:
                detail = getattr(args, "subcommand", None)
                raise BusinessError(
                    f"未知命令: {args.command}" + (f" {detail}" if detail else "")
                )
        finally:
            conn.close()
        return 0
    except BusinessError as exc:
        print(f"fairy: error: {exc}", file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError) as exc:
        print(f"fairy: error: 系统错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
