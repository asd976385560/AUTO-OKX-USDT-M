<!--
doc-version: V2.0
last-updated: 2026-07-29
updated-by: Codex
change-summary: Sync execution intent, ledger reconciliation, stage supervision, public macro, report handoff, lifecycle and regression contracts.
-->

# OKX 自主交易系统 V2.0 · 事实源

本文定义公开代码中的 V2.0 架构、职责边界与安全不变量。`README.md` 是面向使用者的系统地图；发生冲突时，以本文和真实代码为准。

## 1. 设计原则

| 原则 | 约束 |
|---|---|
| 可移植 | 核心逻辑使用 Python、SQLite 和明确契约；项目根目录从 `OKX_ROOT` 或源码位置推导 |
| 确定性管道 | 采集、风控、下单、记账、推送和触发由代码完成；LLM 只做判断或结构化取数 |
| 单 writer | 每张表或明确键域只有一个权威 writer；读者使用 `mode=ro` |
| 幂等 | `stage_dispatch` 约束阶段派发；`execution_intents` 在交易所 I/O 前约束每个 profile 的全局未决交易意图 |
| 模板化 | 分析、交易、推送和日报都有固定回执或模板并在写入前校验 |
| 角色隔离 | 每个 Agent 只加载自己的 `agents/<role>.md`；cron 消息只描述本轮工作 |
| fail-safe | 权威字段、凭证、合约规格、余额或成交确认缺失时拒绝执行，不使用猜测默认值 |

## 2. 系统定位与流程

系统面向 OKX USDT 永续合约，支持 live 与 demo 双盘。两盘共用风控、止损和成交确认路径，只切换执行环境。

```text
fast/slow/news/account collectors
              │
              v
          ledger.db
              │
              v
       core/dispatcher.py
              │
  stage_dispatch unique lock
              │
     ┌────────┴────────┐
     v                 v
unified live       demo trader
analysis+trade      demo trade
     └────────┬────────┘
              v
     scripts/push_pipeline.py
```

主链：

1. fast collector 采集即时行情并同步账户；
2. slow collector 采集合约规格和低频宏观数据；
3. registry 新闻源经 `news_writer` 落库，news-scout 作为非必需旁路；
4. dispatcher 在采集齐全且新鲜时抢 `stage_dispatch(live)`；
5. unified live 先写 analysis，再读取 OKX 权威账户与持仓，经过风控和订单执行层；
6. analysis 就绪后 dispatcher 派 demo；
7. `stage_runner.py` 等待子进程终态并回读真实业务产物，`rc=0` 但缺业务行仍判失败；
8. 双盘 trade cycle 就绪后，dispatcher 运行纯脚本 push pipeline；
9. 日频维护完成对账、账单和质量文件后发布带 SHA-256 的 ready 清单，reviewer 校验后再生成报告。

`cycle_id` 使用 UTC+8 的 `YYYY-MM-DDTHH:MM` 槽位。过窗周期只告警，不自动补单或恢复。

## 3. 调度口径

默认调度表达式：

| 工作 | 类型 | 表达式或触发 |
|---|---|---|
| fast collect | command | `0,15,30,45 * * * *` |
| slow collect | command | `2 * * * *` |
| dispatcher | command | `*/2 * * * *`，writer 成功后可额外 nudge |
| registry news | command | `3,18,33,48 * * * *` |
| news-scout | agent | `5,20,35,50 * * * *`，非必需 |
| daily maintenance | command | 每日一次 |
| reviewer | agent | 每日一次 |

公开仓库不包含真实 cron job id、OpenClaw 数据库、设备配置或宿主状态。部署者应在自己的环境中创建和核验调度。

## 4. 角色边界

| 角色 | 职责 | 写入 |
|---|---|---|
| collectors | 采集与账户同步，失败隔离 | market/news/regime/account + ledger |
| dispatcher | 读取就绪状态、抢阶段锁、起下一棒 | ledger.stage_dispatch |
| analyst | 仅人工回滚时使用的分析角色 | analysis.db |
| unified live trader | 分析、实盘判断、风控和执行 | analysis.db + live_trades.db |
| demo trader | 使用同一分析和同一硬风控完成模拟执行 | demo_trades.db |
| reviewer | 日/周/月复盘和经验摘要 | account.db reports |
| news-scout | X/无 API 新闻取数和结构化，不做方向判断 | news.db + ledger |
| push pipeline | 从数据库组装、渲染、校验、归档和可选发送 | reports + system_state |

Agent 不得直接写表，不得绕过 writer，不得手拼 OKX 下单命令，也不得读取或输出 raw credential。

## 5. 目录与配置

```text
<PROJECT_ROOT>/
├── agents/
├── collectors/
├── core/
├── db/schema.sql
├── docs/
├── scripts/
├── templates/
├── config.example.md
├── README.md
└── skill.md
```

运行时会创建 `db/*.db`、`logs/`、`reports/`、`memory/` 和 `tmp/`，这些内容都被 `.gitignore` 排除。

配置规则：

- 环境变量优先；
- 允许采集器从本地 `config.md` 读取受控 fallback；
- `config.md` 从 `config.example.md` 复制后填写，永远不得提交；
- QQ 目标必须由 `OKX_QQ_TARGET` 或 CLI 参数提供，公开代码无默认目标；
- OKX API credential 由仓库外的 CLI profile 或部署环境管理；
- 代理由 `OKX_PROXY_URL` 或当前启用的系统代理提供，代码不带私网地址或端口默认值。

## 6. 数据库与 writer

| 数据库 | 关键表 | 权威写入方 |
|---|---|---|
| market.db | tick_snapshots, kline_cache, derivatives, market_microstructure, market_trade_flow, market_positioning, instruments_cache | fast/slow collectors |
| regime.db | cross_market, macro_observations, macro_events | slow collector / macro maintenance |
| news.db | news_items, coin_sentiment, news_events_index | `collectors/news_writer.py` |
| analysis.db | analysis_runs, analysis_signals | `collectors/analyst_writer.py` |
| live_trades.db | trade_cycles, trades | `collectors/trades_writer.py --profile live` |
| demo_trades.db | trade_cycles, trades | `collectors/trades_writer.py --profile demo` |
| account.db | snapshots, trade_experiences, bills, reports, playbook, system_state | 对应表或键域 writer |
| ledger.db | collection_runs, stage_dispatch, execution_intents | `collectors/ledger.py` / dispatcher / `core/execution_intent.py` |
| lessons.db | error_patterns, missed_opportunities, signal_perf | reviewer |
| drill.db | drill_* | 只读兼容数据 |

SQLite 连接使用 WAL、`busy_timeout=5000` 和 `synchronous=NORMAL`。schema 变更通过幂等迁移脚本完成；`db/schema.sql` 是公开 DDL，不包含运行数据。

## 7. 数据源

`collectors/sources/registry.json` 是声明式源注册表。每个源声明：

```text
id / type / endpoint / native_cadence / required /
staleness_sec / enabled / auth_env / adapter / timeout_sec
```

规则：

- 采集器只迭代 enabled 且有确定性 adapter 的新闻源；
- `auth_env` 只存环境变量名，不存值；
- required 源缺失或过期会阻断本轮派发；
- optional 源失败只记降级；
- event 源不因无新事件自动判 stale；
- `event_time` 使用源发布时间，缺失时保持 NULL，不能伪造为当前时间；
- Alternative.me 与 ECB 复算 DXY 分别按自己的 observation date 判时效；
- ETF 净流单源只记 provisional，只有两个独立来源同日一致才进入 cross-checked 字段；
- news-scout 只取数和结构化，方向与影响判断仍归分析阶段。

## 8. 风控契约

live 开仓唯一路径是 `core/order_executor.open_position()`；该函数内部强制调用 `core/risk_validator.validate()`。

权威常量：

| 限制 | 值 | 处置 |
|---|---:|---|
| 单笔保证金占权益 | `MAX_MARGIN_PCT = 0.20` | clamp |
| 可用 USDT 保证金使用比例 | `AVAILABLE_MARGIN_USE_PCT = 0.98` | clamp / 不可行时 reject |
| 最大杠杆 | `MAX_LEVERAGE = 10.0` | reject |
| 最小名义价值占权益 | `MIN_NOTIONAL_PCT = 0.01` | clamp |
| 最大止损偏离 | `MAX_SL_DEVIATION = 0.30` | reject |

保证金与名义价值必须包含 `ctVal`：

```text
margin_per_contract = mark_px * ct_val / leverage
notional = mark_px * size * ct_val
```

其他不变量：

- 非 dry-run 交易在任何交易所 I/O 前必须验证同 cycle 的完整 `receipt_context`；
- 每个 profile 只要存在 pending/submitted/uncertain 等未决意图，所有标的的新交易都 fail-closed；已完成同参重放只返回缓存回执；
- 交易前将 OKX 全量现仓与本 profile 已确认交易账本做全集合核对，不一致、坏行或账本不可读时在取 mark/下单前拒绝；
- `ctVal` 和 `lotSz` 来自 instruments cache，缺失时现拉，仍缺则 reject；
- 当前持仓来自 OKX API，不能由 position snapshot 聚合推断；
- 可用 USDT 保证金字段缺失时 fail-safe reject；
- live/demo 开仓都必须提供止损；
- 附挂止损必须按本次保护单身份回读；独立保护单还必须精确匹配新 `algoId`，仍失败则立即平掉裸仓并报告 P0；
- 成交只接受 fills、订单状态或订单历史等权威端点；`fill_sz/fill_px/fill_ts/ts_source` 不完整时不得伪造成交；
- 成交回执必须在执行交易的同一确定性进程内提交 writer；同 cycle 只允许完整重发或完全不相交的增量，部分重叠拒写；
- 写命令超时后不能盲目重试，以免重复下单；
- 组合集中度与持仓数量只作观察，不改变上述硬闸。

## 9. 交易经验

live/demo 成交写入后，由 `trades_writer.write_experiences` 调用 `trade_experience_writer` 更新 `account.db.trade_experiences`。交易库与经验库使用独立事务；经验写入失败不得回滚已经确认的交易行。

经验状态为 `open|closed|expired`。只有已确认且可计算的 PnL 才能进入经验统计；`pnl_approx=True` 或成交未确认的关闭事件不得污染经验。

经验检索同时返回相似盈利、相似亏损和错失机会。历史样本是 Agent 的参考输入，不自动批准或禁止 live。

## 10. 报告与推送

| 产物 | 入口 |
|---|---|
| 分析回执 | `collectors/analyst_writer.py` |
| 交易回执 | `collectors/trades_writer.py` |
| 15 分钟战报 | `scripts/push_pipeline.py` |
| 日/周/月报 | `scripts/daily_report_writer.py` |

push 顺序固定为：

```text
build_push_payload -> render_push_report -> validate_push_format
-> push_archive -> optional send -> system_state_writer
```

公开代码不包含目标。未配置 `OKX_QQ_TARGET` 时，发送入口必须返回配置错误，不能使用隐藏 fallback。

日报/周报只统计 `action=open|close`、数量和成交价为正且未 rejected 的有效 fill。
`risk_reject:*` 作为“开仓尝试被风控拒绝”单列；live 对账未清零时允许生成
provisional 报告，但不得标成最终事实。

## 11. 失败与安全响应

以下情况应视为 P0：鉴权失败、硬风控被绕过、writer 连续失败、裸 live 仓无法补止损或平仓、凭证疑似泄漏。

响应原则：

1. 停止业务调度；
2. 禁止自动恢复；
3. 在运行环境配置的安全渠道告警；
4. 保存本地事件记录，不提交日志或凭证；
5. 等待维护者决定是否恢复。

发现疑似泄漏时先轮换凭证。公开仓库历史问题只报告，不在常规同步中重写 Git 历史。

## 12. 当前实装状态

- dispatcher 使用 unified live → demo → push 完成触发；
- analysis、trade、news、report 和 system state 都有明确 writer；
- 新闻采集由 registry 驱动，news-scout 是解耦旁路；
- push 固定为纯脚本管道；
- `scripts/lifecycle.json` 记录公开脚本的 runtime/helper/manual/research/migration 生命周期；
- `tests/` 提供分层隔离回归，覆盖执行安全、writer、dispatcher、报告、公开宏观和运行修复；这不是完整 money-path 环境回归；
- 无生产数据库时的发布验证为全量 `py_compile`、隔离测试、registry/schema/doc/lifecycle 静态检查、dry-run 和独立敏感扫描。

公开发布不改变风控、订单执行、writer、账本幂等或交易业务判断逻辑。
