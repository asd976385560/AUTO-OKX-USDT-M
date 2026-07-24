# -*- coding: utf-8 -*-
"""_tsnorm.py — 时间戳归一的**单一权威实现**（架构评审 #6，2026-07-07）。

背景：本项目 ts 混两种格式并存——market/regime 存 UTC-Z（`YYYY-MM-DDTHH:MM:SSZ`），
news/analysis/trades/account/ledger 存 UTC+8 裸串（`YYYY-MM-DD HH:MM:SS`）。跨表比较
靠每个消费方各自记得写 CASE 归一，漏一处即 8h 偏移（多次造成假 stale/假 P0）。
本模块把归一收敛成一处，新代码/迁移一律 import 此处，**禁再各自手写 CASE/parse**。

约定：项目「墙钟」= UTC+8（CST）。归一目标 = **CST naive datetime**（无 tzinfo，
可与 datetime.now(CST).replace(tzinfo=None) 直接比）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

CST = timezone(timedelta(hours=8))


def parse_to_cst(ts) -> Optional[datetime]:
    """任意项目内 ts 串 → CST naive datetime。无法解析返 None。

    - 带 'Z' 或 ISO 'T' 偏移：按 UTC 解析后转 CST。
    - 裸空格串 'YYYY-MM-DD HH:MM:SS'：项目约定即 CST，原样。
    """
    if ts is None:
        return None
    s = str(ts).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.astimezone(CST).replace(tzinfo=None)
        if "T" in s and ("+" in s[10:] or s[10:].count("-") > 0):
            # 带偏移的 ISO（非 Z）
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is not None:
                return dt.astimezone(CST).replace(tzinfo=None)
            return dt
        # 裸串（空格或无偏移 T）：约定 CST
        return datetime.strptime(s.replace("T", " ", 1)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def age_minutes(ts, now: Optional[datetime] = None) -> Optional[float]:
    """ts 距今分钟数（CST 口径）。now 缺省取 datetime.now(CST) naive。解析失败 None。"""
    dt = parse_to_cst(ts)
    if dt is None:
        return None
    ref = now if now is not None else datetime.now(CST).replace(tzinfo=None)
    if getattr(ref, "tzinfo", None) is not None:
        ref = ref.astimezone(CST).replace(tzinfo=None)
    return (ref - dt).total_seconds() / 60.0


def sql_norm(col: str) -> str:
    """返回把某列归一到「可比 CST 空格串」的 SQL 片段（供 SQLite 查询内联）。

    统一各处自写的 `CASE WHEN ts LIKE '%Z' THEN datetime(ts,'+8 hours') ELSE datetime(ts) END`：
    带 Z 的按 UTC 解析 +8h 转 CST；裸串已是 CST 原样 datetime()。
    用法：f"... WHERE {sql_norm('ts')} >= datetime('now','+8 hours','-1 hour')"
    """
    return (f"CASE WHEN {col} LIKE '%Z' THEN datetime({col}, '+8 hours') "
            f"ELSE datetime({col}) END")


def is_utc_z(ts) -> bool:
    """该 ts 串是否为 UTC-Z 格式（用于巡检/迁移判定）。"""
    return bool(ts) and str(ts).strip().endswith("Z")
