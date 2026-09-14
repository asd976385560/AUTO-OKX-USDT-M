# -*- coding: utf-8 -*-
"""预注册的开仓策略纪元（policy epoch）——EV 先验的样本可比性单一真源。

**这个模块只影响 EV 先验（`p_win`）的取样，绝不影响任何验收或报表口径。**
已平仓交易的盈亏、胜率、日/周/月报统计与 `audit_live_profitability` 生命周期诊断
全部照旧全量计入；目标10的周交易净利润也只按完整官方账单周窗计算，不受策略纪元
筛选——那批钱是真亏的，永远算数。本模块解决的是另一个问题：**拿已撤回策略
下产生的样本，去当现行策略下这一笔的胜率先验，统计上是错的**（policy shift）。
这与 2026-08-06 demo 全量下线时把 117 条 demo 样本移出经验池是同一个道理：不同
授权口径的样本不能混池当先验。

背景事实（2026-08-20 实测）：
  - 2026-08-14 上线的「低占用主动性/探针」条款于 2026-08-17 撤回（复盘：5 天净值
    -20.6%）。该区间产生的已结算样本 n=38、胜 6，胜率 15.8%。
  - 经验池全池（live/open/已结算）n=176、胜 59，p_win=33.5%；剔除该区间后
    n=138、胜 53，p_win=38.4%。
  - 典型候选的盈亏平衡胜率（3×ATR 止损约 4%、RR2、摩擦 0.2%）≈ 34.97%。
    也就是说这一个纪元的样本足以把全池先验从「越过盈亏平衡」压到「压线之下」，
    而系统没有任何机制让它回来——不开仓就不产生新样本。

纪元边界只向前预注册、不追溯改写；判定是 `cycle_id`（开仓轮）的纯函数，因此
不需要 schema 列、不需要写库回填，任何时候重算都得到同一结果。
本模块只有常量与纯函数：不读库、不写文件、不发请求、不下单。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

CST = timezone(timedelta(hours=8))

EPOCH_BASELINE = "baseline"
EPOCH_WITHDRAWN_PROBE = "withdrawn_probe_20260814"
EPOCH_UNKNOWN = "unknown"

# 2026-08-14 「低占用主动性/探针」条款实装 → 2026-08-17 16:30 撤回并重新部署
# （证据：`reports/quality/backups/loss-remediation-20260817-1630/`，
#  `agents/live_trader.md` V2.4 change-summary，skill.md 2026-08-17 条）。
# 起点取 08-14 自然日开始（当日首个开仓信号为 2026-08-14T00:45），终点取撤回
# 部署时刻；区间左闭右开。
PROBE_CLAUSE_START_CST = "2026-08-14T00:00:00+08:00"
PROBE_CLAUSE_END_CST = "2026-08-17T16:30:00+08:00"

# 当前生效纪元：EV 先验默认只采本纪元样本。
CURRENT_EPOCH = EPOCH_BASELINE

# 被排除出 EV 先验的纪元。**只此一处登记**，消费端不得各自硬编码区间。
EXCLUDED_FROM_EV_PRIOR = frozenset({EPOCH_WITHDRAWN_PROBE})


def _parse_cst(value: str | datetime) -> Optional[datetime]:
    """把 cycle_id / 时间戳解析成北京时间；无法解析返回 None（不猜）。"""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip().replace(" ", "T")
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            # cycle_id 恒为 `YYYY-MM-DDTHH:MM`；只在更短前缀时退到日粒度。
            try:
                parsed = datetime.strptime(text[:10], "%Y-%m-%d")
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CST)
    return parsed.astimezone(CST)


def policy_epoch(cycle_id: str | datetime | None) -> str:
    """返回该开仓轮所处的策略纪元。

    无法解析的 cycle_id 返回 `unknown`——**按当前纪元同等采信**（见
    `is_ev_prior_eligible`）：宁可把身份不明的样本留在先验里，也不静默剔除样本。
    """
    moment = _parse_cst(cycle_id)
    if moment is None:
        return EPOCH_UNKNOWN
    start = _parse_cst(PROBE_CLAUSE_START_CST)
    end = _parse_cst(PROBE_CLAUSE_END_CST)
    if start is not None and end is not None and start <= moment < end:
        return EPOCH_WITHDRAWN_PROBE
    return EPOCH_BASELINE


def is_ev_prior_eligible(cycle_id: str | datetime | None) -> bool:
    """该样本是否可作为**现行策略**下新交易的 EV 先验。

    注意语义边界：返回 False 只表示「不作本笔胜率先验」，不表示这笔交易不算数、
    不进胜率验收、不进净利统计——那些口径一律全量。
    """
    return policy_epoch(cycle_id) not in EXCLUDED_FROM_EV_PRIOR
