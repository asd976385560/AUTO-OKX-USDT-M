# -*- coding: utf-8 -*-
"""system_state_writer.py — v7.0c 统一交易员每轮 system_state 关键字段更新

写入：<PROJECT_ROOT>\\db\\account.db → system_state (key, value, updated_utc)
输入：stdin JSON: {"updates": {"key1": "value1", "key2": "value2", ...}, "ts": "2026-06-05 21:46:00"}
- ts 可选；缺省用 UTC ISO 当前时间
- 每个 key 写一行；INSERT OR REPLACE（key 唯一）
- 保护键（cum_pnl 冻结基线权威键）默认拒写，需 --force-protected（仅维护用）

调用：
  echo '{"updates":{...}}' | pwsh -NoProfile -File <PROJECT_ROOT>\\scripts\\run_okx_python.ps1 <PROJECT_ROOT>\\scripts\\system_state_writer.py --stdin

退出码：0=成功；2=批内含保护键被拒写（其余键正常写入）；非0=失败
"""

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(
    _project_os.environ.get("OKX_ROOT")
    or _ProjectPath(__file__).resolve().parents[1]
).resolve()

def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))

import argparse, json, os, sqlite3, sys
from datetime import datetime, timedelta, timezone

PROTECTED_KEYS = {
    "live_cum_pnl",
    "demo_cum_pnl",
    "live_cum_pnl_reset_ts",
    "demo_cum_pnl_reset_ts",
}

def utc_now_iso() -> str:
    # fallback 使用 CST，保持 account.db 业务时间口径统一。函数名因调用面广而保留。
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

def fail(msg: str, code: int = 2):
    print(f"[system_state_writer][FAIL] {msg}", file=sys.stderr)
    sys.exit(code)

def load_payload(args) -> dict:
    if args.stdin:
        raw = sys.stdin.read()
    elif args.json_file:
        with open(args.json_file, "r", encoding="utf-8") as f:
            raw = f.read()
    elif args.json:
        raw = args.json
    else:
        fail("缺少输入：需 --stdin / --json-file / --json 之一")
    try:
        return json.loads(raw)
    except Exception as e:
        fail(f"输入 JSON 解析失败: {e}")

def main():
    p = argparse.ArgumentParser(description="v7.0c system_state 关键字段更新")
    p.add_argument("--db-root", default=_project_path('db'))
    g = p.add_mutually_exclusive_group()
    g.add_argument("--stdin", action="store_true")
    g.add_argument("--json-file")
    g.add_argument("--json")
    p.add_argument("--force-protected", action="store_true",
                   help="放行保护键写入（仅维护用）")
    args = p.parse_args()
    data = load_payload(args)
    updates = data.get("updates")
    if not isinstance(updates, dict) or not updates:
        fail("输入 JSON 缺 'updates' 字段或为空 dict")
    ts = data.get("ts") or utc_now_iso()
    db_path = os.path.join(args.db_root, "account.db")
    if not os.path.exists(db_path):
        fail(f"account.db 不存在: {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    written_keys = []
    rejected_keys = []
    try:
        # v7.0e.1: 自动给 key 加 profile 命名空间（如果没显式带前缀）
        # live_xxx / demo_xxx 显式带前缀 → 保持原样
        # 其他 key → 视为 live（默认）
        for k, v in updates.items():
            # 如果 key 已带 live_/demo_ 前缀，保持；否则默认视为 live
            if k.startswith("live_") or k.startswith("demo_"):
                final_key = k
            else:
                final_key = f"live_{k}"
            # 保护键闸：前缀归一后的最终键落在保护集即拒写（除非 --force-protected）
            if final_key in PROTECTED_KEYS and not args.force_protected:
                rejected_keys.append(final_key)
                continue
            conn.execute(
                "INSERT OR REPLACE INTO system_state (key, value, updated_utc) VALUES (?, ?, ?)",
                (final_key, str(v) if v is not None else "", ts),
            )
            written_keys.append(final_key)
        conn.commit()
    except Exception as e:
        conn.rollback()
        fail(f"写入异常: {e}")
    finally:
        conn.close()
    print(json.dumps({"ok": not rejected_keys, "ts": ts, "updated": len(written_keys),
                      "keys": written_keys, "rejected": rejected_keys}, ensure_ascii=False))
    if rejected_keys:
        print(f"[system_state_writer][PROTECTED] 拒写保护键: {', '.join(rejected_keys)}；"
              f"保护键需 --force-protected（仅维护用）", file=sys.stderr)
        sys.exit(2)
    sys.exit(0)

if __name__ == "__main__":
    main()
