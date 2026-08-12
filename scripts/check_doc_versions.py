# -*- coding: utf-8 -*-
"""V2.0 文档头覆盖与既有 doc_versions 登记检查。

两层职责严格分开：
1. ``COVERAGE_DOCS``：只读校验当前文档、模板与角色手册均有版本头；
2. ``DB_TRACKED_DOCS``：公开版本只比对可发布的 ``skill.md``；私有
   ``config.md`` 不属于仓库，扩大的静态覆盖不会要求数据库新增记录。

默认全程只读。``--apply`` 仅在显式提供 ``--backup-dir`` 后 UPSERT 既有登记
子集；绝不删除、重排或接管其它 doc_versions 行。
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CST = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%d %H:%M:%S"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "db" / "account.db"

COVERAGE_DOCS = (
    "skill.md",
    "README.md",
    "README.en.md",
    "config.example.md",
    "PUBLIC_RELEASE.md",
    "SECURITY.md",
    "docs/README.md",
    "docs/agent-deployment.zh-CN.md",
    "docs/agent-deployment.en.md",
    "templates/analysis_template.md",
    "templates/trade_template.md",
    "templates/push_template.md",
    "templates/daily_template.md",
    "agents/analyst.md",
    "agents/live_trader.md",
    "agents/reviewer.md",
    "agents/news_scout.md",
)

# 公开仓库只登记其可发布事实源。本地 config.md 永不纳入公开静态面。
DB_TRACKED_DOCS = ("skill.md",)


def strip_version_prefix(value: str) -> str:
    value = (value or "").strip()
    while value and value[0] in ("v", "V"):
        value = value[1:]
    return value


def parse_doc_header(path: Path | str) -> dict[str, str]:
    path = Path(path)
    if not path.exists():
        return {
            "version": "MISSING",
            "updated": "",
            "by": "",
            "summary": f"file not found: {path}",
        }
    try:
        text = path.read_text(encoding="utf-8")[:4000]
    except Exception as exc:  # noqa: BLE001
        return {
            "version": "ERROR",
            "updated": "",
            "by": "",
            "summary": f"read error: {exc}",
        }
    out = {"version": "?", "updated": "", "by": "", "summary": ""}
    for key, target in (
        ("doc-version", "version"),
        ("last-updated", "updated"),
        ("updated-by", "by"),
        ("change-summary", "summary"),
    ):
        match = re.search(rf"{key}\s*:\s*([^\r\n]*)", text, re.IGNORECASE)
        if match:
            out[target] = match.group(1).strip()
    return out


def online_backup(source: Path, target: Path) -> None:
    """只导出 doc_versions 一张表，**不再克隆整个 account.db**。

    2026-08-06 修：原实现是 `src.backup(dst)`，把整库（38 MB）复制一份，只为
    保护一次两行 UPSERT——`backups/doc-versions/` 因此攒了 4 份共 152 MB 的
    account.db 副本。而 `_sync_tracked` 的写入面**只有 doc_versions**，回滚要
    的也只有这张表，整库副本里其余 99.99% 是纯噪音（还让人误以为那是一份可用
    的账户库备份——它只是某次文档登记前的随机时点快照，不该被当账本备份使用）。

    导出目标仍是独立 SQLite 文件：同一套工具可校验、可直接 ATTACH 回滚，
    体积从 38 MB 降到几 KB。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    dst = sqlite3.connect(target, timeout=10)
    try:
        ddl = src.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='doc_versions'").fetchone()
        if not ddl or not ddl[0]:
            raise RuntimeError("源库无 doc_versions 表，拒绝在无备份的情况下写入")
        cursor = src.execute("SELECT * FROM doc_versions")
        columns = [d[0] for d in cursor.description]
        rows = cursor.fetchall()
        with dst:
            dst.execute(ddl[0])
            if rows:
                dst.executemany(
                    "INSERT INTO doc_versions VALUES "
                    f"({','.join('?' * len(columns))})", rows)
        integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"backup integrity_check={integrity}")
        # 回读校验：备份行数必须与源一致，否则这份备份不可信，宁可不写库
        n_dst = dst.execute("SELECT COUNT(*) FROM doc_versions").fetchone()[0]
        if n_dst != len(rows):
            raise RuntimeError(f"backup 行数不符 src={len(rows)} dst={n_dst}")
    finally:
        dst.close()
        src.close()


def _parse_coverage() -> tuple[dict[str, dict[str, str]], list[str]]:
    parsed: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    for relative in COVERAGE_DOCS:
        info = parse_doc_header(ROOT / relative)
        parsed[relative] = info
        if info["version"] in {"?", "MISSING", "ERROR"}:
            errors.append(f"{relative}: {info['summary'] or '缺 doc-version'}")
        if not info["updated"]:
            errors.append(f"{relative}: 缺 last-updated")
    return parsed, errors


def _read_db_rows(db_path: Path) -> dict[str, dict[str, str]]:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        rows = con.execute(
            "SELECT doc_path, doc_version, last_updated, updated_by, change_summary "
            "FROM doc_versions ORDER BY doc_path"
        ).fetchall()
    finally:
        con.close()
    return {
        row[0]: {
            "version": row[1] or "",
            "updated": row[2] or "",
            "by": row[3] or "",
            "summary": row[4] or "",
        }
        for row in rows
    }


def _sync_tracked(db_path: Path, parsed: dict[str, dict[str, str]]) -> None:
    con = sqlite3.connect(db_path, timeout=30)
    try:
        con.execute("BEGIN IMMEDIATE")
        for name in DB_TRACKED_DOCS:
            info = parsed[name]
            con.execute(
                "INSERT INTO doc_versions "
                "(doc_path, doc_version, last_updated, updated_by, change_summary) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(doc_path) DO UPDATE SET "
                "doc_version=excluded.doc_version, last_updated=excluded.last_updated, "
                "updated_by=excluded.updated_by, change_summary=excluded.change_summary",
                (name, info["version"], info["updated"], info["by"], info["summary"]),
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查当前文档版本头与既有 DB 登记")
    parser.add_argument("--apply", action="store_true", help="仅同步既有 DB 登记子集")
    parser.add_argument("--backup-dir", help="--apply 必填；写库前做 SQLite 在线备份")
    parser.add_argument("--static-only", action="store_true", help="只校验文件头，不访问 DB")
    parser.add_argument("--db", default=os.fspath(DEFAULT_DB), help="doc_versions 所在 account.db")
    args = parser.parse_args(argv)

    if args.apply and args.static_only:
        parser.error("--apply 与 --static-only 互斥")
    if args.apply and not args.backup_dir:
        parser.error("--apply 必须同时提供 --backup-dir")

    parsed, errors = _parse_coverage()
    print(f"== 文档头覆盖：{len(COVERAGE_DOCS)} 份 ==")
    for name in COVERAGE_DOCS:
        info = parsed[name]
        print(
            f"  {name:52s} "
            f"v{strip_version_prefix(info['version']):16s} {info['updated']:10s}"
        )
    if errors:
        for item in errors:
            print(f"[ERROR] {item}", file=sys.stderr)
        return 2
    if args.static_only:
        print("OK 静态文档头完整；未访问生产 DB")
        return 0

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[ERROR] DB 不存在: {db_path}", file=sys.stderr)
        return 2
    if args.apply:
        stamp = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
        # 文件名改叫 doc_versions-*：旧名 account-pre-doc-versions-* 会让人
        # 以为那是一份 account.db 备份（它确实是整库，但只是文档登记前的随机
        # 时点快照，不可当账本备份用）。现在内容名副其实——就只有这张表。
        backup = Path(args.backup_dir) / f"doc_versions-pre-{stamp}.db"
        online_backup(db_path, backup)
        _sync_tracked(db_path, parsed)
        print(f"[backup] {backup}")
        print(f"[UPSERT] {len(DB_TRACKED_DOCS)} 行；未删除或改动其它登记")

    actual = _read_db_rows(db_path)
    differences: list[str] = []
    for name in DB_TRACKED_DOCS:
        wanted = parsed[name]
        found = actual.get(name)
        if found is None:
            differences.append(f"{name}: DB 缺既有登记")
            continue
        for field in ("version", "updated", "by", "summary"):
            if found[field] != wanted[field]:
                differences.append(
                    f"{name}.{field}: db={found[field]!r} file={wanted[field]!r}"
                )

    print(f"== DB 既有登记：{len(DB_TRACKED_DOCS)} 份 ==")
    if differences:
        for item in differences:
            print(f"[DIFF] {item}")
        if not args.apply:
            print("需要同步时使用 --apply --backup-dir <目录>")
        return 1
    print("OK 文档头完整，既有 doc_versions 登记一致；其它 DB 行未纳入也未改动")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
