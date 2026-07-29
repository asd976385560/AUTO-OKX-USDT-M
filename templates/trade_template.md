<!--
doc: trade_template
doc-version: V2.0-template
last-updated: 2026-07-29
updated-by: Codex
change-summary: 对齐交易前全账户未决意图闸、止损身份、部分成交、权威成交时间与writer提交时间契约。
role: 交易回执模板（live/demo trader -> live_trades.db / demo_trades.db）
权威: skill.md（交易执行契约节）+ core/order_executor.py + core/risk_validator.py + collectors/trades_writer.py
落点: <PROJECT_ROOT>\db\live_trades.db / demo_trades.db（trade_cycles + trades）
writer: <PROJECT_ROOT>\collectors\trades_writer.py（唯一通道，禁手写 INSERT）
-->

> ⚠️ **2026-07-29 一致性审计校正**：本模板已对齐成交前决策卡校验、同 profile 全局未决执行意图闸、交易主账与 OKX API 全仓一致性闸、部分成交和原子落账契约；与 skill.md / 对应 writer·core 代码冲突时以后者为准。

# 交易回执模板 — live/demo trader -> *_trades.db

> live 下单**唯一路径** = `core/order_executor.open_position()` / `close_position()`，其内部**强制调** `core/risk_validator.validate()`（LLM 物理越不过闸，红线 #7）。
> 执行器产出已携完整 cycle/决策卡的**回执 dict**。任何确认成交轮都必须在调用 executor 的同一确定性 Python 进程内立即调用 `collectors.trades_writer.commit_receipt(receipt, profile)`；禁止先退出、再由模型下一次工具调用补落账。HOLD/ADJUST 等无成交回执才可走 `--json-file`。
> 红线：写库必走 writer，禁手写 INSERT；现仓以 OKX API 为准（禁 position_snapshots GROUP BY）；勿用 ctVal 直接比硬上限（先算每张保证金）。零模型名。

## 1. order_executor 回执 dict（每笔执行产物）

`open_position(symbol, side, intended_sz, lev, sl_trigger_px, profile, mgn_mode='cross', mark_px, equity, open_positions, reasoning, db_root, cycle_id, available_margin=None, receipt_context=None)`
`close_position(symbol, profile, pos_side, mgn_mode='cross', reasoning, db_root, cycle_id, receipt_context=None)`

> 非 dry-run 的 `cycle_id` 与 `receipt_context` 必传。`receipt_context` 必须是有效 JSON dict，含 `status=ok`、同一 `cycle_id`、`decision_protocol=decision_card_v1` 和完整 `decision_card`；执行器在任何 OKX I/O 前校验，失败直接 reject。

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
  "risk": { "...见 §3 风控留痕..." },
  "ord_id": "...",
  "clamped": false,
  "adjustments": [],
  "lev_warn": null
}
```

| 字段 | 说明 |
|---|---|
| `profile` | `'live'` 或 `'demo'`（执行器归一） |
| `ok` | 本笔执行是否成功（true=已成交/已平/无仓可平；false=被拒/兜底失败） |
| `action_taken` | `OPEN_LONG`/`OPEN_SHORT`/`CLOSE`/`REJECT`/`UNWIND`。**推送动作段正则只认** `OPEN_LONG OPEN_SHORT CLOSE STOP_LOSS ADJUST`（见 push 校验） |
| `symbol` / `side` | 标的 / 方向（CLOSE 回执 side 取自 OKX API 现仓确认的 posSide） |
| `trades[]` | 真实成交明细（回读 fills 后），喂 trades_writer。被拒/无仓时为 `[]` |
| `p0` | true=触发 P0（裸仓 unwind / fills 拉不到 / close+reduceOnly 兜底均败）。trader 见 p0 必走 P0 流程 |
| `risk` | risk_validator 完整返回（§3），全程留痕 |
| `ord_id` / `algo_id` | OKX 订单号 / 独立 algo SL 单号（在 trade 内） |
| `clamped`/`adjustments` | clamp 标记 / 调整说明（透传自 risk） |
| `unwind` | UNWIND 时附平裸仓子回执 |
| `reject_reason`/`reject_detail` | REJECT/UNWIND 时拒因（机读码 + 人读）。码含 `no_sl`/`bad_sl`/`bad_lev`/`bad_mark_px`/`bad_equity`/`available_margin_missing`/`insufficient_available_margin`/`instrument_unknown`/`set_leverage_failed`/`place_failed`/`sl_failed_unwound`/`fills_missing` 等 |
| `reduce_only_fallback`/`fills_ok` | CLOSE 专属：是否经 reduceOnly 市价单完成平仓（2026-07-03 起为主路径，正常情况即 True；False=降级走 swap close CLI 兜底）/ fills 是否回读成功 |
| `note` | 如 `no_open_position`（已平，幂等成功） |

> **OPEN 不变量**：装配现场 -> 强制 risk_validator -> 市价开仓即附挂 SL -> 按 symbol、posSide、平仓 side、reduceOnly、数量、触发价和 live 状态回读，独立 algo 必须命中本次精确 algoId -> 附挂失败独立 algo SL（重试1）-> 仍失败立即市价平掉裸仓 unwind(p0) -> 从 fills/订单状态/订单历史端点求真实际成交（均失败 -> repair_queue + reject + p0）。
> **CLOSE 不变量**（2026-07-03 主路径反转）：OKX API 现仓确认 posSide -> reduceOnly 反向市价单（主路径，拿 ordId 即时确认，绝不翻反向仓）-> 被拒（51023/51169 等）转 swap close CLI 兜底 -> 51087 下架/51001 不存在明确拒因 -> 权威端点回读真实 pnl、实际 `fill_sz` 和 `fill_ts`。
> **live/demo 开仓必须传 `sl_trigger_px`**；缺失或非有限、long 不低于 mark、short 不高于 mark、偏离超过 30% 均 reject（双盘一致）。

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
  "fill_source": "fills",
  "fill_ts": "2026-07-29 10:15:04",
  "ts_source": "fills.fillTime",
  "ct_val": 0.01,
  "ordId": "<ordId>"
}
```

> 补键释义（2026-07-29）：`sl_mode`=`'attached'`（随开仓附挂）/`'algo'`（独立 algo SL belt）/`'none'`（仅 dry-run/明确降级留痕）；`sl_verified`=同一张 SL 挂单经严格身份字段回读确认；confirmed 的 `fill_source` 只接受真实确认来源 `fills|order_status|orders_history`。confirmed 的 `sz` 是权威端点实际 `fill_sz`，`approved_sz` 只记录风控批准上限，部分成交时两者可以不同；`fill_ts/ts_source` 记录权威成交时间与来源。fills 取最后一笔 `fillTime`（缺失用该 fill `ts`），订单状态取 `fillTime`（缺失用终态 `uTime`），禁止用 `cTime` 冒充。OPEN 所有权威端点都无法确认时必须 reject，不得以 mark price、历史聚合或估算值伪造成交；CLOSE 可记录仓位已消失但成交事实待对账的 `unconfirmed` 状态，此时 `fill_sz/fill_px/pnl/fill_ts=null`，`sz` 仅保留仓前请求量用于审计，不计作已确认成交统计。`ct_val`=本环境真实合约面值；`ordId`=成交订单号（合并闸/journal 重放按此精确匹配）。示例数字按 `notional = sz*ctVal*fill_px` 自洽。

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

执行器回执 `risk` 字段即 `validate()` 完整返回，**全程留痕**（push「风控」段 + 复盘消费）：

```json
{
  "approved": true,
  "approved_sz": 1,
  "clamped": true,
  "adjustments": ["intended 3 张超单笔保证金 20% 上限，clamp 至 1 张"],
  "reject_reason": null,
  "reject_detail": null,
  "math": {
    "symbol": "BTC-USDT-SWAP", "side": "long", "profile": "live",
    "intended_sz": 3, "requested_lev": 5, "effective_lev": 5,
    "mark_px": 62500.0, "ct_val": 0.01, "lot_sz": 1,
    "equity": 1000.0, "available_margin_raw": 180.0,
    "equity_margin_budget": 200.0, "available_margin_budget": 176.4,
    "margin_budget": 176.4, "margin_budget_binding": "available_margin"
  }
}
```

模块级**硬上限常量**（`core/risk_validator.py`，live/demo 同一套硬上限判定，均越不过）：

| 常量 | 值 | 含义 |
|---|---|---|
| `MAX_MARGIN_PCT` | `0.20` | 单笔保证金占 equity 上限 20% |
| `AVAILABLE_MARGIN_USE_PCT` | `0.98` | 本笔最多使用当前 USDT 可用保证金的 98% |
| `MAX_LEVERAGE` | `10.0` | 杠杆上限 10x |
| `MIN_NOTIONAL_PCT` | `0.01` | 名义下限 1%（太小拒） |
| `MAX_SL_DEVIATION` | `0.30` | SL 偏离 mark_px 上限 30% |

> （MAX_SAME_SIDE_PCT 同侧 60% 闸已于 2026-07-15 主人拍板取消——除硬上限+强制 SL 外不加条件；同侧暴露仅算入 `math_box["same_side_existing_notional"]` 观察留痕，无闸。）

> **20% 仅是本次 OPEN/ADD 单笔上限，不是组合总保证金上限。** HOLD/ADJUST 的组合估算比例放 `risk.portfolio_observation.estimated_margin_pct_equity`，不得冒充 `risk.margin_pct`；有成交时直接保留执行器返回的 risk，禁 trader 重算。真实开仓有效预算=`min(20%×totalEq,98%×details.USDT可用保证金)`。

> `position_budget(mark_px, ct_val, lot_sz, equity, lev)` 供 `decision_briefing` 预算（先算每张保证金 `per_contract_margin = mark_px*ct_val/lev`，再得 `max_sz_margin`/`min_notional_sz`）——红线 #8：**勿用 ctVal 直接比硬上限**。

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
    "historical_experience": {
      "matched_wins": [], "matched_losses": [], "missed_opportunities": [],
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
| `mode` | writer 按 `profile` 固定 | `live` 或 `demo`；payload 中自填的 `full` 不作为落库事实 |
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
  "missed_opportunities": [{"actual_4h_pct": 3.4, "would_hit_1R": 1}],
  "usage": "partial",
  "reason": "采用趋势延续经验，忽略旧样本中过时的流动性条件"
}
```

- `usage` 必须是 `adopt|partial|ignore|none`，并写理由。
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

无成交 HOLD/ADJUST 才可把完整 cycle 回执写入 `<PROJECT_ROOT>/tmp/_receipt_<profile>_YYYY-MM-DDTHH-MM.json`，再调 `trades_writer.py --json-file ...`。文件名中的 `:` 必须换成 `-`，防止 NTFS ADS。

| 校验项 | 由谁 | 失败行为 |
|---|---|---|
| 风控硬闸（live 越不过） | `core/risk_validator.validate`（执行器内部强制调） | reject -> 执行器回执 `ok=false action_taken=REJECT`，**不下单** |
| SL 保障（live/demo 必带、方向正确且必须挂上） | `core/risk_validator.validate` + `core/order_executor.open_position` | 无效/反向 SL reject；严格身份回读失败后重试独立 SL；挂单全败 -> 市价平裸仓 UNWIND + p0 |
| 现仓真伪 | OKX API（`fetch_open_positions`） | 禁 position_snapshots GROUP BY（红线 #6） |
| 成交真伪 | fills → order status / orders-history 双源确认 | OPEN 均确认不了 -> repair_queue + reject + p0；禁止 mark/聚合估算兜底。CLOSE 未确认只留 null 待对账，不计确认成交 |
| 回执 schema（必填 `cycle_id`；完整 decision_card；`trades` 是 list；动作合法；拒单不得进 trades；成交 `sz>0`；OPEN `fill_px>0`） | `trades_writer.validate` | 错误列表 -> exit 1，**不写库** |
| 落地核对 | 读 `*_trades.db` trade_cycles/trades + `ledger.py show` | 账本核对真落地 |

成功输出：`{"ok": true, "cycle_id": "...", "n_orders": N}`（exit 0）。
