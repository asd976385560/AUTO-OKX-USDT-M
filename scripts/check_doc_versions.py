# -*- coding: utf-8 -*-
"""V2.0 权威文档版本登记与完整性检查。

扫描 2 份当前权威文档（skill.md/config.md，见下 DOCS）头部 doc-version +
last-updated。默认只读比对；仅显式传入 --apply 且提供 --backup-dir 时同步
doc_versions 并移除不再属于当前权威文档集的旧登记。
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))

import argparse
import os, re, sys, sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')

CST = timezone(timedelta(hours=8))
DB = _project_path('db', 'account.db')
TS_FMT = "%Y-%m-%d %H:%M:%S"

# 当前仅登记 2 份运行权威文档。README 是系统地图、版本头自维护，不进入 DOCS。
DOCS = [
    (_project_path('skill.md'),  'skill.md'),
    (_project_path('config.md'), 'config.md'),
]


def strip_version_prefix(v: str) -> str:
    """去除版本号前导 v/V，避免展示时出现 vV7.1.1 这种双前缀"""
    s = (v or '').strip()
    while s and s[0] in ('v', 'V'):
        s = s[1:]
    return s or v  # 全剥光则保留原值


def parse_doc_header(path: str) -> dict:
    """解析 md 文件头部 HTML 注释里的 doc-version / last-updated / updated-by / change-summary"""
    if not os.path.exists(path):
        return {'version': 'MISSING', 'updated': '', 'by': '', 'summary': f'file not found: {path}'}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read(4000)  # 头部 4KB 足够
    except Exception as e:
        return {'version': 'ERROR', 'updated': '', 'by': '', 'summary': f'read error: {e}'}
    out = {'version': '?', 'updated': '', 'by': '', 'summary': ''}
    for key, target in [
        ('doc-version', 'version'),
        ('last-updated', 'updated'),
        ('updated-by', 'by'),
        ('change-summary', 'summary'),
    ]:
        # 支持 doc-version: x.x 格式
        m = re.search(rf'{key}\s*:\s*([^\n]*)', text, re.IGNORECASE)
        if m:
            out[target] = m.group(1).strip()
    return out


def online_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    dst = sqlite3.connect(target, timeout=10)
    try:
        src.backup(dst)
        integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"backup integrity_check={integrity}")
    finally:
        dst.close()
        src.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="检查或同步当前权威文档版本登记")
    ap.add_argument("--apply", action="store_true", help="显式同步 doc_versions；默认只读检查")
    ap.add_argument("--backup-dir", default=None, help="--apply 必填；写 account.db 前做 SQLite 在线备份")
    args = ap.parse_args()

    print('== V2.0 doc_versions 校验 ==')
    print(f'Now (CST): {datetime.now(CST).strftime(TS_FMT)}')
    print()

    if args.apply and not args.backup_dir:
        print('[ERROR] --apply 必须同时提供 --backup-dir', file=sys.stderr)
        return 2

    if args.apply:
        stamp = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
        backup_path = Path(args.backup_dir) / f"account-pre-doc-versions-{stamp}.db"
        online_backup(Path(DB), backup_path)
        print(f'[backup] {backup_path}')

    print(f'[1/3] 解析 {len(DOCS)} 份文档头部')
    parsed = []
    for path, name in DOCS:
        info = parse_doc_header(path)
        print(f'  {name:35s} v{strip_version_prefix(info["version"]):8s}  {info["updated"]:10s}  by={info["by"]}')
        parsed.append((name, path, info))

    invalid = [
        (name, info['summary'])
        for name, _, info in parsed
        if info['version'] in {'?', 'MISSING', 'ERROR'} or not info['updated']
    ]
    if invalid:
        print('\n[ERROR] 文档头不完整，拒绝继续：')
        for name, reason in invalid:
            print(f'  - {name}: {reason or "缺 doc-version/last-updated"}')
        return 2

    if args.apply:
        print('\n[2/3] 同步 doc_versions（显式 apply）')
        c = sqlite3.connect(DB, timeout=30)
        cur = c.cursor()
        now = datetime.now(CST).strftime(TS_FMT)
        doc_names = [name for _, name in DOCS]
        placeholders = ','.join('?' for _ in doc_names)
        try:
            cur.execute('BEGIN IMMEDIATE')
            cur.execute(
                f"DELETE FROM doc_versions WHERE doc_path NOT IN ({placeholders})",
                doc_names,
            )
            removed = cur.rowcount
            for name, _, info in parsed:
                cur.execute(
                    "INSERT INTO doc_versions "
                    "(doc_path, doc_version, last_updated, updated_by, change_summary) "
                    "VALUES (?,?,?,?,?) "
                    "ON CONFLICT(doc_path) DO UPDATE SET "
                    "doc_version=excluded.doc_version, last_updated=excluded.last_updated, "
                    "updated_by=excluded.updated_by, change_summary=excluded.change_summary",
                    (name, info['version'], info['updated'] or now, info['by'], info['summary']),
                )
            c.commit()
            print(f'  [UPSERT] {len(parsed)} rows; [REMOVE] {removed} legacy rows')
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()
    else:
        print('\n[2/3] 只读比对（未传 --apply，不写库）')

    uri = Path(DB).resolve().as_uri() + '?mode=ro'
    c = sqlite3.connect(uri, uri=True, timeout=10)
    cur = c.cursor()
    rows = cur.execute(
        "SELECT doc_path, doc_version, last_updated, updated_by, change_summary "
        "FROM doc_versions ORDER BY doc_path"
    ).fetchall()
    c.close()

    actual = {
        row[0]: {
            'version': row[1] or '',
            'updated': row[2] or '',
            'by': row[3] or '',
            'summary': row[4] or '',
        }
        for row in rows
    }
    expected = {
        name: {
            'version': info['version'],
            'updated': info['updated'],
            'by': info['by'],
            'summary': info['summary'],
        }
        for name, _, info in parsed
    }
    differences = []
    for name, wanted in expected.items():
        if name not in actual:
            differences.append(f'{name}: DB 缺行')
            continue
        for field, value in wanted.items():
            if actual[name][field] != value:
                differences.append(
                    f'{name}.{field}: db={actual[name][field]!r} file={value!r}'
                )
    for name in sorted(set(actual) - set(expected)):
        differences.append(f'{name}: 非当前权威文档的遗留登记')

    print('\n[3/3] 比对结果')
    if differences:
        for item in differences:
            print(f'  [DIFF] {item}')
        if not args.apply:
            print('  需要同步时使用 --apply --backup-dir <目录>')
        return 1

    for row in rows:
        print(f'  {row[0]:35s} v{strip_version_prefix(row[1]):8s}  {row[2]:20s}  by={row[3]}')
    print('\nOK V2.0 doc_versions 与文件头一致')
    return 0


if __name__ == '__main__':
    sys.exit(main())
