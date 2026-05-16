# -*- coding: utf-8 -*-
"""
_timeutils.py —— 项目统一的时间工具。

时区约定（详见 README "时区约定" 小节）：
    - 存储与计算：UTC ISO8601，格式 "YYYY-MM-DDTHH:MM:SSZ"
    - 人类输出：UTC + UTC+8 双显示，格式 "YYYY-MM-DDTHH:MM:SSZ (HH:MM +08)"
    - 妙想新闻 date 字段假设为 UTC+8

新代码请从本文件导入。历史模块保留其本地副本以避免 ms_to_iso 微秒精度差异
导致的下游格式变化。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))


def utc_now_iso() -> str:
    """当前 UTC 时刻，秒级精度，'Z' 后缀。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ms_to_iso(value: str | int | None) -> str | None:
    """毫秒 epoch (UTC) -> 'YYYY-MM-DDTHH:MM:SSZ'；输入空/非法返回 None。"""
    if value in (None, ""):
        return None
    try:
        ms = int(str(value))
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_to_dt(value: str | None) -> datetime | None:
    """容错读 ISO8601。同时支持 'Z' / '+00:00' / 带微秒，返回 aware datetime (UTC)。"""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    # datetime.fromisoformat 在 3.11+ 支持 'Z'，但为兼容旧版本统一替换
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_dual(value: str | datetime | None) -> str:
    """人类输出：'2026-04-20T07:30:00Z (15:30 +08)'。

    输入为 None / 解析失败时返回 '-'。
    """
    if value is None:
        return "-"
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
    else:
        dt = iso_to_dt(value)
        if dt is None:
            return str(value)
    utc_str = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    cst_str = dt.astimezone(CST).strftime("%H:%M +08")
    return f"{utc_str} ({cst_str})"


if __name__ == "__main__":
    print("utc_now_iso():", utc_now_iso())
    print("ms_to_iso(1713607800000):", ms_to_iso(1713607800000))
    print("ms_to_iso(None):", ms_to_iso(None))
    print("iso_to_dt('2026-04-20T07:30:00Z'):", iso_to_dt("2026-04-20T07:30:00Z"))
    print("iso_to_dt('2026-04-20T07:30:00+00:00'):", iso_to_dt("2026-04-20T07:30:00+00:00"))
    print("fmt_dual('2026-04-20T07:30:00Z'):", fmt_dual("2026-04-20T07:30:00Z"))
    print("fmt_dual(None):", fmt_dual(None))
