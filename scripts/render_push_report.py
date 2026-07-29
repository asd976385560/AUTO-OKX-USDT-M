# -*- coding: utf-8 -*-
"""render_push_report.py — V7.2 QQ 推送模板渲染器

输入：stdin / --json-file / --json 的结构化 JSON。
输出：JSON {ok,title,content,char_count,sections}。

用途：禁止由 Agent 自由拼 Markdown，必须先组装结构化 JSON，
再由本脚本固定渲染，降低长 session 运行后的模板漂移。

权威回读覆盖（2026-07-02/04）：以下字段由库权威覆盖 agent 传值，agent 传值仅作回退——
  - 轮次 cycle_count ← ledger.stage_dispatch push 计数（传 9999 也被纠正）；
  - 资金 ← account_snapshots 按 profile 最新行（≤30min 新鲜才覆盖）；
  - 累计收益 ← cum_pnl.py 口径（冻结基线 + reset_ts 后 trades.pnl 增量）；
  - 持仓数 ← position_snapshots 最新批次行数。
每项独立回退：DB stale/不可用 → 保留 agent 值，渲染永不因回读失败中断。
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(
    _project_os.environ.get("OKX_ROOT")
    or _ProjectPath(__file__).resolve().parents[1]
).resolve()

def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")


# QQ 单条消息上限约 3700 字（主人 2026-06-26 实测）；留余量到 3500，超则裁剪 verbose 段。
# 完整报告始终落 reports/ 归档，QQ 只发可读摘要（决策依据/异常裁剪后标"详情见归档"）。
MAX_CONTENT_CHARS = 3500
DECISION_REASON_MAX = 1300
EXCEPTIONS_MAX = 800
SUMMARY_MAX = 250


def fail(message: str, code: int = 2) -> None:
    print(f"[render_push_report][FAIL] {message}", file=sys.stderr)
    sys.exit(code)


def sanitize_text(value: str) -> str:
    return value.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def read_stdin_text() -> str:
    if hasattr(sys.stdin, "buffer"):
        return sys.stdin.buffer.read().decode("utf-8", errors="replace")
    return sys.stdin.read()


def load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.stdin:
        raw = read_stdin_text()
    elif args.json_file:
        with open(args.json_file, "r", encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
    elif args.json:
        raw = args.json
    else:
        fail("缺少输入：需 --stdin / --json-file / --json 之一")
    try:
        payload = json.loads(sanitize_text(raw))
    except Exception as exc:
        fail(f"输入 JSON 解析失败: {exc}")
    if not isinstance(payload, dict):
        fail("输入 JSON 顶层必须是对象")
    return payload


def section(payload: dict[str, Any], name: str) -> dict[str, Any]:
    value = payload.get(name, {})
    return value if isinstance(value, dict) else {}


def list_section(payload: dict[str, Any], name: str) -> list[Any]:
    value = payload.get(name, [])
    return value if isinstance(value, list) else []


def value_at(mapping: dict[str, Any], keys: list[str], default: Any = "-") -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return default


def money(value: Any, default: str = "-") -> str:
    try:
        return f"{float(value):.2f}"
    except Exception:
        return str(value) if value not in (None, "") else default


def pct(value: Any, default: str = "-") -> str:
    try:
        return f"{float(value):.4f}"
    except Exception:
        return str(value) if value not in (None, "") else default


def num_only(value: Any, default: str = "0") -> str:
    """提取首个数字串，兼容带 profile 标签的历史归档值。"""
    import re as _re
    if value in (None, ""):
        return default
    m = _re.search(r"-?\d+(?:\.\d+)?", str(value))
    return m.group(0) if m else default


def strip_pct(value: Any, default: str = "-") -> str:
    """去掉末尾 %，防止模板再加 % 形成 32%%（K3）。"""
    if value in (None, ""):
        return default
    return str(value).rstrip("%％").strip()


def with_pct(value: Any, default: str = "-") -> str:
    """有效数→'值%'；占位/空→'-'（不留裸 % 形成 -%）。K3 空值补丁 2026-06-13。"""
    s = strip_pct(value, default)
    return s if s in ("-", default, "") else f"{s}%"


def text(value: Any, default: str = "-") -> str:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def first_nonempty(*values: Any, default: str = "-") -> str:
    for candidate in values:
        if candidate not in (None, ""):
            return text(candidate)
    return default


def clip(value: Any, limit: int, tail: str = "…（详情见归档）") -> str:
    """裁剪长文到 limit 字以内，超出补尾标。用于把 verbose 段压进 QQ 单条上限。"""
    s = text(value)
    if len(s) <= limit:
        return s
    return s[: max(0, limit - len(tail))].rstrip() + tail


def card_text(value: Any, limit: int = 150) -> str:
    """Compact one decision-card field without exposing raw JSON blobs."""
    if isinstance(value, str):
        raw = value
    elif isinstance(value, list):
        raw = "；".join(
            str(
                item.get("summary")
                or item.get("evidence")
                or item.get("reason")
                or item
            )
            if isinstance(item, dict)
            else str(item)
            for item in value
        )
    elif isinstance(value, dict):
        raw = "；".join(
            f"{key}={item}"
            for key, item in value.items()
            if item not in (None, "", [], {})
        )
    else:
        raw = str(value or "-")
    return clip(" ".join(raw.replace("\r", " ").replace("\n", " ").split()), limit)


def _short_symbol(value: Any) -> str:
    """'LAB-USDT-SWAP' → 'LAB'：去合约后缀，供标题短显示。"""
    s = str(value or "").strip()
    up = s.upper()
    for suffix in ("-USDT-SWAP", "-USDC-SWAP", "-USD-SWAP", "-USDT", "-USDC", "-USD"):
        if up.endswith(suffix):
            return s[: -len(suffix)]
    return s


def _iter_trades(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """payload.trades 展开为逐笔 list：兼容 {live:[...],demo:[...]} 与 list
    两种形态，dict 形态各笔补 profile；任何异常返回 []（fail-safe，不影响主渲染）。"""
    try:
        raw = payload.get("trades")
        out: list[dict[str, Any]] = []
        if isinstance(raw, dict):
            for prof in ("live", "demo"):
                items = raw.get(prof)
                if isinstance(items, list):
                    for it in items:
                        if isinstance(it, dict):
                            rec = dict(it)
                            rec.setdefault("profile", prof)
                            out.append(rec)
        elif isinstance(raw, list):
            out = [it for it in raw if isinstance(it, dict)]
        return out
    except Exception:
        return []


def _normalize_positions(value: Any) -> list[dict[str, Any]]:
    """positions 形态兼容：dict {live:[...],demo:[...]} 合并展开为 list（各项补 profile）；
    list 原样过滤非 dict 项；异常返回 []。"""
    try:
        out: list[dict[str, Any]] = []
        if isinstance(value, dict):
            for prof in ("live", "demo"):
                items = value.get(prof)
                if isinstance(items, list):
                    for it in items:
                        if isinstance(it, dict):
                            rec = dict(it)
                            rec.setdefault("profile", prof)
                            out.append(rec)
        elif isinstance(value, list):
            out = [it for it in value if isinstance(it, dict)]
        return out
    except Exception:
        return []


def _fallback_symbol(payload: dict[str, Any], positions: list[Any]) -> str:
    """标题 symbol 回退链：trades 各笔 symbol → positions；
    去后缀去重、'/'拼接、最多 3 个。全空返回 ''（调用方保持 'UNKNOWN' 现行为）。"""
    names: list[str] = []

    def _push(v: Any) -> None:
        s = _short_symbol(v)
        if s and s != "__FLAT__" and s not in names:
            names.append(s)

    for rec in _iter_trades(payload):
        _push(rec.get("symbol") or rec.get("instId"))
    if not names:
        for pos in positions:
            if isinstance(pos, dict):
                _push(pos.get("symbol") or pos.get("instId"))
    return "/".join(names[:3])


def _numeric_field(value: Any, default: str = "-") -> str:
    """行情数值字段防御：btc/eth 等价格字段收到 dict 时
    → 提取 price/last 等键或首个数字，杜绝 JSON 字面量直出；标量保持原 text() 行为。"""
    try:
        if isinstance(value, dict):
            for k in ("price", "last", "px", "close", "value"):
                if value.get(k) not in (None, ""):
                    return text(value[k])
            n = num_only(json.dumps(value, ensure_ascii=False), default="")
            return n if n else default
        return text(value, default) if value not in (None, "") else default
    except Exception:
        return default


def format_position(position: dict[str, Any]) -> str:
    symbol = first_nonempty(position.get("symbol"), position.get("instId"), default="UNKNOWN")
    # 2026-07-15 主人要求：side 显中文多/空（原英文 LONG/SHORT 不醒目）；未知值保底原样大写。
    _side_raw = first_nonempty(position.get("side"), position.get("posSide"), default="-")
    side = {"long": "多", "short": "空"}.get(str(_side_raw).lower(), str(_side_raw).upper())
    size = first_nonempty(position.get("sz"), position.get("size"), default="-")
    avg_price = first_nonempty(position.get("avgPx"), position.get("avg_px"), position.get("avg_price"), position.get("entry_px"), default="-")
    leverage = first_nonempty(position.get("lev"), position.get("leverage"), default="-")
    try:
        leverage = f"{float(leverage):g}"  # 10.0 → 10（显示去尾零）
    except (TypeError, ValueError):
        pass
    hold_min = first_nonempty(position.get("hold_min"), position.get("holding_minutes"), default="-")
    # 持有时长友好显示（2026-07-07）：<90min 显 'Nmin'，否则 'Xh Ym'（多小时持仓 '446min' 不友好）。
    # 非数值/缺失 → '-'（不再拼裸 '-min'）。
    if isinstance(hold_min, bool):
        _hm = None
    elif isinstance(hold_min, (int, float)):
        _hm = int(hold_min)
    elif isinstance(hold_min, str) and hold_min.strip().lstrip("-").isdigit():
        _hm = int(hold_min)
    else:
        _hm = None
    if _hm is None:
        hold_disp = "持有-" if str(hold_min).strip() in ("-", "") else f"持有{hold_min}min"
    elif _hm < 90:
        hold_disp = f"持有{_hm}min"
    else:
        hold_disp = f"持有{_hm // 60}h{_hm % 60:02d}m"
    upl = first_nonempty(position.get("upl"), position.get("unrealized_pnl"), default="-")
    stop_distance = first_nonempty(position.get("sl_pct"), position.get("stop_distance_pct"), default="")
    profile = first_nonempty(position.get("profile"), position.get("channel"), default="-")
    # L2 (2026-06-14): SL 距离取绝对值——LONG 仓 sl<mark 算出负号无意义（"SL距-3.6%"误导），
    # 距离本身是无符号量，方向由 side 已表达。
    if str(stop_distance) not in ("", "-"):
        _sd = strip_pct(stop_distance)
        try:
            _v = abs(float(_sd))
            # N3 (2026-06-14): SL 距离 >30% 几乎必是填值错误（sl_pct 口径混乱），标注核对而非显示误导值。
            sl_txt = f"SL距{_v:.1f}%" if _v <= 30 else f"SL距{_v:.0f}%(值异常,核对slTriggerPx)"
        except (TypeError, ValueError):
            sl_txt = f"SL距{_sd}%"
    else:
        sl_txt = "SL未挂"
    # 2026-07-15 主人要求：补保证金 USD + 占净值%（payload margin_usd/margin_pct，
    # build_push_payload 按 sz×ctVal×avgPx÷lev 算）；缺失静默省略该字段。
    _mu, _mp = position.get("margin_usd"), position.get("margin_pct")
    if isinstance(_mu, (int, float)):
        margin_txt = f"保证金≈${_mu}" + (f"/{_mp}%净值" if isinstance(_mp, (int, float)) else "") + " | "
    else:
        margin_txt = ""
    return f"{profile} {symbol} {side} {size}张 @{avg_price} {leverage}x | {margin_txt}{hold_disp} | 浮盈{upl} | {sl_txt}"


def format_positions(positions: list[Any]) -> str:
    lines = []
    for item in positions:
        if not isinstance(item, dict):
            continue
        # __FLAT__ 是 position_snapshots 真实空仓标记，绝不渲染进持仓详情。
        if str(item.get("symbol") or item.get("instId") or "").strip() == "__FLAT__":
            continue
        lines.append(format_position(item))
    return "\n".join(lines) if lines else "空仓"


# 异常段允许的运行故障关键词（命中其一=真异常，保留）。
# 未命中的条目=行情/风控判断，移出异常段（它们属于决策依据）。
_RUNTIME_FAULT_KEYWORDS = (
    "stale", "degraded", "timeout", "error", "failed", "fail",
    "missing", "empty", "pending", "p0", "p1",
    "writer", "api", "collector", "dispatch", "regime_latency",
    "backfill", "stale ", "reset", "regression", "0 bytes",
    "partial", "skipped", "abort",
)

# 明确排除的关键词——即使命中上面的关键词，如果同时命中这些则归为决策依据
_DECISION_KEYWORDS = (
    "dxy extreme", "pb-354", "pb-373", "regime_suppression",
    "funding 极值", "funding极值", "whale", "鲸鱼", "卖压",
    "bearish", "bullish", "chain risk", "squeeze",
    "score", "conf", "calibration", "playbook",
)


def _is_runtime_fault(item: Any) -> bool:
    """判断异常条目是否为运行故障（True=保留在异常段，False=移到决策依据）。"""
    if isinstance(item, dict):
        name = text(first_nonempty(item.get("name"), item.get("type"), item.get("check"), default=""))
        status = text(first_nonempty(item.get("status"), item.get("level"), default=""))
        detail = text(first_nonempty(item.get("detail"), item.get("message"), item.get("fix"), default=""))
        combined = f"{name} {status} {detail}".lower()
    else:
        combined = text(item).lower()

    # 先检查是否为明确的决策依据关键词
    for kw in _DECISION_KEYWORDS:
        if kw in combined:
            return False
    # 再检查是否命中运行故障关键词
    for kw in _RUNTIME_FAULT_KEYWORDS:
        if kw in combined:
            return True
    # 未命中任何关键词 → 默认归为决策依据（保守策略：宁可不报也不误报）
    return False


def filter_exceptions(exceptions: list[Any]) -> tuple[list[Any], list[Any]]:
    """把异常列表分成 (运行故障, 决策依据) 两组。

    运行故障保留在异常段；决策依据条目返回给调用方追加到决策依据段。
    """
    faults: list[Any] = []
    decisions: list[Any] = []
    for item in exceptions:
        if _is_runtime_fault(item):
            faults.append(item)
        else:
            decisions.append(item)
    return faults, decisions


def format_exceptions(exceptions: list[Any]) -> str:
    if not exceptions:
        return "无"
    lines = []
    for item in exceptions:
        if isinstance(item, dict):
            name = first_nonempty(item.get("name"), item.get("type"), item.get("check"), default="异常")
            status = first_nonempty(item.get("status"), item.get("level"), default="-")
            detail = first_nonempty(item.get("detail"), item.get("message"), item.get("fix"), default="-")
            lines.append(f"{name} [{status}] {detail}")
        else:
            lines.append(text(item))
    return "\n".join(lines)


def qq_markdown_hardbreak(content: str) -> str:
    """Add Markdown hard breaks so QQ format=3 does not fold soft line breaks."""
    lines = content.splitlines()
    return "\n".join(f"{line}  " if line.strip() else "" for line in lines).rstrip() + "\n"


LEDGER_DB = _project_path('db', 'ledger.db')
ACCOUNT_DB = _project_path('db', 'account.db')
DB_ROOT = _project_path('db')
SNAPSHOT_FRESH_MIN = 30


def _ts_age_minutes(ts: Any) -> float | None:
    """快照 ts → 距今分钟数；解析失败 None。兼容偶发 UTC-Z 混入（latent 脚雷，归一到 UTC+8）。"""
    s = str(ts or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            dt = (datetime.fromisoformat(s.replace("Z", "+00:00"))
                  .astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None))
        else:
            dt = datetime.fromisoformat(s.replace("T", " ", 1)[:19])
        return (datetime.now() - dt).total_seconds() / 60.0
    except Exception:
        return None


def authoritative_equity(profile: str) -> float | None:
    """资金权威（2026-07-04）：account_snapshots 按 profile 最新行（rowid DESC，禁 MAX(ts)）。

    ts 距今 ≤SNAPSHOT_FRESH_MIN 分钟才覆盖 agent 传值（#588 事故：agent 把 demo totalEq
    73275.01 填进实盘 current_equity 直接上屏）。stale/不可用 → None（回退 agent 值），
    渲染不因回读失败。
    """
    try:
        con = sqlite3.connect(f"file:{ACCOUNT_DB}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT ts, totalEq FROM account_snapshots "
                "WHERE profile=? ORDER BY rowid DESC LIMIT 1", (profile,)).fetchone()
        finally:
            con.close()
        if not row or row[1] is None:
            return None
        age = _ts_age_minutes(row[0])
        if age is None or not (-5 <= age <= SNAPSHOT_FRESH_MIN):
            return None
        return float(row[1])
    except Exception:
        return None


def authoritative_position_count(profile: str) -> int | None:
    """持仓数权威：position_snapshots 按 profile 最新批次（同 ts 行数）。

    最新批次 ts ≤SNAPSHOT_FRESH_MIN 分钟才覆盖。
    flat 哨兵适配：空仓时写侧写一行 symbol='__FLAT__' 哨兵，
    批次里全为哨兵行 → 权威持仓数=0。计数一律剔除哨兵行；
    无批次/stale 仍 None 回退 agent 值。
    """
    try:
        con = sqlite3.connect(f"file:{ACCOUNT_DB}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT ts FROM position_snapshots "
                "WHERE profile=? ORDER BY rowid DESC LIMIT 1", (profile,)).fetchone()
            if not row:
                return None
            age = _ts_age_minutes(row[0])
            if age is None or not (-5 <= age <= SNAPSHOT_FRESH_MIN):
                return None
            rows = con.execute(
                "SELECT symbol FROM position_snapshots WHERE profile=? AND ts=?",
                (profile, row[0])).fetchall()
        finally:
            con.close()
        if not rows:
            return None
        real = [r for r in rows if str(r[0] or "").strip() != "__FLAT__"]
        return len(real)
    except Exception:
        return None


def authoritative_cum_pnl(profile: str) -> float | None:
    """累计收益权威 = cum_pnl.py 口径（冻结基线 system_state.{p}_cum_pnl + reset_ts 后 trades.pnl 增量）。

    同目录 import 复用；import/查询失败、baseline 缺（ok=false，防 delta-only 假小值上屏）
    → None（回退 agent 值）；agent 手写污染由此隔断。
    """
    try:
        import cum_pnl as _cum_pnl
        info = _cum_pnl.cum_for(DB_ROOT, profile)
        if not isinstance(info, dict) or not info.get("ok"):
            return None
        v = info.get("cum_pnl")
        return float(v) if v is not None else None
    except Exception:
        return None


def authoritative_cycle_count(cycle_id: Any) -> int | None:
    """轮次唯一权威：ledger.stage_dispatch push 按 rowid 插入序计数。

    旧口径 COUNT(cycle_id<=?) 是词典序时点——乱序补派下撞轮号：07-14 22:00 槽晚于 22:15 补派
    （rowid 5888→5890）、07-15 20:30 晚于 20:45（rowid 6158→6159），两 cycle 同得一个 COUNT →
    qq_push round 哨兵把后到战报 round_duplicate_skip 吞掉。改按 **rowid 插入序**：该 cycle 的
    push 闩锁行在全部 stage='push' 行中的插入序号=实际派发次序，天然唯一；retry 重查同值幂等。
    - 子查询查无该 cycle push 行（--no-send 开发跑/手工渲染未经闩锁；生产路径 dispatcher 先写
      闩锁再起 pipeline，不会走到）→ 回退旧 cycle_id<=? 口径，保持开发路径行为不变。
    - 不带 cycle_id 退化全量计数（仍单调）；ledger 不可用/计数<=0 → None（回退 agent 传值），
      渲染不因此失败。注意保留 stage='push' 过滤，排除 skip_warn 等其他 stage。
    """
    try:
        con = sqlite3.connect(f"file:{LEDGER_DB}?mode=ro", uri=True, timeout=5)
        try:
            if isinstance(cycle_id, str) and cycle_id.strip():
                cid = cycle_id.strip()
                row = con.execute(
                    "SELECT COUNT(*) FROM stage_dispatch "
                    "WHERE stage='push' AND rowid <= ("
                    "  SELECT rowid FROM stage_dispatch WHERE stage='push' AND cycle_id=?)",
                    (cid,),
                ).fetchone()
                n = int(row[0]) if row else 0
                if n <= 0:
                    # 该 cycle 尚无 push 闩锁行（子查询 NULL → COUNT=0）——旧口径退化最稳
                    row = con.execute(
                        "SELECT COUNT(*) FROM stage_dispatch WHERE stage='push' AND cycle_id<=?",
                        (cid,),
                    ).fetchone()
                    n = int(row[0]) if row else 0
            else:
                row = con.execute(
                    "SELECT COUNT(*) FROM stage_dispatch WHERE stage='push'"
                ).fetchone()
                n = int(row[0]) if row else 0
            return n if n > 0 else None
        finally:
            con.close()
    except Exception:
        return None


def authoritative_cycle_duration(cycle_id: Any) -> int | None:
    """cycle 用时权威：历史取 analyst 派发；2026-07-23 统一链取 live 首棒派发。
    dispatched_at → 渲染时刻的秒数。

    agent 传值长期乱填（⏱?s / 固定 900，97 份归档慢性），ledger 可得即覆盖；
    查不到 / 不可用 / 解析失败 / 值异常（负数或 >6h）→ None（回退 agent 值），
    无值渲染仍为 '?s' 不新造假数，渲染永不因回读失败中断。
    """
    try:
        if not (isinstance(cycle_id, str) and cycle_id.strip()):
            return None
        con = sqlite3.connect(f"file:{LEDGER_DB}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT dispatched_at FROM stage_dispatch "
                "WHERE cycle_id=? AND stage IN ('analyst','live') "
                "ORDER BY CASE stage WHEN 'analyst' THEN 0 ELSE 1 END, rowid LIMIT 1",
                (cycle_id.strip(),)).fetchone()
        finally:
            con.close()
        if not row or not row[0]:
            return None
        s = str(row[0]).strip()
        if s.endswith("Z"):
            dt = (datetime.fromisoformat(s.replace("Z", "+00:00"))
                  .astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None))
        else:
            dt = datetime.fromisoformat(s.replace("T", " ", 1)[:19])
        secs = (datetime.now() - dt).total_seconds()
        if secs < 0 or secs > 6 * 3600:
            return None
        return int(round(secs))
    except Exception:
        return None


def validate_input(payload: dict[str, Any]) -> None:
    """校验输入 JSON 关键字段完整性。缺关键字段 → exit 报错，不让残缺内容进 render-vs-validator 裂缝。"""
    cycle = section(payload, "cycle")
    # 0 是合法轮次占位（build 填 0 → render L624-628 用
    # authoritative_cycle_count 覆盖；ledger 回读失败返 None 时 payload 保持 0）。**禁用 `or` 链**
    # ——`0 or ...` 把合法 0 当缺失 → fail(3) → 唯一 QQ 出口整轮零推送且闩锁已占永不重派。
    cc = payload.get("cycle_count")
    if cc is None:
        cc = cycle.get("cycle_count")
    if cc is None:
        cc = cycle.get("count")
    if cc is None or (isinstance(cc, str) and cc.strip() in ("", "?")):
        fail("缺少 cycle_count（必填）", 3)
    try:
        int(cc)
    except (TypeError, ValueError):
        fail(f"cycle_count 必须是数字，got={cc!r}", 3)

    action_taken = (payload.get("action_taken") or
                    section(payload, "decision").get("action_taken") or
                    section(payload, "execution").get("action_taken"))
    if not action_taken or str(action_taken).strip() == "":
        fail("缺少 action_taken（必填），agent 必传 action_taken 字段", 3)

    assets = section(payload, "assets")
    if not isinstance(assets.get("live"), dict):
        fail("assets.live 必为 dict，包括 equity/totalEq", 3)
    if not isinstance(assets.get("demo"), dict):
        fail("assets.demo 必为 dict，包括 equity/totalEq", 3)

    market = section(payload, "market")
    if market.get("btc") is None and market.get("btc_price") is None:
        fail("market.btc 或 market.btc_price 必填", 3)

    # decision_card_v1 不使用 confidence；仅兼容格式缺失时保留告警。
    confidence = first_nonempty(payload.get("confidence"),
                                section(payload, "decision").get("confidence"), default="")
    decision_protocol = section(payload, "decision").get("decision_protocol")
    if decision_protocol != "decision_card_v1" and confidence in ("", "-"):
        print("[render_push_report][WARN] confidence 缺失（不拦截，渲染为 -）", file=sys.stderr)
    risk = section(payload, "risk")
    for _label, _keys in (("margin_pct", ["margin_pct", "imr_pct", "single_margin_pct"]),
                          ("leverage", ["lev", "leverage", "max_leverage"]),
                          ("side_pct", ["side_pct", "same_side_pct", "same_side_exposure_pct"])):
        if value_at(risk, _keys, default="") in ("", "-"):
            if (_label == "margin_pct"
                    and risk.get("margin_pct_scope") == "max_current_cycle_open_trade"):
                # 本轮没有 OPEN/ADD 时空值是正确语义，渲染层会明确显示“本轮无开/加仓”。
                continue
            print(f"[render_push_report][WARN] 风控字段 {_label} 缺失（不拦截，渲染为 -）", file=sys.stderr)


def render(payload: dict[str, Any]) -> dict[str, Any]:
    _cycle_pre = section(payload, "cycle")
    _cycle_id = payload.get("cycle_id") or _cycle_pre.get("cycle_id") or _cycle_pre.get("id")
    _auth_cc = authoritative_cycle_count(_cycle_id)
    if _auth_cc is not None:
        payload["cycle_count"] = _auth_cc
    validate_input(payload)
    cycle = section(payload, "cycle")
    assets = section(payload, "assets")
    risk = section(payload, "risk")
    market = section(payload, "market")
    decision = section(payload, "decision")
    execution = section(payload, "execution")
    timeline = section(payload, "timeline")
    macro = section(payload, "macro")

    cycle_count = first_nonempty(payload.get("cycle_count"), cycle.get("cycle_count"), cycle.get("count"), default="?")
    cycle_duration = first_nonempty(payload.get("cycle_duration_s"), cycle.get("cycle_duration_s"), cycle.get("duration_s"), default="?")
    # duration 权威推算：cycle_id 可得时从 ledger.stage_dispatch 取该 cycle
    # 首棒 dispatched_at（历史 analyst/新 live）→ 渲染时刻秒数；ledger 不可用回退 agent 值。
    try:
        _auth_dur = authoritative_cycle_duration(
            payload.get("cycle_id") or cycle.get("cycle_id") or cycle.get("id"))
        if _auth_dur is not None:
            cycle_duration = str(_auth_dur)
    except Exception:
        pass
    hhmm = first_nonempty(payload.get("hhmm"), cycle.get("hhmm"), default=datetime.now().strftime("%H:%M"))
    # channel 使用固定语义 live|demo，不接收 agent 自报值。
    channel = "live|demo"
    action = first_nonempty(payload.get("action_taken"), decision.get("action_taken"), execution.get("action_taken"), default="OPEN_LONG").upper()
    # 动作枚举归一（2026-07-03 C4a）：agent 传下划线粘连的复合标签（HOLD_NONE / HOLD_BOTH_LANES_NO_TRADE 等）时，
    # validate_push_format 的 \b 枚举校验必挂（_ 是 word char，\bHOLD\b 匹配不到 HOLD_NONE）。
    # 无裸枚举词时确定性提取首个内嵌枚举词；完全无枚举词的自由文本保持原值——让 validate 照旧拦截，不静默臆造动作。
    import re as _re_act
    if not _re_act.search(r"\b(OPEN_LONG|OPEN_SHORT|CLOSE|STOP_LOSS|ADJUST|HOLD|WAIT|NONE|REDUCE|ADD)\b", action):
        _m_act = _re_act.search(r"OPEN_LONG|OPEN_SHORT|STOP_LOSS|CLOSE|ADJUST|HOLD|WAIT|NONE|REDUCE|ADD", action)
        if _m_act:
            action = _m_act.group(0)
    symbol = first_nonempty(payload.get("symbol"), decision.get("symbol"), execution.get("symbol"), default="UNKNOWN")
    # symbol 回退链：顶层 symbol 缺失时
    # 缺省时依次从 trades.live/demo 各笔 symbol（去 -USDT-SWAP 后缀、去重、'/'拼接、最多 3 个）
    # → positions 回退；仍无才保持 'UNKNOWN'。独立 try/except：回退失败保持现行为。
    if str(symbol).strip().upper() in ("UNKNOWN", "", "-"):
        try:
            _fb_sym = _fallback_symbol(payload, _normalize_positions(payload.get("positions")))
            if _fb_sym:
                symbol = _fb_sym
        except Exception:
            pass
    confidence = first_nonempty(payload.get("confidence"), decision.get("confidence"), default="-")
    action_summary = first_nonempty(decision.get("summary"), execution.get("summary"), payload.get("action_summary"), default="")

    live = section(assets, "live")
    demo = section(assets, "demo")
    # positions dict 形态兼容：{live:[...],demo:[...]} 合并展开为 list（各项补 profile）。
    positions = _normalize_positions(payload.get("positions"))
    exceptions = list_section(payload, "exceptions")
    # 过滤：运行故障保留在异常段，行情/风控判断移到决策依据
    exceptions, misplaced_decisions = filter_exceptions(exceptions)

    live_equity = money(value_at(live, ["current_equity", "equity", "totalEq", "total_eq"], "-"))
    live_available = money(value_at(
        live, ["available_margin", "availBal", "available_balance", "available"], "-"
    ))
    live_pnl = money(value_at(live, ["realized_pnl", "pnl", "sum_pnl"], "-"))
    live_positions = num_only(first_nonempty(live.get("positions"), live.get("position_count"), live.get("n_positions"), default=""), default="-")
    demo_equity = money(value_at(demo, ["current_equity", "equity", "totalEq", "total_eq"], "-"))
    demo_available = money(value_at(
        demo, ["available_margin", "availBal", "available_balance", "available"], "-"
    ))
    demo_pnl = money(value_at(demo, ["realized_pnl", "pnl", "sum_pnl"], "-"))
    demo_positions = num_only(first_nonempty(demo.get("positions"), demo.get("position_count"), demo.get("n_positions"), default=""), default="-")

    # 纯脚本 builder 已把 as-of 最近快照投影到构建时点前的全部落账成交；这能覆盖
    # trader 跨 cycle 交错完成。标记必须与 cycle_id 精确相同，旧/外部 payload仍走
    # 下方 DB 权威回读，避免放宽历史防漂移保护。
    _positions_projected = bool(_cycle_id) and str(payload.get("positions_projected_cycle") or "") == str(_cycle_id)
    if _positions_projected:
        _live_n = sum(1 for p in positions if str(p.get("profile") or "live").lower() == "live")
        _demo_n = sum(1 for p in positions if str(p.get("profile") or "").lower() == "demo")
        live_positions = str(_live_n)
        demo_positions = str(_demo_n)

    margin_pct = pct(value_at(risk, ["margin_pct", "imr_pct", "single_margin_pct"], "-"))
    max_position_margin_pct = pct(risk.get("max_position_margin_pct"))
    if margin_pct == "-":
        margin_display = "本轮无开/加仓"
        if max_position_margin_pct != "-":
            margin_display += f" | 当前最大持仓 {with_pct(max_position_margin_pct)}(观察)"
    else:
        margin_display = f"{with_pct(margin_pct)} / 20%"
    leverage = first_nonempty(risk.get("lev"), risk.get("leverage"), risk.get("max_leverage"), default="-")
    side_pct = pct(value_at(risk, ["side_pct", "same_side_pct", "same_side_exposure_pct"], "-"))
    position_count = num_only(first_nonempty(risk.get("position_count"), risk.get("pos_count"), default=live_positions), default="-")
    risk_status = first_nonempty(risk.get("status"), risk.get("hard_limit_status"), default="PASS")

    # 资金/持仓数/累计收益权威回读（2026-07-04）：cycle_count 权威覆盖的同模式推广——
    # 库权威值覆盖 agent 传值（#588 双盘 equity 填错 / 假 0 仓 / 累计收益手写污染即此因）；
    # 每项独立回退：DB stale/不可用 → 保留 agent 值，渲染永不因回读失败。
    for _profile in ("live", "demo"):
        _eq = authoritative_equity(_profile)
        _cum = authoritative_cum_pnl(_profile)
        _pos = None if _positions_projected else authoritative_position_count(_profile)
        if _profile == "live":
            if _eq is not None:
                live_equity = money(_eq)
            if _cum is not None:
                live_pnl = money(_cum)
            if _pos is not None:
                live_positions = str(_pos)
                position_count = live_positions
        else:
            if _eq is not None:
                demo_equity = money(_eq)
            if _cum is not None:
                demo_pnl = money(_cum)
            if _pos is not None:
                demo_positions = str(_pos)

    if _positions_projected:
        position_count = live_positions

    # 行情数值 dict 防御：btc/eth 等价格字段收到 dict 时
    # 提取 price/last 键或首个数字，杜绝 JSON 字面量直出（_numeric_field 内部 fail-safe）。
    btc = _numeric_field(market.get("btc") if market.get("btc") not in (None, "") else market.get("btc_price"))
    btc_chg = _numeric_field(market.get("btc_chg24h") if market.get("btc_chg24h") not in (None, "") else market.get("btc_change_24h"))
    eth = _numeric_field(market.get("eth") if market.get("eth") not in (None, "") else market.get("eth_price"))
    eth_chg = _numeric_field(market.get("eth_chg24h") if market.get("eth_chg24h") not in (None, "") else market.get("eth_change_24h"))
    regime = first_nonempty(market.get("regime"), payload.get("regime"), default="-")
    dxy = first_nonempty(market.get("dxy"), macro.get("dxy"), default="-")
    # L2 (2026-06-14): 摘要缺失时用 action+regime+BTC 兜底（替代旧"按结构化模板完成本轮闭环"空话），
    # 至少带市场实质；skill 仍要求 agent 显式传摘要。
    if not str(action_summary).strip() or str(action_summary).strip() == "-":
        # 兜底摘要复用已做过 dict 防御的 btc，防 JSON 字面量直出。
        action_summary = f"{action} @ regime={regime} BTC ${btc if btc != '-' else '?'}（摘要缺失补位）"
    action_summary = clip(action_summary, SUMMARY_MAX)

    decision_reason = clip(
        first_nonempty(decision.get("reason"), decision.get("rationale"),
                       default="regime、技术指标、新闻事件、历史相似度与 playbook 综合确认。"),
        DECISION_REASON_MAX)
    decision_card = decision.get("decision_card")
    if not isinstance(decision_card, dict):
        decision_card = {}
    historical = decision_card.get("historical_experience")
    if not isinstance(historical, dict):
        historical = {}
    # 被从异常段移出的行情/风控判断条目 → 追加到决策依据段末尾
    if misplaced_decisions:
        moved_lines = []
        for item in misplaced_decisions:
            if isinstance(item, dict):
                name = first_nonempty(item.get("name"), item.get("type"), item.get("check"), default="")
                detail = first_nonempty(item.get("detail"), item.get("message"), item.get("fix"), default="")
                if name or detail:
                    moved_lines.append(f"{name}: {detail}".strip(": "))
            else:
                moved_lines.append(text(item))
        if moved_lines:
            moved_text = " | ".join(moved_lines)
            decision_reason = clip(f"{decision_reason} | {moved_text}", DECISION_REASON_MAX)
    play_id = first_nonempty(decision.get("play_id"), decision.get("playbook_id"), default="-")
    play_title = first_nonempty(decision.get("play_title"), decision.get("playbook_title"), default="-")
    hit_rate = strip_pct(first_nonempty(decision.get("hit_rate"), decision.get("similar_hit_rate"), default="-"))
    avg_return = strip_pct(first_nonempty(decision.get("avg_return"), decision.get("similar_avg_return"), default="-"))
    uncertainty = first_nonempty(decision.get("uncertainty"), decision.get("max_uncertainty"), default="-")

    fill_price = first_nonempty(execution.get("fill_px"), execution.get("fill_price"), execution.get("price"), default="-")
    stop_price = first_nonempty(execution.get("stop_px"), execution.get("stop_price"), default="-")
    # 当前 payload 报真实落库行数（db_rows_*，0 是合法值禁 falsy 链）；
    # 归档 payload 无该键时回退兼容字段展示。
    _dbl = execution.get("db_rows_live")
    _dbd = execution.get("db_rows_demo")
    if _dbl is None and _dbd is None:
        exec_meta = "落库 live=-笔 | demo=-笔"
    else:
        exec_meta = (f"落库 live={_dbl if _dbl is not None else '-'}笔"
                     f" | demo={_dbd if _dbd is not None else '-'}笔")
    execution_result = first_nonempty(execution.get("result"), default=f"{action} {symbol} fill={fill_price} stop={stop_price}")
    # 执行段回退：execution.* 全缺而 payload.trades 有内容时，
    # 从 trades（live+demo）逐笔拼
    # "SIDE symbol sz@fill_px pnl=…"（≤3 笔 + 溢出计数）。独立 try/except：回退失败保持现行为。
    try:
        _has_exec = any(execution.get(k) not in (None, "", "-")
                        for k in ("result", "fill_px", "fill_price", "price",
                                  "stop_px", "stop_price"))
        if not _has_exec:
            _trade_recs = _iter_trades(payload)
            if _trade_recs:
                _tlines = []
                for _rec in _trade_recs[:3]:
                    _side = " ".join(t for t in (
                        str(_rec.get("action") or "").strip().upper(),
                        str(_rec.get("side") or "").strip().upper()) if t) or "-"
                    _sym = text(first_nonempty(_rec.get("symbol"), _rec.get("instId"), default="-"))
                    _sz = text(first_nonempty(_rec.get("sz"), _rec.get("size"), default="-"))
                    _fp = text(first_nonempty(_rec.get("fill_px"), _rec.get("fill_price"),
                                              _rec.get("avgPx"), _rec.get("px"), default="-"))
                    _tline = f"{_side} {_sym} {_sz}@{_fp}"
                    if _rec.get("pnl") not in (None, ""):
                        _tline += f" pnl={money(_rec.get('pnl'))}"
                    _tlines.append(_tline)
                if len(_trade_recs) > 3:
                    _tlines.append(f"…另{len(_trade_recs) - 3}笔见归档")
                execution_result = " | ".join(_tlines)
    except Exception:
        pass

    next_hh01 = first_nonempty(timeline.get("next_hh01_min"), default="-")
    # 下次复盘 = okx-reviewer-cron（cron "5 8 * * *"，08:05 起）。
    # 兼容：旧键 next_p7_time 仍读（历史 agent 传值）；新中性键 next_review_time 优先。
    next_review = first_nonempty(timeline.get("next_review_time"), timeline.get("next_p7_time"), default="08:05")

    content_parts = [
        f"【{hhmm}】第{cycle_count}轮 / ⏱{cycle_duration}s / {channel} / {action} {symbol}",
        (f"Agent自主裁决 | {action_summary}" if decision_card
         else f"兼容格式置信度 {confidence} | {action_summary}"),
        "",
        "📊 资产",
        f"🟢 实盘：资金 ${live_equity} | 可用USDT ${live_available} | 累计收益(交易PnL·未扣费) {live_pnl} USDT | {live_positions}仓",
        f"🟡 模拟盘：资金 ${demo_equity} | 可用USDT ${demo_available} | 累计收益(交易PnL·未扣费) {demo_pnl} USDT | {demo_positions}仓",
        "",
        "💼 持仓详情",
        format_positions(positions),
        "",
        "🛡 风控",
        f"单笔保证金 {margin_display} | 杠杆 {leverage}x / 10x | 同侧 {with_pct(side_pct)}(观察) | 持仓 live {position_count} / demo {demo_positions}(数量仅观察) | {risk_status}",
        "",
        "🌍 行情",
        f"BTC ${btc} ({with_pct(btc_chg)}) | ETH ${eth} ({with_pct(eth_chg)}) | regime={regime} | USD_BROAD {dxy}",
        "",
        "🎯 Agent裁决",
        decision_reason,
        "",
        "🧭 六项决策卡",
        (
            f"方向：{card_text(decision_card.get('direction_evidence'))}\n"
            f"反对：{card_text(decision_card.get('opposing_evidence'))}\n"
            f"执行：{card_text(decision_card.get('execution_conditions'))}\n"
            f"失效：{card_text(decision_card.get('invalidation_point'))}\n"
            f"风险收益：{card_text(decision_card.get('risk_reward'))}\n"
            f"组合：{card_text(decision_card.get('portfolio_impact'))}"
            if decision_card else "旧轮次无 decision_card_v1"
        ),
        "",
        "📚 历史经验",
        (
            f"盈利样本 {len(historical.get('matched_wins') or [])} | "
            f"亏损样本 {len(historical.get('matched_losses') or [])} | "
            f"错失机会 {len(historical.get('missed_opportunities') or [])} | "
            f"取舍={historical.get('usage') or 'none'}："
            f"{card_text(historical.get('reason'), 220)}"
            if historical else
            f"兼容格式 play_id={play_id} \"{play_title}\" | "
            f"hit_rate={with_pct(hit_rate)} / avg_return={with_pct(avg_return)} "
            f"| 不确定性={uncertainty}"
        ),
        "",
        "⚙️ 执行",
        f"{execution_result}\n{exec_meta}",
        "",
        "⏰ 时间线",
        f"下次HH:01: {next_hh01}min | 下次复盘: {next_review}",
        "",
        "⚠️ 异常",
        clip(format_exceptions(exceptions), EXCEPTIONS_MAX, tail="…（更多异常见归档）"),
    ]

    include_macro = bool(payload.get("is_hh01")) or bool(macro.get("enabled"))
    if include_macro:
        degraded_sources = first_nonempty(macro.get("degraded_sources"), market.get("degraded_sources"), default="无")
        top_gainers = first_nonempty(market.get("top_gainers"), default="-")
        top_losers = first_nonempty(market.get("top_losers"), default="-")
        funding_anomalies = first_nonempty(market.get("funding_anomalies"), default="无")
        content_parts.extend([
            "",
            "🌐 宏观 HH:01",
            f"USD_BROAD(DTWEXBGS) {first_nonempty(macro.get('dxy'), dxy)} ({with_pct(macro.get('dxy_d1'))}) | VIX {first_nonempty(macro.get('vix'), default='-')} | SPX {first_nonempty(macro.get('spx'), default='-')} ({with_pct(macro.get('spx_d1'))})",
            f"DXY_CALC_ECB {first_nonempty(macro.get('dxy_calc_ecb'), default='-')} ({with_pct(macro.get('dxy_calc_ecb_d1'))}, 非ICE官方报价) | Fear&Greed {first_nonempty(macro.get('fear_greed'), default='-')}/{first_nonempty(macro.get('fear_greed_label'), default='-')}",
            f"BTC市值Δ24h(≠ETF净流) {first_nonempty(macro.get('btc_mcap_chg_24h_usd'), macro.get('btc_etf_proxy'), default='-')} | TVL {first_nonempty(macro.get('tvl'), default='-')} | BTC.D {with_pct(macro.get('btc_dominance'))}",
            f"BTC ETF净流 {first_nonempty(macro.get('btc_etf_net_flow_usd'), default='-')} | 状态 {first_nonempty(macro.get('btc_etf_flow_status'), default='missing')} | as_of {first_nonempty(macro.get('btc_etf_flow_as_of'), default='-')}",
            f"降级源: {degraded_sources}",
            "",
            "📊 全市场 HH:01",
            f"TOP3 涨幅: {top_gainers}",
            f"TOP3 跌幅: {top_losers}",
            f"资金费率异常: {funding_anomalies}",
        ])

    content_body = "\n".join(content_parts).strip()
    content = qq_markdown_hardbreak(content_body)
    # 单条上限闸：禁止再整体从尾部裁剪。时间线/决策卡等必填段位于后半部，
    # 旧逻辑会把它们一起裁掉，导致 validator 在发送前拦截整轮推送。
    # 超长时改用结构化压缩版：缩正文，不删除任何必填段标题。
    if len(content) > MAX_CONTENT_CHARS:
        compact_card = (
            f"方向：{card_text(decision_card.get('direction_evidence'), 80)}\n"
            f"反对：{card_text(decision_card.get('opposing_evidence'), 80)}\n"
            f"执行：{card_text(decision_card.get('execution_conditions'), 80)}\n"
            f"失效：{card_text(decision_card.get('invalidation_point'), 80)}\n"
            f"风险收益：{card_text(decision_card.get('risk_reward'), 80)}\n"
            f"组合：{card_text(decision_card.get('portfolio_impact'), 80)}"
            if decision_card else "旧轮次无 decision_card_v1"
        )
        compact_history = (
            f"盈利样本 {len(historical.get('matched_wins') or [])} | "
            f"亏损样本 {len(historical.get('matched_losses') or [])} | "
            f"错失机会 {len(historical.get('missed_opportunities') or [])} | "
            f"取舍={historical.get('usage') or 'none'}："
            f"{card_text(historical.get('reason'), 100)}"
            if historical else
            f"兼容格式 play_id={play_id} \"{clip(play_title, 50)}\" | "
            f"hit_rate={with_pct(hit_rate)} / avg_return={with_pct(avg_return)} "
            f"| 不确定性={uncertainty}"
        )
        compact_parts = [
            f"【{hhmm}】第{cycle_count}轮 / ⏱{cycle_duration}s / {channel} / {action} {symbol}",
            (f"Agent自主裁决 | {clip(action_summary, 140)}" if decision_card
             else f"兼容格式置信度 {confidence} | {clip(action_summary, 140)}"),
            "",
            "📊 资产",
            f"🟢 实盘：资金 ${live_equity} | 可用USDT ${live_available} | 累计收益 {live_pnl} USDT | {live_positions}仓",
            f"🟡 模拟盘：资金 ${demo_equity} | 可用USDT ${demo_available} | 累计收益 {demo_pnl} USDT | {demo_positions}仓",
            "",
            "💼 持仓详情",
            clip(format_positions(positions), 520, tail="\n…（其余持仓见账本）"),
            "",
            "🛡 风控",
            clip(
                f"单笔保证金 {margin_display} | 杠杆 {leverage}x / 10x | "
                f"同侧 {with_pct(side_pct)}(观察) | 持仓 live {position_count} / "
                f"demo {demo_positions}(数量仅观察) | {risk_status}",
                210,
            ),
            "",
            "🌍 行情",
            f"BTC ${btc} ({with_pct(btc_chg)}) | ETH ${eth} ({with_pct(eth_chg)}) | "
            f"regime={regime} | USD_BROAD {dxy}",
            "",
            "🎯 Agent裁决",
            clip(decision_reason, 180),
            "",
            "🧭 六项决策卡",
            compact_card,
            "",
            "📚 历史经验",
            compact_history,
            "",
            "⚙️ 执行",
            clip(f"{execution_result}\n{exec_meta}", 180),
            "",
            "⏰ 时间线",
            f"下次HH:01: {next_hh01}min | 下次复盘: {next_review}",
            "",
            "⚠️ 异常",
            clip(format_exceptions(exceptions), 120, tail="…（更多异常见账本）"),
        ]
        if include_macro:
            compact_parts.extend([
                "",
                "🌐 宏观 HH:01",
                clip(
                    f"USD_BROAD {first_nonempty(macro.get('dxy'), dxy)} "
                    f"({with_pct(macro.get('dxy_d1'))}) | "
                    f"DXY_CALC_ECB {first_nonempty(macro.get('dxy_calc_ecb'), default='-')} "
                    f"({with_pct(macro.get('dxy_calc_ecb_d1'))}, 非ICE官方报价) | "
                    f"Fear&Greed {first_nonempty(macro.get('fear_greed'), default='-')}/"
                    f"{first_nonempty(macro.get('fear_greed_label'), default='-')}",
                    260,
                ),
                f"BTC ETF净流 {first_nonempty(macro.get('btc_etf_net_flow_usd'), default='-')} | "
                f"状态 {first_nonempty(macro.get('btc_etf_flow_status'), default='missing')} | "
                f"as_of {first_nonempty(macro.get('btc_etf_flow_as_of'), default='-')}",
            ])
        compact_parts.extend(["", "…（推送过长已结构化压缩，完整事实以账本和分析归档为准）"])
        content = qq_markdown_hardbreak("\n".join(compact_parts).strip())
        # 极端兜底仍按“逐段再压缩”而非裁尾，保证所有必填段存在。
        if len(content) > MAX_CONTENT_CHARS:
            compact_parts[8] = clip(format_positions(positions), 260, tail="\n…（其余持仓见账本）")
            compact_parts[17] = clip(decision_reason, 100)
            compact_parts[20] = clip(compact_card, 360)
            compact_parts[23] = clip(compact_history, 90)
            compact_parts[26] = clip(f"{execution_result}\n{exec_meta}", 100)
            compact_parts[32] = clip(format_exceptions(exceptions), 60)
            content = qq_markdown_hardbreak("\n".join(compact_parts).strip())
        if len(content) > MAX_CONTENT_CHARS:
            minimal_card = (
                f"方向：{card_text(decision_card.get('direction_evidence'), 30)}\n"
                f"反对：{card_text(decision_card.get('opposing_evidence'), 30)}\n"
                f"执行：{card_text(decision_card.get('execution_conditions'), 30)}\n"
                f"失效：{card_text(decision_card.get('invalidation_point'), 30)}\n"
                f"风险收益：{card_text(decision_card.get('risk_reward'), 30)}\n"
                f"组合：{card_text(decision_card.get('portfolio_impact'), 30)}"
                if decision_card else "旧轮次无 decision_card_v1"
            )
            minimal_parts = [
                f"【{hhmm}】第{cycle_count}轮 / ⏱{cycle_duration}s / {channel} / {action} {symbol}",
                f"Agent自主裁决 | {clip(action_summary, 80)}",
                "", "📊 资产",
                f"🟢 实盘：资金 ${live_equity} | 可用 ${live_available} | {live_positions}仓",
                f"🟡 模拟盘：资金 ${demo_equity} | 可用 ${demo_available} | {demo_positions}仓",
                "", "💼 持仓详情",
                clip(format_positions(positions), 120, tail="\n…（其余见账本）"),
                "", "🛡 风控",
                clip(f"保证金 {margin_display} | 杠杆 {leverage}x | 同侧 {with_pct(side_pct)} | {risk_status}", 100),
                "", "🌍 行情",
                f"BTC ${btc} | ETH ${eth} | regime={regime} | USD_BROAD {dxy}",
                "", "🎯 Agent裁决", clip(decision_reason, 70),
                "", "🧭 六项决策卡", minimal_card,
                "", "📚 历史经验", clip(compact_history, 60),
                "", "⚙️ 执行", clip(f"{execution_result}\n{exec_meta}", 70),
                "", "⏰ 时间线",
                f"下次HH:01: {next_hh01}min | 下次复盘: {next_review}",
                "", "⚠️ 异常", clip(format_exceptions(exceptions), 40),
                "", "…（推送过长已最小化，完整事实以账本和分析归档为准）",
            ]
            content = qq_markdown_hardbreak("\n".join(minimal_parts).strip())
    title = first_nonempty(payload.get("title"), default=f"【{hhmm}】{action} {symbol}")
    return {
        "ok": True,
        "title": title,
        "content": content,
        "char_count": len(content),
        "sections": ["header", "assets", "positions", "risk", "market", "decision",
                     "decision_card", "experience", "execution", "timeline", "exceptions"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="V7.2 QQ 推送模板渲染器")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--stdin", action="store_true")
    inputs.add_argument("--json-file")
    inputs.add_argument("--json")
    parser.add_argument("--out-file",
                        help="渲染产物写入 UTF-8 文件（2026-06-13：绕开控制台 GBK 管道污染——"
                             "agent 捕获 stdout 中文会乱码；后续 validate --file / 归档 content_file 直读此文件）")
    parser.add_argument("--db-root", default=None,
                        help="库权威覆盖（轮次/资金/持仓数/累计收益/耗时）的读取根目录；"
                             "默认 <PROJECT_ROOT>/db")
    args = parser.parse_args()

    if args.db_root:
        global LEDGER_DB, ACCOUNT_DB, DB_ROOT
        _root = str(args.db_root).rstrip("\\/").replace("\\", "/")
        LEDGER_DB = f"{_root}/ledger.db"
        ACCOUNT_DB = f"{_root}/account.db"
        DB_ROOT = _root

    payload = load_payload(args)
    result = render(payload)
    if args.out_file:
        content = result.get("content", "") if isinstance(result, dict) else str(result)
        with open(args.out_file, "w", encoding="utf-8") as fh:
            fh.write(content)
        receipt = {"ok": True, "out_file": args.out_file,
                   "bytes": len(content.encode("utf-8"))}
        if isinstance(result, dict) and result.get("title"):
            receipt["title"] = result["title"]
        print(json.dumps(receipt, ensure_ascii=False))
        return 0
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
