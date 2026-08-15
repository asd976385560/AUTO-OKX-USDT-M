<!--
doc: daily_template
doc-version: V2.1-template
last-updated: 2026-08-13
updated-by: Codex
change-summary: 规格书四段：writer 自动渲染市场总览/全市场扫描/数据完善率，新增 reviewer 必填 focus_next_day 次日关注（2026-08-14 激活边界起 validator 硬性要求，历史归档不反向加责）。
role: 日/周/月复盘模板（reviewer / okx-reviewer -> account.db + reports/daily-reports/ + reports/weekly/）
权威: skill.md（复盘/推送相关节）+ scripts/daily_report_writer.py + scripts/validate_daily_report.py
落点: account.db（daily_reports / weekly_reports / monthly_reports）+ reports/daily-reports/daily-YYYY-MM-DD.md + reports/weekly/weekly-YYYY-MM-DD.md
writer: <PROJECT_ROOT>\scripts\daily_report_writer.py（唯一通道，禁手写 INSERT；默认 dry-run，--apply 才真写）
推送: 经 qq_push.py 推统一默认 target，以 dedupe-key 区分 daily/weekly/monthly
-->

> ⚠️ **2026-07-29 一致性审计校正**：本模板已与当前统计、维护交接、对账和外发边界同步；与 skill.md / 对应 writer 代码冲突时以后者为准。

# 复盘模板 — 日 / 周 / 月 -> account.db

> reviewer（okx-reviewer）聚合账户绩效 + 信号/playbook 绩效，装配回执 JSON 喂 `<PROJECT_ROOT>\scripts\daily_report_writer.py` 落 `account.db`，并持续落盘日报/周报 UTF-8 Markdown；复盘正文经 `qq_push.py` 推统一默认 target。
> 红线：写库必走 writer，禁手写 INSERT；时间 UTC+8 字符串；现仓/绩效以 OKX API + 库账本为准；查最新行用 ts 词典序（`MAX(ts)` / `ORDER BY ts DESC`，前提列格式统一），禁 `rowid DESC`（主要表 `INSERT OR REPLACE` 会改 rowid）。零模型名。
> writer **默认 dry-run**，`--apply` 才真写。2026-08-06 demo 全量下线后只写 live 一段。
> 08:05 开场必须先运行 `reviewer_preflight.py --wait-seconds 1200`；只接受当日 ready 清单、三个关键维护步骤与质量文件 SHA-256 全部一致。非 0 时不生成、不写库、不外发；`report_mode=provisional` 必须贯穿报告。
> 日报成交、风控拒绝、周期 PnL 与 `account_bills` 统一使用固定24小时事实窗 `[前一日 08:00, 当日 08:00)`；累计 PnL、资金与持仓仍按报告 `ts` 回读。

## 1. 日报回执 JSON（write_daily -> daily_reports）

writer 按 profile 优先读 `live_` 前缀字段（`pf()`），兼容旧无前缀字段：

> Markdown 资产段不依赖 reviewer 手填：`daily_report_writer` 按 payload `ts` 回读当时最新
> `account_snapshots`、精确批次 `position_snapshots`（禁 GROUP BY）和 `cum_pnl.py` 冻结基线口径。
> 因而漏传 `*_equity` / `*_realized_pnl` / `*_positions_summary` 不会再被默认为 0/空仓；
> 快照不可用时明确显示“持仓数据不可用”。

```json
{
  "ts": "2026-06-24 08:00:00",
  "summary": "本复盘周期 live 2 开 1 平，净 +12.4 USDT",
  "lessons": "BTC 趋势单守住，ETH 区间假突破止损 1 次",
  "raw": "{...完整原始复盘 JSON...}",
  "live_reconcile_status": "clean",
  "live_reconcile_issue_count": 0,
  "live_risk_reject_count": 1,
  "order_ids": ["<允许随日报外发的订单标识>"],

  "live_open_count": 2,  "live_close_count": 1,
  "live_total_pnl": 12.4, "live_total_fees": 1.2,
  "live_best_trade": "BTC long +9.1", "live_worst_trade": "ETH long -3.0",

  "focus_next_day": "- BTC 4H MA20 得失与 96k 区间上沿\n- 周四 20:30 美国 CPI（importance=3）事件窗 ±4H 谨慎开新仓\n- SOL 资金费连续 3 轮极端偏多，警惕回摆"
}
```

> **`focus_next_day`（2026-08-13 新增，激活边界 2026-08-14 起 validator 硬性要求非空）**：
> 次日关注清单，reviewer 唯一判断型新增段——3~6 条，覆盖标的结构位、已排期高重要度
> 事件窗（macro_events/公告/解锁）、需警惕的仓位或数据风险。只写观察与关注点，
> **禁**写交易指令、禁伪造"可信度概率"（独立前向门未过时 confidence_claim_allowed=false）。

| `daily_reports` 落库列 | 字段（前缀按 profile） | 说明 |
|---|---|---|
| `ts` | `ts`（缺则 now UTC+8） | 复盘时刻 `'YYYY-MM-DD HH:MM:SS'` |
| `profile` | 固定 `live` | — |
| `open_count` / `close_count` | `<pf>_open_count` / `<pf>_close_count` | 固定24小时复盘周期的开/平笔数 |
| `total_pnl` | `<pf>_total_pnl` | 固定24小时复盘周期净 realized pnl |
| `total_fees` | `<pf>_total_fees` | 固定24小时复盘周期手续费 |
| `best_trade` / `worst_trade` | `<pf>_best_trade` / `<pf>_worst_trade` | 最佳/最差笔（人读） |
| `summary` / `lessons` | `summary` / `lessons`（双段共享） | 本复盘周期小结 / 教训 |
| `raw` | `raw` | 原始复盘 JSON 留痕 |
| `trade_day_num` | writer 自动 | `next_trade_day_num`：同一天**共享编号**；否则 `MAX+1`（禁跳号/回滚，事务内）。**禁**自己算编号——一律由 writer 在同一事务内续号（防跳号/并发错号） |

> `live_reconcile_status`、`live_reconcile_issue_count` 和 live `risk_reject_count`（2026-08-06 起纯实盘单段，无双盘）是报告状态/展示字段：风控拒绝必须与成交开仓分列。订单标识允许随日报外发用于逐笔对账；API 密钥、签名、会话令牌仍禁止进入报告。

## 2. 周报 / 月报回执（write_weekly / 同形月报）

周报必填报告键 `week_start_ts`（本周一 `'YYYY-MM-DD HH:MM:SS'` UTC+8，仍是 00:00 键）；统计事实窗口固定为**上周一 08:00（含）到本周一 08:00（不含）**——与日报同 08:00 相位，七份日报恰好平铺该窗，可互相对账。PK = `week_start_ts + profile`，重复即报错不覆盖：

```json
{
  "week_start_ts": "2026-06-22 00:00:00",
  "summary": "...", "lessons": "...", "raw": "...",
  "live_open_count": 9, "live_close_count": 7, "live_total_pnl": 41.2,
  "live_win_rate": 0.57, "live_avg_hold_hours": 6.3,
  "live_margin_util_pct": 0.12, "live_idle_ratio": 0.4,
}
```

| `weekly_reports` 落库列 | 字段 | 说明 |
|---|---|---|
| `week_start_ts` | `week_start_ts` | 必填，PK 之一 |
| `open_count`/`close_count`/`total_pnl` | `<pf>_*` | 周聚合 |
| `win_rate` | `<pf>_win_rate` | 胜率 |
| `avg_hold_hours` | `<pf>_avg_hold_hours` | 确认 close fill 经 FIFO 配对的已平仓平均持仓时长；配对不完整为 null |
| `margin_util_pct` | `<pf>_margin_util_pct` | 保证金利用率 |
| `idle_ratio` | `<pf>_idle_ratio` | 空仓占比 |
| `summary`/`lessons`/`raw` | 同名（双段共享） | — |
| `trade_week_num` | writer 自动 | `_shared_period_num`：同周期共享，否则 `MAX+1`（禁跳号/回滚） |

> 月报同形（按月聚合，PK 含月起始 ts + profile，同样禁覆盖 / 禁跳号）。
>
> 周报除写 `weekly_reports` 外，必须持续生成 `reports/weekly/weekly-<本周一日期>.md`；数据库已有周报行不等于可以省略 Markdown。

## 3. 复盘正文（统一 target + 落盘 .md）

复盘正文（日报落 `reports/daily-reports/daily-YYYY-MM-DD.md`，周报另落 `reports/weekly/weekly-<week_start>.md`，并推统一默认 target）建议结构：

```
# 复盘 YYYY-MM-DD（第N交易日）

## 账户绩效
统计窗口：[前一日 08:00, 当日 08:00)，UTC+8（固定24小时）
🟢 实盘：equity $X | 周期净 X USDT | 开N/平M | 手续费 X
（equity 取 OKX API / account_snapshots，查最新按 `ORDER BY ts DESC`；禁 rowid DESC——该表 `INSERT OR REPLACE` 会改 rowid）

## 信号 / playbook 绩效
- per-信号：各 analysis_signals action 命中率 / 平均收益（按 trade_experiences 关联）
- playbook：只按已闭合 `trade_experiences.playbook_ref` 显式归因；当前没有可归因样本或 current-source marker 未就绪时标“当前统计未建立”，不得展示旧 drill/legacy `trade_events` 数值

- 经验库可信度（find_similar_experience credibility）随样本积累的变化

## 教训 / 改进
- error_patterns（lessons.db）本周期新增 / 复发
- missed_opportunities（lessons.db）：使用交易事实窗整体前移4小时的连续24小时候选窗；只统计已有16根连续15分钟K线的完整4小时后验，并外显候选窗

## 异常 / 数据降级
- 当期 stale 源 / 降级权重=0 的源（data_source_quality）
```

> **2026-08-13 规格书四段（writer 自动生成 + reviewer 判断段）**：日报 Markdown 由
> `daily_report_writer` 额外渲染四段，激活边界 `2026-08-14 00:00:00` 起
> `validate_daily_report.py` 硬性要求（历史归档不反向加责）：
>
> | 段 | 来源 | reviewer 职责 |
> |---|---|---|
> | `## 🛰 全市场扫描` | writer 确定性回读（tick 宇宙规模 + 窗口内 analysis 轮次/信号/动作分布 + 判断吞吐影子计数；不显示可信度分值） | 只读，不加工 |
> | `## 📡 数据完善率` | writer 确定性回读 ledger.collection_runs 窗口完成率 + 失败源清单 | 只读；归因写进复盘观察 |
> | `### 市场总览（writer 权威回读）` | cross_market + tick_snapshots（BTC/ETH、总市值、BTC.D、恐贪、TVL、regime） | 只读，不复算 |
> | `## 🔭 次日关注` | **reviewer 判断**（payload `focus_next_day`） | 必填 3~6 条，见上方字段说明 |

> 复盘一律经 `scripts/qq_push.py --content-file <UTF-8 文件> --dedupe-key reviewer:<YYYY-MM-DD>:<用途>`（禁 channels PUT、禁直接使用数字目标）。发送前执行 `scripts/validate_daily_report.py --file <日报 Markdown>`；日报/周报禁止复用 15M 战报专用 `validate_push_format.py`。正文中文只走 UTF-8 文件。

## 4. 调用

```powershell
# 先由文件工具写 <PROJECT_ROOT>\tmp\review.json（UTF-8）；禁止 echo/内联 here-string 拼中文 JSON。
# 开场有界等待并验证 07:55 维护交接
pwsh -NoProfile -File <PROJECT_ROOT>\scripts\run_okx_python.ps1 <PROJECT_ROOT>\scripts\reviewer_preflight.py --wait-seconds 1200
# dry-run（默认，只 print 不写）
pwsh -NoProfile -File <PROJECT_ROOT>\scripts\run_okx_python.ps1 <PROJECT_ROOT>\scripts\daily_report_writer.py --json-file <PROJECT_ROOT>\tmp\review.json
# 校验事实回执无误后才 apply
pwsh -NoProfile -File <PROJECT_ROOT>\scripts\run_okx_python.ps1 <PROJECT_ROOT>\scripts\daily_report_writer.py --json-file <PROJECT_ROOT>\tmp\review.json --apply --profiles both

# 仅修复/重渲染 Markdown（不重复 INSERT daily_reports；仍需显式 --apply）
pwsh -NoProfile -File <PROJECT_ROOT>\scripts\run_okx_python.ps1 <PROJECT_ROOT>\scripts\daily_report_writer.py --json-file <PROJECT_ROOT>\tmp\review.json --markdown-only --apply

# 既有日报只补 revision 元数据：先 dry-run；apply 必须指定全新备份目录。
# 该入口只改目标日 live 行 raw 与 Markdown 的一条 revision 行，不重算、不重发。
pwsh -NoProfile -File <PROJECT_ROOT>\scripts\run_okx_python.ps1 <PROJECT_ROOT>\scripts\daily_report_writer.py --backfill-daily-revision --report-ts "2026-07-28 08:05:00"
pwsh -NoProfile -File <PROJECT_ROOT>\scripts\run_okx_python.ps1 <PROJECT_ROOT>\scripts\daily_report_writer.py --backfill-daily-revision --report-ts "2026-07-28 08:05:00" --apply --backup-dir <PROJECT_ROOT>\tmp\archive\<新目录>
```

## 5. 校验

| 校验项 | 由谁 | 失败行为 |
|---|---|---|
| 维护交接 | `reviewer_preflight.py` 校验当日 ready 清单、关键步骤 rc、质量文件哈希和 provisional 状态 | exit 非 0 不生成、不写库、不外发 |
| 周期号禁跳号/回滚（同日/周共享，否则 MAX+1，事务内） | `daily_report_writer.next_trade_day_num` / `_shared_period_num` | — |
| 周/月报禁覆盖（PK 重复即报错） | writer `IntegrityError` 捕获 | `fail()` exit≠0（视为 P0） |
| read-after-write 校验（按 `last_insert_rowid` 回读） | writer | 回读不到 -> `fail()` exit≠0 |
| 输入 JSON 解析（含中文走 `--json-file`） | writer `load_payload` + `sanitize_text` | 解析失败 -> `fail()` |
| 默认 dry-run 保护 | writer（`--apply` 才真写） | 无 `--apply` 只 print，不动库 |
| 既有日报 revision 补正 | `daily_report_writer --backfill-daily-revision` 强制备份、目标行并发指纹、Markdown 单行一致性检查 | 仅允许两行 raw + 一条 Markdown revision；不重算、不自动重发；重复执行幂等 |
| 独立日报校验 | `scripts/validate_daily_report.py --file <日报 Markdown> --db-root <PROJECT_ROOT>\db` 独立断言 `[前一日 08:00, 当日 08:00)` 固定24小时右开窗口及与上一日报连续，再只读复算标题/报告日期、live 成交开/平、风控拒绝、对账状态、审计与 revision；周报另核 `[上周一 08:00, 本周一 08:00)` 和 `reports/weekly/` 文件 | exit 非 0 不推；修正文档后重验。**不得调用 15M `validate_push_format.py`** |
| 订单标识外发边界 | 日报可列用于对账的 `ordId`/订单标识；凭据、签名、令牌禁止出现 | 凭据类内容立即阻断，订单标识本身不阻断 |
| 外发入口 | `scripts/qq_push.py --content-file <UTF-8 文件> --dedupe-key reviewer:<日期>:<用途>`，使用统一默认 target | 非统一入口不推 |

成功：writer 退出码 `0`（且 read-after-write 通过）；日报 Markdown 落盘；周一还必须存在对应 weekly Markdown；独立日报校验通过后才可外发。退出码非 0 -> Agent 视为 P0。
