<!--
doc: trade_template
role: 交易回执模板（live/demo trader -> live_trades.db / demo_trades.db）
权威: skill.md（交易执行契约节）+ core/order_executor.py + core/risk_validator.py + collectors/trades_writer.py
落点: <PROJECT_ROOT>\db\live_trades.db / demo_trades.db（trade_cycles + trades）
writer: <PROJECT_ROOT>\collectors\trades_writer.py（唯一通道，禁手写 INSERT）
-->

> ⚠️ **2026-07-17 一致性审计校正**：本模板曾冻结在 ~2026-06-24 契约，以下已按现行实现修正；与 skill.md / 对应 writer·core 代码冲突时以后者为准。

# 交易回执模板 — live/demo trader -> *_trades.db

> live 下单**唯一路径** = `core/order_executor.open_position()` / `close_position()`，其内部**强制调** `core/risk_validator.validate()`（LLM 物理越不过闸，红线 #7）。
> 执行器产出**回执 dict**（兼容 trades_writer）；trader 据此装配 cycle 级回执，用文件工具写入无冒号的 tmp UTF-8 JSON，再经 `trades_writer.py --json-file ... --profile live|demo` 落库。
> 红线：写库必走 writer，禁手写 INSERT；现仓以 OKX API 为准（禁 position_snapshots GROUP BY）；勿用 ctVal 直接比硬上限（先算每张保证金）。零模型名。

## 1. order_executor 回执 dict（每笔执行产物）

`open_position(symbol, side, intended_sz, lev, sl_trigger_px, profile, mgn_mode='cross', mark_px, equity, open_positions, reasoning, db_root, cycle_id, available_margin=None)`
`close_position(symbol, profile, pos_side, mgn_mode='cross', reasoning, db_root, cycle_id)`

> `cycle_id` 必传——执行 journal 归账用（成交即留痕 `db/journal/exec_{profile}.jsonl`，2026-07-16 rank1）。签名里为可选参数（默认 None），但 trader 配方一律显式传本轮 cycle_id。

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

> **OPEN 不变量**：装配现场 -> 强制 risk_validator -> 市价开仓即附挂 SL（原子无裸仓窗口）-> 附挂失败独立 algo SL（重试1）-> 仍失败立即市价平掉裸仓 unwind(p0) -> 回读 fills 求真成交（拉不到 -> repair_queue + reject + p0）。
> **CLOSE 不变量**（2026-07-03 主路径反转）：OKX API 现仓确认 posSide -> reduceOnly 反向市价单（主路径，拿 ordId 即时确认，绝不翻反向仓）-> 被拒（51023/51169 等）转 swap close CLI 兜底 -> 51087 下架/51001 不存在明确拒因 -> 回读 fills 求真 pnl。
> **live/demo 开仓必须传 `sl_trigger_px`**，否则 reject `no_sl`（双盘一致）。

## 2. trades[] 每笔成交字段（喂 trades_writer.trades 表）

执行器 OPEN 产出（回读 fills 后）：

```json
{
  "symbol": "BTC-USDT-SWAP",
  "action": "open",
  "side": "long",
  "sz": 1,
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
  "ct_val": 0.01,
  "ordId": "<ordId>"
}
```

> 补键释义（2026-07-17 补齐至现行回执）：`sl_mode`=`'attached'`（随开仓附挂）/`'algo'`（独立 algo SL belt）/`'none'`（S2d 如实标注）；`sl_verified`=SL 挂单回读确认真挂上（2026-07-07 #5，dryrun 跳过）；`fill_source`=成交确认来源（`'fills'` 主源，回退订单状态/orders-history 双源，`'approx_agg'` 兜底标记，CLOSE 另有 `'unconfirmed'`）；`ct_val`=本环境真实合约面值（2026-07-07 起回执携带，writer 补算优先行内值——demo 分列合约与 market.db 缓存的 live 口径可差 100x）；`ordId`=成交订单号（合并闸/journal 重放按此精确匹配）。示例数字已按 `notional = sz*ctVal*fill_px` 自洽（1×0.01×62500=625）。

CLOSE 产出：`action="close"`、`pnl`=回读 fills 真实 pnl（拉不到为 null）、`reduce_only_fallback`、无 `lev/margin/notional`。

| trades_writer 落库列（`trades` 表） | 取自 trade 字段 | 说明 |
|---|---|---|
| `symbol` | `symbol` | 必填，writer 校验非空 |
| `action` | `action` | `'open'`/`'close'`/`'add'`/`'reduce'`/`'none'`。**`none` 行 writer 跳过不落** |
| `side` | `side` | long/short |
| `sz` | `sz` | 张数（risk_validator 已按 lot_sz 取整的 approved_sz） |
| `fill_px` | `fill_px` | 回读 fills 真实成交价（拉不到回退 mark_px） |
| `lev` | `lev` | 杠杆 |
| `margin` | `margin` | 每仓保证金 = notional / lev |
| `notional` | `notional` | 名义 = approved_sz * ctVal * fill_px |
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
  "mode": "full",
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

| `trade_cycles` 落库列 | 取自 cycle 字段 | 说明 |
|---|---|---|
| `cycle_id` | `cycle_id` | 必填（writer 校验），UTC+8 槽位 |
| `ts` | `ts`（缺则 now UTC+8） | 完成时刻 |
| `mode` | `mode`（固定 full） | full |
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

```powershell
# 先用 write 文件工具直接写 <PROJECT_ROOT>/tmp/_receipt_live_YYYY-MM-DDTHH-MM.json
# （文件名用 HH-MM；raw cycle 的 HH:MM 会在 NTFS 上变成 ADS，禁止）
pwsh -NoProfile -File <PROJECT_ROOT>\scripts\run_okx_python.ps1 <PROJECT_ROOT>\collectors\trades_writer.py --json-file <PROJECT_ROOT>\tmp\_receipt_live_YYYY-MM-DDTHH-MM.json --cycle-id YYYY-MM-DDTHH:MM --profile live
# demo 同形，文件名/profile 换 demo
# 执行器干跑（不真下单）：先 OKX_EXECUTOR_DRYRUN=1
```

| 校验项 | 由谁 | 失败行为 |
|---|---|---|
| 风控硬闸（live 越不过） | `core/risk_validator.validate`（执行器内部强制调） | reject -> 执行器回执 `ok=false action_taken=REJECT`，**不下单** |
| SL 保障（live 必带且必挂上） | `core/order_executor.open_position` | 无 SL reject；挂单全败 -> 市价平裸仓 UNWIND + p0 |
| 现仓真伪 | OKX API（`fetch_open_positions`） | 禁 position_snapshots GROUP BY（红线 #6） |
| 成交真伪 | 回读 fills（`_read_fills`，重试 3） | 拉不到 -> repair_queue + reject + p0 |
| 回执 schema（必填 `cycle_id`；`decision` 可归一；`trades` 是 list 且每元素含 `symbol`） | `trades_writer.validate` | 错误列表 -> exit 1，**不写库** |
| 落地核对 | 读 `*_trades.db` trade_cycles/trades + `ledger.py show` | 账本核对真落地 |

成功输出：`{"ok": true, "cycle_id": "...", "n_orders": N}`（exit 0）。
