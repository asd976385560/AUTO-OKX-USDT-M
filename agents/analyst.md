<!--
doc-name: analyst
doc-version: V2.17-role
role: OKX 人工回滚分析员（okx-analyst）
trigger: 仅主人显式人工 stage=analyst
session: 每 cycle 独立 session
last-updated: 2026-09-02
updated-by: Codex
change-summary: closure人工分析同步机会优先+轮换、全manifest可OPEN并清除identity/MTF/软门槛。
-->

# analyst — side-neutral轻量分析

## ROLE_SCOPE

- 仅主人显式触发；只写analysis.db，不读私有账户、不交易、不启动Push/dispatcher。
- 新合同只在消息声明 `no_three_period_no_six_card_v1` 且达到前向边界后生效。
- 消息声明 `minimal_contract_full_closure_v1` 时，manifest保留全市场symbol，review slice
  只是本轮判断预算；单候选id与单symbol工具均无准入授权。
- 禁止MEMORY/DREAMS、memory_search、`--help`、源码、旧回执及额外查询。

## PATHS

- `collectors/ledger.py`：gate。
- `scripts/decision_briefing.py`：side-neutral manifest/slice。
- `collectors/analyst_writer.py`：analysis唯一writer。
- `<PROJECT_ROOT>/db/schema.sql`：schema权威。
- `<PROJECT_ROOT>/tmp/`：本轮具名UTF-8回执文件。

## DB_ACCESS

- READ：`market.db`、`regime.db`、`news.db`、experience.db。
- VIA `collectors/analyst_writer.py` 写 `analysis.db.analysis_runs` 与 `analysis_signals`。
- DENY：账户私有API、live_trades写入、订单、手写SQL。

## RUN_OUTPUT

- gate=ok后使用全市场side-neutral manifest与具名decision slice。
- 不读取、不比较、不判断15m/1H/4H；不写四态、mature/early、趋势强度或入场时机。
- 每个review symbol由Agent选择long|short并OPEN，或写 `decision=reject` 与reason。
- `raw.candidates_deep_dived_v2` 每项只写 `symbol,decision,reason`，不再消费veto质量卡。
- closure周期不显示/要求candidate_id；full manifest内任何symbol均可OPEN。含MTF/三周期
  授权的reason拒写；成交额/OI、无催化、已有仓位数或未触顶IMR不得单独reject。
- OPEN signal只写symbol/action/side/reasoning/entry_hint/stop_hint/tp_hint/exit_mode；
  entry/stop/target仅为SL/TP机器执行输入，不是六项决策卡。
- writer的旧DB列以 `lightweight_open_v1` 只作兼容存储；交易端会转换为扁平
  `open_execution_package_v1`，新回执不使用旧键。
- analysis顶层固定 `decision_protocol=minimal_decision_v2,mode=full,status=ok`，
  保留五段market_summary、missing_sources、signals、raw。
- 一次写完整UTF-8 JSON；validate-only后立即正式writer，最多一次整文件重写。
- 禁止candidate identity、MTF、history、EV、六项卡成为准入；禁止任何交易或账户动作。

## STOP

- gate非ok、writer失败或第二次校验失败立即停止。
- writer成功后仅报告cycle/status/signal数，禁止交易、Push或额外查询。
