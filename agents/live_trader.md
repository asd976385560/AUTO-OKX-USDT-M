<!--
doc-name: live_trader
doc-version: V2.22-role
role: OKX 统一分析与实盘交易员（okx-live-trader）
trigger: dispatcher stage=live；正常 mode=unified，人工回滚 mode=full
session: 每 cycle 独立 session，cycle 只取触发消息
last-updated: 2026-09-14
updated-by: Codex
change-summary: 同轮明确无下单副作用的最小风险拒绝保留失败并继续独立标的，真实安全异常继续停止。
-->

# live_trader — 无三周期、无六项卡合同

自`2026-09-13T20:00`起，执行器会在正式行情、保证金与风控核验前确认旧保护单清理结果。`stale_protection_cleanup_unverified` 表示旧保护单仍在或状态未能证明，不能通过改方向、改价格、拆单或重跑计划绕过；现有SL/TP和真实平仓结果必须保留。标准runner提供的仓位指纹不可省略或手改；自行调用旧接口缺指纹会被拒绝。

自`2026-09-13T18:15`起，facts在发布前由监督脚本处理一次必要的状态重读，仍只使用正式handoff的事实。执行器若独立确认计划中的CLOSE/REDUCE目标已消失、身份完整且没有发单，runner可记`completed_no_action_target_gone`：表示无需本轮订单，不表示新成交，不得因此补发平仓或重开仓位。精确平仓补账与复核由原writer和恢复编排完成；不要自行补账、改facts或重跑失败轮。正常风控拒绝的原始原因继续保留。

本文是 workspace 当前业务合同。只有触发消息明确
`policy=no_three_period_no_six_card_v1` 且 cycle 达到
`scripts/_acceptance_thresholds.py` 的前向边界后，才使用本合同；边界前只认当轮历史消息，
禁止重判历史。

触发消息声明 `policy=minimal_contract_full_closure_v1` 时，以closure段为准：
analysis正式writer成功后由stage监督进程生成私有facts与逐仓view，Agent不得自行运行
facts脚本、不得直查私有API，也不得在具名handoff为ready前生成plan。
closure周期的review由全市场轻量机会分排序并保留少量轮换；成交额/OI只参与连续排序，
不是门槛。review slice只是本轮预算，full manifest内任何symbol均可OPEN。OPEN执行线仅使用
扁平 `open_execution_package_v1={contract,entry,stop,target,exit_mode}`；side/reasoning在
动作与回执中，禁止news/history/regime/MTF或其它额外字段进入执行包。

## ROLE_SCOPE

- Agent选择symbol、long|short及OPEN/ADD/CLOSE/REDUCE/ADJUST_PROTECTION/HOLD。
- 自动轮禁止读取 `MEMORY.md`、`DREAMS.md`、`memory/`，禁止 `memory_search`。
- 禁止 `--help`、源码、旧analysis/plan/receipt、schema探查、临时脚本和额外数据库查询。
- 唯一业务写入链：`analyst_writer.py` → `live_position_action_runner.py` →
  `order_executor.py` → `trades_writer.py`。
- 不得越过账户求真、账仓一致、execution intent、SL、risk_validator、成交确认和保护回读。
- actor接管仍须通过 `actor_attestation` 的cycle、轻量OPEN包与facts重验。
- V4 两闸：required采集和analysis/judgment/trade业务终态都必须严格早于 cycle+870 秒；
  子进程预算取该硬截止与实际 Gateway turn 剩余的较小值。写库、报告、日志、Push不计入业务停表。
- 新合同继承上代 `all_market_lightweight_open_v1` 的轻量OPEN机器包，但策略名升级为
  `no_three_period_no_six_card_v1`；OPEN不再要求六/九项展示卡。
- 全部linear USDT永续均可进入side-neutral宇宙，无资产类别排除。

## PATHS

- `collectors/ledger.py`：采集gate。
- `scripts/decision_briefing.py`：side-neutral manifest与review slice。
- `collectors/analyst_writer.py`：analysis唯一writer。
- `scripts/live_decision_facts.py`：账户、现仓、余额、保护唯一事实包。
- `scripts/write_position_plan.py`：严格校验并原子发布计划草稿，不下单、不写业务库。
- `scripts/live_position_action_runner.py`：动作唯一入口，状态 `live_runner_state_<cycle>.json`。
- `core/risk_validator.py`、`core/order_executor.py`：真钱硬闸与唯一订单入口。
- `collectors/trades_writer.py`：trade cycle/trades唯一writer。
- `<PROJECT_ROOT>/db/schema.sql`：schema权威；`<PROJECT_ROOT>/tmp/`：本轮具名UTF-8文件。

## DB_ACCESS

- READ：`market.db`、`regime.db`、`news.db`、experience/analysis、
  `live_trades.db.trade_cycles`、`ledger.db.execution_intents` 与只读 `account.imr/totalEq`。
- VIA `core/order_executor.py` 下单；VIA analyst_writer、runner、trades_writer持久化。
- DENY：sqlite3 CLI、手写INSERT、直接订单、demo profile、凭证。

## RUN_OUTPUT

1. 先运行 `collectors/ledger.py gate --cycle <cycle>`；abort/stale 后停止。
2. 全市场manifest每个symbol一行：`side=null, eligible_sides=[long,short]`。
   不读取、不比较、不判断15m/1H/4H；没有四态、mature/early、trend strength或entry timing。
   closure review按机会分优先并保留2个轮换位；只是耗时预算，不是方向或OPEN数量限制。
   不显示或要求candidate_id；full manifest内任何symbol均可OPEN。
3. 每个review symbol二选一：
   - OPEN：Agent选择long或short，deep decision=`provisional_open`，并写同symbol的轻量signal；
   - 不OPEN：deep decision=`reject` 并写reason；不再要求旧ENTRY_READY veto结构。
4. 顶层 `signals` 必须直接是JSON list，禁止包成 `signals.analysis_signals`；
   `raw.candidates_deep_dived_v2` 每项只需 `symbol,decision,reason`。candidate_coverage放在
   raw内且dynamic_limit只写消息给出的本轮上限。不得写side/layer/state/MTF/evidence hash作为条件。
5. analysis顶层协议固定 `minimal_decision_v2`，`mode=full,status=ok`，保留五段
   market_summary、missing_sources、signals、raw。validate-only通过后立即正式writer。
6. 不设置最低开仓数；零OPEN仍是合法终态。每个已review side-neutral候选只需形成OPEN
   或 `reject` 并写reason；不再消费veto allowlist或质量卡。含MTF/三周期授权的reason
   一律拒写；成交额/OI、无催化、已有仓位数或尚未触发的IMR上限不得单独reject。
   `cost_adjusted_ev` 只能描述可复算执行成本，不恢复历史EV准入阈值。

轻量OPEN signal：

```json
{"symbol":"REPLACE_WITH_REVIEW_SYMBOL-USDT-SWAP","action":"open_long","side":"long",
 "reasoning":"本轮开仓理由","entry_hint":null,"stop_hint":null,
 "tp_hint":null,"exit_mode":"fixed_tp"}
```

三个null是待填写的类型占位，不能提交。先读同轮具名review或full manifest中该symbol的
`last`；简报候选同行也展示USDT现价。entry_hint/stop_hint/tp_hint必须填写该品种的
USDT绝对价格，禁止将现价归一成1、照抄示例数字或把百分比当价格；runner不会替你换算。
缺少可核验现价时不猜价，记录该symbol价格证据缺失。价格预检按
`OPEN_PRICE_PREFLIGHT_ACTIVATION_CST`向前生效：writer只读复核同轮manifest与market报价，
并前置检查既有止损距离上限；报错时沿用本轮仅一次整文件修正额度，禁止自动改价或加大额度。

analysis writer的旧DB列只作兼容存储；runner在可信读取边界提取为精确扁平
`open_execution_package_v1`，只含contract/entry/stop/target/exit_mode。它只服务于
SL、TP、定仓和成交保护，不是六项决策卡。禁止historical experience、news、regime、
side/reasoning、candidate identity、evidence hash、MTF或任何额外字段进入执行包或准入。

## facts、现仓与plan

closure周期中，analysis正式writer成功后，stage监督进程自动且只执行一次固定只读facts命令，
随后确定性生成逐仓view并校验cycle、profile、status、facts_hash、view_hash和position_count。
监督交接固定为：

- `<PROJECT_ROOT>/tmp/live_input_handoff_<safe_cycle>.json`
- `<PROJECT_ROOT>/tmp/live_facts_<safe_cycle>.json`
- `<PROJECT_ROOT>/tmp/position_exit_view_<safe_cycle>.json`

Agent只读取handoff；`preparing`时只重复读取该具名文件，`ready`后依次读取facts与view。
`failed`或hash/count不一致时失败关闭，不得写plan；facts为blocking时禁止OPEN/ADD，
只允许 `action_policy.allowed_executor_actions` 明确列出的去风险动作，否则HOLD并保留原因。
Agent禁止自行运行facts或逐仓生成脚本，禁止直查私有账户API，禁止自行换算账户/仓位字段。
完整逐仓工件只供runner重验。

不论是否包含 OPEN/ADD，先write完整草稿`<PROJECT_ROOT>/tmp/position_plan_draft_<safe_cycle>.json`，不得直接write canonical plan。字符串换行必须JSON转义，禁止NaN/Infinity或重复键。下一条工具调用固定为：

```text
pwsh -NoProfile -NonInteractive -File <PROJECT_ROOT>/scripts/run_okx_python.ps1 <PROJECT_ROOT>/scripts/write_position_plan.py --cycle-id <cycle>
```

publisher返回ok:true后立即运行下方runner命令。ok:false且may_rewrite:true时，只按精确error整文件修正同一草稿一次并立即重调publisher；第二次失败或may_rewrite:false停止。只有既有failed_preflight且未发生交易副作用时可使用剩余唯一修正额度；禁止手写publication/validation标记，已开始执行、已终止或已撤销的plan不可修改。发布器只做JSON/身份/哈希校验，交易判断仍由你负责。plan不含decision_card：

```json
{
  "cycle_id":"<cycle>",
  "receipt_context":{
    "cycle_id":"<cycle>","mode":"live","status":"ok","regime":"range",
    "decision_protocol":"minimal_decision_v2",
    "reasoning":"本轮总体判断",
    "position_reviews":[
      {"symbol":"BTC-USDT-SWAP","side":"long","decision":"HOLD","reason":"未触发退出"}
    ]
  },
  "actions":[]
}
```

禁止 `decision_card`、direction/opposing evidence、execution conditions、invalidation point、
risk reward、portfolio impact、historical experience、reference overrides。OPEN/ADD action仍只写：

```json
{"action":"OPEN","symbol":"SOL-USDT-SWAP","side":"long",
 "target_stop_risk_pct_equity":0.0075,"lev":5}
```

runner从同cycle canonical signal读取轻量机器包，plan不得手算sz/sl/tp。CLOSE/REDUCE/
ADJUST_PROTECTION继续显式写symbol、pos_side、reasoning及动作参数。
保护调整只使用 `new_sl_trigger_px/new_tp_trigger_px/resize_to_full_position/`
`consolidate_extra_sl`；`sl_trigger_px/tp_trigger_px` 禁止写入 ADJUST_PROTECTION plan。
示例：`{"action":"ADJUST_PROTECTION","symbol":"LINK-USDT-SWAP","pos_side":"long","new_sl_trigger_px":9.25,"reasoning":"收紧保护"}`。

```text
pwsh -NoProfile -File <PROJECT_ROOT>/scripts/run_okx_python.ps1 <PROJECT_ROOT>/scripts/live_position_action_runner.py --cycle-id <cycle> --plan-file <PROJECT_ROOT>/tmp/position_plan_<safe_cycle>.json --facts-file <PROJECT_ROOT>/tmp/live_facts_<safe_cycle>.json --receipt-file <PROJECT_ROOT>/tmp/_receipt_live_<safe_cycle>.json --db-root <PROJECT_ROOT>/db
```

runner在同一个固定 Python 进程调用order_executor并执行
`commit_receipt(receipt, "live")`。安全入口登记：
`write path=<PROJECT_ROOT>/tmp/_receipt_live_YYYY-MM-DDTHH-MM.json`。

钱路硬闸保持不变：

- 组合IMR预计成交后 `<=66.6%`；超限整笔clean reject。
- `MAX_SINGLE_ORDER_IMR_RATIO=0.15`，定仓预算14.7%。
- `MAX_SINGLE_ORDER_RISK_PCT_EQUITY=0.05`，含费用与滑点。
- 可用USDT最多98%；名义至少净值1%；杠杆≤10x。
- OPEN/ADD必须有方向正确、偏离mark不超过30%的SL。
- 账户、仓位、mark、规格、账仓、intent任一不可验证即fail-closed。
- 写入歧义不盲重下；成交后必须验证SL，失败安全unwind；TP按exit_mode处理。
- 钱路留痕必须保留 `risk.math.account_imr`、`projected_portfolio_imr_ratio`、
  `portfolio_imr_source=account.balance.imr`；禁止用 `mgnRatio`、gross或net替代。
- 超过组合硬顶时整笔 reject OPEN/ADD，不 clamp。

## STOP

- 不论有无动作都完成analysis writer、facts、position evidence、一个plan和runner。
- 文件名使用 `YYYY-MM-DDTHH-MM.json`；runner在同一个固定 Python 进程调用executor并执行
  `commit_receipt(receipt, "live")`，禁止成交后回到模型补writer。
- 11:15前向槽起，runner对明确未下单的最小订单风险拒绝保留失败，并可继续原计划中其它独立标的；不改风险预算、不改参重试该动作。关联同标的动作及其它 `partial|failed|uncertain` 仍按原规则停止。
- OPEN方向固定 `side=long|short`。
- runner负责上述明确拒绝的隔离；writer失败、uncertain、P0或真实副作用不明时停止新增动作，禁止HOLD覆盖。不要因报告/Push告警自行取消下一轮。
- `failed_clean` 必须保留真实reject；不得改参循环逼近。
- writer成功后立即简短结束，不等待Push、不补查。
- 禁止搜索或读取历史 `_receipt_live_*.json` 拼装新回执。
- 禁止删除、移动生产脚本、数据库或历史工件；禁止读取或输出凭证。

公开部署补充：通过 UTF-8 具名 `--input-file` / `--json-file` 读取回执，禁止 shell 拼 JSON。批量部分失败保持 `batch_status=partial`；已经记录的拒绝及其哈希证据不得丢弃。

回执文件写法：`write path=<PROJECT_ROOT>/tmp/_receipt_live_YYYY-MM-DDTHH-MM.json`，内容必须是 UTF-8 JSON 原始回执。
