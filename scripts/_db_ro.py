# -*- coding: utf-8 -*-
"""共享 SQLite 只读连接 helper。

只读脚本必须用 mode=ro 打开数据库；路径错或文件缺失时立即失败，
禁止 sqlite 静默创建 0 字节假库（如 bash 反斜杠被吃
写出 .db\\account.db 0 字节污染的病灶）。统一改走 mode=ro 只读 URI：
库文件缺失/不可读时直接抛 sqlite3.OperationalError（期望的 fail-fast）。

用法：
    from _db_ro import connect_ro
    con = connect_ro(path)                     # 默认 timeout=5.0
    con = connect_ro(path, timeout=30)
    con = connect_ro(path, row_factory=sqlite3.Row)
"""
import sqlite3
from pathlib import Path


def connect_ro(path, timeout=5.0, row_factory=None):
    """以只读 URI（mode=ro）打开 SQLite 库并返回连接。

    - path: str 或 Path；Windows 反斜杠统一转正斜杠再拼 URI。
    - 缺文件/不可读 → 抛 sqlite3.OperationalError（fail-fast，
      不像可写打开那样静默创建 0 字节假库）。
    - row_factory: 可选，直接设到连接上（如 sqlite3.Row）。
    """
    posix = Path(path).as_posix()
    con = sqlite3.connect(f"file:{posix}?mode=ro", uri=True, timeout=timeout)
    if row_factory is not None:
        con.row_factory = row_factory
    return con
