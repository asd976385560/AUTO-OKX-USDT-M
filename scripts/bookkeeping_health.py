"""Pipeline Freshness Check - 采集到分析的新鲜度自检。

本检查只比较 ``market.db.tick_snapshots`` 与成功的
``analysis.db.analysis_runs`` 最新时间，回答“采集与分析是否仍在向前推进”。
它不证明 trades、execution_intents、journal 或 repair_queue 健康；真实账本
不变量由 ``ledger_invariants.py`` 独立检查并进入 Reviewer ready 硬闸。

两端时间统一换算为 UTC；差值超过阈值（默认 30 分钟）返回非零。
"""


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))

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
    p = argparse.ArgumentParser(description="采集到分析的新鲜度自检")
    p.add_argument("--db-root", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db"))
    p.add_argument("--threshold-min", type=int, default=30,
                   help="采集与成功分析最新时间差告警阈值(分钟)")
    args = p.parse_args()

    market = os.path.join(args.db_root, "market.db")
    problems = []

    # 采集最新
    tick_dt = None
    if os.path.exists(market):
        try:
            mc = connect_ro(market)  # 只读 mode=ro（2026-07-03）
            # MAX(ts) 在本表安全且唯一可行（2026-07-29 审计实测）：tick_snapshots.ts 全列
            # 统一 UTC-Z（ts_audit MIXED=0 持续巡检），词典序即时序，且走覆盖索引 ~2ms。
            # 勿改 rowid DESC——本表 INSERT OR REPLACE 会改 rowid；勿改 datetime(ts)——全排序 ~436ms。
            tick_raw = mc.execute("SELECT MAX(ts) FROM tick_snapshots").fetchone()[0]
            mc.close()
            tick_dt = parse_any_ts(tick_raw)
        except Exception as e:  # noqa: BLE001
            problems.append(f"读取 market.db 失败: {e}")
    else:
        problems.append("market.db 不存在")

    # 分析最新：取 analysis.db.analysis_runs 最新成功完成时刻。
    cyc_dt = None
    cyc_raw = None
    latest_cycle = None
    analysis = os.path.join(args.db_root, "analysis.db")
    if os.path.exists(analysis):
        try:
            ac = connect_ro(analysis)  # 只读 mode=ro（2026-07-03）
            # F1：只认 status='ok'。9:30 硬闸占位行(error)有新鲜 ts 但零业务
            # 内容，不得冒充「分析最新」掩盖记账链停摆。
            r = ac.execute(
                "SELECT ts, cycle_id FROM analysis_runs WHERE status='ok' "
                "ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            if r:
                cyc_raw, latest_cycle = r[0], r[1]
                cyc_dt = parse_any_ts(cyc_raw)
            ac.close()
        except Exception as e:  # noqa: BLE001
            problems.append(f"读取 analysis.db 失败: {e}")
    else:
        problems.append("analysis.db 不存在")

    print("=== 采集到分析的新鲜度自检 ===")
    print(f"采集最新(UTC)  : {tick_dt}")
    print(f"成功分析最新(UTC): {cyc_dt}  (analysis_runs ts={cyc_raw!r}, cycle_id={latest_cycle!r})")

    if tick_dt and cyc_dt:
        gap_min = abs((tick_dt - cyc_dt).total_seconds()) / 60.0
        print(f"采集-分析时间差 : {gap_min:.1f} 分钟 (阈值 {args.threshold_min})")
        if gap_min > args.threshold_min:
            problems.append(
                f"分析链可能断链：采集与成功分析最新时间差 {gap_min:.1f} 分钟 > {args.threshold_min} 分钟"
            )
    else:
        problems.append("无法比对采集/分析时间（存在 None）")

    # T4 劣化金丝雀（2026-06-12 #2295 事故）：最近 3 个推送归档全部 <300B = 推送塌缩特征。
    # 只 WARN 不阻断（推送劣化是 P2，不应卡 P7 复盘）；由维护者查 push_pipeline 环节报告，
    # 不在本检查器内自动重置会话或重跑外发。
    try:
        import os as _os
        rep_dir = _public_project_path('reports', 'agents')
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
        print("\n[ALERT] 采集-分析新鲜度自检发现问题：")
        for x in problems:
            print(f"  - {x}")
        sys.exit(1)

    print("\n[PASS] 采集-分析新鲜度健康。")
    sys.exit(0)


if __name__ == "__main__":
    main()
