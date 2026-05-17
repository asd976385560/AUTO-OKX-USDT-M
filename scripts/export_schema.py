"""Export all OKX database schemas to schema.sql"""
import sqlite3
import os

db_dir = r'E:\OKX\db'
dbs = ['market.db', 'news.db', 'account.db', 'lessons.db']
lines = []
lines.append('-- OKX 永续合约自主交易系统 - 数据库 Schema')
lines.append('-- 导出时间: 2026-05-16 21:56')
lines.append('-- 数据库目录: E:\\OKX\\db\\')
lines.append('-- 本文件供 AI 读取表结构使用，不要手动编辑')
lines.append('-- 实际建表由 init_okx2.py 完成')
lines.append('')

for db_name in dbs:
    db_path = os.path.join(db_dir, db_name)
    if not os.path.exists(db_path):
        lines.append(f'-- 数据库 {db_name} 不存在')
        lines.append('')
        continue
    lines.append('-- ============================================================')
    lines.append(f'-- 数据库: {db_name}')
    lines.append('-- ============================================================')
    lines.append('')
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL ORDER BY name")
    for name, sql in cursor.fetchall():
        lines.append(sql + ';')
        lines.append('')
    conn.close()

output = '\n'.join(lines)
out_path = os.path.join(db_dir, 'schema.sql')
with open(out_path, 'w', encoding='utf-8') as f:
    f.write(output)
print(f'schema.sql written to {out_path}, {len(output)} chars, {len(lines)} lines')
