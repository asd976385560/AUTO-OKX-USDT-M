# -*- coding: utf-8 -*-
"""Rebuild current playbook statistics from account.db.trade_experiences.

The active reviewer path must not use drill.db or legacy trade_events as current
business facts.  Historical numeric playbook fields are preserved only in an
explicit baseline JSON when requested; the values written by this script are a
deterministic projection of current, closed trade_experiences.

Default mode is read-only.  ``--apply`` updates account.db in one transaction.
For the first controlled source cutover, pass ``--baseline-out`` so the previous
numeric fields remain auditable without contaminating current statistics.
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
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = Path(_project_path('db', 'account.db'))
DEFAULT_SOURCE_MARKER = Path(
    _project_path('reports', 'quality', 'playbook_current_source_v1.json'))
CST = timezone(timedelta(hours=8))
PLAYBOOK_REF_RE = re.compile(r"playbook\s*#\s*(\d+)", re.IGNORECASE)


def now_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def parse_playbook_ids(value: Any) -> set[int]:
    """Parse canonical ids while refusing unrelated numbers in free text."""
    refs: set[int] = set()
    if value is None or isinstance(value, bool):
        return refs
    if isinstance(value, int):
        if value > 0:
            refs.add(value)
        return refs
    if isinstance(value, float):
        if value > 0 and value.is_integer():
            refs.add(int(value))
        return refs
    if isinstance(value, (list, tuple, set)):
        for item in value:
            refs.update(parse_playbook_ids(item))
        return refs
    if isinstance(value, dict):
        if "playbook_ref" in value:
            refs.update(parse_playbook_ids(value.get("playbook_ref")))
        return refs

    text = str(value).strip()
    if not text:
        return refs
    if text.startswith(("[", "{")):
        try:
            refs.update(parse_playbook_ids(json.loads(text)))
            return refs
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    if re.fullmatch(r"#?\s*\d+", text):
        number = int(text.replace("#", "").strip())
        if number > 0:
            refs.add(number)
    for match in PLAYBOOK_REF_RE.finditer(text):
        number = int(match.group(1))
        if number > 0:
            refs.add(number)
    return refs


def _playbooks(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT id,summary,evidence_count,win_count,loss_count,win_rate,"
        "avg_pnl_pct,last_validated_cycle FROM playbook ORDER BY id"
    ).fetchall()


def build_plan(con: sqlite3.Connection) -> dict[str, Any]:
    """Return deterministic current-source rows without writing the database."""
    plays = _playbooks(con)
    by_play: dict[int, dict[str, Any]] = {
        int(row["id"]): {
            "experience_ids": set(),
            "profiles": set(),
            "pnls": [],
            "unresolved_outcomes": 0,
        }
        for row in plays
    }
    invalid_refs: list[dict[str, Any]] = []

    rows = con.execute(
        "SELECT id,profile,playbook_ref,pnl_pct,status,closed_at,ts "
        "FROM trade_experiences "
        "WHERE status='closed' AND playbook_ref IS NOT NULL "
        "AND TRIM(CAST(playbook_ref AS TEXT))<>'' ORDER BY id"
    ).fetchall()
    for row in rows:
        ids = parse_playbook_ids(row["playbook_ref"])
        known = sorted(pid for pid in ids if pid in by_play)
        unknown = sorted(pid for pid in ids if pid not in by_play)
        if not known or unknown:
            invalid_refs.append({
                "experience_id": row["id"],
                "raw_ref": row["playbook_ref"],
                "known_ids": known,
                "unknown_ids": unknown,
            })
        for pid in known:
            stats = by_play[pid]
            stats["experience_ids"].add(int(row["id"]))
            stats["profiles"].add(str(row["profile"] or "unknown"))
            if row["pnl_pct"] is None:
                stats["unresolved_outcomes"] += 1
            else:
                stats["pnls"].append(float(row["pnl_pct"]))

    try:
        latest_cycle = con.execute(
            "SELECT MAX(cycle_count) FROM cycle_runs").fetchone()[0] or 0
    except sqlite3.Error:
        latest_cycle = 0

    updates: list[dict[str, Any]] = []
    changed = 0
    attributed_experience_ids: set[int] = set()
    for row in plays:
        pid = int(row["id"])
        stats = by_play[pid]
        attributed_experience_ids.update(stats["experience_ids"])
        pnls = stats["pnls"]
        evidence = len(stats["experience_ids"])
        win = sum(1 for pnl in pnls if pnl > 0)
        loss = sum(1 for pnl in pnls if pnl < 0)
        resolved = len(pnls)
        win_rate = (win / resolved) if resolved else None
        avg_pnl_pct = (sum(pnls) / resolved) if resolved else None
        current = {
            "evidence_count": int(row["evidence_count"] or 0),
            "win_count": int(row["win_count"] or 0),
            "loss_count": int(row["loss_count"] or 0),
            "win_rate": row["win_rate"],
            "avg_pnl_pct": row["avg_pnl_pct"],
            "last_validated_cycle": row["last_validated_cycle"],
        }
        target = {
            "evidence_count": evidence,
            "win_count": win,
            "loss_count": loss,
            "win_rate": win_rate,
            "avg_pnl_pct": avg_pnl_pct,
            "last_validated_cycle": latest_cycle,
        }
        is_changed = current != target
        changed += int(is_changed)
        updates.append({
            "id": pid,
            "summary": row["summary"],
            "profiles": sorted(stats["profiles"]),
            "resolved_outcomes": resolved,
            "unresolved_outcomes": stats["unresolved_outcomes"],
            "current": current,
            "target": target,
            "changed": is_changed,
        })

    return {
        "source": "account.db.trade_experiences.closed.playbook_ref",
        "generated_at": now_cst(),
        "playbooks": len(plays),
        "closed_experiences_with_ref": len(rows),
        "attributed_experiences": len(attributed_experience_ids),
        "changed_playbooks": changed,
        "invalid_refs": invalid_refs,
        "updates": updates,
    }


def write_baseline(path: Path, db_path: Path, plan: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"baseline 已存在，拒绝覆盖: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "kind": "playbook_legacy_numeric_baseline",
        "source_db": str(db_path.resolve()),
        "captured_at": now_cst(),
        "replacement_source": plan["source"],
        "rows": [
            {
                "id": item["id"],
                "summary": item["summary"],
                **item["current"],
            }
            for item in plan["updates"]
        ],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def apply_plan(
    con: sqlite3.Connection,
    plan: dict[str, Any],
) -> int:
    con.execute("BEGIN IMMEDIATE")
    try:
        for item in plan["updates"]:
            target = item["target"]
            con.execute(
                "UPDATE playbook SET evidence_count=?,win_count=?,loss_count=?,"
                "win_rate=?,avg_pnl_pct=?,last_validated_cycle=? WHERE id=?",
                (
                    target["evidence_count"],
                    target["win_count"],
                    target["loss_count"],
                    target["win_rate"],
                    target["avg_pnl_pct"],
                    target["last_validated_cycle"],
                    item["id"],
                ),
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    return int(plan["changed_playbooks"])


def write_source_marker(
    path: Path,
    db_path: Path,
    plan: dict[str, Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": plan["source"],
        "source_db": str(db_path.resolve()),
        "initialized_at": now_cst(),
        "playbooks": plan["playbooks"],
        "attributed_experiences": plan["attributed_experiences"],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "按现役 trade_experiences 重建 playbook 统计；默认只读 dry-run"
        )
    )
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--baseline-out",
        type=Path,
        help="apply 前原子保存旧数值基线；目标已存在则拒绝覆盖",
    )
    parser.add_argument(
        "--source-marker",
        type=Path,
        default=DEFAULT_SOURCE_MARKER,
        help="首次 current-source apply 成功后的原子标记",
    )
    parser.add_argument(
        "--min-attributed",
        type=int,
        default=1,
        help="apply 所需最少可归因 closed experience 数（默认1）",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    uri = f"file:{args.db.resolve().as_posix()}?mode={'rw' if args.apply else 'ro'}"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.text_factory = lambda value: value.decode("utf-8", errors="replace")
    try:
        plan = build_plan(con)
        if args.apply:
            if plan["attributed_experiences"] < max(1, args.min_attributed):
                raise RuntimeError(
                    "current playbook 引用尚未形成，拒绝清零旧统计；"
                    f"attributed={plan['attributed_experiences']} "
                    f"required={max(1, args.min_attributed)}")
            first_cutover = not args.source_marker.exists()
            if first_cutover and args.baseline_out is None:
                raise RuntimeError(
                    "首次 current-source apply 必须传 --baseline-out 保存旧数值")
            if first_cutover:
                write_baseline(args.baseline_out, args.db, plan)
            changed = apply_plan(con, plan)
            quick = con.execute("PRAGMA quick_check").fetchone()[0]
            if quick != "ok":
                raise RuntimeError(f"account.db quick_check failed: {quick}")
            if first_cutover:
                write_source_marker(args.source_marker, args.db, plan)
            result = {
                **plan,
                "mode": "apply",
                "applied_changed_playbooks": changed,
                "quick_check": quick,
            }
        else:
            result = {**plan, "mode": "dry-run"}
    finally:
        con.close()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        prefix = "[APPLY]" if args.apply else "[DRY-RUN]"
        print(
            f"{prefix} source={result['source']} "
            f"playbooks={result['playbooks']} "
            f"closed_refs={result['closed_experiences_with_ref']} "
            f"attributed={result['attributed_experiences']} "
            f"changed={result['changed_playbooks']} "
            f"invalid_refs={len(result['invalid_refs'])}"
        )
        for item in result["updates"]:
            if not item["changed"]:
                continue
            target = item["target"]
            print(
                f"  #{item['id']}: evidence={target['evidence_count']} "
                f"win={target['win_count']} loss={target['loss_count']} "
                f"wr={target['win_rate']} avg_pnl_pct={target['avg_pnl_pct']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
