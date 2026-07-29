# -*- coding: utf-8 -*-
"""schema 漂移只读对账：真库 sqlite_master vs db/schema.sql（权威 DDL）。

比较范围：
  - 表及显式索引存在性；
  - 完整 CREATE TABLE 定义（列类型、默认值、NOT NULL、PK/UNIQUE/CHECK/FK、
    generated column 等约束均包含在内）；
  - 完整 CREATE INDEX 定义（索引列、顺序、表达式、唯一性及 WHERE 条件）。

自动索引不单独比较，其对应的 PRIMARY KEY/UNIQUE 约束已由 CREATE TABLE 覆盖。
发现漂移只报告，修复一律经人工确认后重新导出；本脚本零写入。

用法：
  run_okx_python.ps1 scripts/schema_drift_check.py [--db-root <PROJECT_ROOT>/db] [--schema <PROJECT_ROOT>/db/schema.sql]
退出码：0=一致；1=有漂移；2=输入或 schema 解析错误。
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
import re
import sqlite3
import sys
from pathlib import Path
from typing import TypeAlias

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DBS = (
    "market.db",
    "news.db",
    "account.db",
    "lessons.db",
    "drill.db",
    "regime.db",
    "analysis.db",
    "live_trades.db",
    "demo_trades.db",
    "ledger.db",
    "qq_push_dedupe.db",
)
EXCLUDE_TABLES = ("sqlite_sequence", "sqlite_stat1")
OBJECT_TYPES = ("table", "index")

ObjectMap: TypeAlias = dict[str, dict[str, str]]
SchemaMap: TypeAlias = dict[str, ObjectMap | None]


def _strip_sql_comments(sql: str) -> str:
    """移除 SQL 注释，同时保留引号内的 -- 与 /* */ 字面量。"""
    out: list[str] = []
    i = 0
    quote: str | None = None
    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""
        if quote is not None:
            out.append(ch)
            if quote == "]":
                if ch == "]":
                    quote = None
            elif ch == quote:
                if nxt == quote:
                    out.append(nxt)
                    i += 1
                else:
                    quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "[":
            quote = "]"
            out.append(ch)
            i += 1
            continue
        if ch == "-" and nxt == "-":
            i += 2
            while i < len(sql) and sql[i] not in "\r\n":
                i += 1
            out.append("\n")
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < len(sql) and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i = min(i + 2, len(sql))
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _canonical_sql(sql: str) -> str:
    """规范空白与非字面量大小写，保留字符串/引号标识符的精确内容。"""
    source = _strip_sql_comments(sql).strip().rstrip(";")
    out: list[str] = []
    quote: str | None = None
    pending_space = False
    i = 0
    while i < len(source):
        ch = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if quote is not None:
            out.append(ch)
            if quote == "]":
                if ch == "]":
                    quote = None
            elif ch == quote:
                if nxt == quote:
                    out.append(nxt)
                    i += 1
                else:
                    quote = None
            i += 1
            continue
        if ch.isspace():
            pending_space = True
            i += 1
            continue
        if pending_space and out and out[-1] not in "(," and ch not in "),":
            out.append(" ")
        pending_space = False
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
        elif ch == "[":
            quote = "]"
            out.append(ch)
        else:
            out.append(ch.casefold())
        i += 1
    return "".join(out).strip()


def _catalog_from_connection(con: sqlite3.Connection) -> ObjectMap:
    catalog: ObjectMap = {kind: {} for kind in OBJECT_TYPES}
    rows = con.execute(
        "SELECT type,name,sql FROM sqlite_master "
        "WHERE type IN ('table','index') AND sql IS NOT NULL ORDER BY type,name"
    ).fetchall()
    for kind, name, sql in rows:
        if kind == "table" and name in EXCLUDE_TABLES:
            continue
        if kind == "index" and name.startswith("sqlite_autoindex"):
            continue
        catalog[kind][name] = _canonical_sql(sql)
    return catalog


def parse_schema_sql(path: Path) -> SchemaMap:
    """解析 schema.sql 各数据库段，并通过内存 SQLite 得到完整对象定义。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    out: SchemaMap = {}
    marks = [
        (m.start(), m.group(1))
        for m in re.finditer(
            r"^--\s*数据库[:：]\s*([A-Za-z0-9_.-]+\.db)\b", text, re.MULTILINE
        )
    ]
    if not marks:
        return out
    marks.append((len(text), ""))
    for i in range(len(marks) - 1):
        db_name = marks[i][1]
        chunk = text[marks[i][0] : marks[i + 1][0]]
        con = sqlite3.connect(":memory:")
        try:
            con.executescript(_strip_sql_comments(chunk))
            out[db_name] = _catalog_from_connection(con)
        except sqlite3.DatabaseError as exc:
            raise ValueError(f"{db_name} DDL 无法解析: {exc}") from exc
        finally:
            con.close()
    return out


def read_live_schema(db_root: Path) -> SchemaMap:
    """以 mode=ro 读取真实库；缺库用 None 表示，绝不静默创建。"""
    out: SchemaMap = {}
    for db_name in DBS:
        path = Path(db_root) / db_name
        if not path.exists():
            out[db_name] = None
            continue
        con = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5
        )
        try:
            out[db_name] = _catalog_from_connection(con)
        finally:
            con.close()
    return out


def collect_drifts(declared: SchemaMap, live: SchemaMap) -> list[str]:
    """返回不含 DDL 正文的安全漂移摘要。"""
    drifts: list[str] = []
    for db_name in DBS:
        declared_objects = declared.get(db_name)
        live_objects = live.get(db_name)
        if declared_objects is None:
            drifts.append(f"{db_name}: schema.sql 无该库段（声明缺失）")
            continue
        if live_objects is None:
            drifts.append(f"{db_name}: 真库文件缺失")
            continue
        for kind in OBJECT_TYPES:
            declared_kind = declared_objects.get(kind, {})
            live_kind = live_objects.get(kind, {})
            label = "表" if kind == "table" else "索引"
            for name in sorted(set(declared_kind) | set(live_kind)):
                declared_sql = declared_kind.get(name)
                live_sql = live_kind.get(name)
                if declared_sql is None:
                    drifts.append(
                        f"{db_name}.{name}: 真库有{label}、schema.sql 未声明"
                    )
                elif live_sql is None:
                    drifts.append(
                        f"{db_name}.{name}: schema.sql 声明{label}、真库缺失"
                    )
                elif declared_sql != live_sql:
                    scope = (
                        "完整表定义（类型/默认值/约束/generated）"
                        if kind == "table"
                        else "完整索引定义（唯一性/列序/表达式/WHERE）"
                    )
                    drifts.append(f"{db_name}.{name}: {scope} 漂移")
    return drifts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="schema drift check (read-only)")
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--schema", default=_project_path('db', 'schema.sql'))
    args = ap.parse_args(argv)
    schema_path = Path(args.schema)
    db_root = Path(args.db_root)
    if not schema_path.exists():
        print(f"[schema_drift][FAIL] schema.sql 不存在: {schema_path}", file=sys.stderr)
        return 2
    try:
        declared = parse_schema_sql(schema_path)
        live = read_live_schema(db_root)
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        print(f"[schema_drift][FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    drifts = collect_drifts(declared, live)
    if drifts:
        print(f"[schema_drift] DRIFT x{len(drifts)}:")
        for drift in drifts:
            print(f"  - {drift}")
        print(
            "[schema_drift] 处置：人工确认后跑 export_schema.py 重生成 schema.sql；"
            "本脚本只读不修。"
        )
        return 1
    print(f"[schema_drift] OK — {len(DBS)} 库完整表/约束/索引与 schema.sql 一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
