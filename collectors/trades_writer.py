# -*- coding: utf-8 -*-
"""V2.0 trades 落库 writer（live/demo trader 写 live_trades.db/demo_trades.db 的唯一通道）。

trader agent 完成判断后，把回执 JSON 从 stdin 喂进来，写进对应 profile 的 trade_cycles + trades 表。
红线「写库必走 writer」：trader agent 严禁手写 INSERT，强制走本脚本。

用法：
    # stdin 模式（trader agent 专用）
    echo '<回执JSON>' | python trades_writer.py --stdin --cycle-id 2026-06-18T14:00 --profile live
    cat receipt.json | python trades_writer.py --stdin --cycle-id ... --profile demo

    # 命令行 quick-write（测试/演示）
    python trades_writer.py --cycle-id 2026-06-18T14:00 --profile live \
        --decision traded --n-orders 1 --equity 998.50 \
        --note "HOLD — no clear signal, regime=trend_down"

    # 直接写 hold（无交易）
    python trades_writer.py --cycle-id 2026-06-18T14:00 --profile live \
        --decision hold

输入回执 JSON（从 stdin）：
    {
      "cycle_id": "2026-06-18T14:00",
      "ts": "2026-06-18 14:05:30",   -- 完成时刻（UTC+8）
      "mode": "full",
      "decision": "HOLD",             -- 'traded'|'hold'|'skip'|'degraded'|'error'
      "action": "BTC/USDT: hold",    -- 简短动作描述
      "regime": "trend_down",        -- 供复盘用
      "regime_stale": 0,
      "decision_protocol": "decision_card_v1",
      "decision_card": { ...六项卡+历史取舍... },
      "n_orders": 0,
      "equity": 999.22,
      "avail_before": 999.22,        -- 等效 equity
      "avail_after": 999.22,
      "pnl_session": 0.0,            -- 本轮 realized PnL（live/demo 同义：demo 真交易，pnl 参与 cum_pnl 与对账）
      "pnl_open": -14.50,            -- 当前持仓浮盈亏（live/demo 同义）
      "leverage": 5,
      "note": "...",                 -- 额外说明（可选）
      "trades": [
        {
          "symbol": "BTC-USDT-SWAP",
          "action": "open",          -- 'open'|'close'|'add'|'reduce'|'none'
          "side": "long",
          "sz": 1,
          "fill_px": 62500.00,
          "lev": 5,
          "margin": 12.50,
          "notional": 62500.00,
          "reasoning": "...",
          "deviation": null,
          "degradation": null,
          "pnl": null
        }
      ],
      "errors": [],
      "status": "ok"
    }

- `trades=[]` 或无 trades 字段：合法（hold/skip 路径）
- `decision` 大小写不敏感（统一转小写对应对应 'traded'/'hold'/'skip'/'degraded'/'error'）
- `mode` 由 writer 强制设为 profile（live|demo），回执里的 mode 字段被忽略

输出（stdout）：
    {"ok": true, "cycle_id": "...", "n_orders": N}
    exit 0 = 成功

失败（exit 非 0）：
    stderr: 错误原因
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import argparse
import json
import os
import re
import sys
from datetime import datetime as dt, timezone, timedelta
from pathlib import Path
from typing import Optional

if _project_path() not in sys.path:
    sys.path.insert(0, _project_path())
from core.decision_card import validate_card  # noqa: E402

CST = timezone(timedelta(hours=8))
_TS_ISO_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?$"
)


def normalize_ts(ts: str) -> str:
    """归一化时间为 UTC+8 纯字符串 'YYYY-MM-DD HH:MM:SS'。

    兼容输入：ISO8601 带毫秒+时区 / ISO-Z / 无时区 / 已是纯字符串 / 缺秒。
    """
    if not ts:
        return ts
    s = ts.strip()
    if "T" not in s and " " in s and len(s) >= 19 and s[10] == " ":
        return s[:19]
    # 缺秒 'YYYY-MM-DD HH:MM' → 补 ':00'
    if "T" not in s and " " in s and len(s) == 16 and s[10] == " " and s[13] == ":":
        return s + ":00"
    m = _TS_ISO_RE.match(s)
    if not m:
        return s
    date, time, tz = m.groups()
    if tz in (None, ""):
        dt_obj = dt.fromisoformat(f"{date}T{time}+08:00")
    elif tz == "Z":
        dt_obj = dt.fromisoformat(f"{date}T{time}+00:00").astimezone(CST)
    else:
        norm_tz = tz if ":" in tz else tz[:3] + ":" + tz[3:]
        dt_obj = dt.fromisoformat(f"{date}T{time}{norm_tz}").astimezone(CST)
    return dt_obj.strftime("%Y-%m-%d %H:%M:%S")

import sqlite3

# HANDOFF-4A（2026-07-16）：CLI 落库成功后 detached 拍一次 dispatcher（事件驱动派发）。
# 守卫导入：任何异常→None→静默禁用——writer 落库优先，nudge 永不致命。守护闸详见模块 docstring。
try:
    if _project_path('collectors') not in sys.path:
        sys.path.insert(0, _project_path('collectors'))
    import _dispatch_nudge as _nudge_mod
except Exception:  # noqa: BLE001
    _nudge_mod = None

# ---------------------------------------------------------------------------
# DB paths
# ---------------------------------------------------------------------------
DB_MAP = {
    "live": Path(_project_path('db', 'live_trades.db')),
    "demo": Path(_project_path('db', 'demo_trades.db')),
}

# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------
def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=10)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout=5000;")
    con.row_factory = sqlite3.Row
    return con


# ---------------------------------------------------------------------------
# decision 归一化（小写 → schema 值）
# ---------------------------------------------------------------------------
DECISION_MAP = {
    "traded": "traded",
    "open": "traded",
    "hold": "hold",
    "skip": "skip",
    "degraded": "degraded",
    "error": "error",
    "none": "hold",
    "": "hold",
}


def normalize_decision(raw: Optional[str]) -> str:
    if not raw:
        return "hold"
    return DECISION_MAP.get(str(raw).lower().strip(), "hold")


# ---------------------------------------------------------------------------
# receipt 顶层缺 decision 时从 action_taken/trades 推导，保持 trade_cycles 与 trades 一致。
# 映射：OPEN_*/CLOSE/STOP_LOSS/REDUCE/ADD → traded；ADJUST/HOLD/WAIT/NONE → hold；
# 非空 trades[] 恒 traded；未知值 fail-safe 按 hold。已有 decision 值不覆盖。
# ---------------------------------------------------------------------------
_ACTION_TAKEN_TRADED = {"CLOSE", "STOP_LOSS", "REDUCE", "ADD"}


def derive_cycle_decision(action_taken: Optional[str], incoming_trades: list) -> str:
    if incoming_trades:
        return "traded"
    at = str(action_taken or "").upper().strip()
    if at.startswith("OPEN_") or at in _ACTION_TAKEN_TRADED:
        return "traded"
    return "hold"


# ---------------------------------------------------------------------------
# trades 行 margin/notional 缺失时按合约乘数补算（已有值不覆盖，
# 历史行不动）。文档口径 margin=fill_px×sz÷lev 缺 ctVal，对 ctVal≠1 合约（LAB=10 /
# BTC=0.01 / ETH=0.1）系统性错尺度；正确公式：
#     margin   = fill_px × sz × ctVal ÷ lev
#     notional = fill_px × sz × ctVal
# ctVal 从 market.db.instruments_cache 只读取（mode=ro），取不到回退 1.0 并 WARN
# （fail-safe，绝不阻塞写库）。
# ---------------------------------------------------------------------------
_CTVAL_CACHE: dict = {}


def _ctval_for(symbol: str) -> float:
    mkt_path = os.environ.get("OKX_MARKET_DB", _project_path('db', 'market.db'))
    key = (mkt_path, symbol)
    if key in _CTVAL_CACHE:
        return _CTVAL_CACHE[key]
    ctval = None
    err = None
    try:
        mkt = sqlite3.connect(f"file:{mkt_path}?mode=ro", uri=True, timeout=5)
        try:
            mkt.execute("PRAGMA busy_timeout=5000;")
            row = mkt.execute(
                "SELECT ctVal FROM instruments_cache WHERE instId=?",
                (symbol,)).fetchone()
        finally:
            mkt.close()
        if row and row[0] is not None and float(row[0]) > 0:
            ctval = float(row[0])
    except Exception as e:  # noqa: BLE001 —— 查失败回退 1.0，绝不阻塞写库
        err = e
    if ctval is None:
        sys.stderr.write(
            f"[trades_writer][WARN] ctVal 取不到（回退 1.0）: symbol={symbol}"
            + (f" err={err}" if err else "") + "\n")
        ctval = 1.0
    _CTVAL_CACHE[key] = ctval
    return ctval


def _as_pos_float(v) -> Optional[float]:
    """转正浮点；非数/非正 → None（补算公式只接受正值输入）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


# ---------------------------------------------------------------------------
# 成交行身份键（2026-07-07 B+C 合并保护）：优先交易所订单号（全局唯一，可精确区分
# "重发同一笔" vs "另一笔新成交"）；无 ordId 用内容指纹近似（同 cycle 两笔真实成交
# 恰好同 symbol/action/side/sz/fill_px 的概率极低；带 ordId 即可完全避免误判）。
# ---------------------------------------------------------------------------
def _extract_ordid(t_or_raw) -> Optional[str]:
    """从 trade dict 或 raw（JSON 文本/dict）提取交易所订单号。

    ⚠️ 口径钉死（2026-07-17，勿"顺手统一"）：本函数含 algoId 的**广口径**只用于合并闸/
    消费判定（保守安全——宁可多认已入账）；journal 重放 plan 的身份是**严格 trade.ordId**
    （algoId 禁顶身份，护"无 ordId 一律人工"守卫，见 :895 附近）。两口径刻意不同：
    把重放侧放宽会破人工守卫，把本函数收紧会让缺 ordId 的兼容行失配并重复记账。"""
    def _from_dict(d: dict) -> Optional[str]:
        for k in ("ordId", "ord_id", "algoId", "algo_id"):
            v = d.get(k)
            if v not in (None, "", 0):
                return str(v)
        return None

    if isinstance(t_or_raw, dict):
        oid = _from_dict(t_or_raw)
        if oid:
            return oid
        raw = t_or_raw.get("raw")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                raw = None
        if isinstance(raw, dict):
            return _from_dict(raw)
        return None
    if isinstance(t_or_raw, str):
        try:
            d = json.loads(t_or_raw)
        except (ValueError, TypeError):
            return None
        return _from_dict(d) if isinstance(d, dict) else None
    return None


def _trade_identity(symbol, action, side, sz, fill_px, ordid: Optional[str]) -> tuple:
    """身份键：("oid", 订单号) 或 ("fp", symbol, action, side, sz, fill_px)。"""
    if ordid:
        return ("oid", ordid)

    def _r(v):
        try:
            return round(float(v), 8)
        except (TypeError, ValueError):
            return None

    return ("fp", str(symbol or ""), str(action or ""), str(side or ""),
            _r(sz), _r(fill_px))


def _row_keys(symbol, action, side, sz, fill_px, ordid: Optional[str]) -> tuple:
    """双键：(ordid|None, fp元组)，供合并闸匹配。"""
    return (str(ordid) if ordid else None,
            _trade_identity(symbol, action, side, sz, fill_px, None))


def _rows_match(a: tuple, b: tuple) -> bool:
    """成交行同一性判定：fp 回退仅限“至多一侧有 ordId”。

    双侧都有 ordId → 只认 ordId 相等（两笔不同成交恰好同 symbol/sz/px 同形，禁按
    fp 互相销账=反向销账面）；至多一侧有 ordId → 内容指纹回退（旧行无 ordId、
    journal 重放/对账补写带 ordId 的同一笔成交必须判同，否则合并闸按"不相交"
    把同笔成交重复记账——07-16 架构核验点名的重复面）。"""
    if a[0] and b[0]:
        return a[0] == b[0]
    if a[1] != b[1]:
        return False
    # 核验修（2026-07-16）：fp 相等但双方 fill_px 都是 None（unconfirmed close 常态）
    # → 判不同。None 是"未知"不是值——同 cycle 两笔不同 unconfirmed close 必然同 fp
    # （100% 碰撞，非注释假设的"恰好同价概率极低"）。错向权衡：判不同最坏=重复行
    # （reconcile 净仓 OVER_CLOSED 可见可修）；判相同最坏=covered→REPLACE 静默删掉
    # 真实成交行（不可恢复）。取可见的错。
    if a[1][5] is None and b[1][5] is None:
        return False
    return True


# ---------------------------------------------------------------------------
# 验证
# ---------------------------------------------------------------------------
def validate(data: dict) -> list[str]:
    errors = []
    if not data.get("cycle_id"):
        errors.append("缺少 cycle_id")
    decision = normalize_decision(data.get("decision"))
    if decision not in ("traded", "hold", "skip", "degraded", "error"):
        errors.append(f"decision 归一化后非法: {decision!r}")
    if data.get("decision_protocol") == "decision_card_v1":
        errors.extend(validate_card(data.get("decision_card"), "decision_card"))
    if "trades" in data and data["trades"] is not None:
        if not isinstance(data["trades"], list):
            errors.append("trades 必须是 list")
        else:
            for i, t in enumerate(data["trades"]):
                if not isinstance(t, dict):
                    errors.append(f"trades[{i}] 必须是 dict")
                    continue
                if "symbol" not in t or not str(t.get("symbol", "")).strip():
                    errors.append(f"trades[{i}] 缺少 symbol")
    return errors


# ---------------------------------------------------------------------------
# equity 兜底：trader 回执留空 equity=None 时，下游 push 资金段无从取数。
# 缺失时从 account.db 最新快照只读回填；快照过旧（>20min）
# 或任何读取失败一律保持 None——兜底绝不阻塞写库。
# ---------------------------------------------------------------------------
EQUITY_FALLBACK_MAX_AGE_SEC = 20 * 60


def _equity_snapshot_fallback(profile: str) -> Optional[tuple]:
    """从 account.db account_snapshots 按 profile 取最新快照 equity。

    返回 (equity, snapshot_ts)；快照过旧/缺行/读取失败 → None（fail-safe）。
    最新行按 rowid DESC（ts 是 TEXT，MAX(ts) 词典序不可靠）。
    """
    if profile not in DB_MAP:
        return None
    try:
        acc_path = os.environ.get("OKX_ACCOUNT_DB", _project_path('db', 'account.db'))
        acc = sqlite3.connect(f"file:{acc_path}?mode=ro", uri=True, timeout=5)
        try:
            acc.execute("PRAGMA busy_timeout=5000;")
            row = acc.execute(
                "SELECT ts, totalEq FROM account_snapshots WHERE profile=? "
                "ORDER BY rowid DESC LIMIT 1",
                (profile,)).fetchone()
        finally:
            acc.close()
        if not row or row[1] is None:
            return None
        snap_ts = normalize_ts(str(row[0]))
        snap_dt = dt.strptime(snap_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
        age = (dt.now(CST) - snap_dt).total_seconds()
        if age < -60 or age > EQUITY_FALLBACK_MAX_AGE_SEC:
            return None
        return float(row[1]), snap_ts
    except Exception as e:  # noqa: BLE001 —— 兜底失败非致命，保持 equity=None
        sys.stderr.write(f"[trades_writer][WARN] equity 快照兜底跳过（非致命）: {e}\n")
        return None


# ---------------------------------------------------------------------------
# analysis 上下文只读接线。decision_card_v1 回填 decision_card；兼容格式才回填 score/confidence。
# fail-safe：任何异常返回 {} → 保 NULL + WARN，绝不阻塞写库；每次 write_trades
# 至多开一次 ro 连接（单条索引查询，结果整 cycle 复用）。
# ---------------------------------------------------------------------------
try:
    from analyst_writer import normalize_symbol as _norm_symbol  # 同目录 collectors/
except Exception:  # noqa: BLE001 —— import 失败用本地等价实现（与 analyst_writer 同逻辑）
    def _norm_symbol(sym: str) -> str:
        s = str(sym).strip().upper()
        if s.endswith("-USDT-SWAP"):
            return s
        if s.endswith("-USDT"):
            return s + "-SWAP"
        return s + "-USDT-SWAP"


def _analysis_context_for_cycle(cycle_id: str) -> dict:
    """{symbol: {total, confidence, decision_card}}；异常返回空（非致命）。"""
    ana_path = os.environ.get("OKX_ANALYSIS_DB", _project_path('db', 'analysis.db'))
    out: dict = {}
    try:
        ana = sqlite3.connect(f"file:{ana_path}?mode=ro", uri=True, timeout=5)
        try:
            ana.execute("PRAGMA busy_timeout=5000;")
            for sym, total, conf, card_raw in ana.execute(
                    "SELECT symbol, total, confidence, decision_card FROM analysis_signals "
                    "WHERE cycle_id=?", (cycle_id,)):
                try:
                    card = json.loads(card_raw) if card_raw else None
                except (json.JSONDecodeError, TypeError):
                    card = None
                out[_norm_symbol(str(sym))] = {
                    "total": total,
                    "confidence": conf,
                    "decision_card": card,
                }
        finally:
            ana.close()
    except Exception as e:  # noqa: BLE001 —— 回填源读不到保 NULL，绝不阻塞写库
        sys.stderr.write(
            f"[trades_writer][WARN] analysis_signals 回填源读取失败（保 NULL）: {e}\n")
        return {}
    return out


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------
def write_trades(data: dict, db_path: Path) -> dict:
    cycle_id = data["cycle_id"]
    completed_at = normalize_ts(data.get("ts") or dt.now().strftime("%Y-%m-%d %H:%M:%S"))
    mode = data.get("_profile", "full")  # 由 main() 注入 --profile 值（live|demo）
    incoming_trades = [t for t in (data.get("trades") or [])
                       if isinstance(t, dict) and t.get("action", "none") != "none"]
    # 顶层缺 decision/n_orders → 从 action_taken/trades 推导；已有值不覆盖
    raw_decision = data.get("decision")
    if raw_decision is None or not str(raw_decision).strip():
        decision = derive_cycle_decision(data.get("action_taken"), incoming_trades)
    else:
        decision = normalize_decision(raw_decision)
    n_orders = data.get("n_orders")
    if n_orders is None:
        n_orders = len(incoming_trades)
    equity = data.get("equity")
    equity_fallback_mark = None
    if equity is None:
        fb = _equity_snapshot_fallback(mode)
        if fb is not None:
            equity = fb[0]
            equity_fallback_mark = {"equity_source": "account_snapshot_fallback",
                                    "equity_source_ts": fb[1]}
    pnl_session = data.get("pnl_session", 0.0)
    pnl_open = data.get("pnl_open", 0.0)
    ana_context = _analysis_context_for_cycle(cycle_id)
    card_mode = (
        data.get("decision_protocol") == "decision_card_v1"
        or isinstance(data.get("decision_card"), dict)
        or any(v.get("decision_card") for v in ana_context.values())
    )
    confidence = None if card_mode else data.get("confidence")
    total_score = None if card_mode else data.get("total_score")
    # 兼容格式仅在 score/conf 缺失时回填；decision_card_v1 的评分列保持 NULL。
    ana_scores: dict = {}
    score_backfill_mark = None
    if not card_mode and (total_score is None or confidence is None
            or any(t.get("score_total") is None for t in incoming_trades)):
        ana_scores = ana_context
    if ana_scores and (total_score is None or confidence is None):
        pick = None
        for t in incoming_trades:  # 优先本轮成交 symbol 的信号行
            k = _norm_symbol(str(t.get("symbol") or ""))
            if k in ana_scores and ana_scores[k]["total"] is not None:
                pick = ana_scores[k]
                break
        if pick is None:  # hold/成交 symbol 无信号 → 本 cycle 最强信号（total 最大）
            cands = [v for v in ana_scores.values() if v["total"] is not None]
            pick = max(cands, key=lambda v: v["total"]) if cands else None
        if pick is not None:
            filled = []
            if total_score is None and pick["total"] is not None:
                total_score = pick["total"]
                filled.append("total_score")
            if confidence is None and pick["confidence"] is not None:
                confidence = pick["confidence"]
                filled.append("confidence")
            if filled:
                score_backfill_mark = {"score_source": "analysis_signals",
                                       "score_backfilled": filled}
    regime = data.get("regime")
    regime_stale = data.get("regime_stale", 0)
    note_parts = [data.get("action") or "", data.get("note") or ""]
    if regime:
        note_parts.append(f"regime={regime}" + ("_stale" if regime_stale else ""))
    if confidence is not None and not card_mode:
        note_parts.append(f"conf={confidence:.2f}")
    if pnl_open != 0.0:
        note_parts.append(f"open_pnl={pnl_open}")
    note = " | ".join(p for p in note_parts if p)
    raw_obj = data.get("raw") or data
    if isinstance(raw_obj, dict) and isinstance(data.get("decision_card"), dict):
        raw_obj = {
            **raw_obj,
            "decision_protocol": "decision_card_v1",
            "decision_card": data["decision_card"],
        }
    if equity_fallback_mark and isinstance(raw_obj, dict):
        raw_obj = {**raw_obj, **equity_fallback_mark}
    if score_backfill_mark and isinstance(raw_obj, dict):
        raw_obj = {**raw_obj, **score_backfill_mark}
    raw = json.dumps(raw_obj, ensure_ascii=False)[:100000]  # 截断防爆（§8.5：10KB→100KB，raw 作经验权威）

    con = connect(db_path)
    try:
        # 防降级覆盖闸：同 cycle 已有真实成交行（n_orders>0）时，禁止不带 trades
        # 的重写销毁成交账。traded→traded 幂等重写（重试带 trades）仍允许。
        # 拒绝时返回 ok:true+refused 标记
        # （账面状态良好，勿让 caller 当写失败重试/升级 P0）。
        old = con.execute(
            "SELECT decision, n_orders FROM trade_cycles WHERE cycle_id=?",
            (cycle_id,)).fetchone()
        if old and (old["n_orders"] or 0) > 0 and not incoming_trades:
            print(f"[trades_writer] REFUSE downgrade overwrite: cycle={cycle_id} "
                  f"existing(decision={old['decision']},n_orders={old['n_orders']}) "
                  f"incoming(decision={decision},trades=0) — kept existing row",
                  file=sys.stderr)
            return {"ok": True, "cycle_id": cycle_id,
                    "n_orders": old["n_orders"], "refused": "downgrade_overwrite",
                    "new_trades": []}
        # 合并保护：writer 契约=每 cycle 单份完整回执；同 cycle 已有成交行时再来
        # 一份“有单回执”，必须保护先前成交，不能整体替换。
        # 按身份键（优先 ordId，无则内容指纹）三分：
        #   新⊇旧   → 完整回执重发/扩充 → 保持替换（幂等重试语义不变）；
        #   完全不相交 → 增量回执 → 自动合并（B：旧行原样保留 + 新行追加）；
        #   部分重叠 → 含糊（分不清修正还是增量）→ 拒写（C：不猜，让调用方把该 cycle
        #             全部成交合并成一份完整回执后重写；绝不静默销账也绝不重复记账）。
        # new_trades=本次调用**真正新落**的成交行，经验挂钩只喂这些；
        # 重发或覆盖重写的既有行不再重喂经验库。默认=全部 incoming；covered 分支收敛为
        # "旧行匹配不到的新增行"；拒写分支为 []。
        new_trades: list = list(incoming_trades)
        merge_keep: list = []
        if incoming_trades:
            old_trades = con.execute(
                "SELECT ts, symbol, action, side, sz, fill_px, lev, margin, notional,"
                " score_total, reasoning, deviation, degradation, pnl, raw"
                " FROM trades WHERE cycle_id=?", (cycle_id,)).fetchall()
            if old_trades:
                # 双键逐对匹配：fp 回退仅在至多一侧有 ordId 时生效（_rows_match），
                # 防止兼容行无 ordId、新回执带 ordId 时把同一成交重复记账。
                old_keys = [_row_keys(r["symbol"], r["action"], r["side"],
                                      r["sz"], r["fill_px"],
                                      _extract_ordid(r["raw"]))
                            for r in old_trades]
                new_keys = [_row_keys(t.get("symbol"), t.get("action"),
                                      t.get("side"), t.get("sz"),
                                      t.get("fill_px"), _extract_ordid(t))
                            for t in incoming_trades]
                covered = all(any(_rows_match(o, n) for n in new_keys)
                              for o in old_keys)
                any_match = any(_rows_match(o, n)
                                for o in old_keys for n in new_keys)
                if covered:
                    # 新⊇旧：完整重发/扩充。new_trades 收敛为旧行匹配不到的净新增
                    new_trades = [t for t, k in zip(incoming_trades, new_keys)
                                  if not any(_rows_match(o, k) for o in old_keys)]
                elif not any_match:
                    merge_keep = list(old_trades)
                    n_orders = len(incoming_trades) + len(merge_keep)
                    decision = "traded"
                    note = (note + " | " if note else "") + \
                        f"[merge-guard: kept {len(merge_keep)} prior rows]"
                    if isinstance(raw_obj, dict):
                        raw_obj = {**raw_obj,
                                   "merge_guard_kept_rows": len(merge_keep)}
                        raw = json.dumps(raw_obj, ensure_ascii=False)[:100000]
                    print(f"[trades_writer] MERGE guard: cycle={cycle_id} "
                          f"incremental receipt disjoint with {len(merge_keep)} "
                          f"existing rows -> merged (kept prior rows)",
                          file=sys.stderr)
                else:
                    print(f"[trades_writer] REFUSE ambiguous merge: cycle={cycle_id} "
                          f"new receipt partially overlaps {len(old_trades)} existing "
                          f"rows -- cannot tell resend from increment; rewrite with "
                          f"ONE complete receipt containing ALL trades of this cycle",
                          file=sys.stderr)
                    return {"ok": False, "cycle_id": cycle_id,
                            "n_orders": (old["n_orders"] if old else None),
                            "refused": "ambiguous_merge", "new_trades": []}
        con.execute(
            "INSERT OR REPLACE INTO trade_cycles"
            "(cycle_id, ts, mode, decision, n_orders, equity, note, raw)"
            "VALUES (?,?,?,?,?,?,?,?)",
            (cycle_id, completed_at, mode, decision, n_orders, equity, note, raw),
        )

        # 删本 cycle 旧 trades（防止重跑覆盖；merge_keep 非空时旧行随后原样插回）
        con.execute("DELETE FROM trades WHERE cycle_id=?", (cycle_id,))
        orders_written = 0
        for r in merge_keep:  # B：合并保留的旧行原样插回（原 ts/字段不动）
            con.execute(
                "INSERT INTO trades"
                "(cycle_id, ts, symbol, action, side, sz, fill_px, lev,"
                " margin, notional, score_total, reasoning, deviation, degradation,"
                " pnl, raw)"
                "VALUES (?,?,?,?,?,?,?,?, ?,?,?,?,?,?, ?,?)",
                (cycle_id, r["ts"], r["symbol"], r["action"], r["side"], r["sz"],
                 r["fill_px"], r["lev"], r["margin"], r["notional"],
                 r["score_total"], r["reasoning"], r["deviation"],
                 r["degradation"], r["pnl"], r["raw"]))
            orders_written += 1
        # 行内 reasoning 缺失可从顶层 reasoning/reason 回填
        top_reason = data.get("reasoning") or data.get("reason")
        for t in incoming_trades:
            symbol = t.get("symbol")
            missing_meta = []
            ctx = ana_context.get(_norm_symbol(str(symbol or ""))) or {}
            effective_card = (
                data.get("decision_card")
                or t.get("decision_card")
                or ctx.get("decision_card")
            )
            if card_mode and isinstance(effective_card, dict):
                t["decision_card"] = effective_card
            # reasoning/raw 缺失不拒写（拒写丢真成交更糟），WARN 供巡检。
            # order_executor 行级 trade dict 使用 `reason`，这里保留 reasoning/reason 双键读取。
            reasoning = t.get("reasoning") or t.get("reason")
            if reasoning is None or not str(reasoning).strip():
                if isinstance(top_reason, str) and top_reason.strip():
                    reasoning = top_reason.strip()
                    missing_meta.append("reasoning(已回填顶层)")
                else:
                    missing_meta.append("reasoning")
            row_raw = t.get("raw")
            if row_raw is None:
                # 行未带 raw → 序列化整行 trade dict 兜底（保 SL/fill_source 等可核）
                missing_meta.append("raw(已存行dict)")
                row_raw_obj = t
            elif not isinstance(row_raw, str):
                row_raw_obj = row_raw
            else:
                try:
                    parsed_raw = json.loads(row_raw)
                    row_raw_obj = parsed_raw if isinstance(parsed_raw, dict) else {
                        "execution_raw": row_raw
                    }
                except (json.JSONDecodeError, TypeError):
                    row_raw_obj = {"execution_raw": row_raw}
            if card_mode and isinstance(effective_card, dict) and isinstance(row_raw_obj, dict):
                row_raw_obj = {
                    **row_raw_obj,
                    "decision_card": effective_card,
                    "decision_protocol": "decision_card_v1",
                }
            row_raw = json.dumps(row_raw_obj, ensure_ascii=False)[:20000]
            # margin/notional 缺失按 ctVal 公式补算（已有值不覆盖）
            margin = t.get("margin")
            notional = t.get("notional")
            if margin is None or notional is None:
                px = _as_pos_float(t.get("fill_px"))
                sz = _as_pos_float(t.get("sz"))
                lev = _as_pos_float(t.get("lev"))
                if px is not None and sz is not None and symbol:
                    # 2026-07-07: 优先行内 ct_val（executor 回执带本环境真值——demo
                    # 分列合约与缓存的 live 口径可差 100x）；无行内值才查缓存
                    row_ctval = _as_pos_float(t.get("ct_val"))
                    base = px * sz * (row_ctval if row_ctval is not None
                                      else _ctval_for(str(symbol)))
                    if notional is None:
                        notional = base
                    if margin is None:
                        if lev is not None:
                            margin = base / lev
                        else:
                            missing_meta.append("margin(缺lev未补算)")
                else:
                    missing_meta.append("margin/notional(缺fill_px/sz未补算)")
            # 兼容格式：行级 score_total 缺失 → 该 symbol 的 signals.total → 顶层
            # total_score。注意兼容实现
            # t.get("score_total", total_score) 对「键存在值为 null」不兜底——此处一并修。
            row_score = None if card_mode else t.get("score_total")
            if row_score is None and not card_mode:
                sig_tc = ana_scores.get(_norm_symbol(str(symbol or "")))
                if sig_tc is not None and sig_tc["total"] is not None:
                    row_score = sig_tc["total"]
                    t["score_total"] = row_score  # 传导给 write_experiences（row_raw 已定格不受影响）
                    missing_meta.append("score_total(已回填signals)")
                elif total_score is not None:
                    row_score = total_score
                    t["score_total"] = row_score
                    missing_meta.append("score_total(已回填顶层)")
                else:
                    missing_meta.append("score_total(无源保NULL)")
            if missing_meta:
                sys.stderr.write(
                    f"[trades_writer][WARN] trades 行元数据缺失: cycle={cycle_id} "
                    f"symbol={symbol} 缺={','.join(missing_meta)}\n")
            con.execute(
                "INSERT INTO trades"
                "(cycle_id, ts, symbol, action, side, sz, fill_px, lev,"
                " margin, notional, score_total, reasoning, deviation, degradation,"
                " pnl, raw)"
                "VALUES (?,?,?,?,?,?,?,?, ?,?,?,?,?,?, ?,?)",
                (
                    cycle_id,
                    completed_at,
                    symbol,
                    t.get("action", "none"),
                    t.get("side"),
                    t.get("sz"),
                    t.get("fill_px"),
                    t.get("lev"),
                    margin,
                    notional,
                    row_score,
                    reasoning,
                    t.get("deviation"),
                    t.get("degradation"),
                    t.get("pnl"),
                    row_raw,
                ),
            )
            orders_written += 1

        con.commit()
    finally:
        con.close()

    # 兼容回填值传导给经验挂钩：main() 随后调 write_experiences(data,...) 读同一
    # data dict（取 data['confidence']/data['total_score']）。放在 commit 之后：
    # trade_cycles.raw / merge 路径 raw 均已定格为回执原文，mutation 不回写账面。
    if score_backfill_mark and not card_mode:
        data["total_score"] = total_score
        data["confidence"] = confidence
    return {"ok": True, "cycle_id": cycle_id, "n_orders": orders_written,
            "new_trades": new_trades}


# ---------------------------------------------------------------------------
# V2.0 §8.5 经验库挂钩——trade_experiences 写在 account.db（跨库，故独立事务、非致命）
# 现役 trader 统一走 order_executor→trades_writer，成交行写入后再以独立事务补经验。
# 本挂钩把经验写接到 V2.0 真实写库路径（trades_writer），live+demo 自然统一。
# 红线：交易记录（trade_cycles/trades）已先 commit；经验写失败只记 stderr，绝不阻塞实盘记录。
# ---------------------------------------------------------------------------
def _derive_action_taken(t: dict) -> Optional[str]:
    """逐笔从 trade 的 action+side 推 action_taken（经验库词汇 OPEN_LONG/…/CLOSE/REDUCE）。"""
    a = str(t.get("action", "none")).lower().strip()
    side = str(t.get("side", "")).lower().strip()
    if a == "open":
        return "OPEN_LONG" if side in ("long", "buy", "open_long") else "OPEN_SHORT"
    if a == "add":
        return "ADD"
    if a == "close":
        return "CLOSE"
    if a == "reduce":
        return "REDUCE"
    if a in ("stop_loss", "stop", "sl"):
        return "STOP_LOSS"
    return None  # none / 未知 → 不写经验


def write_experiences(data: dict, profile: str, now_ts: Optional[str]) -> dict:
    """把本轮成交逐笔写进 account.db.trade_experiences（经验库，§8.5）。

    非致命：trade DB 与 account.db 跨库无法同事务，交易记录已先落库；此处失败只记 stderr。
    表不存在（迁移前）→ 安全跳过。开仓插 open 行、平仓 UPDATE 匹配 open 行补 pnl。
    """
    trades = [t for t in (data.get("trades") or [])
              if isinstance(t, dict) and t.get("action", "none") != "none"]
    if not trades:
        return {"exp": 0}
    # 与 write_trades 的 completed_at 缺省口径一致。JSON 回执可不带 ts；若把 None
    # 传给历史 close 的“ts<=平仓时刻”匹配，会错误落入 fallback 而不闭合真实 open。
    now_ts = normalize_ts(now_ts or dt.now().strftime("%Y-%m-%d %H:%M:%S"))
    try:
        if _project_path('scripts') not in sys.path:
            sys.path.insert(0, _project_path('scripts'))
        import trade_experience_writer as _tew  # noqa: E402
        acc_path = os.environ.get("OKX_ACCOUNT_DB", _project_path('db', 'account.db'))
        acc = sqlite3.connect(acc_path, timeout=10)
        acc.execute("PRAGMA busy_timeout=5000;")
        try:
            if not _tew.table_exists(acc):
                return {"exp": 0, "note": "no trade_experiences table"}
            n = 0
            for t in trades:
                at = _derive_action_taken(t)
                if at is None:
                    continue
                is_card = (
                    data.get("decision_protocol") == "decision_card_v1"
                    or isinstance(t.get("decision_card"), dict)
                )
                payload = {
                    "cycle_id": data.get("cycle_id"),
                    "profile": profile,
                    "action_taken": at,
                    "regime": data.get("regime"),
                    "regime_stale": data.get("regime_stale", 0),
                    "confidence": None if is_card else data.get("confidence"),
                    "total_score": None if is_card else data.get("total_score"),
                    "market_snapshot": data.get("market_snapshot"),
                    "hypothesis_id": data.get("hypothesis_id"),
                    "playbook_ref": data.get("playbook_ref"),
                    "trades": [t],
                }
                _tew.insert_or_update_experiences(acc, payload, data.get("cycle_id"), now_ts)
                n += 1
            acc.commit()
            return {"exp": n}
        finally:
            acc.close()
    except Exception as e:  # noqa: BLE001 —— 经验写非致命，绝不阻塞交易记录
        sys.stderr.write(f"[trades_writer][WARN] 经验库写入跳过（非致命）: {e}\n")
        return {"exp": 0, "error": str(e)}


# ---------------------------------------------------------------------------
# journal 重放：order_executor 执行 journal → 补写未入账成交
# ---------------------------------------------------------------------------
def _slot_floor(ts: str) -> Optional[str]:
    """'YYYY-MM-DD HH:MM[:SS]' → 15min 槽 'YYYY-MM-DDTHH:MM'（cycle 兜底归因）。"""
    try:
        d = dt.strptime(str(ts)[:16], "%Y-%m-%d %H:%M")
        return f"{d:%Y-%m-%dT%H}:{(d.minute // 15) * 15:02d}"
    except (ValueError, TypeError):
        return None


def _attribute_cycle(profile: str, rec_ts: str, ledger_db: Path) -> Optional[str]:
    """stage_dispatch 归因：成交时刻前最近一次该盘派发对应的 cycle（45min 内才可信——
    trader 会话时长上界；更早的派发不是这笔成交的来源，回退槽地板）。fail-safe 返 None。"""
    try:
        if not ledger_db.exists():
            return None
        con = sqlite3.connect(f"file:{ledger_db.as_posix()}?mode=ro", uri=True, timeout=8)
        try:
            row = con.execute(
                "SELECT cycle_id, dispatched_at FROM stage_dispatch "
                "WHERE stage=? AND dispatched_at<=? "
                "ORDER BY dispatched_at DESC LIMIT 1",
                (profile, rec_ts)).fetchone()
        finally:
            con.close()
        if not row:
            return None
        d0 = dt.strptime(str(row[1])[:19], "%Y-%m-%d %H:%M:%S")
        d1 = dt.strptime(str(rec_ts)[:19], "%Y-%m-%d %H:%M:%S")
        if (d1 - d0).total_seconds() > 45 * 60:
            return None
        return row[0]
    except Exception:
        return None


def _journal_consumed(con, rec: dict) -> bool:
    """journal 记录是否已入账：带 ordId 精确查（存于行 raw JSON，子串即中——19位唯一号
    碰撞可忽略）；无 ordId 用 symbol/action/side/sz 近似。近似过匹配=不重放，方向保守安全。"""
    t = rec.get("trade") or {}
    oid = _extract_ordid(t)
    if oid:
        n = con.execute("SELECT COUNT(*) FROM trades WHERE raw LIKE ?",
                        (f"%{oid}%",)).fetchone()[0]
        return n > 0
    try:
        sz = float(t.get("sz"))
    except (TypeError, ValueError):
        return True  # 无 ordId 又无 sz：无从匹配，视作已入账（禁盲目重放）
    n = con.execute(
        "SELECT COUNT(*) FROM trades WHERE symbol=? AND action=? AND side=? "
        "AND ABS(COALESCE(sz,0)-?) < 1e-6",
        (t.get("symbol"), t.get("action"), t.get("side"), sz)).fetchone()[0]
    return n > 0


def replay_from_journal(args) -> int:
    """--from-journal 主流程：读 JSONL → 滤 profile/TEST-/已入账 → 按 cycle 归因分组
    （记录内 cycle_id → stage_dispatch 归因 → 槽地板）→ 经 write_trades 合并闸补写。

    刻意不 nudge dispatcher：重放对象多为陈旧 cycle；若仍在回扫窗（~75min）内，
    cron tick 自会补派 push（迟到播报=设计行为，幽灵轮本就漏报）。"""
    jpath = Path(args.from_journal)
    if not jpath.exists():
        print(json.dumps({"ok": False, "error": f"journal 不存在: {jpath}"}, ensure_ascii=False))
        return 1
    db_path = DB_MAP.get(args.profile)
    if not db_path or not db_path.exists():
        print(json.dumps({"ok": False, "error": f"DB 不存在: {db_path}"}, ensure_ascii=False))
        return 1
    recs = []
    bad_lines = 0  # 核验修：撕裂/坏行必须计数外显——静默跳过=真实成交从安全网里无声消失
    with open(jpath, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                bad_lines += 1
                continue
            if not isinstance(rec, dict) or not isinstance(rec.get("trade"), dict):
                bad_lines += 1
                continue
            if rec.get("profile") != args.profile:
                continue
            if args.ordid and _extract_ordid(rec["trade"]) != str(args.ordid):
                continue
            if not args.ordid and str(rec.get("cycle_id") or "").startswith("TEST-"):
                continue  # 微单测试记录默认不重放（显式 --ordid 才碰）
            if not args.ordid and rec.get("unwind"):
                # 核验修：unwind close（SL 失败平裸仓）回执 trades=[]、账本无对应 open——
                # 直接重放=close-without-open 净仓错向。默认不重放（plan 里标 unwind 供
                # 监控 P1 人工），显式 --ordid 才碰（人工核实后 FOGO 式补账由主人决定）。
                continue
            recs.append(rec)
    con = connect(db_path)
    try:
        pending = [r for r in recs if not _journal_consumed(con, r)]
    finally:
        con.close()
    if not pending:
        print(json.dumps({"ok": True, "replayed": 0, "bad_lines": bad_lines,
                          "note": "journal 无未入账记录"}, ensure_ascii=False))
        return 0
    ledger_db = db_path.parent / "ledger.db"
    groups: dict[str, list] = {}
    for r in pending:
        cyc = (r.get("cycle_id")
               or _attribute_cycle(args.profile, str(r.get("ts") or ""), ledger_db)
               or _slot_floor(str(r.get("ts") or "")))
        if cyc:
            groups.setdefault(cyc, []).append(r)
    if args.replay_dry_run:
        # 核验修：plan 的 ordId 字段=trade 内**严格** ordId（不经 _extract_ordid——那会把
        # recovered_timeout 的 algoId 顶成身份，让监控层"无 ordId 一律 P1 人工"守卫变死代码；
        # 消费判定仍用 _extract_ordid 广口径）。unwind 标记透传供监控分级。
        plan = {cyc: [{"ordId": r["trade"].get("ordId") or None,
                       "symbol": r["trade"].get("symbol"),
                       "action": r["trade"].get("action"),
                       "sz": r["trade"].get("sz"), "ts": r.get("ts"),
                       "unwind": bool(r.get("unwind"))}
                      for r in rs] for cyc, rs in groups.items()}
        print(json.dumps({"ok": True, "dry_run": True,
                          "would_replay": sum(len(v) for v in groups.values()),
                          "bad_lines": bad_lines, "plan": plan}, ensure_ascii=False))
        return 0
    results, all_ok = [], True
    for cyc, rs in sorted(groups.items()):
        data = {
            "cycle_id": cyc,
            "ts": max(str(r.get("ts") or "") for r in rs),
            "decision": "traded",
            "trades": [dict(r["trade"]) for r in rs],
            "note": f"journal-replay {len(rs)} row(s)",
            "raw": {"source": "journal_replay",
                    "journal_ms": [r.get("ms") for r in rs]},
            "_profile": args.profile,
        }
        # 核验修：cycle 已有头（trader 正常回执）→ 保留原 ts/equity/note/raw，重放只补
        # trades 行——禁用重放时刻的 equity/占位 raw 覆盖原始回执证据（trade_cycles 是
        # INSERT OR REPLACE，不保护即整头被清洗；对齐 reconcile apply 同款保护）。
        try:
            hcon = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=8)
            hcon.row_factory = sqlite3.Row
            old_hdr = hcon.execute(
                "SELECT ts, equity, note, raw FROM trade_cycles WHERE cycle_id=?",
                (cyc,)).fetchone()
            hcon.close()
        except Exception:
            old_hdr = None
        if old_hdr:
            data["ts"] = old_hdr["ts"] or data["ts"]
            data["equity"] = old_hdr["equity"]
            data["note"] = ((old_hdr["note"] + " | ") if old_hdr["note"] else "") + data["note"]
            prev_raw = None
            try:
                prev_raw = json.loads(old_hdr["raw"]) if old_hdr["raw"] else None
            except (ValueError, TypeError):
                prev_raw = None
            if isinstance(prev_raw, dict):
                data["raw"] = {**prev_raw,
                               "journal_replay_ms": [r.get("ms") for r in rs]}
            elif old_hdr["raw"]:
                data["raw"] = {"source": "journal_replay",
                               "journal_ms": [r.get("ms") for r in rs],
                               "prev_raw_str": str(old_hdr["raw"])[:80000]}
        errs = validate(data)
        if errs:
            results.append({"cycle_id": cyc, "ok": False, "error": "; ".join(errs)})
            all_ok = False
            continue
        res = write_trades(data, db_path)
        # 经验挂钩只喂真正新落的行；重放命中已有行时不重喂经验库。
        exp_trades = res.pop("new_trades", None)
        exp_data = data if exp_trades is None else {**data, "trades": exp_trades}
        exp = write_experiences(exp_data, args.profile, data.get("ts"))
        res["exp"] = exp.get("exp", 0)
        results.append(res)
        if not res.get("ok"):
            all_ok = False
    print(json.dumps({"ok": all_ok,
                      "replayed": sum(len(v) for v in groups.values()),
                      "bad_lines": bad_lines,
                      "results": results}, ensure_ascii=False))
    return 0 if all_ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="V2.0 trades writer")
    parser.add_argument("--stdin", action="store_true", help="从 stdin 读 JSON 回执")
    parser.add_argument("--json-file", type=str, help="从 UTF-8 文件读 JSON 回执（方案A：杜绝 echo 管道外层 shell GBK 编码坏码）")
    parser.add_argument("--cycle-id", type=str, help="cycle_id")
    parser.add_argument("--profile", type=str, choices=["live", "demo"], required=True)
    parser.add_argument("--decision", type=str, help="decision: traded|hold|skip|degraded|error")
    parser.add_argument("--n-orders", type=int, default=0, help="n_orders")
    parser.add_argument("--equity", type=float, help="equity")
    parser.add_argument("--note", type=str, help="note 字段")
    parser.add_argument("--ts", type=str, help="完成时刻（缺省=now）")
    parser.add_argument("--from-journal", type=str,
                        help="journal 重放：从 order_executor 执行 journal(JSONL) 补写未入账成交")
    parser.add_argument("--ordid", type=str, help="仅重放该订单号（--from-journal 模式）")
    parser.add_argument("--replay-dry-run", action="store_true",
                        help="只报告将重放的记录，不写库（--from-journal 模式）")
    args = parser.parse_args()

    if args.from_journal:
        return replay_from_journal(args)

    if args.json_file:
        # 方案A (2026-06-30): 从 UTF-8 文件读，根治 echo 管道经外层 shell 被 GBK 编码致
        # trade_cycles.note/raw 中文坏码（如「鍗囨寚绐佺牬」）。文件 IO 编码确定，无管道编码坑。
        try:
            with open(args.json_file, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except OSError as e:
            print(json.dumps({"ok": False, "error": f"读 --json-file 失败: {e}"}, ensure_ascii=False))
            return 1
        raw = re.sub(r"[\udc80-\udcff]", "?", raw)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            print(json.dumps({"ok": False, "error": f"JSON 解析失败: {e}"}, ensure_ascii=False))
            return 1
        if args.cycle_id:
            data["cycle_id"] = args.cycle_id
    elif args.stdin:
        # 二进制读取 + UTF-8 surrogatepass 解码，避免 PowerShell 管道破坏 UTF-8 bytes。
        raw_bytes = sys.stdin.buffer.read()
        if not raw_bytes.strip():
            print(json.dumps({"ok": False, "error": "stdin 为空"}, ensure_ascii=False))
            return 1
        try:
            raw = raw_bytes.decode("utf-8", errors="replace")
            # 清洗残留 surrogate 字符（U+D800-U+DFFF）
            raw = re.sub(r"[\udc80-\udcff]", "?", raw)
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            print(json.dumps({"ok": False, "error": f"JSON 解析失败: {e}"}, ensure_ascii=False))
            return 1
        # 覆盖 cycle_id / profile（命令行参数优先）
        if args.cycle_id:
            data["cycle_id"] = args.cycle_id
    else:
        if not args.cycle_id:
            print(json.dumps({"ok": False, "error": "需要 --cycle-id"}, ensure_ascii=False))
            return 1
        data = {
            "cycle_id": args.cycle_id,
            "ts": args.ts or dt.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": args.profile,
            "decision": args.decision or "hold",
            "n_orders": args.n_orders,
            "equity": args.equity,
            "note": args.note,
            "trades": [],
            "errors": [],
            "status": "ok",
        }

    errors = validate(data)
    if errors:
        print(json.dumps({"ok": False, "error": "; ".join(errors)}, ensure_ascii=False))
        return 1

    db_path = DB_MAP.get(args.profile)
    if not db_path or not db_path.exists():
        print(json.dumps({"ok": False, "error": f"DB 不存在: {db_path}"}, ensure_ascii=False))
        return 1

    data["_profile"] = args.profile  # 注入 profile 给 write_trades 用作 mode
    result = write_trades(data, db_path)
    # V2.0 §8.5: 经验库挂钩（非致命，交易记录已落库）。只喂 write_trades
    # 判定的真正新落行；重发、覆盖或拒写不再全量重喂。
    exp_trades = result.pop("new_trades", None)  # 喂完即剥：stdout 回执契约不带整段 trade dict
    exp_data = data if exp_trades is None else {**data, "trades": exp_trades}
    exp = write_experiences(exp_data, args.profile, data.get("ts"))
    result["exp"] = exp.get("exp", 0)
    print(json.dumps(result, ensure_ascii=False))
    # 真写入成功（result.ok 且非 refused）才 nudge。ambiguous_merge（ok:false）与
    # downgrade_overwrite（ok:true+refused，库无新变化）都不拍；既有行已触发过 nudge，
    # cron 同时保留兜底。
    # 必须在 write_experiences 与 print 之后：经验行先落、stdout 契约不动。
    if result.get("ok") and not result.get("refused") and _nudge_mod is not None:
        _nudge_mod.nudge(f"trades_writer:{args.profile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
