# -*- coding: utf-8 -*-
"""日报/周报/月报硬化写入器。

当前约束：
1. 周期编号禁跳号/回滚：trade_day_num = MAX(trade_day_num)+1（事务内）
2. ts 为 UTC+8 字符串 YYYY-MM-DD HH:MM:SS
3. 写后 read-after-write 校验
4. 绝不执行 DELETE/UPDATE 已有 trade_day_num（只 INSERT）
5. --rewrite-null-and-renumber 仅用于显式维护：把 #NULL 行重新编号并补缺号
6. 默认 dry-run 模式（--apply 才真写）
7. 同时落盘 reports/daily-reports/daily-YYYY-MM-DD.md（markdown 全文）

调用：
  echo '<json>' | run_okx_python.ps1 scripts/daily_report_writer.py --stdin
  run_okx_python.ps1 scripts/daily_report_writer.py --json-file path.json [--apply] [--profiles live|demo|both]
  run_okx_python.ps1 scripts/daily_report_writer.py --rewrite-null-and-renumber [--apply]

说明：默认 --profiles both，一次 payload 同时写 live/demo 双段；成功后不要再单独重复写 demo。

退出码：0=成功且校验通过；非0=失败（Agent 须视为 P0）
"""

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


def sanitize_text(value: str) -> str:
    """Drop invalid surrogate code points that can appear from PowerShell pipes."""
    return value.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"
DB_PATH = Path(os.environ.get('OKX_ACCOUNT_DB', _project_path('db', 'account.db')))
REPORTS_DIR = Path(os.environ.get('OKX_DAILY_REPORTS_DIR', _project_path('reports', 'daily-reports')))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")


def now_cst() -> str:
    return datetime.now(CST).strftime(TS_FMT)


def _snapshot_equity(db_path, profile: str, as_of_ts: str | None = None):
    """account.db.account_snapshots 截至报告时点的最新 totalEq。

    按 datetime(ts),rowid DESC，避开 MAX(ts) 词典序坑。
    返回 float 或 None（库缺/锁/异常一律降级 None，不抛、不拖垮日报渲染）。"""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            if as_of_ts:
                r = con.execute(
                    "SELECT totalEq FROM account_snapshots WHERE profile=? AND datetime(ts)<=datetime(?) "
                    "ORDER BY datetime(ts) DESC,rowid DESC LIMIT 1", (profile, as_of_ts)).fetchone()
            else:
                r = con.execute(
                    "SELECT totalEq FROM account_snapshots WHERE profile=? ORDER BY rowid DESC LIMIT 1",
                    (profile,)).fetchone()
        finally:
            con.close()
        return float(r[0]) if r and r[0] is not None else None
    except Exception:
        return None


def _authoritative_cum_pnl(db_path, profile: str, as_of_ts: str | None = None):
    """复用 cum_pnl.py 冻结基线口径；失败返回 None，绝不回退裸 SUM。"""
    try:
        import cum_pnl
        info = cum_pnl.cum_for(Path(db_path).parent, profile, as_of_ts=as_of_ts)
        return float(info["cum_pnl"]) if info.get("ok") else None
    except Exception:
        return None


def _fmt_num(value):
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return "-"


def _snapshot_positions_summary(db_path, profile: str, as_of_ts: str | None = None,
                                max_age_min: int = 30):
    """读取报告时点之前最近一批 OKX API position_snapshots，精确按批次、不 GROUP BY。

    有 __FLAT__ 哨兵返回“空仓”；无批次或批次距报告时点过旧返回 None，防把缺数据写成空仓。
    """
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            if as_of_ts:
                batch = con.execute(
                    "SELECT ts FROM position_snapshots WHERE profile=? AND datetime(ts)<=datetime(?) "
                    "ORDER BY datetime(ts) DESC,rowid DESC LIMIT 1", (profile, as_of_ts)).fetchone()
            else:
                batch = con.execute(
                    "SELECT ts FROM position_snapshots WHERE profile=? ORDER BY rowid DESC LIMIT 1",
                    (profile,)).fetchone()
            if not batch:
                return None
            ref = datetime.strptime(as_of_ts or now_cst(), TS_FMT).replace(tzinfo=CST)
            batch_dt = datetime.strptime(str(batch[0]), TS_FMT).replace(tzinfo=CST)
            age_min = (ref - batch_dt).total_seconds() / 60.0
            if age_min < -5 or age_min > max_age_min:
                return None
            rows = con.execute(
                "SELECT symbol,side,sz,avgPx,lev,upl FROM position_snapshots "
                "WHERE profile=? AND ts=? ORDER BY rowid", (profile, batch[0])).fetchall()
        finally:
            con.close()
        if not rows:
            return None
        real = [r for r in rows if str(r[0] or "").strip() != "__FLAT__"]
        if not real:
            return "空仓"
        lines = []
        for symbol, side, sz, avg_px, lev, upl in real:
            side_cn = {"long": "多", "short": "空"}.get(str(side or "").lower(), str(side or "-"))
            line = f"- {symbol} {side_cn} {_fmt_num(sz)}张 @{_fmt_num(avg_px)} {_fmt_num(lev)}x"
            if upl is not None:
                line += f" | 浮盈 {float(upl):+.2f}"
            lines.append(line)
        return "\n".join(lines)
    except Exception:
        return None


def fail(msg: str, code: int = 2):
    print(f"[daily_report_writer][FAIL] {msg}", file=sys.stderr)
    sys.exit(code)


def read_stdin_text() -> str:
    if hasattr(sys.stdin, "buffer"):
        return sys.stdin.buffer.read().decode("utf-8", errors="replace")
    return sys.stdin.read()


def load_payload(args) -> dict:
    if args.stdin:
        raw = read_stdin_text()
    elif args.json_file:
        with open(args.json_file, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    elif args.json:
        raw = args.json
    elif args.rewrite_null_and_renumber:
        return {"_mode": "rewrite_null_and_renumber"}
    else:
        fail("缺少输入：需 --stdin / --json-file / --json / --rewrite-null-and-renumber 之一")
    raw = sanitize_text(raw)
    try:
        return json.loads(raw)
    except Exception as e:
        fail(f"输入 JSON 解析失败: {e}；含中文/特殊符号时建议先写 <PROJECT_ROOT>\\tmp\\*.json 再用 --json-file")


def next_trade_day_num(con, report_ts: str | None = None) -> int:
    """返回日报 trade_day_num。

    v7.0e.7 修复：live/demo 同一天应共享同一个 trade_day_num，不能每 INSERT 一行就 +1。
    - 若当天已有非空 trade_day_num：复用当天编号
    - 否则：取所有历史 MAX(trade_day_num)+1
    """
    date_str = (report_ts or now_cst())[:10]
    cur = con.execute(
        "SELECT MIN(trade_day_num) FROM daily_reports "
        "WHERE substr(ts,1,10)=? AND trade_day_num IS NOT NULL",
        (date_str,),
    )
    same_day = cur.fetchone()[0]
    if same_day is not None:
        return int(same_day)
    cur = con.execute("SELECT MAX(trade_day_num) FROM daily_reports WHERE trade_day_num IS NOT NULL")
    mx = cur.fetchone()[0]
    return (mx if mx is not None else 0) + 1


def write_daily(con, payload: dict, apply: bool) -> dict:
    """INSERT 一行 daily_reports，apply=False 只 print。返回结果 dict。"""
    now = now_cst()
    profile = payload.get("profile", "live")

    def pf(key, default=0):
        """按 profile 优先读取 live_/demo_ 前缀字段，兼容旧无前缀字段。"""
        return payload.get(f"{profile}_{key}", payload.get(key, default))

    # v7.0e.1/e.7: payload 拆分 live / demo 两套字段
    fields = {
        "ts": payload.get("ts") or now,
        "profile": profile,
        "open_count": int(pf("open_count", 0)),
        "close_count": int(pf("close_count", 0)),
        "total_pnl": float(pf("total_pnl", 0.0) or 0.0),
        "total_fees": float(pf("total_fees", 0.0) or 0.0),
        "best_trade": pf("best_trade", None) or None,
        "worst_trade": pf("worst_trade", None) or None,
        "summary": payload.get("summary") or "",
        "lessons": payload.get("lessons") or "",
        "raw": payload.get("raw") or "",
    }

    if not apply:
        print(f"[DRY-RUN] would INSERT daily_reports:")
        for k, v in fields.items():
            v_disp = (v[:120] + '...') if isinstance(v, str) and len(v) > 120 else v
            print(f"  {k:14}= {v_disp}")
        return {"dry_run": True, "fields": fields}

    # 计算 trade_day_num（v7.0e.7：同一天 live/demo 共享编号）
    fields["trade_day_num"] = next_trade_day_num(con, fields["ts"])

    cols = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    try:
        con.execute(
            f"INSERT INTO daily_reports ({cols}) VALUES ({placeholders})",
            list(fields.values())
        )
    except sqlite3.IntegrityError as e:
        fail(f"INSERT 失败（IntegrityError）: {e}")

    # read-after-write 校验：用 last_insert_rowid，避免同一天 live/demo 共享 trade_day_num 时回读到另一行
    rowid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    row = con.execute(
        "SELECT rowid, trade_day_num, ts, profile, open_count, close_count, total_pnl "
        "FROM daily_reports WHERE rowid = ?",
        (rowid,)
    ).fetchone()
    if not row:
        fail(f"read-after-write 校验失败：rowid={rowid} 未找到")
    print(f"[OK] INSERT daily_reports: rowid={row[0]} trade_day_num={row[1]} ts={row[2]} "
          f"profile={row[3]} opn={row[4]} cls={row[5]} pnl={row[6]}")
    return {"applied": True, "rowid": row[0], "trade_day_num": row[1], "fields": fields}


def _shared_period_num(con, table: str, num_col: str, ts_col: str, ts_val: str) -> int:
    """同一周期 live/demo 共享编号；无则 MAX+1（禁跳号/回滚）。"""
    cur = con.execute(
        f"SELECT MIN({num_col}) FROM {table} WHERE {ts_col}=? AND {num_col} IS NOT NULL",
        (ts_val,),
    )
    same = cur.fetchone()[0]
    if same is not None:
        return int(same)
    mx = con.execute(f"SELECT MAX({num_col}) FROM {table} WHERE {num_col} IS NOT NULL").fetchone()[0]
    return (mx if mx is not None else 0) + 1


def write_weekly(con, payload: dict, apply: bool) -> dict:
    """INSERT 一行 weekly_reports（PK: week_start_ts+profile；重复即报错，不覆盖）。"""
    profile = payload.get("profile", "live")

    def pf(key, default=None):
        return payload.get(f"{profile}_{key}", payload.get(key, default))

    week_start = payload.get("week_start_ts")
    if not week_start:
        fail("weekly 必填 week_start_ts（本周一 'YYYY-MM-DD HH:MM:SS' UTC+8）")
    fields = {
        "week_start_ts": week_start,
        "profile": profile,
        "open_count": int(pf("open_count", 0) or 0),
        "close_count": int(pf("close_count", 0) or 0),
        "total_pnl": float(pf("total_pnl", 0.0) or 0.0),
        "win_rate": pf("win_rate", None),
        "avg_hold_hours": pf("avg_hold_hours", None),
        "margin_util_pct": pf("margin_util_pct", None),
        "idle_ratio": pf("idle_ratio", None),
        "summary": payload.get("summary") or "",
        "lessons": payload.get("lessons") or "",
        "raw": payload.get("raw") or "",
    }
    if not apply:
        print("[DRY-RUN] would INSERT weekly_reports:")
        for k, v in fields.items():
            print(f"  {k:16}= {(str(v)[:100] if v is not None else None)}")
        return {"dry_run": True, "kind": "weekly", "fields": fields}

    fields["trade_week_num"] = _shared_period_num(con, "weekly_reports", "trade_week_num",
                                                  "week_start_ts", week_start)
    cols = ", ".join(fields.keys())
    ph = ", ".join(["?"] * len(fields))
    try:
        con.execute(f"INSERT INTO weekly_reports ({cols}) VALUES ({ph})", list(fields.values()))
    except sqlite3.IntegrityError as e:
        fail(f"weekly INSERT 失败（该周期+profile 已存在，禁覆盖；如需重写请人工处理）: {e}")
    row = con.execute(
        "SELECT trade_week_num, week_start_ts, profile, total_pnl FROM weekly_reports "
        "WHERE week_start_ts=? AND profile=?",
        (week_start, profile),
    ).fetchone()
    if not row:
        fail("weekly read-after-write 校验失败")
    print(f"[OK] INSERT weekly_reports: trade_week_num={row[0]} week={row[1]} profile={row[2]} pnl={row[3]}")
    return {"applied": True, "kind": "weekly", "trade_week_num": row[0], "fields": fields}


def write_monthly(con, payload: dict, apply: bool) -> dict:
    """INSERT 一行 monthly_reports（PK: month_start_ts+profile；重复即报错，不覆盖）。"""
    profile = payload.get("profile", "live")

    def pf(key, default=None):
        return payload.get(f"{profile}_{key}", payload.get(key, default))

    month_start = payload.get("month_start_ts")
    if not month_start:
        fail("monthly 必填 month_start_ts（本月 1 号 'YYYY-MM-DD HH:MM:SS' UTC+8）")
    fields = {
        "month_start_ts": month_start,
        "profile": profile,
        "total_pnl": float(pf("total_pnl", 0.0) or 0.0),
        "max_drawdown": pf("max_drawdown", None),
        "sharpe_approx": pf("sharpe_approx", None),
        "summary": payload.get("summary") or "",
        "lessons": payload.get("lessons") or "",
        "raw": payload.get("raw") or "",
    }
    if not apply:
        print("[DRY-RUN] would INSERT monthly_reports:")
        for k, v in fields.items():
            print(f"  {k:16}= {(str(v)[:100] if v is not None else None)}")
        return {"dry_run": True, "kind": "monthly", "fields": fields}

    fields["trade_month_num"] = _shared_period_num(con, "monthly_reports", "trade_month_num",
                                                   "month_start_ts", month_start)
    cols = ", ".join(fields.keys())
    ph = ", ".join(["?"] * len(fields))
    try:
        con.execute(f"INSERT INTO monthly_reports ({cols}) VALUES ({ph})", list(fields.values()))
    except sqlite3.IntegrityError as e:
        fail(f"monthly INSERT 失败（该周期+profile 已存在，禁覆盖；如需重写请人工处理）: {e}")
    row = con.execute(
        "SELECT trade_month_num, month_start_ts, profile, total_pnl FROM monthly_reports "
        "WHERE month_start_ts=? AND profile=?",
        (month_start, profile),
    ).fetchone()
    if not row:
        fail("monthly read-after-write 校验失败")
    print(f"[OK] INSERT monthly_reports: trade_month_num={row[0]} month={row[1]} profile={row[2]} pnl={row[3]}")
    return {"applied": True, "kind": "monthly", "trade_month_num": row[0], "fields": fields}


def rewrite_null_and_renumber(con, apply: bool) -> dict:
    """C 方案：把所有 trade_day_num=NULL 的行重新编号（按 ts 升序）"""
    # 现有 #NULL 行
    cur = con.execute("SELECT rowid, ts, substr(summary, 1, 60) FROM daily_reports "
                       "WHERE trade_day_num IS NULL ORDER BY ts")
    nulls = cur.fetchall()
    print(f"[C 方案] 找到 {len(nulls)} 行 trade_day_num=NULL:")
    for r in nulls:
        print(f"  rowid={r[0]} ts={r[1]} summary={r[2]}...")

    # 当前最大 trade_day_num
    cur = con.execute("SELECT MAX(trade_day_num) FROM daily_reports WHERE trade_day_num IS NOT NULL")
    mx = cur.fetchone()[0] or 0
    print(f"[C 方案] 当前 MAX(trade_day_num)={mx}")

    if not apply:
        print(f"[DRY-RUN] C 方案：会按 ts 升序给 NULL 行分配 #{mx+1} ~ #{mx+len(nulls)}")
        return {"dry_run": True, "nulls_count": len(nulls), "next_num": mx+1}

    # 真写
    next_num = mx
    for rowid, ts, _summ in nulls:
        next_num += 1
        con.execute("UPDATE daily_reports SET trade_day_num = ? WHERE rowid = ?", (next_num, rowid))
        print(f"  [OK] rowid={rowid} ts={ts} → trade_day_num={next_num}")

    return {"applied": True, "renumbered": len(nulls), "next_num": next_num}


def write_markdown(payload: dict, apply: bool) -> str:
    """写 reports/daily-reports/daily-YYYY-MM-DD.md（v7.2 资金 + 已实现收益）"""
    if not apply:
        ts = payload.get("ts", now_cst())
        date_str = ts[:10]
        path = REPORTS_DIR / f"daily-{date_str}.md"
        print(f"[DRY-RUN] would write markdown: {path}")
        return str(path)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = payload.get("ts", now_cst())
    date_str = ts[:10]
    path = REPORTS_DIR / f"daily-{date_str}.md"

    # v7.0e.1: live / demo 数据分别读
    def v(prefix, key, default=0):
        """读 payload[key]，优先用 live_/demo_ 前缀"""
        return payload.get(f"{prefix}_{key}", payload.get(key, default))

    # writer 自取权威值：避免 reviewer 漏传字段时把顶部默认为 0/空仓，而详细 summary 又写真值。
    # 全部按报告 ts 回看，允许安全重渲染历史日报，不误用当前仓位/当前累计收益。
    _live_eq = v('live', 'equity', payload.get('current_equity', None))
    _live_eq_db = _snapshot_equity(DB_PATH, 'live', ts)
    live_eq = _live_eq_db if _live_eq_db is not None else (_live_eq or 0)
    _live_cum_db = _authoritative_cum_pnl(DB_PATH, 'live', ts)
    live_realized_pnl = _live_cum_db if _live_cum_db is not None else v('live', 'realized_pnl', 0)
    live_open = v('live', 'open_count')
    live_close = v('live', 'close_count')
    live_pnl_today = v('live', 'total_pnl', 0)
    live_fees = v('live', 'total_fees', 0)
    live_best = v('live', 'best_trade', '—') or '—'
    live_worst = v('live', 'worst_trade', '—') or '—'
    _live_pos_db = _snapshot_positions_summary(DB_PATH, 'live', ts)
    live_pos = _live_pos_db if _live_pos_db is not None else v(
        'live', 'positions_summary', payload.get('positions_summary', '持仓数据不可用'))

    # P3b (2026-06-29)：payload 优先；缺失/为 0 回退 demo 自身快照（绝不 fallback 到 live，避免收益混淆）
    _demo_eq = v('demo', 'equity', None)
    _demo_eq_db = _snapshot_equity(DB_PATH, 'demo', ts)
    demo_eq = _demo_eq_db if _demo_eq_db is not None else _demo_eq
    # v7.1.2：demo 仍缺（payload 与 account.db 快照均无）→ 显式标 0 + 异常
    if demo_eq is None:
        demo_eq = 0
        payload['anomalies'] = (str(payload.get('anomalies') or '无') + '\n- WARN: demo_equity 缺失（payload 与 account.db 快照均无），已禁止 fallback 到 live equity。').strip()
    _demo_cum_db = _authoritative_cum_pnl(DB_PATH, 'demo', ts)
    demo_realized_pnl = _demo_cum_db if _demo_cum_db is not None else v('demo', 'realized_pnl', 0)
    try:
        le, de = float(live_eq), float(demo_eq)
        lr, dr = float(live_realized_pnl), float(demo_realized_pnl)
        # P3b (2026-06-29)：仅当两盘 equity 均非 0 且相同（且 realized 也同）才告警；全 0/缺数据不再误触
        if le != 0 and de != 0 and le == de and lr == dr:
            payload['anomalies'] = (str(payload.get('anomalies') or '无') + '\n- WARN: demo/live equity 与 realized_pnl 完全相同，疑似口径混淆，请核验 demo 数据源。').strip()
    except Exception:
        pass
    demo_open = v('demo', 'open_count')
    demo_close = v('demo', 'close_count')
    demo_pnl_today = v('demo', 'total_pnl', 0)
    demo_fees = v('demo', 'total_fees', 0)
    demo_best = v('demo', 'best_trade', '—') or '—'
    demo_worst = v('demo', 'worst_trade', '—') or '—'
    _demo_pos_db = _snapshot_positions_summary(DB_PATH, 'demo', ts)
    demo_pos = _demo_pos_db if _demo_pos_db is not None else v(
        'demo', 'positions_summary', '持仓数据不可用')

    md = f"""# 📊 小灵日报 {date_str}（v7.2 资金 + 已实现收益）

> 自动生成 by daily_report_writer.py (P7 复盘写入器) — v7.2 推送口径变更
> ts: {ts} | live/demo 同日共享 trade_day_num（见 db）

---

## 💰 资产（实盘 / 模拟盘分开）

### 🟢 实盘（live）
| 项 | 数值 |
|---|---|
| 资金总额 | ${float(live_eq):.2f} |
| 累计收益（已实现） | ${float(live_realized_pnl):.2f} |

### 🟡 模拟盘（demo）
| 项 | 数值 |
|---|---|
| 资金总额 | ${float(demo_eq):.2f} |
| 累计收益（已实现） | ${float(demo_realized_pnl):.2f} |

> 累计收益 = SUM(pnl) 历史所有已开平仓收益相加（不含浮动盈亏）

> 严禁 live+demo 收益混合 / 用 demo 收益粉饰 live

## 📈 持仓（实盘 / 模拟盘分开）

### 🟢 实盘
{live_pos}

### 🟡 模拟盘
{demo_pos}

## 🎯 交易（实盘 / 模拟盘分开）

### 🟢 实盘
- 今日开仓: {int(live_open)} 笔
- 今日平仓: {int(live_close)} 笔
- 净 PnL: ${float(live_pnl_today):.2f}
- 手续费: ${float(live_fees):.2f}
- 最佳: {live_best} | 最差: {live_worst}

### 🟡 模拟盘
- 今日开仓: {int(demo_open)} 笔
- 今日平仓: {int(demo_close)} 笔
- 净 PnL: ${float(demo_pnl_today):.2f}
- 手续费: ${float(demo_fees):.2f}
- 最佳: {demo_best} | 最差: {demo_worst}

## ⚠️ 异常 / 🛠 自修

{payload.get('anomalies', '无')}

## 🌍 市场

{payload.get('market', '见 push_archive latest')}

## 🧠 教训

{payload.get('lessons', '见 lessons.db')}

---

## 详细 summary

{payload.get('summary', '')}

## 详细 lessons (JSON)

```json
{payload.get('lessons', '')}
```

---

🤖 自动生成 by 小灵 🧚‍♀️ | {now_cst()} CST | daily_report_writer.py v1.2 (v7.2)
"""
    path.write_text(md, encoding='utf-8')
    print(f"[OK] wrote markdown: {path} ({path.stat().st_size}B)")
    return str(path)


def main():
    global DB_PATH, REPORTS_DIR
    ap = argparse.ArgumentParser(description="Daily Report Writer (P7 hardened writer)")
    ap.add_argument("--stdin", action="store_true", help="从 stdin 读 JSON")
    ap.add_argument("--json-file", help="从文件读 JSON")
    ap.add_argument("--json", help="JSON 字符串")
    ap.add_argument("--apply", action="store_true", help="真写模式（默认 dry-run）")
    ap.add_argument("--rewrite-null-and-renumber", action="store_true",
                    help="C 方案：把 trade_day_num=NULL 的行重新编号（需 --apply 才生效）")
    ap.add_argument("--no-markdown", action="store_true", help="不写 markdown 文件")
    ap.add_argument("--markdown-only", action="store_true",
                    help="仅重渲染 markdown，不插入 daily_reports；为防误写仍需 --apply")
    ap.add_argument("--kind", choices=("daily", "weekly", "monthly"), default="daily",
                    help="报告类型：daily=daily_reports（默认）；weekly=weekly_reports（需 week_start_ts）；monthly=monthly_reports（需 month_start_ts）")
    ap.add_argument("--profiles", choices=("live", "demo", "both"), default="both",
                    help="写入 profile 范围：both=同一 payload 写 live+demo（默认）；live/demo=仅写单段，避免重复调用冲突")
    ap.add_argument("--db-path", default=str(DB_PATH), help="account.db 路径（默认 <PROJECT_ROOT>\\db\\account.db；测试可传临时库）")
    ap.add_argument("--reports-dir", default=str(REPORTS_DIR), help="日报 markdown 输出目录")
    args = ap.parse_args()

    DB_PATH = Path(args.db_path)
    REPORTS_DIR = Path(args.reports_dir)

    payload = load_payload(args)

    if not DB_PATH.exists():
        fail(f"db 不存在：{DB_PATH}")

    con = sqlite3.connect(DB_PATH)
    try:
        if args.markdown_only:
            if not args.apply:
                fail("--markdown-only 需同时给 --apply")
            result = {"markdown_only": True, "path": write_markdown(payload, True)}
        elif payload.get("_mode") == "rewrite_null_and_renumber":
            result = rewrite_null_and_renumber(con, args.apply)
        else:
            writer = {"daily": write_daily, "weekly": write_weekly, "monthly": write_monthly}[args.kind]
            # weekly/monthly 判断 demo 段是否需要：demo_total_pnl / demo_equity 任一存在即写
            has_demo = any(payload.get(k) is not None for k in
                           ("demo_equity", "demo_session_pnl", "demo_total_pnl", "demo_realized_pnl"))
            if args.profiles == "demo":
                result = writer(con, {**payload, "profile": "demo"}, args.apply)
            else:
                result = writer(con, payload, args.apply)
                if args.profiles == "both" and has_demo:
                    result["demo"] = writer(con, {**payload, "profile": "demo"}, args.apply)
            if args.kind == "daily" and not args.no_markdown:
                write_markdown(payload, args.apply)
        con.commit()
    except Exception as e:
        con.rollback()
        fail(f"执行失败：{e}")
    finally:
        con.close()

    print(f"\n[result] {json.dumps(result, ensure_ascii=False, default=str)}")
    sys.exit(0)


if __name__ == "__main__":
    main()
