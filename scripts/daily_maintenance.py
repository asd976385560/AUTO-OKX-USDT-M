# -*- coding: utf-8 -*-
r"""daily_maintenance.py — 日频运维合并入口（2026-07-17 cron 治理，两轮合并后=
okx-reconcile + okx-log-rotate + okx-audit-snapshot 三条日频 cron 合为一条
okx-daily-maintenance，07:55 起跑）。

顺序跑：① reconcile_daily.py（交易所侧对账分级：demo 自动 --apply / live dry+P1——
          **排第一**且 07:55 起跑，reviewer 08:05 复盘读到的就是对账后的干净账本）
        ② log_rotate.py --apply --days 7（logs/trigger+push 超 7 天轮转）
        ③ audit_snapshot.py（audit_events 增量导出，防滚动窗丢失）
        ④ reports_rotate.py --apply（reports/agents+push 超 30 天月度压包，
          封顶无界增长——2026-07-17 主人拍板）
        ⑤ collect_macro_events.py（未来7天高重要度经济日历）
        ⑥ collect_account_bills.py（手续费/资金费/已实现盈亏账单）
        ⑦ quality_metrics.py（六项卡、历史取舍、双盘与执行质量指标，供 08:05 reviewer）
每步独立 fail-safe：一步失败/超时不阻断下一步；任一失败聚合 exit 1（cron 记 error
可见），全过 exit 0。新增日频运维项往这里加，不再开新 cron。
注意：本 cron 因含 reconcile（demo 会真动账本）已入 fulltest BUSINESS_CRONS——
测试窗内随业务 cron 一并停/复。
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS = Path(__file__).resolve().parent
STEPS = [
    # (名字, argv, 单步超时秒, 合法退出码)。reconcile 最坏 4 次 OKX API 往返（demo
    # dry→apply→复检→live dry）；其 rc=1=「有账实差异且告警已推」（脚本工作正常，
    # 差异经 QQ P1 走人工通道），只有 rc≥2 才算本步失败。
    ("reconcile", [str(SCRIPTS / "reconcile_daily.py")], 1200, (0, 1)),
    ("log_rotate", [str(SCRIPTS / "log_rotate.py"), "--apply", "--days", "7"], 120, (0,)),
    ("audit_snapshot", [str(SCRIPTS / "audit_snapshot.py")], 120, (0,)),
    ("reports_rotate", [str(SCRIPTS / "reports_rotate.py"), "--apply"], 120, (0,)),
    ("macro_events", [str(SCRIPTS / "collect_macro_events.py")], 90, (0,)),
    ("account_bills", [str(SCRIPTS / "collect_account_bills.py")], 120, (0,)),
    # ⑦ 质量指标生成（Phase 0，2026-07-18）--产出 reports/quality/quality_metrics_YYYY-MM-DD.json
    #    reviewer 08:05 开场读该文件做数据驱动复盘。非致命：失败不阻断日报。
    ("quality_metrics", [str(SCRIPTS / "quality_metrics.py")], 120, (0,)),
]


def main() -> int:
    report = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "steps": {}}
    all_ok = True
    for name, argv, step_timeout, ok_codes in STEPS:
        try:
            p = subprocess.run([sys.executable, *argv], capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=step_timeout)
            tail = (p.stdout or "").strip().splitlines()[-3:]
            report["steps"][name] = {"rc": p.returncode, "tail": tail}
            if p.returncode not in ok_codes:
                all_ok = False
                err_tail = (p.stderr or "").strip().splitlines()[-3:]
                report["steps"][name]["stderr"] = err_tail
        except Exception as e:
            all_ok = False
            report["steps"][name] = {"rc": 99, "error": f"{type(e).__name__}: {e}"}
    report["ok"] = all_ok
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
