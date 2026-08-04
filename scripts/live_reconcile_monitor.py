# -*- coding: utf-8 -*-
"""Post-cycle live/demo reconciliation monitor (read-only, alert-only).

Invoked after a successful push stage.  It shortens detection latency for
exchange-side algo closes without restoring the stateful collection monitor or
ever applying/replaying ledger changes.
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
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(_project_path())
COLLECTORS = str(ROOT / "collectors")
if COLLECTORS not in sys.path:
    sys.path.insert(0, COLLECTORS)
from cycle_contract import validate_cycle_id  # noqa: E402

RECON = ROOT / "scripts" / "reconcile_exchange_closes.py"
QQ_PUSH = ROOT / "scripts" / "qq_push.py"
LOG_DIR = ROOT / "logs" / "reconcile"
STAGE_STATUS_DIR = Path(
    os.environ.get("OKX_STAGE_STATUS_DIR") or ROOT / "logs" / "stage-status")
CST = timezone(timedelta(hours=8))
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_MARKERS = (
    "[GHOST-EXACT]", "[GHOST-FUZZY]", "[OVER_CLOSED]",
    "[UNRECORDED]", "[LEFTOVER]",
)
_ROOT_SUFFIX_RE = re.compile(r"-r[0-9a-f]{10}$")


def now_cst() -> datetime:
    return datetime.now(CST)


def _root_namespace(db_root: Path) -> str:
    resolved = Path(db_root).resolve()
    if resolved == (ROOT / "db").resolve():
        return ""
    return "r" + hashlib.sha256(
        os.path.normcase(os.fspath(resolved)).encode("utf-8")
    ).hexdigest()[:10]


def _findings(out: str, cap: int = 10) -> str:
    # 与 reconcile_daily 的用户可读摘要保持一致，但本脚本独立可运行。
    lines: list[str] = []
    capture_children = False
    for raw in str(out or "").splitlines():
        text = raw.strip()
        if any(marker in text for marker in _MARKERS):
            lines.append(text)
            capture_children = (
                text.startswith("[OVER_CLOSED]")
                or text.startswith("[UNRECORDED]")
            )
            continue
        if capture_children and raw[:1].isspace() and text:
            lines.append(text)
            continue
        capture_children = False
    return " | ".join(lines[:cap]) or "(无分类行，见完整输出)"


def evaluate(rc: int, out: str) -> dict:
    markers = [marker for marker in _MARKERS if marker in str(out or "")]
    issue = rc != 0 or bool(markers)
    if rc == 0 and not markers:
        return {"ok": True, "issue": False, "rc": rc, "markers": []}
    classes = []
    mapping = {
        "[GHOST-EXACT]": "GHOST-EXACT 可补",
        "[GHOST-FUZZY]": "GHOST-FUZZY 含糊",
        "[OVER_CLOSED]": "OVER_CLOSED 缺 open",
        "[UNRECORDED]": "UNRECORDED 缺 open",
        "[LEFTOVER]": "LEFTOVER 未归因成交",
    }
    for marker in markers:
        classes.append(mapping[marker])
    if not classes:
        classes.append("对账执行错误")
    findings = _findings(out)
    if not markers:
        detail = " ".join(str(out or "").strip().split())
        findings = detail[-900:] or "(执行无输出)"
    return {
        "ok": rc in (0, 1, 3),
        "issue": issue,
        "rc": rc,
        "markers": markers,
        "classification": " + ".join(classes),
        "findings": findings,
    }


def active_runner(profile: str, db_root: Path | None = None) -> dict | None:
    cutoff = now_cst() - timedelta(minutes=45)
    namespace = _root_namespace(Path(db_root or (ROOT / "db")))
    try:
        paths = sorted(
            (
                path for path in STAGE_STATUS_DIR.glob(f"{profile}-*.json")
                if (
                    path.stem.endswith(f"-{namespace}")
                    if namespace else not _ROOT_SUFFIX_RE.search(path.stem)
                )
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:8]
    except OSError:
        return None
    for path in paths:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            started = datetime.strptime(
                str(item.get("started_at") or "")[:19],
                "%Y-%m-%d %H:%M:%S",
            ).replace(tzinfo=CST)
        except (OSError, ValueError, TypeError):
            continue
        if item.get("status") == "running" and started >= cutoff:
            return {
                "path": str(path),
                "cycle_id": item.get("cycle_id"),
                "started_at": item.get("started_at"),
            }
    return None


def active_live_runner() -> dict | None:
    """Backward-compatible live probe used by existing diagnostics/tests."""
    return active_runner("live")


def run_reconcile(profile: str = "live",
                  db_root: Path | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            [sys.executable, str(RECON), "--profile", profile,
             "--db-root", str(Path(db_root or (ROOT / "db")))],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=180, creationflags=_CREATE_NO_WINDOW)
        return int(proc.returncode), (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:
        return 99, f"{type(exc).__name__}: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", default=None, type=validate_cycle_id)
    ap.add_argument("--profile", choices=("live", "demo"), default="live")
    ap.add_argument("--db-root", default=str(ROOT / "db"))
    ap.add_argument("--dry-run", action="store_true",
                    help="只检测和打印，不写告警文件、不推 QQ")
    args = ap.parse_args()

    runtime_db_root = Path(args.db_root).resolve()
    active = active_runner(args.profile, runtime_db_root)
    if active:
        print(json.dumps({
            "ok": True,
            "issue": False,
            "skipped": f"{args.profile}_runner_active",
            "active": active,
            "cycle_id": args.cycle or None,
        }, ensure_ascii=False, indent=1))
        return 0

    rc, out = run_reconcile(args.profile, runtime_db_root)
    report = {
        "ts": now_cst().strftime("%Y-%m-%d %H:%M:%S"),
        "cycle_id": args.cycle or None,
        "profile": args.profile,
        **evaluate(rc, out),
    }
    if not report["issue"]:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    text = (
        f"⚠️ {args.profile.upper()} 日内账实对账告警 [P1] "
        f"[{now_cst():%Y-%m-%d %H:%M}]"
        f" (统一QQ告警)\n"
        f"· cycle={args.cycle or 'post-cycle'} rc={rc} "
        f"{report.get('classification', '')}\n"
        f"· {report.get('findings')}\n"
        "· 处置：只告警、不自动 apply/replay；先核交易所订单/fills，"
        "GHOST-EXACT 也必须逐笔 --ordid 并先在线备份。\n"
    )
    if args.dry_run:
        report["alert"] = {"dry_run": True, "would_send": text}
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 1

    digest = hashlib.sha256(
        (str(report.get("classification")) + "|" + str(report.get("findings")))
        .encode("utf-8")
    ).hexdigest()[:20]
    namespace = _root_namespace(runtime_db_root)
    alert_dir = LOG_DIR / namespace if namespace else LOG_DIR
    alert_dir.mkdir(parents=True, exist_ok=True)
    alert_file = (
        alert_dir / f"{args.profile}_monitor_{now_cst():%Y%m%d_%H%M%S}.txt")
    alert_file.write_text(text, encoding="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, str(QQ_PUSH), "--content-file", str(alert_file),
             "--alert",  # 告警走 C2C 私聊，不混进业务播报群（2026-08-04）
             "--dedupe-key", (
                 f"{args.profile}-reconcile-monitor:{namespace}:{digest}"
                 if namespace else f"{args.profile}-reconcile-monitor:{digest}"
             ),
             "--db-root", str(runtime_db_root)],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60, creationflags=_CREATE_NO_WINDOW)
        report["alert"] = {
            "rc": int(proc.returncode),
            "dedupe_key": (
                f"{args.profile}-reconcile-monitor:{namespace}:{digest}"
                if namespace else f"{args.profile}-reconcile-monitor:{digest}"
            ),
        }
    except Exception as exc:
        report["alert"] = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
