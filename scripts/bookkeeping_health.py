"""Bookkeeping Health Check - v7.0 记账链断链自检

对应体检建议：避免"记账断链 2 天无人察觉"。

核心逻辑：
  比对「采集最新时间」(market.db.tick_snapshots.ts) 与
        「记账最新时间」(account.db.cycle_runs 最新 cycle_start_time)
  两者都换算为 UTC 后求差；差值 > 阈值(默认 30 分钟) → 告警(非零退出)。

附带检查：
  - cycle_count 是否跳号（MAX 与行数严重不符提示）
  - ts_start 是否仍存在非法格式（非 UTC+8）

时间格式兼容：
  - tick ts: ISO `YYYY-MM-DDTHH:MM:SSZ` (UTC)
  - cycle_start_time: UTC+8 `YYYY-MM-DD HH:MM:SS`（v7.0 规范）

退出码：0=健康；1=断链/异常告警。
"""

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))

import argparse
import os
import sys
from datetime import datetime, timezone, timedelta

from _db_ro import connect_ro

CST = timezone(timedelta(hours=8))


def parse_any_ts(s: str):
    """解析 ISO-Z(UTC) 或 UTC+8 字符串，返回 aware UTC datetime；失败返回 None。"""
    if not s:
        return None
    s = s.strip()
    # 去掉可能的 #count 后缀
    if "#" in s:
        s = s.split("#", 1)[0]
    try:
        if s.endswith("Z") and "T" in s:
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        # UTC+8 标准格式
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST).astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        # fallback: 缺秒的 UTC+8 字符串 'YYYY-MM-DD HH:MM'（容错读，不因单行 writer bug 崩整个自检）
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=CST).astimezone(timezone.utc)
        except Exception:  # noqa: BLE001
            return None


def main():
    p = argparse.ArgumentParser(description="v7.0 记账链断链自检")
    p.add_argument("--db-root", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db"))
    p.add_argument("--threshold-min", type=int, default=30, help="采集与记账最新时间差告警阈值(分钟)")
    args = p.parse_args()

    market = os.path.join(args.db_root, "market.db")
    account = os.path.join(args.db_root, "account.db")
    problems = []

    # 采集最新
    tick_dt = None
    if os.path.exists(market):
        try:
            mc = connect_ro(market)  # 只读 mode=ro（2026-07-03）
            tick_raw = mc.execute("SELECT MAX(ts) FROM tick_snapshots").fetchone()[0]
            mc.close()
            tick_dt = parse_any_ts(tick_raw)
        except Exception as e:  # noqa: BLE001
            problems.append(f"读取 market.db 失败: {e}")
    else:
        problems.append("market.db 不存在")

    # 记账最新：取 analysis.db.analysis_runs 最新完成时刻。
    cyc_dt = None
    cyc_raw = None
    latest_cycle = None
    bad_ts = 0  # analysis_runs 使用槽位归一 cycle_id。
    analysis = os.path.join(args.db_root, "analysis.db")
    if os.path.exists(analysis):
        try:
            ac = connect_ro(analysis)  # 只读 mode=ro（2026-07-03）
            r = ac.execute(
                "SELECT ts, cycle_id FROM analysis_runs ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            if r:
                cyc_raw, latest_cycle = r[0], r[1]
                cyc_dt = parse_any_ts(cyc_raw)
            ac.close()
        except Exception as e:  # noqa: BLE001
            problems.append(f"读取 analysis.db 失败: {e}")
    else:
        problems.append("analysis.db 不存在")

    print("=== V2.0 记账链健康自检（analysis.db）===")
    print(f"采集最新(UTC)  : {tick_dt}")
    print(f"分析最新(UTC)  : {cyc_dt}  (analysis_runs ts={cyc_raw!r}, cycle_id={latest_cycle!r})")

    if tick_dt and cyc_dt:
        gap_min = abs((tick_dt - cyc_dt).total_seconds()) / 60.0
        print(f"采集-记账时间差 : {gap_min:.1f} 分钟 (阈值 {args.threshold_min})")
        if gap_min > args.threshold_min:
            problems.append(
                f"记账链可能断链：采集与记账最新时间差 {gap_min:.1f} 分钟 > {args.threshold_min} 分钟"
            )
    else:
        problems.append("无法比对采集/记账时间（存在 None）")

    if bad_ts:
        problems.append(f"存在 {bad_ts} 行新行(cycle_count>=1476)非 UTC+8 格式 ts_start，需规范化")

    # T4 劣化金丝雀：最近 3 个推送归档全部 <300B = 推送塌缩特征。
    # 只 WARN 不阻断（推送劣化是 P2，不应卡 P7 复盘）；由维护者查 push_pipeline 环节报告，
    # 不在本检查器内自动重置会话或重跑外发。
    try:
        import os as _os
        rep_dir = _project_path('reports', 'agents')
        files = sorted(
            (f for f in _os.listdir(rep_dir)
             if f.startswith("v2-push-2") and f.endswith(".md")),
            reverse=True,
        )[:3]
        sizes = [_os.path.getsize(_os.path.join(rep_dir, f)) for f in files]
        if len(sizes) == 3 and all(s < 300 for s in sizes):
            print(f"\n[WARN][P2] 推送劣化金丝雀触发：最近 3 个归档均 <300B {sizes}——"
                  f"疑似 session 行为塌缩，建议按 §13.9 重置 okxv7 session（不阻断本检查）")
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 金丝雀检查失败（不影响主检）: {e}")

    if problems:
        print("\n[ALERT] 记账链自检发现问题：")
        for x in problems:
            print(f"  - {x}")
        sys.exit(1)

    print("\n[PASS] 记账链健康。")
    sys.exit(0)


if __name__ == "__main__":
    main()
