<!--
doc: trade_template
doc-version: V2.0-template
last-updated: 2026-08-14
updated-by: Codex
change-summary: 三周期执行闸增加 writer 已验证锚点，允许同槽后续行情修订但保留双时点 hash 审计。
role: 交易回执模板（live trader -> live_trades.db）
权威: skill.md（交易执行契约节）+ core/order_executor.py + core/risk_validator.py + collectors/trades_writer.py
落点: <PROJECT_ROOT>\db\live_trades.db（trade_cycles + trades）
writer: <PROJECT_ROOT>\collectors\trades_writer.py（唯一通道，禁手写 INSERT）
-->

> ⚠️ **2026-07-29 一致性审计校正**：本模板已对齐成交前决策卡校验、同 profile 全局未决执行意图闸、交易主账与 OKX API 全仓一致性闸、部分成交和原子落账契约；与 skill.md / 对应 writer·core 代码冲突时以后者为准。

# 交易回执模板 — live trader -> live_trades.db

> live 交易所副作用**唯一路径** = `core/order_executor.open_position()` / `close_position()` / `reduce_position()` / `adjust_protection()`；OPEN/ADD 内部**强制调** `core/risk_validator.validate()`（LLM 物理越不过闸，红线 #7）。
> 执行器产出已携完整 cycle/决策卡的**回执 dict**。任何成交或保护调整轮都必须在调用 executor 的同一确定性 Python 进程内立即调用 `collectors.trades_writer.commit_receipt(receipt, profile)`；禁止先退出、再由模型下一次工具调用补落账。只有 HOLD/WAIT 等无交易所副作用回执可走 `--json-file`。
> live trader 在交易阶段先运行 `scripts/live_decision_facts.py --cycle-id <cycle> --profile live --out-file <facts>`。持仓时长、ctVal/币数、实际 live SL、到 SL 损失和 `account.imr/totalEq` 只能引用该文件，不得手算。HOLD/WAIT 的 CLI writer 必须带 `--facts-file <facts>`；有交易所副作用则将完整 `live_facts` 原样挂进 receipt 后在同进程提交。
> 红线：写库必走 writer，禁手写 INSERT；现仓以 OKX API 为准（禁 position_snapshots GROUP BY）；Live 组合 IMR 只认同次 OKX `account.balance.imr/totalEq` 与本单增量，禁用 `mgnRatio`、gross、net 替代。零模型名。

## 1. order_executor 回执 dict（每笔执行产物）

`open_position(symbol, side, intended_sz, lev, sl_trigger_px, profile, mgn_mode='cross', mark_px, equity, open_positions, reasoning, db_root, cycle_id, available_margin=None, receipt_context=None, tp_trigger_px=None)`

> `available_margin` 仅参与 Live 的实时可用保证金边界；Live OPEN/ADD 还必须用同次 `account.imr/totalEq` 计算预计成交后组合 IMR。
`close_position(symbol, profile, pos_side, mgn_mode='cross', reasoning, db_root, cycle_id, receipt_context=None)`
`reduce_position(symbol, profile, reduce_sz, pos_side, mgn_mode='cross', reasoning, db_root, cycle_id, receipt_context=None)`
`adjust_protection(symbol, profile, pos_side, new_sl_trigger_px=None, new_tp_trigger_px=None, resize_to_full_position=False, reasoning, db_root, cycle_id, receipt_context=None)`

> 非 dry-run 的 `cycle_id` 与 `receipt_context` 必传。`receipt_context` 必须是有效 JSON dict，含 `status=ok`、同一 `cycle_id`、`decision_protocol=decision_card_v1` 和完整 `decision_card`；执行器在任何 OKX I/O 前校验，失败直接 reject。**接管场景（2026-08-10 Wave1 序6）**：executor 会按 cycle 派生 session-key 独立重算本会话 actor 时间线（只算不透明指纹，零模型名）；分析与执行 actor epoch 不同（OPEN/ADD 路径）时，`receipt_context["actor_attestation"]` 必须携带 `scripts/actor_attestation.py` 的完整产物——缺失/hash 不符/重验包未全过/超 10 分钟时效/凭证生成后 actor 再变，均拒单 `handoff_attestation_required`。非 dry-run 时间线不可得时不能证明未接管，账户/订单 I/O 前拒单 `actor_timeline_required`；CLOSE/REDUCE/ADJUST_PROTECTION 恒不受本闸约束。新开仓的 `risk_reward.exit_mode` 必须显式为 `fixed_tp|dynamic_exit|no_fixed_tp`；`fixed_tp` 的 `tp_trigger_px` 必须等于卡内 target，executor 先确认 SL，再按实际 fill_sz 独立挂 TP。TP 未回读只留 repair，不因 SL 已安全而 unwind。移动 TP 时使用新全仓 OCO 双腿确认后再撤旧保护，普通 conditional 不得同时承载 TP+SL。

> OPEN/ADD 还必须原样携带 analysis 的 `decision_card.multitimeframe_analysis`：固定 cycle、15m/1H/4H 三周期逐项证据与唯一 rank 1/2/3、rank=1 的选择、`calibrated_confidence=null`、`confidence_claim_allowed=false`，以及 `multitimeframe_decision_evidence.py` 返回的完整 evidence_contract。executor 在任何 OKX 账户/订单 I/O 前只读重验当前 exact 已收盘三周期数据；未就绪拒绝 `multitimeframe_data_not_ready`。完全一致走 `current_market_exact`；若同槽后续采集修订已收盘数据，卡内契约必须逐字段命中同 cycle/symbol/side 的 `analysis.db` writer 已验证锚点 `analysis_db_writer_validated`，并在回执留 `post_analysis_market_revision=true` 与 supplied/current/persisted hash。否则拒绝 `multitimeframe_context_mismatch`；持久化锚点不能替代当前 readiness。CLOSE/REDUCE 不受此开仓闸阻断。

返回统一回执（OPEN/CLOSE 同形）：

```json
{
  "profile": "live",
  "ok": true,
  "action_taken": "OPEN_LONG",
  "symbol": "BTC-USDT-SWAP",
  "side": "long",
  "trades": [ "...见 §2 trades[]..." ],
  "p0": false,
  "risk": "<对象：见 §3 风控留痕>",
  "ord_id": "...",
  "clamped": false,
  "adjustments": [],
  "lev_warn": null
}
```

| 字段 | 说明 |
|---|---|
| `profile` | 固定 `'live'`（执行器硬拒其它值） |
| `ok` | 本笔执行是否成功（true=已成交/已平/无仓可平；false=被拒/兜底失败） |
| `action_taken` | 执行回执使用 `OPEN_LONG`/`OPEN_SHORT`/`CLOSE`/`REDUCE`/`ADJUST_PROTECTION`/`REJECT`/`UNWIND`；无交易所副作用使用 `HOLD`/`WAIT`。`ADJUST` 仅是推送展示词，不得手写进 Live 回执；只有含 `protection_change`、受支持 `path`、`protection_state.ok=true` 与 `applied` 的成功 `ADJUST_PROTECTION` 回执才渲染为 `ADJUST`。 |
| `symbol` / `side` | 标的 / 方向（CLOSE 回执 side 取自 OKX API 现仓确认的 posSide） |
| `trades[]` | 真实成交明细（回读 fills 后），喂 trades_writer。被拒/无仓时为 `[]` |
| `p0` | true=触发 P0（裸仓 unwind / fills 拉不到 / close+reduceOnly 兜底均败）。trader 见 p0 必走 P0 流程 |
| `risk` | risk_validator 完整返回（§3），全程留痕 |
| `ord_id` / `algo_id` | OKX 订单号 / 独立 algo SL 单号（在 trade 内） |
| `tp_mode` / `tp_verified` / `tp_warning` | 可选 TP 的装配与回读状态；`tp_unsecured` 表示未确认止盈，但不等于裸仓（SL 仍是安全底线） |
| `clamped`/`adjustments` | clamp 标记 / 调整说明（透传自 risk） |
| `unwind` | UNWIND 时附平裸仓子回执 |
| `reject_reason`/`reject_detail` | REJECT/UNWIND 时拒因（机读码 + 人读）。码含 `no_sl`/`bad_sl`/`bad_lev`/`bad_mark_px`/`bad_equity`/Live `available_margin_missing|insufficient_available_margin`/`instrument_unknown`/`set_leverage_failed`/`place_failed`/`sl_failed_unwound`/`fills_missing` 等 |
| `capacity` | 执行容量证据：同次账户余额的 `account.imr/totalEq` 与实时 USDT 可用额 |
| `reduce_only_fallback`/`fills_ok` | CLOSE 专属：是否经 reduceOnly 市价单完成平仓（2026-07-03 起为主路径，正常情况即 True；False=降级走 swap close CLI 兜底）/ fills 是否回读成功 |
| `note` | 如 `no_open_position`（已平，幂等成功） |

> **OPEN 不变量**：装配现场 -> Live 同次取 `account.imr/totalEq` 与实时 USDT 可用额并计算预计组合 IMR-> 强制 profile-aware risk_validator -> 市价开仓即附挂 SL -> 按 symbol、posSide、平仓 side、reduceOnly、数量、触发价和 live 状态回读，独立 algo 必须命中本次精确 algoId -> 附挂失败独立 algo SL（重试1）-> 仍失败立即市价平掉裸仓 unwind(p0) -> 从 fills/订单状态/订单历史端点求真实际成交（均失败 -> repair_queue + reject + p0）。
> **CLOSE 不变量**（2026-07-03 主路径反转）：OKX API 现仓确认 posSide -> reduceOnly 反向市价单（主路径，拿 ordId 即时确认，绝不翻反向仓）-> 被拒（51023/51169 等）转 swap close CLI 兜底 -> 51087 下架/51001 不存在明确拒因 -> 权威端点回读真实 pnl、实际 `fill_sz` 和 `fill_ts`。
> **开仓必须传 `sl_trigger_px`**；缺失或非有限、long 不低于 mark、short 不高于 mark、偏离超过 30% 均 reject。

## 2. trades[] 每笔成交字段（喂 trades_writer.trades 表）

执行器 OPEN 产出（回读 fills 后）：

```json
{
  "symbol": "BTC-USDT-SWAP",
  "action": "open",
  "side": "long",
  "sz": 1,
  "fill_sz": 1,
  "approved_sz": 2,
  "fill_px": 62500.0,
  "px": 62500.0,
  "lev": 5,
  "margin": 125.0,
  "notional": 625.0,
  "pnl": 0.0,
  "channel": "live",
  "reason": "趋势向上开多",
  "open_id": "<ordId>",
  "sl_trigger_px": 60800.0,
  "algo_id": null,
  "sl_mode": "attached",
  "sl_verified": true,
  "tp_trigger_px": 65500.0,
  "tp_mode": "attached",
  "tp_verified": true,
  "tp_warning": null,
  "fill_source": "fills",
  "fill_ts": "2026-07-29 10:15:04",
  "ts_source": "fills.fillTime",
  "ct_val": 0.01,
  "ordId": "<ordId>",
  "partial_fill": true,
  "fill_ratio": 0.5,
  "single_order_imr_ratio": 0.125,
  "max_single_order_imr_ratio": 0.15,
  "single_order_cap_breached": false,
  "single_order_risk_usdt": 18.4,
  "single_order_risk_pct_equity": 0.019,
  "max_single_order_risk_pct_equity": 0.05,
  "single_order_risk_cap_breached": false,
  "risk_clamped": false,
  "risk_adjustments": []
}
```

> 补键释义（2026-07-29）：`sl_mode`=`'attached'`（随开仓附挂）/`'algo'`（独立 algo SL belt）/`'none'`（仅 dry-run/明确降级留痕）；`sl_verified`=同一张 SL 挂单经严格身份字段回读确认；`tp_mode/tp_verified/tp_warning` 分别记录可选止盈的装配方式、严格回读结果和未确认原因，TP 未确认但 SL 已安全时只进入修复队列，不反向平仓；confirmed 的 `fill_source` 只接受真实确认来源 `fills|order_status|orders_history`。confirmed 的 `sz` 是权威端点实际 `fill_sz`，`approved_sz` 只记录风控批准上限，部分成交时两者可以不同；`fill_ts/ts_source` 记录权威成交时间与来源。fills 取最后一笔 `fillTime`（缺失用该 fill `ts`），订单状态取 `fillTime`（缺失用终态 `uTime`），禁止用 `cTime` 冒充。OPEN 所有权威端点都无法确认时必须 reject，不得以 mark price、历史聚合或估算值伪造成交；CLOSE 可记录仓位已消失但成交事实待对账的 `unconfirmed` 状态，此时 `fill_sz/fill_px/pnl/fill_ts=null`，`sz` 仅保留仓前请求量用于审计，不计作已确认成交统计。`ct_val`=本环境真实合约面值；`ordId`=成交订单号（合并闸/journal 重放按此精确匹配）。示例数字按 `notional = sz*ctVal*fill_px` 自洽（上例 approved 2 张仅成交 1 张，故 `partial_fill=true, fill_ratio=0.5`）。**单笔保证金审计键（2026-08-08）**：`single_order_imr_ratio`=真实成交保证金÷执行时 equity、`max_single_order_imr_ratio`=硬边界 0.15、`single_order_cap_breached`=成交后滑点突破 15%（只登记入 repair_queue 人工出口，不阻断不外发）、`risk_clamped`/`risk_adjustments`=validator 缩量透传；该上限作用于本次 OPEN/ADD 增量，既有仓位不扣减下一笔预算，累计组合另走 66.6% IMR 闸；push 风控行与复盘直读这些键，trader 禁重算。**止损风险审计键（2026-08-10 Wave1 序7）**：`single_order_risk_usdt`=名义×(成交价距SL+0.2%缓冲)、`single_order_risk_pct_equity`=÷执行时 equity、硬边界 `max_single_order_risk_pct_equity`=0.05（主人确认的当前预算）、`single_order_risk_cap_breached`=成交后滑点突破仅告警+repair_queue 不阻断——语义与保证金审计键完全同型。

CLOSE 产出：`action="close"`、`pnl`=回读 fills 真实 pnl（拉不到为 null）、`reduce_only_fallback`、无 `lev/margin/notional`。

| trades_writer 落库列（`trades` 表） | 取自 trade 字段 | 说明 |
|---|---|---|
| `symbol` | `symbol` | 必填，writer 校验非空 |
| `action` | `action` | `'open'`/`'close'`/`'add'`/`'reduce'`/`'none'`。**`none` 行 writer 跳过不落** |
| `side` | `side` | long/short |
| `sz` | `sz` | confirmed 时为权威成交端点确认的实际 `fill_sz`；unconfirmed close/reduce 时仅为仓前请求量审计，禁止计作已确认成交 |
| `fill_sz` | trade raw 审计字段 | confirmed 时必须与 `sz` 相等；unconfirmed 时必须为 null，等待对账 |
| `fill_px` | `fill_px` | 权威成交端点确认的真实成交价；OPEN 缺失即拒绝，禁止回退 mark price/历史聚合。CLOSE 仅 `fill_source=unconfirmed` 时可为 null，等待对账且不计入已确认成交统计 |
| `lev` | `lev` | 杠杆 |
| `margin` | `margin` | 每仓保证金 = notional / lev |
| `notional` | `notional` | 名义 = 实际 `sz * ctVal * fill_px` |
| `score_total` | null | 历史兼容列；decision_card_v1 不再回填评分 |
| `reasoning` | `reasoning`（执行器用 `reason`，trader 装配时映射） | 决策依据 |
| `deviation` | `deviation` | 偏离信号留痕（可选） |
| `degradation` | `degradation` | 降级留痕（可选） |
| `pnl` | `pnl` | open=0.0；close=回读真实 pnl |

## 3. risk/clamp 留痕（risk_validator.validate 返回）

执行器回执 `risk` 字段即 `validate()` 完整返回，**全程留痕**（push「风控」段 + 复盘消费）。下例是 **Live** 回执：

```json
{
  "approved": true,
  "approved_sz": 1,
  "clamped": false,
  "adjustments": [],
  "reject_reason": null,
  "reject_detail": null,
  "math": {
    "symbol": "BTC-USDT-SWAP", "side": "long", "profile": "live",
    "intended_sz": 1, "requested_lev": 5, "effective_lev": 5,
    "mark_px": 62500.0, "ct_val": 0.01, "lot_sz": 1,
    "equity": 1000.0,
    "per_contract_margin": 125.0,
    "account_imr": 475.0,
    "incremental_order_imr": 125.0,
    "projected_account_imr": 600.0,
    "current_portfolio_imr_ratio": 0.475,
    "projected_portfolio_imr_ratio": 0.60,
    "max_portfolio_imr_ratio": 0.666,
    "portfolio_imr_ratio_unit": "fraction",
    "portfolio_imr_source": "account.balance.imr",
    "available_margin_raw": 300.0,
    "available_margin_budget": 294.0,
    "margin_budget_binding": "available_margin",
    "max_sz_available": 2,
    "max_single_order_imr_ratio": 0.15,
    "single_order_sizing_budget": 147.0,
    "max_sz_single_order": 1,
    "single_order_imr_ratio": 0.125
  }
}
```

> 示例数字全程自洽（2026-08-08 校正，原示例 180/1000 标 0.60 系笔误）：每张保证金=62500×0.01÷5=125；组合当前 475/1000=0.475、预计 (475+125)/1000=0.60≤0.666 放行；可用保证金预算 300×0.98=294→最多 2 张；单笔预算 1000×0.15×0.98=147→最多 1 张；本单占比 125/1000=0.125≤0.15。旧键 `equity_margin_budget`/`margin_budget` 已随 20% 时代废除，现行键如上。`math` 为节选，完整键以 `validate()` 实产为准，trader 禁重算。

模块级 **Live 硬上限常量**（`core/risk_validator.py`；仅 `profile=live`）：

| 常量 | 值 | 含义 |
|---|---|---|
| `MAX_PORTFOLIO_IMR_RATIO` | `0.666` | 预计成交后组合 `(account.imr+incremental_order_imr)/totalEq` 上限 66.6%；超限整笔 reject，不 clamp |
| `MAX_SINGLE_ORDER_IMR_RATIO` | `0.15` | 下一笔 OPEN/ADD 增量保证金 ≤15% 净值（2026-08-08 加；定仓预算 14.7%=×`SINGLE_ORDER_SIZING_HEADROOM_PCT=0.98`）；既有仓位不扣减下一笔单笔预算，累计组合另走 66.6% IMR 闸；超限按 lotSz 缩量，与最小单位/名义下限冲突整笔 reject（`single_order_cap_infeasible`）；成交后滑点突破仅登记（stderr + repair_queue 人工出口，战报标「已入修复队列」；无 C2C 外发），不阻断 |
| `AVAILABLE_MARGIN_USE_PCT` | `0.98` | 本笔最多使用当前 USDT 可用保证金的 98% |
| `MAX_LEVERAGE` | `10.0` | 杠杆上限 10x |
| `MIN_NOTIONAL_PCT` | `0.01` | 名义下限 1%（太小拒） |
| `MAX_SL_DEVIATION` | `0.30` | SL 偏离 mark_px 上限 30% |

> （旧同侧/集中度固定阈值已于 2026-07-15 主人拍板取消——除现行保证金/止损风险约束与强制 SL 外不加条件；同侧暴露仅算入 `math_box["same_side_existing_notional"]` 观察留痕。回执不得复述已废止阈值，避免被误当成现行规则。）

> **Live 组合 IMR 闸**：当前值=`account.imr/totalEq`，预计值=`(account.imr+incremental_order_imr)/totalEq`；预计超过 66.6% 时整笔拒绝 OPEN/ADD，禁止 clamp 或自动缩量重试，CLOSE/REDUCE 不受影响。有开仓时直接保留执行器返回的 risk，禁 trader 重算。
>
> `mgnRatio` 是 OKX 风险健康度，gross/net 是名义敞口，逐仓 `notional/lev` 求和也只可作推送观察；它们均不得冒充或替代执行时同次 `account.balance.imr/totalEq`。

> `position_budget(mark_px, ct_val, lot_sz, equity, lev, available_margin, account_imr, profile="live")` 只展示名义下限、可用资金张数、组合 IMR 剩余空间与**单笔 15% 预算**（`single_order_sizing_budget`/`max_sz_single_order`/`max_single_order_imr_ratio`，2026-08-08 与 validate 同口径加入），不能预批订单；2026-08-08 核实**无生产调用方**（Agent 侧确定性预算入口是 facts 的 `balance.single_order_margin_budget_usdt`）。Live 放行仍须 executor 使用同次账户余额和本单增量。

> Demo 的 `risk.math` 语义（`sizing_policy=okx_demo_max_size_only` 等）随 2026-08-06 demo 全量下线移除。
## 4. cycle 级回执（trader -> trades_writer.write_trades）

trader 聚合本轮各币执行回执，装配 cycle 级 JSON 喂 writer（落 `trade_cycles` 每轮一行）：

```json
{
  "cycle_id": "2026-06-24T14:00",
  "ts": "2026-06-24 14:06:10",
  "mode": "live",
  "decision": "traded",
  "action": "BTC/USDT: open_long",
  "regime": "risk_on",
  "regime_stale": 0,
  "decision_protocol": "decision_card_v1",
  "decision_card": {
    "direction_evidence": ["..."],
    "opposing_evidence": ["..."],
    "execution_conditions": {"status": "..."},
    "invalidation_point": {"condition": "..."},
    "risk_reward": {"rr": 2.1},
    "portfolio_impact": {"summary": "..."},
    "multitimeframe_analysis": {
      "cycle_id": "2026-06-24T14:00",
      "required_timeframes": ["15m", "1H", "4H"],
      "timeframes": {
        "15m": {"direction": "long", "evidence": ["..."], "relative_rank": 2},
        "1H": {"direction": "neutral", "evidence": ["..."], "relative_rank": 3},
        "4H": {"direction": "long", "evidence": ["..."], "relative_rank": 1}
      },
      "selected_timeframe": "4H",
      "selected_direction": "long",
      "selection_reason": "4H 为三周期相对第一且方向匹配",
      "selection_method": "relative_rank_1_among_15m_1H_4H_not_calibrated",
      "calibrated_confidence": null,
      "confidence_claim_allowed": false,
      "evidence_contract": {"protocol": "multitimeframe_market_evidence_v1", "完整对象": "原样复制工具输出"}
    },
    "historical_experience": {
      "matched_wins": [], "matched_losses": [], "missed_opportunities": [],
      "evidence_contract": {"protocol": "experience_evidence_v2", "query": {}, "summaries": {}, "samples_truncated": true, "evidence_hash": "<工具原值>"},
      "usage": "partial", "reason": "..."
    },
    "agent_judgement": "...",
    "reference_overrides": []
  },
  "n_orders": 1,
  "equity": 1000.0,
  "pnl_session": 0.0,
  "pnl_open": -3.2,
  "leverage": 5,
  "note": "...",
  "experiences_cited": [ "...见 §5..." ],
  "trades": [ "...§2 每笔..." ],
  "errors": [],
  "status": "ok"
}
```

> 正常生产写入时 `trade_cycles.ts` 由 writer 取当前 CST 提交时间；调用方传入的 `ts` 只保留在 raw 作为 `reported_ts`，不得控制业务时间。每笔 `trades.ts` 优先用 executor 的权威 `fill_ts`；缺失时 writer 才使用提交时间，并在 raw 写明 `ts_source=writer_commit_fallback`。只有受控 journal/维护入口可以显式使用可信内部时间覆盖。

| `trade_cycles` 落库列 | 取自 cycle 字段 | 说明 |
|---|---|---|
| `cycle_id` | `cycle_id` | 必填（writer 校验），UTC+8 槽位 |
| `ts` | `ts`（缺则 now UTC+8） | 完成时刻 |
| `mode` | writer 按 `profile` 固定 | `live`；payload 中自填的 `full` 不作为落库事实 |
| `decision` | `decision`（归一：traded/hold/skip/degraded/error；open->traded、none/空->hold） | writer `normalize_decision` |
| `n_orders` | `n_orders` | 实际写出订单数（writer 按落库 trades 计） |
| `equity` | `equity` | 账户权益（OKX API，禁 position_snapshots） |
| `note` | 由 `action`+`note`+`regime`+`open_pnl` 拼 | writer 自动组装 |
| `raw` | `raw` 或整 payload（截断 10000 字符） | 留痕 |

> `pnl_session`/`pnl_open`/`leverage`/`regime` 入 `note`/`raw`；`decision_card`、历史经验、errors/status 入 raw 留痕。旧 `total_score/confidence` 可读但新协议不写。

## 5. historical_experience（经验取舍留痕）

trader 决策前搜相似经验，把盈利、亏损与错失机会写进决策卡：

```json
{
  "matched_wins": [{"sim": 0.86, "pnl_pct": 2.2, "lesson": "..."}],
  "matched_losses": [{"sim": 0.81, "pnl_pct": -1.1, "lesson": "..."}],
  "missed_opportunities": [{"actual_4h_pct": 3.4, "would_hit_1r_fixed2pct": 1}],
  "evidence_contract": {"protocol": "experience_evidence_v2", "query": {}, "summaries": {}, "samples_truncated": true, "evidence_hash": "<工具原值>"},
  "usage": "partial",
  "reason": "采用趋势延续经验，忽略旧样本中过时的流动性条件"
}
```

- `usage` 必须是 `adopt|partial|ignore|none`，并写理由。
- open 信号的 `evidence_contract` 必须从固定 cycle `--as-of` 工具输出原样继承；数字只认 `exact_setup/same_symbol_similar/cross_symbol_similar` 具名 summary，展示数组已截断，禁止重数或混栏。
- 样本数、胜率和可信度只描述过去，不能自动批准/否决。
- 经验由 `trades_writer` 挂钩 `trade_experience_writer.py` 写入 `account.db.trade_experiences`。

## 6. 调用与校验

```python
# 该片段必须与 open_position/close_position 调用处于同一临时 Python 进程。
from collectors.trades_writer import commit_receipt

receipt = open_position(..., cycle_id=cycle_id, receipt_context=receipt_context)
result = commit_receipt(receipt, profile)
if not result.get("ok") or result.get("refused"):
    raise RuntimeError(result)
```

只有 HOLD/WAIT 等无交易所副作用动作才可把完整 cycle 回执**一次性整文件写入** `<PROJECT_ROOT>/tmp/_receipt_<profile>_YYYY-MM-DDTHH-MM.json`，再调 `trades_writer.py --json-file ... --facts-file <本轮 facts>`。REDUCE/ADJUST_PROTECTION 即使没有普通成交行，也必须在 executor 同一进程中原样挂 facts 并提交 writer。顶层 `decision_card` 必须直接使用 §4 示例的九个固定键，不能改成摘要/候选/持仓列表。使用 `--facts-file` 时回执应省略 `live_facts` 让 writer 原样注入；若携带则必须与 facts 文件整份完全相同。文件名中的 `:` 必须换成 `-`，防止 NTFS ADS。禁止用 edit/局部补丁循环拼 JSON；schema 失败最多整文件重写一次，第二次仍失败即终止，不补派、不重推。

| 校验项 | 由谁 | 失败行为 |
|---|---|---|
| Live 组合 IMR 硬闸 | `core/risk_validator.validate`（执行器内部强制调） | 预计成交后 `account.imr/totalEq>66.6%` 时整笔 reject OPEN/ADD，不 clamp；CLOSE/REDUCE 不受影响 |
| SL 保障（必带、方向正确且必须挂上） | `core/risk_validator.validate` + `core/order_executor.open_position` | 无效/反向 SL reject；严格身份回读失败后重试独立 SL；挂单全败 -> 市价平裸仓 |
| 现仓真伪 | OKX API（`fetch_open_positions`） | 禁 position_snapshots GROUP BY（红线 #6） |
| 成交真伪 | fills → order status / orders-history 双源确认 | OPEN 均确认不了 -> repair_queue + reject + p0；禁止 mark/聚合估算兜底。CLOSE 未确认只留 null 待对账，不计确认成交 |
| 回执 schema（必填 `cycle_id`；完整 decision_card；`trades` 是 list；动作合法；拒单不得进 trades；成交 `sz>0`；OPEN `fill_px>0`） | `trades_writer.validate` | 错误列表 -> exit 1，**不写库** |
| 落地核对 | 读 `*_trades.db` trade_cycles/trades + `ledger.py show` | 账本核对真落地 |

成功输出：`{"ok": true, "cycle_id": "...", "n_orders": N}`（exit 0）。
