# -*- coding: utf-8 -*-
"""导出 V2.0 业务数据库 schema 到 db/schema.sql。"""

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(_project_os.environ.get("OKX_ROOT") or _ProjectPath(__file__).resolve().parents[1]).resolve()


def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))

import os
import re
from datetime import datetime, timezone, timedelta

from _db_ro import connect_ro

SCHEMA_VERSION = 'V2.0'
db_dir = _project_path('db')
# V2.0 规范库；drill.db 暂保留为只读兼容库，待现役 reviewer 读依赖迁走后再清理。
dbs = [
    'market.db', 'news.db', 'account.db', 'lessons.db', 'drill.db',
    'regime.db', 'analysis.db', 'live_trades.db', 'demo_trades.db', 'ledger.db',
]
# 排除 sqlite 系统表
EXCLUDE = ('sqlite_sequence', 'sqlite_stat1')


def normalize_schema_comments(db_name: str, table_name: str, sql: str) -> str:
    """清理旧 DDL 内嵌注释；只改注释，不改表、列、约束或索引。"""
    if db_name in {'live_trades.db', 'demo_trades.db'} and table_name == 'trade_cycles':
        return re.sub(
            r'(?m)^(\s*mode\s+TEXT,\s+--\s*).*$',
            r'\1live|demo（由 trades_writer 按 profile 写入）',
            sql,
        )
    return sql


CST = timezone(timedelta(hours=8))
now = datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')

lines = []
lines.append('-- OKX 永续合约自主交易系统 - 数据库 Schema')
lines.append(f'-- 导出时间: {now} CST')
lines.append(f'-- 版本: {SCHEMA_VERSION}')
lines.append('-- 数据库目录: <PROJECT_ROOT>\\db\\')
lines.append('-- 本文件供 AI 读取表结构使用，不要手动编辑（改 schema 后跑 export_schema.py 重生成）')
lines.append('-- 核心拆分库由 init_v20_dbs.py 初始化；增量变更走 apply_* 幂等迁移脚本')
lines.append('')

for db_name in dbs:
    db_path = os.path.join(db_dir, db_name)
    if not os.path.exists(db_path):
        lines.append(f'-- 数据库 {db_name} 不存在')
        lines.append('')
        continue
    lines.append('-- ' + '=' * 60)
    lines.append(f'-- 数据库: {db_name}')
    lines.append('-- ' + '=' * 60)
    lines.append('')
    conn = connect_ro(db_path)  # 只读 mode=ro（2026-07-03）
    cursor = conn.cursor()
    # 表
    cursor.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL ORDER BY name")
    for name, sql in cursor.fetchall():
        if name in EXCLUDE:
            continue
        lines.append(normalize_schema_comments(db_name, name, sql) + ';')
        lines.append('')
    # 索引
    cursor.execute("SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY name")
    for (sql,) in cursor.fetchall():
        # 排除自动 UNIQUE 索引（已含在 CREATE TABLE 里）和 sqlite 内部
        if 'sqlite_autoindex' in (sql or ''):
            continue
        lines.append(sql + ';')
        lines.append('')
    conn.close()

output = '\n'.join(lines)
out_path = os.path.join(db_dir, 'schema.sql')
with open(out_path, 'w', encoding='utf-8') as f:
    f.write(output)
print(f'schema.sql written to {out_path}')
print(f'  {len(output)} chars, {len(lines)} lines, {SCHEMA_VERSION}')
