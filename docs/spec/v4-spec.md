# FairyLedger v4 完整规格（可执行）

> 规格来源：wayfinder map「Wayfinding: FairyLedger v4 进销存规格」（T1–T7 决策 tickets 的 resolution）＋ ADR 0001–0004 ＋ 根目录 `CONTEXT.md` ＋ 2026-08-13 已确认的 fog 约定。ticket resolution 是决策的唯一出处；本 spec 将其收束为可执行规格，文字歧义以本 spec 为准。`refer/` 下 v2/v3 文档仅作历史背景，**不是设计标准**。

## Problem Statement

主人经营柴油机配件进销存：进货、售出、退换、往来欠款全靠口头和记忆，账目分散在脑子里、纸面或随手记，说不清当前库存、卖出去的毛利、谁欠多少钱。要管清楚就得记流水账，但记账本身不能成为负担——主人不会坐在电脑前敲键盘录入，也不会为了记一笔先建档案、查价目表。

需要一个极轻量的方案：主人通过飞书语音口述给云端代理 Fairy（hermesAgent），Fairy 听懂后把业务事实写进 SQLite 单文件库，按需产出日报、查询报表、周期汇总和整库导出。系统要保证账实一致（状态由事实推导、随查随算）、成本可回溯（每笔售出快照当时加权平均成本）、纠错可溯源（修改留审计、退货走红冲关联原单），让主人随时开口一问就能知道库存、毛利和往来欠款。

## Solution

部署形态：SQLite 单文件库 + Python CLI 单入口 `fairy`，运行在 Fairy 云端环境；**单写者假设**——Fairy 是唯一写入方、串行执行，读可并发（SQLite 天然支持），不需要锁或排队。

- **只存事实，状态现算**（ADR 0001）：库中只落期初库存、流水、收付款三个事实源；当前库存 = 期初合计 + 流水净量，金额 = 数量 × 单价，往来欠款 = 挂账交易累计 − 收付款累计，全部随查随算、不落列。
- **语音意图驱动**：11 个意图覆盖全部操作（进货/售出/查询/修改/红冲/收款/付款/期初录入/生成报告/放弃·取消），Fairy 识别意图 → 缺省规则补默认 → 歧义时列候选让主人选 → 写入后复述要素回显。
- **成本语义**（ADR 0004）：进货必录进价，移动加权平均，只有进货触发重算；每笔售出快照当时加权成本；红冲必须关联原单、按原单成本冲；毛利 = 收入 − 快照成本。进货运费当期费用化，不进成本、不进毛利，各报表单列求和（T7）。
- **纠错双轨**：录入错误 → 修改原单并自动写行级审计日志（可完整还原）；业务变化（退货/换货）→ 红冲关联原单，不删除。审计日志永久保留。
- **报告**：日报（次日清晨发昨日，Fairy 发文字消息）+ 按需查询报表（报价参考/当前库存/对账单/毛利）+ 周期汇总（周/月/年一套结构，出 Excel）+ 整库 Excel 导出 + 每日备份轮转 7 份。
- **输出契约**：CLI 全部输出结构化 JSON 给 Fairy 解析，退出码 0/1/2；报告/导出产出的文件直接给主人（一线内容 = 未经 AI 加工的文件，Fairy 不润色），Fairy 的文字转述（摘要）仍不可少。

## User Stories

### 商品主数据

1. As a 主人, I want 第一次进货/售出时顺口建档（图号/名称/别名/单位）, so that 不用单独开电脑先录档案.
2. As a 主人, I want 厂家没给图号的商品自动编 `ZC-` 编号, so that 每个商品都有唯一图号可查.
3. As a 主人, I want 商品有别名（俗名）时挂上别名, so that 我口述俗称也能找到它.
4. As a 主人, I want 单位只是纯文本（件/副/对）, so that 不用维护换算关系.

### 进货

5. As a 主人, I want 口述进货时必报进价, so that 成本根基不缺失.
6. As a 主人, I want 进货运费可空默认 0, so that 不是每笔都要报.
7. As a 主人, I want 一票多产品运费只报一次、记在本批第一笔流水上, so that 不用分摊.
8. As a 主人, I want 每笔进货后库存和加权平均成本自动更新, so that 后续售出成本自动正确.
9. As a 主人, I want 进货没提供应商也能落库（供应商可空）, so that 不会为记一笔先建档案.

### 售出

10. As a 主人, I want 售出没提客户时默认现结、不挂账, so that 最快的日常场景一句话搞定.
11. As a 主人, I want 售出缺单价时自动带出上次成交价直接写入, so that 不用现场查价.
12. As a 主人, I want 卖到库里没有的新商品时确认建档并自动补录等量期初库存归零, so that 库存不显示假负数.
13. As a 主人, I want 进货+出货一起说时拆成两笔常规流水, so that 每笔都有独立记录.

### 红冲与修改

14. As a 主人, I want 进货退货按原进货价红冲原单, so that 数量金额都冲得对.
15. As a 主人, I want 售出退货按原售出成本快照把货退回库存, so that 毛利随该笔正确冲减.
16. As a 主人, I want 换货时先红冲原单再录新单, so that 换货有据可查.
17. As a 主人, I want 改错时修改原单并自动留审计日志, so that 任何时候能还原.
18. As a 主人, I want 修改不能改商品（图号）, so that 不会改乱商品归属.
19. As a 主人, I want 修改售出单价格不影响成本快照, so that 历史毛利口径不变.

### 收款 / 付款 / 挂账

20. As a 主人, I want 往来单位打挂账标记后交易自动累计欠款, so that 不用每次自己算.
21. As a 主人, I want 收款/付款一笔冲减累计欠款, so that 欠款余额自动对.
22. As a 主人, I want 查某单位欠款时看到一维对账单（挂账流水+收付款逐笔）, so that 一眼看清往来.

### 期初库存

23. As a 主人, I want 启用时一次录入期初库存, so that 不用导入历史流水.
24. As a 主人, I want 期初库存可分多次追加（货不都在一处）, so that 盘点补录方便.
25. As a 主人, I want 期初缺成本时默认为 0 并收到 Fairy 提醒, so that 录入不被卡住.

### 查询

26. As a 主人, I want 查某产品历史成交价（含每笔成交的成本快照）, so that 报价有依据.
27. As a 主人, I want 按客户+产品过滤历史成交价, so that 知道给这个客户卖过多少钱.
28. As a 主人, I want 查当前库存（数量/当前均价/金额）, so that 知道手里有什么、值多少.
29. As a 主人, I want 查期间毛利（金额/成本/毛利/毛利率，可按产品/客户分组）, so that 知道赚了多少.
30. As a 主人, I want 查单产品全部流水（进货/售出/红冲逐笔）, so that 知道它动过什么.

### 报告

31. As a 主人, I want 每天早晨收到昨日日报, so that 当天心里有数.
32. As a 主人, I want 日报里列出当日单价偏离上次成交价 ±30% 的流水, so that 能发现口误或录错.
33. As a 主人, I want 月报/年报（期间汇总+按产品客户 TOP+明细）, so that 月末年尾有总结.
34. As a 主人, I want 整库导出 Excel（5 个 Sheet）, so that 要核对或交给别人时有完整文件.
35. As a 主人, I want 每日自动备份库文件并保留 7 份, so that 库坏了能找回.

### 交互

36. As a 主人, I want 漏说可空字段时 Fairy 先落库再追问、之后回填, so that 不会因为一句话卡住.
37. As a 主人, I want 多商品命中时 Fairy 列出候选报序号, so that 我选一个就行.
38. As a 主人, I want 图号精确匹配优先于名称/别名模糊匹配, so that 唯一图号最可靠.
39. As a 主人, I want 写入前说"算了"就不落库、写入后要撤走红冲/修改, so that 怎么反悔都有路.
40. As a 主人, I want Fairy 不设超时、不主动催, so that 我想好了再说.

### 数据与运维

41. As a Fairy, I want 所有命令输出结构化 JSON 和明确退出码, so that 我能可靠解析并如实报给主人.
42. As a Fairy, I want 每条写入命令单事务原子执行, so that 不会留半截数据.
43. As a Fairy, I want 数据恢复由我执行（复制备份覆盖主库并校验）, so that 主人不用碰服务器文件.
44. As a 主人, I want 审计日志永久保留, so that 任何时候都能追溯纠错.

## Implementation Decisions

### D1 数据模型（8 表 DDL，T1 定稿）

SQLite 单文件库，8 张表。`products`、`aliases`、`counterparties`、`transactions`、`payments`、`opening_stock`、`audit_log`（建表语句见下）。要点：

- **图号身份**：`drawing_no` 唯一且非空，是唯一查询凭据；厂家无图号时按 `ZC-` 前缀递增自编，与厂家号共用同一列。
- **别名**：独立表，0..N 可空；查询时图号/名称/别名一起模糊搜。
- **往来单位**：单表，客户/供应商标记位可同时勾选；`is_credit` 布尔挂账标记，无额度上限。
- **流水**：`cost` 为售出时成本快照；`freight` 进货/售出均可、默认 0；**不留 `amount` 物理列**，一律 `qty × price` 现算；`ref_id` 红冲关联原单。
- **收付款**：累计制，一笔冲减总额，不逐笔关联 transactions，余额现算。
- **期初库存**：常量基线，启用时录、之后不改；当前库存 = 期初合计 + 流水净量现算。
- **审计日志**：行级快照，改/删前整行 + 后整行，可完整还原；永久保留，不设删除。

```sql
-- 商品主数据
CREATE TABLE products (
    id          INTEGER PRIMARY KEY,
    drawing_no  TEXT NOT NULL UNIQUE,  -- 图号：厂家号或自编号(ZC-前缀)，唯一查询凭据
    name        TEXT NOT NULL,         -- 正式名称
    unit        TEXT DEFAULT '件',     -- 件/副/对，纯文本，无换算
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);

-- 别名（俗名），可空：0..N 个
CREATE TABLE aliases (
    id          INTEGER PRIMARY KEY,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    alias       TEXT NOT NULL
);

-- 往来单位：客户/供应商可同时勾选
CREATE TABLE counterparties (
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

-- 流水（事实源）：amount 不落列，一律 qty×price 现算
CREATE TABLE transactions (
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
CREATE INDEX idx_tx_date    ON transactions(biz_date);
CREATE INDEX idx_tx_product ON transactions(product_id);
CREATE INDEX idx_tx_cp      ON transactions(counterparty_id);

-- 收/付款：累计制，一笔冲减总额，不逐笔关联
CREATE TABLE payments (
    id              INTEGER PRIMARY KEY,
    pay_date        TEXT NOT NULL,
    pay_type        TEXT NOT NULL,   -- receive收客户款 / pay付供应商款
    counterparty_id INTEGER NOT NULL REFERENCES counterparties(id),
    amount          REAL NOT NULL,
    note            TEXT,
    created_at      TEXT DEFAULT (datetime('now','localtime'))
);

-- 期初库存：常量基线，启用时录一次，之后不改
CREATE TABLE opening_stock (
    id          INTEGER PRIMARY KEY,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    qty         REAL NOT NULL,
    cost        REAL,            -- 期初成本，供加权起点
    recorded_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 审计日志：行级快照，改/删前整行+后整行
CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY,
    table_name  TEXT NOT NULL,
    record_id   INTEGER NOT NULL,
    action      TEXT NOT NULL,   -- update/delete
    before_json TEXT,
    after_json  TEXT,
    note        TEXT,
    changed_at  TEXT DEFAULT (datetime('now','localtime'))
);
```

派生值一律不落表（ADR 0001）：金额、当前库存、欠款余额、当前加权均价全部现算。单位不建换算（ADR 0002）；同一商品混单位录入的提醒归交互层。

### D2 库存成本语义（ADR 0004，T2/T7）

**加权平均公式**：单位成本 =（进货前结存金额 + 本次进货金额）÷（进货前结存数量 + 本次进货数量）。

- 只有进货触发重算；售出按当时加权均价快照成本出库，本身不改变均价（移动加权平均）。
- 期初库存为首笔结存基数；无期初时公式退化为本次进货单价。
- 运费**不进**公式分子分母（T7 费用化）。

**不允许负库存（业务纪律，非系统闸门）**：业务上不发生先卖后进（没货不会卖，人的判断）。系统不设负库存闸门、无暂估/校正分支，售出成本快照一律取当时加权均价；记录层面库存不足（漏录期初/进货，实物有货）靠补录修正，日报异常提醒兜底。

**红冲（必须关联原单 `ref_id`，按原单成本冲；退货价 = 原单价格）**：

- 进货退货：数量与金额按原进货单价红字冲减，结存均价随之重算；不直接冲减当期毛利（只减少后续可售成本）。
- 售出退货：按原售出单的成本快照把货退回库存（数量、金额按快照恢复），均价重算；收入与成本同步冲减，毛利按该笔减少。
- 换货 = 红冲原单 + 新录一张。
- 红冲致记录为负不硬拒（同「不允许负库存」原则：人在场判断）。

**毛利口径**：毛利 = 收入 − 快照成本（金额现算，`cost` 为售出时快照，无额外扣减）。售出退货冲减当期毛利；进货退货不直接冲。历史已结转成本不回滚。

**进货运费（T7）**：当期费用化（小企业准则第六十五条批发/零售业口子），不算成本、不进加权平均、不摊入毛利；售出运费属期间费用，不计入毛利扣除项；一票多产品共担运费不做分摊——整票运费记在本次进货第一笔流水（或主人口述明说的那笔），其余流水为 0，运费合计 = 全部流水 freight 求和。

### D3 语音意图与字段缺省规则（T3，T4 覆盖处已并入）

意图集 11 个：进货 / 售出 / 查询（产品历史、库存、报价参考、欠款）/ 修改 / 红冲 / 收款 / 付款 / 期初库存录入 / 生成报告 / 放弃·取消。

| 字段 | 规则 |
|---|---|
| 售出·客户 | 缺省默认现结、不挂账（没提就是不挂账；挂账需明说） |
| 售出·单价 | 缺省带出上次成交价**直接填写入库并报告**（T4 覆盖 T3「带出供确认」；主人觉得不对说一声 → 修改） |
| 进货·进价 | **必问**，不自动带出（成本根基，进货必录进价） |
| 数量 | **必问，不猜**（流水核心事实） |
| 日期 | 缺省今天，不必确认；非当天主人会主动说（"昨天的"） |
| 运费/备注 | 可空默认 0/空 |
| 商品识别 | 唯一命中直接确认（回显图号） |

歧义消解：

1. 多命中 → 列候选报序号（"1.缸盖 300.14.14 2.缸盖 300.18.12"），报"取消/都不是"作废。
2. 图号精确匹配优先，无命中再走名称/别名模糊搜索。
3. 新商品 → 提示"库里没有，新建商品对吗？"，确认后自动建档（图号唯一，缺图号自编 `ZC-`，此时追问图号）；名称/别名随报的录。出货新商品 → **自动补录一笔等量期初（数量 = 出货量）使库存归零**（ADR 0003）。
4. 进货+出货一起 → 拆成间隔极短的常规流水（进货一笔 + 售出一笔），不做复合单。

### D4 Fairy 交互流程（T4）

**简单三态状态机**（无确认 gate、无暂存区状态机、无专门纠错状态、无取消机制）：

| 状态 | 行为 |
|---|---|
| 待命 | Fairy 静默听主人说话 |
| 意图明确 | 信息齐 → **直接写入** → 报一句写入了什么；缺信息 → **先写入（可空字段留空）→ 报已写 → 追问缺项 → 回填**（回填 = 修改该流水，走审计） |
| 意图不明 | **直接问一句**，澄清后继续 |

- **必问字段**（schema `NOT NULL` 且无法留空）：数量、进价/单价——缺了先问。
- **缺了落库再回填**：运费（默认 0）、客户/供应商（空）、备注（空）、日期（默认今天）。
- **回显话术**：复述要素固定顺序——名称 → 图号 → 数量+单位 → 进价/单价 → 客户/挂账（如有）→ 日期（精确显示，如"8月12日"）→ 运费/备注 → 副作用（新建商品/出货归零补录，如有）；结尾单句明确提问（"对吗？""进价多少？"）。例：*"已记：进货 缸盖 300.14.14 ×3，进价 50，运费没记 —— 运费多少？"*
- **超时**：不设；主人不回，可空字段留空，之后补说一声 → 修改意图。Fairy 不主动催。
- **取消的定位**：交互上无独立「取消」操作——写入前说"算了/不要了" → 该笔不落库（无 DB 操作）；写入后要撤 → 红冲/修改（既有意图）。「放弃·取消」保留为识别词。
- **厂家** = 供应商/进货渠道（`counterparties.is_supplier` 覆盖），不是商品品牌属性。

### D5 CLI 命令面（T6）

**单入口 `fairy`**（Python 实现），写入命令全部命名参数（Fairy 从语音提取字段直接传）；红冲/修改按 **transaction id** 引用原单（写入命令 `--json` 返回 id，Fairy 记住）。库文件路径必须可配置（实现方自定机制：默认路径 + 测试用临时库），spec 不限定具体形式。

**写入类**：

| 命令 | 说明 |
|---|---|
| `fairy purchase` | 进货（--drawing-no --qty --price --freight --supplier --date --note） |
| `fairy sale` | 售出（--drawing-no --qty --price --customer --credit --freight --date --note） |
| `fairy receive` | 收款（--counterparty --amount --date --note） |
| `fairy pay` | 付款（--counterparty --amount --date --note） |
| `fairy opening` | 期初库存录入（--drawing-no --qty [--cost]，追加语义，可多次；cost 缺省 0） |
| `fairy reverse --tx <id> [--date --note]` | 红冲（关联原单，按原单成本冲） |
| `fairy edit --tx <id> [字段...]` | 修改（部分更新，触发审计） |
| `fairy product add` | 建档（--drawing-no 缺失时自动编 `ZC-`，--name --unit [--alias...]） |

**查询类**（统一 `fairy query <报表>`，任意条件组合过滤）：

| 命令 | 说明 |
|---|---|
| `fairy query price --drawing-no <图号> [--customer 名] [--from --to]` | 报价参考（历史成交价 + 成本快照，日期降序） |
| `fairy query stock [--drawing-no <图号>]` | 当前库存（期初+流水净量现算/当前均价/金额） |
| `fairy query credit [--counterparty 名] [--from --to]` | 挂账对账单（一维表 + 单位欠款总览） |
| `fairy query margin --from D --to D [--product <图号>] [--customer 名]` | 期间毛利（默认本月） |
| `fairy query history --drawing-no <图号> [--from --to]` | 单产品全部流水（进货/售出/红冲逐笔） |

**报告类**：

| 命令 | 说明 |
|---|---|
| `fairy report daily [--date YYYY-MM-DD]` | 日报（默认昨天），输出结构化 JSON，Fairy 以文字消息发四块内容，不产文件 |
| `fairy report period --from YYYY-MM-DD --to YYYY-MM-DD [--out 路径]` | 周期汇总（周/月/年同一套，仅区间长短；出 Excel：流水明细 + 简单分析 sheet） |
| `fairy export [--out 路径]` | 整库 Excel 5-Sheet 导出，默认文件名 `FairyLedger_YYYYMMDD.xlsx` |
| `fairy backup` | 备份库文件（复制到 `backups/`，保留 7 份轮转，`ledger-YYYYMMDD.db`） |

**输出与错误约定**：

- 所有命令输出结构化 JSON（Fairy 解析用），无默认人读表格模式。写入返回新流水 id / 修改后完整行 + 审计 id / 红冲结果；查询返回行数组（字段名 = schema 列名）。
- **一线内容 = 文件**：报告/导出类命令产出的文件直接给主人，Fairy 不润色（避免 AI 幻觉）；Fairy 的文字转述（摘要）仍不可少。
- 查询类文件产出可选：带 `--out` 时同时落表格文件，Fairy 判断主人要核对/留证时自动带上。
- 退出码：`0` 成功 / `1` 业务失败（可预期：无此商品、参数缺失）/ `2` 系统错误（DB 异常等）。错误消息走 stderr；Fairy 对非 0 统一报"没记成，原因 X"。

**修改语义（含审计）**：`fairy edit --tx <id>` 按 transaction id 定位、部分更新（传了哪些改哪些）。**不允许改商品（图号）**——换商品 = 红冲 + 新单（换货语义）；订单信息（数量/价格/客户/日期/备注）可改。每次修改自动写审计日志（整行 before/after 快照），无需额外参数。**修改售出单 qty/price 不改成本快照**（成本是售出时快照，改价只影响收入侧，毛利随之变）。

**原子性与幂等**：每条写入命令 = 一个 SQLite 事务，全成或全不成（红冲 = 冲原单 + 退货流水 + 均价重算，进货 = 流水 + 均价重算，同事务不留半截）。**不做幂等键**：单写者、Fairy 是唯一调用方，交互层已保证先落库、Fairy 不自动重试——非 0 就报错、不重发，宁可人去确认也不重复记账；查询/报告/导出/备份天然可重跑。

**新商品建档与归零补录**：`fairy product add` 独立建档；`purchase`/`sale` 遇到未知图号 → 业务错误"无此商品"，Fairy 走新商品流程（先 `product add` 再记流水）。出货新商品 → Fairy 编排：`product add` → `opening` 补录等量期初 → `sale`。命令单一职责，Fairy 的编排逻辑集中。

**意图 ↔ 命令映射**（T3 11 意图）：

| 意图 | 命令 |
|---|---|
| 进货 | `fairy purchase` |
| 售出 | `fairy sale` |
| 查询·产品历史 | `fairy query history` |
| 查询·库存 | `fairy query stock` |
| 查询·报价参考 | `fairy query price` |
| 查询·欠款 | `fairy query credit` |
| 修改 | `fairy edit` |
| 红冲 | `fairy reverse` |
| 收款 | `fairy receive` |
| 付款 | `fairy pay` |
| 期初库存录入 | `fairy opening` |
| 生成报告（日报） | `fairy report daily` |
| 生成报告（周期汇总） | `fairy report period` |
| 放弃·取消 | 无命令（识别词：写入前"算了"= 不落库，无 CLI 调用） |
| 辅助 | `fairy product add` / `fairy export` / `fairy backup` |

### D6 报告与表格规约（T5，T6 覆盖处已并入）

**日报（四块）**：

1. 当日汇总：进货（笔数/总金额）、售出（笔数/总金额）、运费合计、毛利合计（= 售出金额 − 成本快照合计）。
2. 挂账变动：今日新增挂账额、今日收款合计、今日付款合计、当前累计欠款余额（现算）。
3. 流水明细：当日进货+售出逐笔（时间/图号/名称/数量+单位/单价/金额/往来单位），退货单带「红冲」标记。
4. 异常提醒：**仅金额异常**——当日单价偏离该商品上次成交价 ±30% 的流水列出供核对。**不设负库存提醒**（负库存业务上不发生）。

发送：每天早晨发**昨日**完整日报；无业务日照发（汇总为零、异常区为空）。**触发语义**：日报 = 次日清晨发昨日；年报 = 每年 1 月 15 日发上年。具体时刻与定时调度归 Fairy 平台侧配置，spec 不写死。

**按需查询报表**（查 = 基本功能：任意条件组合过滤，日期降序；不做统计加工）：

- **报价参考单**：列 = 日期/图号/名称/数量+单位/单价/金额/成本（该笔成交的成本快照）/客户/备注；筛选 = 产品/客户/日期任意组合；日期降序，第一条即上次成交价。只给两个参考信息：历史成交价 + 成本；不现算当前均价（归库存报表）。
- **当前库存报表**：列 = 图号/名称/数量+单位（期初+流水净量现算）/单位成本（当前加权均价）/金额（qty×均价）；按商品过滤；默认图号序，可选按数量排序；不做预警线、不做变动明细。
- **往来单位对账单**：一维表，把带挂账的记录（挂账流水 + 收/付款）按时间列出：日期/往来单位/单据（进货/售出/收款/付款）/金额/备注；期末欠款 = 挂账交易累计 − 收付款累计现算；筛选单位/日期任意组合，默认日期升序。**全部挂账单位总览** = 同一结构按单位聚合导出（单位/期末欠款，欠款降序）。
- **毛利报表**：期间必选（默认本月），可再按产品/客户过滤；内容 = 期间售出金额/期间成本（快照合计）/毛利/毛利率（毛利÷售出金额）；分组 = 按产品 + 按客户（金额/成本/毛利/毛利率，毛利降序）；售出退货按原单冲减当期，口径自动正确。不做月累计曲线/同比环比/均价统计。

**周期汇总（周/月/年同一套结构，仅期间不同）**：

1. 期间汇总：进货（笔数/金额/运费）、售出（笔数/金额/运费）、毛利、期初期末库存金额。
2. 按产品分组：图号/名称/售出数量/售出金额/成本/毛利/毛利率，毛利降序，TOP 10。
3. 按客户分组：客户/售出金额/毛利/毛利率，毛利降序，TOP 10。
4. 明细页：期间全部流水逐笔（同日报明细结构）。

- TOP 10 仅为参考，重点是期间总毛利。
- 措辞统一用**毛利**（= 收入 − 快照成本），不引入「净利润」术语。

**导出与备份**：

- **运费全局约定**：毛利口径维持「收入 − 快照成本」，运费不进毛利计算；各报表把运费单独拎出求和、单列展示（参考性质，因运费录入不全）；运费与毛利/成本不耦合。
- **导出**：Excel 单工作簿 5 Sheet（文件名带日期，如 `FairyLedger_20260812.xlsx`，导出时点整库快照）：①流水（全部 transactions：时间/类型/图号/名称/数量+单位/单价/金额/运费/成本/往来单位/备注，退货带关联原单标记）②商品（图号/名称/单位/别名）③往来单位（名称/类型标记/挂账标记/联系方式）④库存（图号/名称/数量/当前均价/金额，现算）⑤毛利（按产品分组的期间毛利汇总）。
- **备份**：每日复制库文件一次到备份目录，保留最近 7 份轮转（`ledger-YYYYMMDD.db`），超出删除最旧；备份目录与库同目录下 `backups/`。

### D7 运维约定（fog 已定）

- **并发写入假设**：**单写者**——Fairy 是唯一写入方、串行执行；读可并发（SQLite 天然支持）；不需要锁/排队。
- **数据恢复**：恢复由 **Fairy 执行**（人工不碰服务器文件）——选备份文件复制覆盖主库路径、校验；**不加 CLI `restore` 命令**（备份是整库复制，恢复即整库替换）。
- **审计日志保留策略**：**永久保留，不设删除**（行级快照量级小、是纠错还原依据）；备份轮转只作用于库文件副本。
- **定时调度**：日报/年报只定触发语义（见 D6），定时任务由 Fairy 平台侧配置触发 CLI。

## Testing Decisions

**单一 seam：CLI 命令面黑盒测试**（已与主人确认）。v4 是全新实现、仓库当前无任何测试与实现代码，因此这是新增 seam；选择最高层的 CLI 边界——它恰好是 Fairy（系统唯一调用方）实际使用的契约面，覆盖全部意图、报告与成本语义。

- **什么是好测试**：只测外部行为——`fairy` 命令的 stdout 结构化 JSON、退出码（0/1/2）、以及写入后通过后续 `fairy query` / `fairy report` 命令可观察到的状态变化。不测实现细节（内部函数、中间变量、SQL 写法）。
- **观察点**：每条测试用临时 SQLite 库（`--db` 指向 tmp 路径，测完清理）运行命令序列；`audit_log` 等无 CLI 暴露处的行级快照，允许测试只读打开该临时库文件核对 before/after 整行内容——这是同一 seam 的验证手段，不引入第二个 seam。
- **被测行为矩阵**：
  - 写入类全部命令：purchase / sale / receive / pay / opening / reverse / edit / product add（含 `ZC-` 自编号、别名、归零补录编排）。
  - 成本语义：仅进货触发加权平均重算；售出成本快照 = 当时均价；期初为基数、无期初退化为进货单价；运费不进公式；红冲按原单成本（进货退货/售出退货两种）、换货 = 红冲 + 新单。
  - 毛利口径：毛利 = 收入 − 快照成本，运费不参与；售出退货冲减当期毛利、进货退货不冲。
  - 挂账：交易自动累计、收/付款一笔冲减、余额现算；现结默认不挂账。
  - edit：部分更新、不允许改图号、每次写审计、改价不改成本快照。
  - 原子性：制造中途失败（如红冲不存在的原单）断言不留半截数据。
  - 退出码与错误路径：无此商品、参数缺失、DB 异常。
  - 查询类与报告类：price / stock / credit / margin / history、report daily（JSON 四块）、report period（Excel 产出）、export（5 Sheet）、backup（7 份轮转）。
- **prior art**：仓库内无既有测试；`refer/` 下 v3 脚本仅历史背景、非设计标准。本 spec 为 v4 确立第一套测试约定，后续实现沿用此单一 seam 模式。

## Out of Scope

- **历史流水导入**（主人明确：不导入；启用时只录一次期初库存）。
- **补货预警线**（主人明确：不要）。
- **逐笔核销式应收应付**（主人选累计制）。
- **读图/OCR 转录**（v2 已否决：读图不可靠）。
- **语音转文字（STT）**（不归本 effort）。
- **Web/GUI/ERP 形态**（v3 定：SQLite + CLI 轻量）。
- **客户专享价表**（v4 报价参考以历史成交价为准，除非主人重开）。
- **季报**（已被 `report period` 覆盖取消）。
- **定时调度实现**（日报/年报的调度归 Fairy 平台侧，spec 只定触发语义与内容）。
- **CLI `restore` 命令**（恢复 = Fairy 的文件复制覆盖，无命令面）。
- **负库存闸门 / 暂估 / 补录校正分支**（不允许负库存是业务纪律，系统不设闸门）。
- **一票多产品运费分摊**（不做分摊，整票记第一笔流水）。
- **多写者并发**（单写者假设）。
- **审计日志删除/归档策略**（永久保留）。

## Further Notes

- **一线内容 = 文件**：报告/导出产出的文件直接给主人，Fairy 不润色；Fairy 的文字转述（摘要）仍不可少。人类几乎不跑命令行，CLI 输出全部结构化 JSON 给 Fairy 解析。
- **查是基本功能**：查询报表就是任意条件组合过滤的列表，避免统计加工和过度设计。
- **运费与毛利/成本不耦合**：费用化，各报表单列求和，录入不全仅参考；规格中不得再出现「运费入成本」的表述。
- **派生值一律现算**（ADR 0001）：状态由事实推导，库存、金额、欠款、均价永不落列，杜绝账实打架。
- **报价参考与库存报表分工**：报价参考给历史成交价 + 成本快照；当前均价只出现在库存报表。
- **新商品出货归零补录**（ADR 0003）与整体「不允许负库存」纪律一致，期初库存因此不再"只录一次"——补录型期初与启用型期初并存，审计需覆盖归零补录。
- **术语**：统一用「毛利」（= 收入 − 快照成本），不引入净利润；领域术语以根目录 `CONTEXT.md` 为准，出现新术语时同步更新。
- 本 spec 由 T1–T7 resolutions ＋ ADR 0001–0004 ＋ CONTEXT.md ＋ 已确认 fog 约定收束而成；实现过程中的新决策按 domain-modeling / ADR 流程记录。
