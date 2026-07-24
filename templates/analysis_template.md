<!--
doc: analysis_template
role: 分析回执模板（analyst -> analysis.db）
权威: skill.md §8 + collectors/analyst_writer.py
落点: <PROJECT_ROOT>\db\analysis.db（analysis_runs + analysis_signals）
writer: <PROJECT_ROOT>\collectors\analyst_writer.py（唯一通道，禁手写 INSERT）
-->

> ⚠️ **2026-07-17 一致性审计校正**：本模板曾冻结在 ~2026-06-24 契约，以下已按现行实现修正；与 skill.md / 对应 writer·core 代码冲突时以后者为准。

# 分析回执模板 — analyst -> analysis.db

> analyst 产出**固定结构 JSON 回执**，写成 UTF-8 文件后经 `--input-file` 喂给 `<PROJECT_ROOT>\collectors\analyst_writer.py`（禁 echo|管道 --stdin 传中文，GBK 坏码）；
> writer 校验通过后写入 `analysis.db` 两张表（`analysis_runs` 每轮一行 + `analysis_signals` 每币一行）。
> 红线：写库必走 writer，analyst **严禁**手写 INSERT。模型分配只在 `openclaw config agents.list.*.model`，本文件零模型名。

## 1. 回执 JSON 顶层 schema

```json
{
  "cycle_id": "2026-06-24T14:00",
  "ts": "2026-06-24 14:05:30",
  "mode": "full",
  "decision_protocol": "decision_card_v1",
  "regime": "risk_on",
  "regime_stale": 0,
  "market_summary": { "...见 §2 五段..." },
  "missing_sources": null,
  "signals": [ "...见 §3 每币..." ],
  "raw": "{...完整原始报告 JSON...}",
  "status": "ok"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `cycle_id` | str | 是 | UTC+8 槽位 `'YYYY-MM-DDTHH:MM'`（`:00/:15/:30/:45`）。来自派单卡，禁自造。 |
| `ts` | str | 是 | 完成时刻 UTC+8 `'YYYY-MM-DD HH:MM:SS'`。writer 的 `normalize_ts` 会把 ISO8601/带时区统一成此格式，但回执应直接给纯字符串（红线 #2，禁裸 UTC-Z）。 |
| `mode` | str | 是 | 固定为 `'full'`，以 dispatcher 触发消息为准。writer 强校验其它值拒。 |
| `decision_protocol` | str | 是（正常轮） | 固定 `decision_card_v1`；缺失仅用于切换前旧回执兼容。 |
| `regime` | str | - | `'risk_on'`/`'risk_off'`/`'range'`/… 慢采 regime 判定。`status=skipped/stale` 时可空。 |
| `regime_stale` | int | - | `0`=新鲜；`1`=carry-forward 上一轮（regime 缺时禁伪装 range，必标 1）。默认 0。 |
| `market_summary` | dict 或 null | - | 五段结构化报告（§2）。必须是 dict 或 null，writer 拒非 dict。 |
| `missing_sources` | list 或 null | - | 缺源 id 列表（如 `["x_search","fred_dxy"]`）或 null/`[]`（等价，无缺源）。缺源源自 registry `freshness_report` / 采集账本，只作证据披露，由 Agent 在六项决策卡中自主判断影响。 |
| `signals` | list | - | 每币一行（§3）。`[]` 合法（无机会给全 hold）；缺省视为空。 |
| `raw` | any | - | 完整原始报告 JSON（留痕），writer JSON 序列化存 `analysis_runs.raw`。 |
| `status` | str | - | `'ok'`（默认）/`'skipped'`/`'stale'`/`'error'`。gate 失败路径（skipped/stale）允许 regime/signals 空。 |

## 2. market_summary 五段（macro / news / tech / sentiment / quant）

`market_summary` 是 dict，建议含以下五段。writer 仅校验其为 dict（不逐段强校验），五段是**约定结构**，便于 push/复盘/经验检索消费——缺段不报错但削弱下游可读性，应尽量给全。

```json
{
  "macro": {
    "dxy": 104.2,
    "fred_flags": ["fred_dxy d1 缺 -> 权重=0"],
    "summary": "美元指数走弱，风险资产承接"
  },
  "news": {
    "top_events": [
      {"symbol": "BTC-USDT-SWAP", "event_time": "2026-06-24 13:40:00", "severity": 3, "headline": "..."}
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
| `news` | news.db.news_items（news_writer 落库） | `event_time`=源给的（缺则 NULL，禁回退成当前时刻）；`ingested_at`=落库时刻；二者分离。`severity`/`tags` 见 news edge schema。 |
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
    "risk_reward": {"entry": 62000, "target": 64500, "stop": 60800, "rr": 2.08},
    "portfolio_impact": {"before": "已有两笔多仓", "after": "提高多头相关暴露", "cash": "可用USDT充足"},
    "historical_experience": {
      "matched_wins": [],
      "matched_losses": [],
      "missed_opportunities": [],
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
| `action` | str | 是 | `'open_long'`/`'open_short'`/`'hold'`/`'close'`/`'wait'`。trader 据此决策。 |
| `side` | str 或 null | - | `'long'`/`'short'`/null（hold/wait 给 null）。 |
| `entry_hint` | num 或 null | - | 建议入场价（trader 参考，非硬约束）。 |
| `stop_hint` | num 或 null | - | 建议止损价。**live trader 开仓必带 SL**（order_executor 无 SL 直接 reject）；此 hint 供 trader 算 `sl_trigger_px`。 |
| `tp_hint` | num 或 null | - | 建议止盈价。 |
| `reasoning` | str 或 null | - | 人读决策依据（push「决策依据」段与复盘消费）。 |
| `decision_card` | dict | 是 | 六项卡 + 历史经验取舍 + Agent 最终裁决。writer 只校验完整性，不把内容变成交易闸。 |
| `raw` | str/dict 或 null | - | 原始证据与来源留痕。 |

> 对拟执行标的调用 `find_similar_experience.py`，把盈利、亏损、错失机会及 `usage` 取舍写入 `historical_experience`。统计只供参考。

## 4. 写入路径与表落点

- writer 对 `analysis_runs`：已有 `status='ok'` 行**拒绝覆盖**（`already_exists` 闩锁，race-safe；返回 exit 0 幂等语义但 `ok:false`）；仅失败行（`skipped`/`stale`/`error`）可重写。写入本身用 INSERT OR REPLACE + 事务保证原子性。
- `analysis_signals`：**先 DELETE 本 cycle 旧行再插**，`signals=[]` 时只删不插（重跑安全）。
- `market_summary`/`missing_sources`/`raw` 由 writer `json.dumps(ensure_ascii=False)` 序列化存 TEXT。

```bash
# 调用（经 wrapper）：先把回执写成 UTF-8 文件（bash heredoc UTF-8-native；或用文件写入能力写 UTF-8），
# 再 --input-file 喂 writer——禁 `echo '<中文JSON>' | pwsh … --stdin`（PowerShell 下 echo 按 GBK 出字节
# 即坏成 U+FFFD，2026-07-09 简报乱码根因；writer 坏码哨兵会拒写）
cat > <PROJECT_ROOT>/tmp/analyst_receipt.json <<'RECEIPT_EOF'
<回执JSON>
RECEIPT_EOF
pwsh -NoProfile -File <PROJECT_ROOT>/scripts/run_okx_python.ps1 <PROJECT_ROOT>/collectors/analyst_writer.py --input-file <PROJECT_ROOT>/tmp/analyst_receipt.json
```

## 5. 校验

| 校验项 | 由谁 | 失败行为 |
|---|---|---|
| schema 校验（必填 `cycle_id`/`ts`/`mode`；`mode=full`；`market_summary` 是 dict/null；`signals` 是 list/null；每 signal 含 `symbol`+`action`） | `analyst_writer.validate_receipt` | 返回错误列表 -> stdout `{"ok":false,"error":"..."}` + exit 1，**不写库** |
| 坏码哨兵（输入解码后含 ≥3 个 U+FFFD `�` 替换符即判编码坏码，--input-file/--stdin 两路均设） | `analyst_writer`（2026-07-09） | stdout `{"ok":false,"error":"...编码坏码..."}` + exit 1，**不写库**——改用 UTF-8 文件（--input-file）重试 |
| 只验不写预检 `--validate-only`（rank8 2026-07-16，复用同套硬校验+坏码哨兵；禁自写 `_preflight_*.py`） | `analyst_writer --validate-only` | 通过输出 `{"ok":true,"validate_only":true}` exit 0；失败同 schema 校验，**不写库** |
| `ts` 归一化（ISO8601/带时区 -> UTC+8 纯字符串） | `analyst_writer.normalize_ts` | 解析失败原样返回（不致命）；回执应直接给纯字符串规避 |
| 落地核对 | `<PROJECT_ROOT>\collectors\ledger.py show --cycle <cycle_id>` + 读 analysis.db | 账本/库行核对真落地，不靠时间戳猜 |
| schema 权威 | `<PROJECT_ROOT>\db\schema.sql`（自动生成，禁手编） | 改 schema 走幂等迁移 + `export_schema.py` 重生成 |

成功输出：`{"ok": true, "cycle_id": "...", "signals_written": N}`（exit 0）。
