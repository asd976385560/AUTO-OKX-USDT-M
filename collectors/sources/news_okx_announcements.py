# -*- coding: utf-8 -*-
"""V2.0 §6 —— OKX 官方公告 adapter（上币/下架/交易更新/API/活动 → news_writer）。

挂聚合采集 cron 的 news 步（news_collect registry 驱动，registry id=okx_announcements），
与 RSS/中文快讯/okx_news 同链、**不进 fast_collect**。补齐规格书「官方动态」维度：
上币/下架通知、交易与维护更新、活动公告，供分析侧做事件风险窗观察
（重大公告前后降权是 Agent 自主判断，本采集器只提供事实，不设闸）。

数据源（公共无鉴权，经 scripts/_okx_support_http.py 统一域名回退 + 限速）：
  GET /api/v5/support/announcement-types  → 本地域可用类别（按站点/地域可能不同）
  GET /api/v5/support/announcements?annType=&page=1 → 每类别第一页

契约（与其它 news_* 一致）：
  - 写库只经 news_writer（禁手写 INSERT）
  - event_time 来自 pTime(ms)→UTC+8；缺则 NULL，禁 fallback now
    （businessPTime=业务生效时刻，保留在 raw，不冒充发布时刻）
  - severity/tags 由 annType 规则映射（确定性，非 LLM）：
    delistings→high；new-listings/trading-updates→medium；api/其它→low
  - symbol 仅在标题模式高置信时提取（list/delist X 或 X/USDT），
    统一 <BASE>-USDT-SWAP；提不出宁缺勿假
  - 失败隔离：本源 fail 不拖垮 news_collect 其它源；单类别失败记 err 继续其余类别

零模型名（红线①）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

_COLLECTORS = str(Path(__file__).resolve().parents[1])  # .\collectors
_SCRIPTS = str(Path(__file__).resolve().parents[2] / "scripts")
for _p in (_COLLECTORS, _SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import news_writer  # noqa: E402
import _okx_support_http as _okx_http  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"
SOURCE_ID = "okx_announcements"

_REQUEST_TIMEOUT = 8.0
_MAX_ITEMS_PER_TYPE = 30
_COLD_RETRY_DELAY_SECONDS = 3.0
_COLD_RETRY_TIMEOUT_SECONDS = 6.0
_MAX_FETCH_PHASES = 2
_TOTAL_NETWORK_BUDGET_SECONDS = 75.0
_MIN_REQUEST_BUDGET_SECONDS = 0.25

# annType 关键词 → (业务类别 tag, severity, level)。类型全集按地域漂移，
# 匹配用「包含关键词」而非精确串；未命中 wanted 的类型直接跳过（如 P2P/broker）。
_TYPE_RULES: tuple[tuple[str, str, str, str], ...] = (
    # (annType 关键词, tag, severity, level)
    ("delisting", "delisting", "high", "A"),
    ("new-listing", "listing", "medium", "B"),
    ("trading-update", "trading_update", "medium", "B"),
    ("deposit-withdrawal", "deposit_withdrawal", "medium", "B"),
    ("maintenance", "maintenance", "medium", "B"),
    ("latest-event", "activity", "low", "C"),
    ("api", "api", "low", "C"),
)

# 标题高置信 symbol 模式（宁缺勿假）：
#   "list GRVT/USDT" / "delist BTC" / "GRVT/USDT perpetual" 等。
_PAIR_RE = re.compile(r"\b([A-Z0-9]{2,10})\s*/\s*USDT\b")
_LIST_VERB_RE = re.compile(
    r"\b(?:list|launch|delist|remove|suspend)\w*\s+"
    r"(?:the\s+)?([A-Z0-9]{2,10})\b")
_SYMBOL_STOPWORDS = {
    "USDT", "USDC", "USD", "OKX", "API", "P2P", "SPOT", "MARGIN", "FUTURES",
    "PERP", "PERPETUAL", "SWAP", "WEB3", "THE", "AND", "FOR", "NEW", "ALL",
}


def _ms_to_cst(value) -> str | None:
    if value in (None, ""):
        return None
    try:
        ms = int(str(value))
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).astimezone(CST)
        return dt.strftime(TS_FMT)
    except (TypeError, ValueError, OSError):
        return None


def classify_ann_type(ann_type: str) -> tuple[str, str, str] | None:
    """annType → (tag, severity, level)；不在 wanted 集合返回 None（跳过）。"""
    low = str(ann_type or "").lower()
    if not low:
        return None
    for keyword, tag, severity, level in _TYPE_RULES:
        if keyword in low:
            return tag, severity, level
    return None


def extract_symbols(title: str, tag: str) -> list[str]:
    """高置信标题模式提取 base 币，统一 <BASE>-USDT-SWAP；提不出返回 []。

    只对交易影响直接的类别（listing/delisting/trading_update）提取；
    活动/API 公告标题里的代币名噪音大，宁缺勿假。
    """
    if tag not in {"listing", "delisting", "trading_update", "deposit_withdrawal"}:
        return []
    text = str(title or "")
    bases: list[str] = []
    for m in _PAIR_RE.finditer(text):
        bases.append(m.group(1))
    if not bases:
        for m in _LIST_VERB_RE.finditer(text):
            bases.append(m.group(1))
    out: list[str] = []
    for b in bases:
        base = b.upper().strip()
        if not base or base in _SYMBOL_STOPWORDS:
            continue
        sym = f"{base}-USDT-SWAP"
        if sym not in out:
            out.append(sym)
    return out


def _item_to_news(item: dict, tag: str, severity: str, level: str) -> dict | None:
    title = str(item.get("title") or "").strip()
    if not title:
        return None
    url = item.get("url") or item.get("htmlUrl")
    event_time = _ms_to_cst(item.get("pTime"))
    symbols = extract_symbols(title, tag)
    return {
        "source": SOURCE_ID,
        "title": title[:500],
        "url": url,
        "event_time": event_time,
        "symbols": symbols,
        "symbol": symbols[0] if symbols else None,
        "severity": severity,
        "level": level,
        "tags": ["okx_announcement", tag],
        "sentiment": None,
        "raw": {
            "annType": item.get("annType"),
            "pTime": item.get("pTime"),
            "businessPTime": item.get("businessPTime"),
            "url": url,
        },
    }


def fetch_items(timeout_sec: float = _REQUEST_TIMEOUT,
                errors: list[str] | None = None,
                retry_stats: dict | None = None) -> list[dict]:
    """类型列表 + 命中类别各一页；精确失败项各有一次冷恢复。"""
    started = time.monotonic()
    deadline = started + _TOTAL_NETWORK_BUDGET_SECONDS

    def remaining_timeout(cap: float) -> float | None:
        remaining = deadline - time.monotonic()
        if remaining < _MIN_REQUEST_BUDGET_SECONDS:
            return None
        return max(
            _MIN_REQUEST_BUDGET_SECONDS,
            min(float(cap), remaining),
        )

    type_initial_error: str | None = None
    type_timeout = remaining_timeout(float(timeout_sec))
    if type_timeout is None:
        raise RuntimeError("announcement budget exhausted before types request")
    try:
        types = _okx_http.fetch_support_announcement_types_sync(
            request_timeout_s=type_timeout,
            transport_stats=retry_stats,
        )
    except Exception as e:  # noqa: BLE001
        type_initial_error = f"{type(e).__name__}: {e}"[:150]
        delay = min(
            _COLD_RETRY_DELAY_SECONDS,
            max(0.0, deadline - time.monotonic()),
        )
        if delay > 0:
            time.sleep(delay)
        try:
            cold_timeout = remaining_timeout(min(
                float(timeout_sec), _COLD_RETRY_TIMEOUT_SECONDS))
            if cold_timeout is None:
                raise TimeoutError(
                    "announcement budget exhausted before types cold retry")
            types = _okx_http.fetch_support_announcement_types_sync(
                request_timeout_s=cold_timeout,
                transport_stats=retry_stats,
            )
        except Exception as cold_exc:  # noqa: BLE001
            detail = (
                f"types: initial={type_initial_error[:55]}; "
                f"cold={type(cold_exc).__name__}: {cold_exc}"
            )[:150]
            if errors is not None:
                errors.append(detail)
            if retry_stats is not None:
                retry_stats.update({
                    "types_attempts": 2,
                    "types_recovered_after_cold_retry": False,
                    "category_initial_failed": 0,
                    "category_recovered_after_cold_retry": 0,
                    "final_failed": 1,
                    "maximum_fetch_phases": _MAX_FETCH_PHASES,
                    "maximum_network_budget_seconds": (
                        _TOTAL_NETWORK_BUDGET_SECONDS),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "historical_retry": False,
                    "unbounded_retry": False,
                })
            return []
    wanted: list[tuple[str, str, str, str]] = []
    for t in types or []:
        ann_type = str((t or {}).get("annType") or "")
        rule = classify_ann_type(ann_type)
        if rule is not None:
            wanted.append((ann_type, *rule))
    out: list[dict] = []
    seen_keys: set[tuple] = set()
    failed_categories: list[tuple[str, str, str, str, str]] = []

    def consume_page(page: dict, tag: str, severity: str, level: str) -> None:
        for item in (page.get("details") or [])[:_MAX_ITEMS_PER_TYPE]:
            if not isinstance(item, dict):
                continue
            news = _item_to_news(item, tag, severity, level)
            if not news:
                continue
            key = (news["title"].lower(), news["event_time"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(news)

    for ann_type, tag, severity, level in wanted:
        request_timeout = remaining_timeout(float(timeout_sec))
        if request_timeout is None:
            msg = (
                f"{ann_type}: TimeoutError: announcement total budget "
                "exhausted before initial category request"
            )[:150]
            failed_categories.append((ann_type, tag, severity, level, msg))
            continue
        try:
            page = _okx_http.fetch_support_announcements_sync(
                ann_type,
                page=1,
                request_timeout_s=request_timeout,
                transport_stats=retry_stats,
            )
        except Exception as e:  # noqa: BLE001
            msg = f"{ann_type}: {type(e).__name__}: {e}"[:150]
            print(f"[WARN] okx announcements initial {msg}", file=sys.stderr)
            failed_categories.append((ann_type, tag, severity, level, msg))
            continue
        consume_page(page, tag, severity, level)

    category_recovered = 0
    category_final_failed = 0
    if failed_categories:
        delay = min(
            _COLD_RETRY_DELAY_SECONDS,
            max(0.0, deadline - time.monotonic()),
        )
        if delay > 0:
            time.sleep(delay)
    for ann_type, tag, severity, level, initial_error in failed_categories:
        cold_timeout = remaining_timeout(min(
            float(timeout_sec), _COLD_RETRY_TIMEOUT_SECONDS))
        if cold_timeout is None:
            category_final_failed += 1
            detail = (
                f"{ann_type}: initial={initial_error[len(ann_type) + 2:][:45]}; "
                "cold=TimeoutError: announcement total budget exhausted"
            )[:150]
            if errors is not None:
                errors.append(detail)
            continue
        try:
            page = _okx_http.fetch_support_announcements_sync(
                ann_type,
                page=1,
                request_timeout_s=cold_timeout,
                transport_stats=retry_stats,
            )
            consume_page(page, tag, severity, level)
            category_recovered += 1
        except Exception as cold_exc:  # noqa: BLE001
            category_final_failed += 1
            detail = (
                f"{ann_type}: initial={initial_error[len(ann_type) + 2:][:45]}; "
                f"cold={type(cold_exc).__name__}: {cold_exc}"
            )[:150]
            print(f"[WARN] okx announcements final {detail}", file=sys.stderr)
            if errors is not None:
                errors.append(detail)
    if retry_stats is not None:
        retry_stats.update({
            "types_attempts": 2 if type_initial_error else 1,
            "types_recovered_after_cold_retry": bool(type_initial_error),
            "category_initial_failed": len(failed_categories),
            "category_recovered_after_cold_retry": category_recovered,
            "final_failed": category_final_failed,
            "maximum_fetch_phases": _MAX_FETCH_PHASES,
            "maximum_network_budget_seconds": _TOTAL_NETWORK_BUDGET_SECONDS,
            "cold_retry_delay_seconds": _COLD_RETRY_DELAY_SECONDS,
            "cold_retry_timeout_seconds": _COLD_RETRY_TIMEOUT_SECONDS,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "historical_retry": False,
            "unbounded_retry": False,
        })
    return out


def collect(db_path: str, apply: bool = False,
            timeout_sec: float = _REQUEST_TIMEOUT) -> dict:
    fetch_errs: list[str] = []
    retry_stats: dict = {}
    items = fetch_items(
        timeout_sec=timeout_sec, errors=fetch_errs, retry_stats=retry_stats)
    err_txt = "; ".join(fetch_errs)[:150] if fetch_errs else None
    if not apply:
        out = {
            "ok": True,
            "dry_run": True,
            "fetched": len(items),
            "retry_stats": retry_stats,
            "sample": [
                {
                    "t": it["title"][:70],
                    "et": it["event_time"],
                    "sym": it.get("symbols"),
                    "sev": it.get("severity"),
                    "tags": it.get("tags"),
                }
                for it in items[:8]
            ],
        }
        if err_txt:
            out["err"] = err_txt
        if err_txt and not items:
            out["ok"] = False
        return out

    if not items and err_txt:
        return {"ok": False, "fetched": 0, "inserted": 0,
                "retry_stats": retry_stats, "err": err_txt}

    res = news_writer.write_news(items, db_path)
    res["fetched"] = len(items)
    res["retry_stats"] = retry_stats
    if err_txt and not res.get("err"):
        res["err"] = err_txt
    return res


def main() -> int:
    ap = argparse.ArgumentParser(
        description="V2.0 OKX 官方公告 adapter → news_writer")
    ap.add_argument("--db", default=str(news_writer.DEFAULT_NEWS_DB))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--timeout", type=float, default=_REQUEST_TIMEOUT)
    args = ap.parse_args()
    res = collect(args.db, apply=args.apply, timeout_sec=args.timeout)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
