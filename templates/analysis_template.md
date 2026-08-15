<!--
doc: analysis_template
doc-version: V2.0-template
last-updated: 2026-08-14
updated-by: Codex
change-summary: 说明 executor 的 writer 已验证三周期锚点与同槽后续行情修订审计语义。
role: 分析回执模板（analyst -> analysis.db）
权威: skill.md §8 + collectors/analyst_writer.py
落点: <PROJECT_ROOT>\db\analysis.db（analysis_runs + analysis_signals）
writer: <PROJECT_ROOT>\collectors\analyst_writer.py（唯一通道，禁手写 INSERT）
-->

> ⚠️ **2026-07-29 一致性审计校正**：本模板与 `analyst_writer.validate_receipt` 及推送消费者字段同步；与 skill.md / 对应 writer·core 代码冲突时以后者为准。

# 分析回执模板 — analyst -> analysis.db

> analyst 产出**固定结构 JSON 回执**，写成 UTF-8 文件后经 `--input-file` 喂给 `<PROJECT_ROOT>\collectors\analyst_writer.py`（禁 echo|管道 --stdin 传中文，GBK 坏码）；
> writer 校验通过后写入 `analysis.db` 两张表（`analysis_runs` 每轮一行 + `analysis_signals` 每币一行）。
> 红线：写库必走 writer，analyst **严禁**手写 INSERT。模型分配只在 `openclaw config agents.list.*.model`，本文件零模型名。
> 回执必须一次性整文件写完，禁止用 edit/局部补丁循环拼 JSON。先以同一文件运行 `analyst_writer.py --validate-only --input-file ...`，通过后立即正式写入；writer 按 cycle 确定性记录失败预算，失败最多整文件重写一次，第二次仍失败即锁死本轮；正式写入只接受与 validate-only 通过时 SHA-256 完全相同的文件，不得因修 JSON 耗尽输出后跳过 writer。

## 1. 回执 JSON 顶层 schema

```json
{
  "cycle_id": "2026-06-24T14:00",
  "ts": "2026-06-24 14:05:30",
  "mode": "full",
  "decision_protocol": "decision_card_v1",
  "regime": "risk_on",
  "regime_stale": 0,
  "market_summary": "<对象：见 §2 五段>",
  "missing_sources": null,
  "signals": [ "...见 §3 每币..." ],
  "raw": "{...完整原始报告 JSON...}",
  "status": "ok"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `cycle_id` | str | 是 | UTC+8 槽位 `'YYYY-MM-DDTHH:MM'`（`:00/:15/:30/:45`）。来自派单卡，禁自造。 |
| `ts` | str | 是 | Agent 报告时刻 UTC+8 `'YYYY-MM-DD HH:MM:SS'`，仅作为 raw 中的 `reported_ts` 留痕；`analysis_runs.ts` 由 writer 使用实际 CST 提交时间，调用方不得控制。 |
| `mode` | str | 是 | 固定为 `'full'`。dispatcher 的 `dispatch_mode=unified` 仅表示“分析+实盘由同一 Agent 承担”，不是回执 mode；无论统一路由还是人工回滚，writer 都强校验其它值拒。 |
| `decision_protocol` | str | 是 | 固定 `decision_card_v1`；当前 writer 不再接受缺失或未知协议。 |
| `regime` | str | - | `'risk_on'`/`'risk_off'`/`'range'`/… 慢采 regime 判定。`status=skipped/stale` 时可空。 |
| `regime_stale` | int | - | `0`=新鲜；`1`=carry-forward 上一轮（regime 缺时禁伪装 range，必标 1）。默认 0。 |
| `market_summary` | dict 或 null | 正常轮是 | 五段结构化报告（§2）。`status=ok` 时必须为 dict，且 `macro/news/tech/sentiment/quant` 五段均存在并各自为 dict；仅 skipped/stale/error 可为 null。 |
| `missing_sources` | list 或 null | - | 缺源 id 列表（如 `["x_search","fred_dxy"]`）或 null/`[]`（等价，无缺源）。缺源源自 registry `freshness_report` / 采集账本，只作证据披露，由 Agent 在六项决策卡中自主判断影响。**标签用小写 snake_case**（如 `dxy_zone_stale_carry_forward`，不要写成 `carryforward`）——同义标签两种拼写会让按 key 聚合的统计分叉；writer 侧 `analyst_writer.MISSING_SOURCE_ALIASES` 会把已知别名归一，但源头写对更好。**报什么缺源仍由 Agent 自主判断，这里只统一写法。** |
| `signals` | list | `status=ok` 时是 | 每币一行（§3）。`[]` 合法；非 ok 状态只允许 null/`[]`，当前 writer 不再替正常轮补默认值。 |
| `raw` | any | - | 完整原始报告 JSON（留痕），writer JSON 序列化存 `analysis_runs.raw`。 |
| `status` | str | 是 | `'ok'`/`'skipped'`/`'stale'`/`'error'`；无默认值。gate 失败路径（skipped/stale）允许 regime/signals 空。 |

## 2. market_summary 五段（macro / news / tech / sentiment / quant）

`market_summary` 是 dict。`status=ok` 时 writer **强制校验**以下五段全部存在且均为 dict；缺任一段或段类型错误都会拒写。skipped/stale/error 降级回执才允许置 null。

```json
{
  "macro": {
    "dxy": 104.2,
    "fred_flags": ["fred_dxy d1 缺 -> 权重=0"],
    "summary": "美元指数走弱，风险资产承接"
  },
  "news": {
    "events": [
      {"src": "news_rss", "headline": "...", "severity": 3, "symbols": ["BTC-USDT-SWAP"], "event_time": "2026-06-24 13:40:00"}
    ],
    "sentiment_note": "BTC 情绪偏多，无重大利空",
    "stale": false
  },
  "tech": {
    "BTC-USDT-SWAP": {"trend": "up", "key_level": 62000, "rsi": 58},
    "ETH-USDT-SWAP": {"trend": "range", "key_level": 3400}
  },
  "sentiment": {
    "funding": {"BTC-USDT-SWAP": 0.012},
    "oi_change": {"BTC-USDT-SWAP": "+3.2%"},
    "note": "资金费率温和，无逼仓信号"
  },
  "quant": {
    "vol_regime": "mid",
    "btc_mcap_chg_24h_usd": 1.2e9,
    "note": "波动率中性，趋势延续概率高"
  }
}
```

| 段 | 数据来源（只读库） | 要点 |
|---|---|---|
| `macro` | regime.db.cross_market（DXY 等）；FRED 降级源标 `权重=0` | 降级源不 abort，标 flag。注意 `cross_market.btc_etf_flow` 实为 BTC 24h 市值变化 USD（生成列 `btc_mcap_chg_24h_usd`），**不是** ETF 净流入。 |
| `news` | news.db.news_items（news_writer 落库） | 催化新鲜度只看 `event_occurred_at`；`published_at`=媒体发布时间，`first_seen_at`=系统观察首见，均不得冒充事件发生时间。事件日未知不得写 fresh；一级源看 `source_grade/primary_source_url`。 |
| `tech` | market.db（kline/tick/derivatives） | 每币技术结构 |
| `sentiment` | market.db.derivatives（funding/OI） | 资金费率/持仓量异动 |
| `quant` | regime.db.cross_market + market.db | 波动率/市值口径 |

> regime 缺 -> carry-forward 上一轮 + `regime_stale=1`，**禁伪装 range**（§10 降级）。

## 3. analysis_signals 每币字段

`signals[]` 每元素一币，写入 `analysis_signals`。writer 强校验：每元素是 dict，必含 `symbol` 且非空、必含 `action`。

```json
{
  "symbol": "BTC-USDT-SWAP",
  "action": "open_long",
  "side": "long",
  "entry_hint": 62000,
  "stop_hint": 60800,
  "tp_hint": 64500,
  "reasoning": "多周期趋势延续，执行条件满足；反对证据和失效点见决策卡",
  "decision_card": {
    "direction_evidence": ["4H/1D 趋势向上", "OI 与量能同步增加", "新闻催化仍有效"],
    "opposing_evidence": ["短周期接近局部阻力", "资金费率偏高"],
    "execution_conditions": {"liquidity": "可执行", "entry": "回踩 62000 附近承接"},
    "invalidation_point": {"condition": "4H 收盘跌破结构低点", "stop_price": 60800},
    "risk_reward": {"entry": 62000, "target": 64500, "stop": 60800, "rr": 2.08, "exit_mode": "dynamic_exit", "ev_override": null},
    "portfolio_impact": {"before": "已有两笔多仓", "after": "提高多头相关暴露", "cash": "可用USDT充足"},
    "multitimeframe_analysis": {
      "cycle_id": "2026-06-24T14:00",
      "required_timeframes": ["15m", "1H", "4H"],
      "timeframes": {
        "15m": {"direction": "long", "evidence": ["读取工具契约中的 exact 已收盘 15m 数值后形成的证据"], "relative_rank": 2},
        "1H": {"direction": "neutral", "evidence": ["读取工具契约中的 exact 已收盘 1H 数值后形成的反对证据"], "relative_rank": 3},
        "4H": {"direction": "long", "evidence": ["读取工具契约中的 exact 已收盘 4H 数值后形成的主证据"], "relative_rank": 1}
      },
      "selected_timeframe": "4H",
      "selected_direction": "long",
      "selection_reason": "4H 在本轮三周期相对排序第一，且方向与开仓一致",
      "selection_method": "relative_rank_1_among_15m_1H_4H_not_calibrated",
      "calibrated_confidence": null,
      "confidence_claim_allowed": false,
      "evidence_contract": {"protocol": "multitimeframe_market_evidence_v1", "完整对象": "必须用 multitimeframe_decision_evidence.py 输出原样替换本占位对象"}
    },
    "historical_experience": {
      "matched_wins": [],
      "matched_losses": [],
      "missed_opportunities": [],
      "evidence_contract": {"protocol": "experience_evidence_v2", "query": {}, "summaries": {}, "samples_truncated": true, "evidence_hash": "<原样复制工具输出>"},
      "usage": "partial",
      "reason": "历史样本方向一致，但本轮催化更强"
    },
    "agent_judgement": "开多；仓位由风险收益和组合影响自主确定",
    "reference_overrides": []
  },
  "dim1": null, "dim2": null, "dim3": null, "dim4": null, "dim5": null,
  "total": null, "confidence": null,
  "raw": {"source_notes": "..."}
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `symbol` | str | 是 | `'BTC-USDT-SWAP'` 等 OKX SWAP instId |
| `dim1`..`dim5` / `total` / `confidence` | null | - | 旧协议兼容列。`decision_card_v1` 一律填 null；不用于排序、仓位或执行。 |
| `action` | str | 是 | 仅允许 `'open_long'`/`'open_short'`/`'hold'`/`'close'`/`'reduce'`/`'adjust_protection'`/`'wait'`。未知动作必须由 writer 拒绝，trader 不得猜测。 |
| `side` | str 或 null | - | `open_long` 必须 `long`，`open_short` 必须 `short`；`hold` 必须 null；**`wait` 可选 `long`/`short`/null**；`close/reduce/adjust_protection` 必须明确现仓方向 `long` 或 `short`。 |
| `entry_hint` | num 或 null | - | 建议入场价（trader 参考，非硬约束）。 |
| `stop_hint` | num 或 null | - | 建议止损价/技术失效点。分析阶段尚未生成 `live_decision_facts`，不得把本字段或 `invalidation_point.stop_price` 写成“当前交易所 live SL”。live trader 开仓必带方向正确的 SL：long 严格低于 mark、short 严格高于 mark，且偏离不超过 30%；最终实际保护单只认交易阶段 facts 与确定性风控回读。 |
| `tp_hint` | num 或 null | - | 建议/参考目标价。只有 `risk_reward.exit_mode=fixed_tp` 时交易阶段把 target 附挂为交易所 TP；`dynamic_exit|no_fixed_tp` 时 target 只用于 EV 与复盘。 |
| `reasoning` | str 或 null | - | 人读决策依据（push「决策依据」段与复盘消费）。 |
| `decision_card` | dict | 是 | 六项卡 + 历史经验取舍 + Agent 最终裁决；上述字段必须直接嵌在本 dict，禁止放到 signal 顶层，也禁止改名为 `rationale/final_judgement/overrides`，所有动作（含 HOLD/WAIT/REDUCE/ADJUST_PROTECTION）同样完整填写。`open_*` 另须完整 `multitimeframe_analysis`：只认固定 cycle 下 exact 已收盘 15m/1H/4H、至少 34 根历史、完整指标及工具原样 evidence_contract；三周期逐一给证据和唯一 rank 1/2/3，选择 rank=1 且方向匹配。relative rank 不是校准概率；独立 90% 门未过期间 `calibrated_confidence=null`、`confidence_claim_allowed=false`。**Wave1 序5（2026-08-10）对 `open_*` 卡新增算术契约**：`risk_reward` 必含数值 `entry/stop/target`（方向几何必须合法：long `stop<entry<target`、short `target<entry<stop`）、显式 `exit_mode=fixed_tp|dynamic_exit|no_fixed_tp`；`rr` 字段若填必须与几何重算一致（±0.05）；writer 按 evidence_contract 首个 n≥5 具名 scope 的 wins/n 算 `ev_r`（净口径含 0.2% 摩擦），**ev_r<0 时必须带 `risk_reward.ev_override={reason, p_win_claim}`**——负 EV 不禁开，但要显式承认基线并给修正胜率；样本不足=indeterminate 无 EV 要求。canonical `ev_check` 块由 writer 注入落库卡，模型手写同名块会被覆盖，禁止引用自算 EV。 |
| `raw` | 任意 JSON 值或 null | - | 调用方原始证据；writer 会统一封装成对象 JSON（含 `schema_version/source/input_kind/payload/canonical_signal`）写入 `analysis_signals.raw`，完整原回执仍保存在 `analysis_runs.raw`。 |

> 先确定 entry/stop/target，再对拟执行标的调用 `find_similar_experience.py --symbol <完整instId> --side <long|short> --regime <本轮regime> --action open --profile live --as-of <固定cycle> --entry <entry> --stop <stop> --target <target> --compact --out-file <PROJECT_ROOT>/tmp/findsim_<cycle>_<symbol>.json`；禁止自行换算百分比或 RR。读取 UTF-8 文件，把 `evidence_contract` 原样写入 `historical_experience`；工具与 writer 从同一组三价经共享函数生成 `query.setup`，并与 `query.instrument_context` 一起冻结。历史数字只允许 writer 注入 `historical_experience.scope_counts`，模型的 reason、证据和最终判断禁止手写 n/W/L/WR/胜率；样例数组有截断，禁止数数组或混栏。禁止管道、重定向或内联解析器。统计只供参考。

> 对每个拟执行标的还须运行 `multitimeframe_decision_evidence.py --db-root <PROJECT_ROOT>/db --symbol <完整instId> --cycle-id <固定cycle> --out-file <PROJECT_ROOT>/tmp/mtf_<cycle>_<symbol>.json`。仅 `ok=true` 才能形成 open 卡；把文件中的完整 `evidence_contract` 对象原样替换上例占位对象。writer 校验 hash/身份/结构。executor 在任何交易所账户或订单 I/O 前按同 cycle 独立重读：当前三周期必须仍 ready；完全一致走 `current_market_exact`。若同槽后续采集修订已收盘数据，只接受与同 cycle/symbol/side 的 `analysis.db` writer 已验证契约逐字段相同的 `analysis_db_writer_validated` 锚点，并留 `post_analysis_market_revision` 与 supplied/current/persisted hash；否则 fail-closed。不得拿较旧 K 线补 exact 缺口，持久化锚点也不得掩盖当前 readiness 缺失。

## 4. 写入路径与表落点

- writer 对 `analysis_runs`：已有 `status='ok'` 行**拒绝覆盖**（`already_exists` 闩锁，race-safe；返回 exit 0 幂等语义但 `ok:false`）；仅失败行（`skipped`/`stale`/`error`）可重写。写入本身用 INSERT OR REPLACE + 事务保证原子性。
- `analysis_signals`：**先 DELETE 本 cycle 旧行再插**，`signals=[]` 时只删不插（重跑安全）。
- `market_summary`/`missing_sources`/`raw` 由 writer `json.dumps(ensure_ascii=False)` 序列化存 TEXT。

```powershell
# 先用 Agent 文件写入能力把完整回执直接保存为 UTF-8：
# <PROJECT_ROOT>/tmp/analyst_receipt.json
# 再经项目 wrapper 喂 writer。禁止 echo/管道/here-string/重定向拼中文 JSON。
pwsh -NoProfile -File <PROJECT_ROOT>/scripts/run_okx_python.ps1 <PROJECT_ROOT>/collectors/analyst_writer.py --input-file <PROJECT_ROOT>/tmp/analyst_receipt.json
```

## 5. 校验

| 校验项 | 由谁 | 失败行为 |
|---|---|---|
| schema 校验（必填 `cycle_id`/`ts`/`mode`；`mode=full`；正常轮 `market_summary` 五段全部存在且为 dict；`signals` 是 list/null；每 signal 的 `action/side` 必须符合上表组合） | `analyst_writer.validate_receipt` | 返回错误列表 -> stdout `{"ok":false,"error":"..."}` + exit 1，**不写库** |
| 坏码哨兵（输入解码后含 ≥3 个 U+FFFD `�` 替换符即判编码坏码，--input-file/--stdin 两路均设） | `analyst_writer`（2026-07-09） | stdout `{"ok":false,"error":"...编码坏码..."}` + exit 1，**不写库**——改用 UTF-8 文件（--input-file）重试 |
| 只验不写预检 `--validate-only`（rank8 2026-07-16，复用同套硬校验+坏码哨兵；禁自写 `_preflight_*.py`） | `analyst_writer --validate-only` | 通过输出 `{"ok":true,"validate_only":true}` exit 0；失败同 schema 校验，**不写库** |
| `ts` 归一化（ISO8601/带时区 -> UTC+8 纯字符串） | `analyst_writer.normalize_ts` | 解析失败原样返回（不致命）；回执应直接给纯字符串规避 |
| 落地核对 | `<PROJECT_ROOT>\collectors\ledger.py show --cycle <cycle_id>` + 读 analysis.db | 账本/库行核对真落地，不靠时间戳猜 |
| schema 权威 | `<PROJECT_ROOT>\db\schema.sql`（自动生成，禁手编） | 改 schema 走幂等迁移 + `export_schema.py` 重生成 |

成功输出：`{"ok": true, "cycle_id": "...", "signals_written": N}`（exit 0）。
