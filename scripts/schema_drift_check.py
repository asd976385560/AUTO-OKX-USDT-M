# -*- coding: utf-8 -*-
"""schema 漂移只读对账：真库 PRAGMA vs db/schema.sql（权威 DDL）。

用途：reviewer 每日复盘的系统健康段调用。
只比【表存在性 + 列名集合】（类型/约束差异误报多，不比）；发现漂移只报告，
**修复一律走 export_schema.py 重生成 + 人工确认，本脚本零写入**。

用法：
  run_okx_python.ps1 scripts/schema_drift_check.py [--db-root <PROJECT_ROOT>/db] [--schema <PROJECT_ROOT>/db/schema.sql]
退出码：0=一致；1=有漂移（明细见 stdout）；2=输入错误。
"""
from __future__ import annotations

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))


import argparse
import re
import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 与 export_schema.py 的规范库清单一致
DBS = [
    "market.db", "news.db", "account.db", "lessons.db", "drill.db",
    "regime.db", "analysis.db", "live_trades.db", "demo_trades.db", "ledger.db",
]
EXCLUDE_TABLES = ("sqlite_sequence", "sqlite_stat1")

_CREATE_RE = re.compile(
    r"CREATE TABLE (?:IF NOT EXISTS )?[\"'`]?(\w+)[\"'`]?\s*\((.*?)\)\s*;",
    re.IGNORECASE | re.DOTALL)


def parse_schema_sql(path: Path) -> dict[str, dict[str, set[str]]]:
    """解析 schema.sql → {db_name: {table: {col, ...}}}。按 '-- xxx.db' 分节。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    out: dict[str, dict[str, set[str]]] = {}
    # 切分各库段：export_schema.py 输出以 "-- 数据库: <db>.db" 行分节
    marks = [(m.start(), m.group(1)) for m in
             re.finditer(r"^--\s*数据库[:：]\s*(\w+\.db)\b", text, re.MULTILINE)]
    if not marks:
        return out
    marks.append((len(text), None))
    for i in range(len(marks) - 1):
        db_name = marks[i][1]
        if db_name is None:
            continue
        chunk = text[marks[i][0]:marks[i + 1][0]]
        tables = out.setdefault(db_name, {})
        for tm in _CREATE_RE.finditer(chunk):
            tname, body = tm.group(1), tm.group(2)
            if tname in EXCLUDE_TABLES:
                continue
            # 剥行内 -- 注释（注释里的逗号/词会污染列解析）
            body = "\n".join(line.split("--", 1)[0] for line in body.splitlines())
            cols: set[str] = set()
            depth = 0
            for raw_line in body.split(","):
                token = raw_line.strip()
                # 跳过括号内延续（如 CHECK(...) 内逗号）与表级约束
                if depth > 0:
                    depth += token.count("(") - token.count(")")
                    continue
                depth += token.count("(") - token.count(")")
                if not token:
                    continue
                first = token.split()[0].strip("\"'`[]")
                if first.upper() in ("PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT"):
                    continue
                cols.add(first)
            tables[tname] = cols
    return out


def read_live_schema(db_root: Path) -> dict[str, dict[str, set[str]]]:
    out: dict[str, dict[str, set[str]]] = {}
    for db in DBS:
        p = db_root / db
        if not p.exists():
            out[db] = {}
            continue
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5)
        tables = {}
        try:
            names = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            for t in names:
                if t in EXCLUDE_TABLES:
                    continue
                # table_xinfo：含 GENERATED 列（hidden=2/3，table_info 不显示——
                # regime.cross_market.btc_mcap_chg_24h_usd 实证）；hidden=1 真隐藏列不算
                cols = {r[1] for r in con.execute(f"PRAGMA table_xinfo('{t}')")
                        if r[6] in (0, 2, 3)}
                tables[t] = cols
        finally:
            con.close()
        out[db] = tables
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="schema drift check (read-only)")
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--schema", default=_project_path('db', 'schema.sql'))
    args = ap.parse_args()
    schema_path = Path(args.schema)
    db_root = Path(args.db_root)
    if not schema_path.exists():
        print(f"[schema_drift][FAIL] schema.sql 不存在: {schema_path}", file=sys.stderr)
        return 2
    declared = parse_schema_sql(schema_path)
    live = read_live_schema(db_root)
    drifts: list[str] = []
    for db in DBS:
        d_tables = declared.get(db, {})
        l_tables = live.get(db, {})
        if not d_tables:
            drifts.append(f"{db}: schema.sql 无该库段（声明缺失）")
            continue
        for t in sorted(set(d_tables) | set(l_tables)):
            dc, lc = d_tables.get(t), l_tables.get(t)
            if dc is None:
                drifts.append(f"{db}.{t}: 真库有表、schema.sql 未声明（疑新表未 export）")
            elif lc is None:
                drifts.append(f"{db}.{t}: schema.sql 声明、真库无表（疑被 DROP 未同步）")
            else:
                only_live = lc - dc
                only_decl = dc - lc
                if only_live:
                    drifts.append(f"{db}.{t}: 真库多列 {sorted(only_live)}（疑加列未 export）")
                if only_decl:
                    drifts.append(f"{db}.{t}: 真库缺列 {sorted(only_decl)}")
    if drifts:
        print(f"[schema_drift] DRIFT x{len(drifts)}:")
        for d in drifts:
            print(f"  - {d}")
        print("[schema_drift] 处置：人工确认后跑 export_schema.py 重生成 schema.sql；"
              "本脚本只读不修。")
        return 1
    print("[schema_drift] OK — 10 库表/列与 schema.sql 一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
