# FairyLedger v4

柴油机配件进销存 CLI：SQLite 单文件库 + 单入口 `fairy`。云端代理 Fairy 识别语音意图后，把业务事实写库、按需产出日报/查询报表/周期汇总/整库导出。规格见 `docs/spec/v4-spec.md`。

## 安装

```bash
python3 -m venv .venv
.venv/bin/pip install .        # 安装 fairy 命令 + openpyxl 依赖
```

装完后 `fairy` 即可直接执行（console_script）。不安装也可用 `.venv/bin/python fairy` 直接跑。

> `./fairy` 的 shebang 指向系统 `python3`，需要系统 python3 已装 `openpyxl`；否则 `--out` / `export` / `report period` 会报系统错误（退出码 2）。推荐用 venv 或 `pip install .`。

## 跑测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

当前 188 个用例。测试是 CLI 黑盒：只断言 stdout 结构化 JSON、退出码 0/1/2、以及写入后经后续命令可观察的状态变化（`audit_log` 等无 CLI 暴露处允许只读打开临时库核对）。

## 冒烟验收（效果自测）

用临时库跑一遍主流程，每步退出码应为 0、stdout 为合法 JSON：

```bash
DB=$(mktemp -d)/smoke.db
F=".venv/bin/python fairy"        # 若已 pip install，可写 F=fairy

# 建档 / 进货（必报进价）/ 售出（成本快照=当时均价）
$F --db "$DB" product add --name 缸盖 --unit 件 --drawing-no 300.14.14
$F --db "$DB" purchase --drawing-no 300.14.14 --qty 10 --price 50 --freight 20 --supplier 供应商A --date 2026-08-12
$F --db "$DB" sale     --drawing-no 300.14.14 --qty 3  --price 80 --date 2026-08-12

# 查询：库存 / 毛利
$F --db "$DB" query stock                                        # 数量 7、当前均价 50、金额 350
$F --db "$DB" query margin --from 2026-08-01 --to 2026-08-31     # margin = 售出金额 − 成本快照，运费不参与

# 挂账售出 + 收款 + 对账单
$F --db "$DB" sale     --drawing-no 300.14.14 --qty 2 --price 80 --customer 客户B --credit --date 2026-08-12
$F --db "$DB" receive  --counterparty 客户B --amount 100 --date 2026-08-12
$F --db "$DB" query credit                                       # records（逐笔）+ balances（欠款 60）

# 排序 / 文件产出
$F --db "$DB" query stock --sort-by-qty
$F --db "$DB" query stock --out /tmp/stock.xlsx                  # stdout 仍是 JSON，且落 /tmp/stock.xlsx

# 报告 / 导出 / 备份
$F --db "$DB" report daily --date 2026-08-12
$F --db "$DB" export --out /tmp/export.xlsx
$F --db "$DB" backup                                             # 库同目录 backups/ 轮转 7 份
```

验收要点：

- 每个命令 stdout 是合法 JSON；非 0 时错误消息走 stderr。
- `query stock` 的 `qty` = 期初 + 流水净量（现算），`unit_cost` = 当前加权均价。
- `query margin` 的 `margin` = 售出金额 − 成本快照合计；运费不进成本/毛利。
- 红冲/修改：`fairy reverse --tx <id>` / `fairy edit --tx <id> [--qty/--price/--customer/--date/--note]`，原单 id 从写入命令返回 JSON 的 `id` 取。
- 审计：`opening` / `edit` / `reverse` 都写 `audit_log`，action 为 `insert` / `update` / `reverse`（无 CLI 暴露，可只读开库核对）。

## 命令面

- 写入：`product add` / `purchase` / `sale` / `opening` / `receive` / `pay` / `reverse` / `edit`
- 查询：`query stock | price | credit | history | product | margin`（均可带 `--out` 落表格文件）
- 报告/产出：`report daily` / `report period` / `export` / `backup`
- 完整字段见 `fairy <命令> --help` 与 `docs/spec/v4-spec.md` D5。
