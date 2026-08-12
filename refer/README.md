# 柴油机配件进销存记账本

SQLite 单文件数据库 + 命令行工具，记录每天的进货和售出流水。

## 文件位置

```
projects/parts-ledger/
├── ledger.py          # 主程序（纯 Python 标准库，无需安装依赖）
├── ledger.db          # 数据库（单文件，备份就是复制它）
└── 流水导出_*.csv     # 导出结果（可用 Excel 打开）
```

## 快速上手

### 1. 记一笔售出

```bash
python3 ledger.py sell \
  --date 8/9 \
  --name "缸盖总成" --model "210" --unit 件 \
  --qty 1 --price 5200 \
  --counterparty "徐丽" --region "湖南" --phone "13812345678"
```

- `--date` 可省略（默认今天），支持 `8/9`、`2026-8-9`、`2026-08-09` 格式
- 商品（名称+型号）自动匹配：录过的直接复用，没录过的自动新建
- 金额自动计算 = 数量 × 单价，不用手算
- 客户自动匹配：录过的直接复用，没录过的自动新建

### 2. 记一笔进货

```bash
python3 ledger.py buy \
  --date 8/9 \
  --name "喷油嘴套耐磨垫" --model "260" --unit 个 \
  --qty 20 --price 30 --cost 28 \
  --counterparty "济南配件商" --region "济南"
```

- `--cost 28` 顺带记录该商品的参考进价（算利润用）
- 进货售出同一套命令，`buy` 就是进货、`sell` 就是售出

### 3. 查询

```bash
python3 ledger.py daily                 # 今天日报
python3 ledger.py daily --date 8/7      # 指定日期日报
python3 ledger.py monthly               # 本月月报（按天汇总）
python3 ledger.py monthly 2026-08       # 指定月份
python3 ledger.py stock                 # 当前库存（进货-售出）
python3 ledger.py profit                # 本月毛利（需先记录进价）
python3 ledger.py list-products         # 商品列表
python3 ledger.py list-customers        # 客户/供应商列表
```

### 4. 导出和备份

```bash
python3 ledger.py export                # 导出全部流水为 CSV（Excel 可打开）
python3 ledger.py backup                # 备份数据库为 backup_日期.db
```

建议每周备份一次，备份文件复制走就完事（单文件）。

## 设计说明

- **商品表**：名称 + 型号分开存（如"缸盖总成"+型号"210"），单位分件/个
- **往来单位表**：客户和供应商统一存放，带地区、电话
- **流水表**：进货售出同表，`biz_type` 区分（purchase/sale），库存 = 进货量 - 售出量，一条 SQL 可算任意时段
- **库存负数提醒**：只录了售出还没录进货时，`stock` 会显示 ⚠️ 库存为负，提醒你补录进货
- **毛利**：= (售价 - 参考进价) × 数量，参考进价来自最近一次 `buy` 时记录的 `--cost` 或 `add-product --cost`

## 注意事项

- 数据不涉及密钥/敏感信息，但属于你的业务数据，注意别把 ledger.db 传到公共场合
- 误录了怎么办：告诉我哪条记错了，我帮你改（当前没有 delete/undo 命令，避免误删，需要时我手动处理）
