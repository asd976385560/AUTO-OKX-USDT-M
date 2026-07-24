<!--
doc: daily_template
role: 日/周/月复盘模板（reviewer / okx-reviewer -> account.db + reports/daily-reports/）
权威: skill.md（复盘/推送相关节）+ scripts/daily_report_writer.py
落点: account.db（daily_reports / weekly_reports / monthly_reports）+ reports/daily-reports/daily-YYYY-MM-DD.md
writer: <PROJECT_ROOT>\scripts\daily_report_writer.py（唯一通道，禁手写 INSERT；默认 dry-run，--apply 才真写）
推送: 复盘推 QQ 731765529（**不是** 729624934）
-->

> ⚠️ **2026-07-17 一致性审计校正**：本模板曾冻结在 ~2026-06-24 契约，以下已按现行实现修正；与 skill.md / 对应 writer·core 代码冲突时以后者为准。

# 复盘模板 — 日 / 周 / 月 -> account.db

> reviewer（okx-reviewer）聚合账户绩效 + 信号/playbook 绩效 + demo vs live 对照，装配回执 JSON 喂 `<PROJECT_ROOT>\scripts\daily_report_writer.py` 落 `account.db`，并落盘 `reports/daily-reports/daily-YYYY-MM-DD.md` 全文；复盘正文仅发送到运行环境配置的 `OKX_QQ_TARGET`。
> 红线：写库必走 writer，禁手写 INSERT；时间 UTC+8 字符串；现仓/绩效以 OKX API + 库账本为准；`MAX(ts)` 词典序坑（查最新用 `rowid DESC` / `datetime(ts)`）。零模型名。
> writer **默认 dry-run**，`--apply` 才真写；`--profiles both`（默认）一次 payload 同写 live/demo 双段，成功后勿再单独重复写 demo。

## 1. 日报回执 JSON（write_daily -> daily_reports）

writer 按 profile 优先读 `live_`/`demo_` 前缀字段（`pf()`），兼容旧无前缀字段。`--profiles both` 时一份 payload 同写双段：

> Markdown 资产段不依赖 reviewer 手填：`daily_report_writer` 按 payload `ts` 回读当时最新
> `account_snapshots`、精确批次 `position_snapshots`（禁 GROUP BY）和 `cum_pnl.py` 冻结基线口径。
> 因而漏传 `*_equity` / `*_realized_pnl` / `*_positions_summary` 不会再被默认为 0/空仓；
> 快照不可用时明确显示“持仓数据不可用”。

```json
{
  "ts": "2026-06-24 08:00:00",
  "summary": "当日 live 2 开 1 平，净 +12.4 USDT；demo 5 开 3 平，净 +88 USDT",
  "lessons": "BTC 趋势单守住，ETH 区间假突破止损 1 次",
  "raw": "{...完整原始复盘 JSON...}",

  "live_open_count": 2,  "live_close_count": 1,
  "live_total_pnl": 12.4, "live_total_fees": 1.2,
  "live_best_trade": "BTC long +9.1", "live_worst_trade": "ETH long -3.0",

  "demo_open_count": 5,  "demo_close_count": 3,
  "demo_total_pnl": 88.0, "demo_total_fees": 4.5,
  "demo_best_trade": "SOL long +40", "demo_worst_trade": "ETH short -12"
}
```

| `daily_reports` 落库列 | 字段（前缀按 profile） | 说明 |
|---|---|---|
| `ts` | `ts`（缺则 now UTC+8） | 复盘时刻 `'YYYY-MM-DD HH:MM:SS'` |
| `profile` | （写双段时各自 `live`/`demo`） | — |
| `open_count` / `close_count` | `<pf>_open_count` / `<pf>_close_count` | 当日开/平笔数 |
| `total_pnl` | `<pf>_total_pnl` | 当日净 realized pnl |
| `total_fees` | `<pf>_total_fees` | 当日手续费 |
| `best_trade` / `worst_trade` | `<pf>_best_trade` / `<pf>_worst_trade` | 最佳/最差笔（人读） |
| `summary` / `lessons` | `summary` / `lessons`（双段共享） | 当日小结 / 教训 |
| `raw` | `raw` | 原始复盘 JSON 留痕 |
| `trade_day_num` | writer 自动 | `next_trade_day_num`：同一天 live/demo **共享编号**；否则 `MAX+1`（禁跳号/回滚，事务内）。**禁**自己按 `MAX(ts)` 算（词典序坑） |

## 2. 周报 / 月报回执（write_weekly / 同形月报）

周报必填 `week_start_ts`（本周一 `'YYYY-MM-DD HH:MM:SS'` UTC+8）；PK = `week_start_ts + profile`，**重复即报错不覆盖**：

```json
{
  "week_start_ts": "2026-06-22 00:00:00",
  "summary": "...", "lessons": "...", "raw": "...",
  "live_open_count": 9, "live_close_count": 7, "live_total_pnl": 41.2,
  "live_win_rate": 0.57, "live_avg_hold_hours": 6.3,
  "live_margin_util_pct": 0.12, "live_idle_ratio": 0.4,
  "demo_open_count": 28, "demo_close_count": 22, "demo_total_pnl": 310.0,
  "demo_win_rate": 0.55, "demo_avg_hold_hours": 5.1
}
```

| `weekly_reports` 落库列 | 字段 | 说明 |
|---|---|---|
| `week_start_ts` | `week_start_ts` | 必填，PK 之一 |
| `open_count`/`close_count`/`total_pnl` | `<pf>_*` | 周聚合 |
| `win_rate` | `<pf>_win_rate` | 胜率 |
| `avg_hold_hours` | `<pf>_avg_hold_hours` | 平均持仓时长 |
| `margin_util_pct` | `<pf>_margin_util_pct` | 保证金利用率 |
| `idle_ratio` | `<pf>_idle_ratio` | 空仓占比 |
| `summary`/`lessons`/`raw` | 同名（双段共享） | — |
| `trade_week_num` | writer 自动 | `_shared_period_num`：同周期 live/demo 共享，否则 `MAX+1`（禁跳号/回滚） |

> 月报同形（按月聚合，PK 含月起始 ts + profile，同样禁覆盖 / 禁跳号）。

## 3. 复盘正文（推 731765529 + 落盘 .md）

复盘正文（落盘 `reports/daily-reports/daily-YYYY-MM-DD.md` + 推 QQ 731765529）建议结构：

```
# 复盘 YYYY-MM-DD（第N交易日）

## 账户绩效
🟢 实盘：equity $X | 当日净 X USDT | 开N/平M | 手续费 X
🟡 模拟盘：equity $X | 当日净 X USDT | 开N/平M | 手续费 X
（equity 取 OKX API / account_snapshots，查最新按 rowid DESC / datetime(ts)，禁 MAX(ts) 词典序）

## 信号 / playbook 绩效
- per-信号：各 analysis_signals action 命中率 / 平均收益（按 trade_experiences 关联）
- playbook：各 playbook_ref 触发数 / 胜率 / avg_pnl（account.db.playbook）

## demo vs live 对照
- 同 regime 下双盘动作一致性 / 收益差 / demo 探索是否领先 live
- 经验库可信度（find_similar_experience credibility）随样本积累的变化

## 教训 / 改进
- error_patterns（lessons.db）本周期新增 / 复发
- missed_opportunities（lessons.db）

## 异常 / 数据降级
- 当期 stale 源 / 降级权重=0 的源（data_source_quality）
```

> 推 731765529 群的复盘一律经 `scripts/qq_push.py --content-file <UTF-8 文件> --dedupe-key reviewer:<YYYY-MM-DD>:<用途>`（禁 channels PUT 旧伪代码、禁直接用群号），发送前先 `validate_push_format.py` 自检；正文中文走 content 文件（**禁**进 cron message——cron 含中文 GBK 坏码）。长度下限由 `push_archive` rc=2 把关（`validate_push_format.py` 无 300B 阈值）。

## 4. 调用

```bash
# dry-run（默认，只 print 不写）
echo '<复盘JSON>' | pwsh -NoProfile -File <PROJECT_ROOT>\scripts\run_okx_python.ps1 <PROJECT_ROOT>\scripts\daily_report_writer.py --stdin
# 含中文/特殊符号建议先写文件再 --json-file（规避管道编码）
pwsh -NoProfile -File <PROJECT_ROOT>\scripts\run_okx_python.ps1 <PROJECT_ROOT>\scripts\daily_report_writer.py --json-file <PROJECT_ROOT>\tmp\review.json --apply --profiles both

# 仅修复/重渲染 Markdown（不重复 INSERT daily_reports；仍需显式 --apply）
pwsh -NoProfile -File <PROJECT_ROOT>\scripts\run_okx_python.ps1 <PROJECT_ROOT>\scripts\daily_report_writer.py --json-file <PROJECT_ROOT>\tmp\review.json --markdown-only --apply
```

## 5. 校验

| 校验项 | 由谁 | 失败行为 |
|---|---|---|
| 周期号禁跳号/回滚（同日/周 live/demo 共享，否则 MAX+1，事务内） | `daily_report_writer.next_trade_day_num` / `_shared_period_num` | — |
| 周/月报禁覆盖（PK 重复即报错） | writer `IntegrityError` 捕获 | `fail()` exit≠0（视为 P0） |
| read-after-write 校验（按 `last_insert_rowid` 回读） | writer | 回读不到 -> `fail()` exit≠0 |
| 输入 JSON 解析（含中文走 `--json-file`） | writer `load_payload` + `sanitize_text` | 解析失败 -> `fail()` |
| 默认 dry-run 保护 | writer（`--apply` 才真写） | 无 `--apply` 只 print，不动库 |
| 复盘推送格式 | `scripts/validate_push_format.py`（推 731765529 前自检；无 300B 阈值，长度下限由 push_archive rc=2 把关）；外发经 `scripts/qq_push.py --content-file <UTF-8 文件> --dedupe-key reviewer:<日期>:<用途>` | 不过不推 |

成功：writer 退出码 `0`（且 read-after-write 通过）；落盘 `reports/daily-reports/daily-YYYY-MM-DD.md`。退出码非 0 -> Agent 视为 P0。
