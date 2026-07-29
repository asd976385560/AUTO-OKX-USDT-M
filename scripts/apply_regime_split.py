# -*- coding: utf-8 -*-
"""V2.0 §5 迁移 —— regime 真拆：cross_market 数据 market.db → regime.db。

本脚本把 regime 数据从 market.db 迁入 regime.db，只搬数据（幂等 upsert），
不改任何读写路径——「切 analyst 读 regime.db」「停 market.db 写 cross_market」是
独立的代码步（慢采/analyst 改动），按 §5 顺序在迁移后单独做：
  1. 先灌 regime.db（本脚本）→ 2. 切 analyst 读路径 → 3. 最后停 market.db 写。
  顺序错会断 regime。

introspect-safe：运行时取两库 cross_market 的**非生成列**交集来拷，规避
`btc_mcap_chg_24h_usd`（GENERATED VIRTUAL，禁 INSERT）等坑（别名坑随迁，§5）。

幂等：INSERT OR REPLACE（按 ts 主键），可重复跑。

用法：
  # 干跑（只报将拷多少行、列交集，不写）
  python apply_regime_split.py --db-root <PROJECT_ROOT>\\db --dry-run
  # 真迁 + 校验
  python apply_regime_split.py --db-root <PROJECT_ROOT>\\db --verify
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
import sys
from pathlib import Path

sys.path.insert(0, _project_path('collectors'))
import ledger  # noqa: E402  复用 connect()（WAL/ro 单一来源）

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _columns(con, table: str) -> list[dict]:
    """返回 [{name, generated(bool)}]。PRAGMA table_xinfo.hidden: 2/3 = 生成列。"""
    rows = con.execute(f"PRAGMA table_xinfo({table})").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        hidden = d.get("hidden", 0)
        out.append({"name": d["name"], "generated": hidden in (2, 3)})
    return out


def copyable_columns(src_con, dst_con) -> list[str]:
    """两库非生成列交集（保持 src 顺序）。"""
    src = _columns(src_con, "cross_market")
    dst_names = {c["name"] for c in _columns(dst_con, "cross_market")}
    return [c["name"] for c in src if not c["generated"] and c["name"] in dst_names]


def migrate(db_root: Path, dry_run: bool = False) -> dict:
    market = db_root / "market.db"
    regime = db_root / "regime.db"
    if not market.exists():
        return {"ok": False, "error": f"market.db 不存在: {market}"}
    if not regime.exists():
        return {"ok": False, "error": f"regime.db 不存在（先跑 init_v20_dbs）: {regime}"}

    src = ledger.connect(market, readonly=True)
    dst = ledger.connect(regime)
    try:
        cols = copyable_columns(src, dst)
        if "ts" not in cols:
            return {"ok": False, "error": f"列交集缺 ts: {cols}"}
        src_n = src.execute("SELECT COUNT(*) FROM cross_market").fetchone()[0]
        dst_n_before = dst.execute("SELECT COUNT(*) FROM cross_market").fetchone()[0]
        if dry_run:
            return {"ok": True, "dry_run": True, "columns": cols,
                    "src_rows": src_n, "dst_rows_before": dst_n_before}

        col_list = ",".join(cols)
        placeholders = ",".join("?" for _ in cols)
        rows = src.execute(f"SELECT {col_list} FROM cross_market").fetchall()
        dst.executemany(
            f"INSERT OR REPLACE INTO cross_market ({col_list}) VALUES ({placeholders})",
            [tuple(r[c] for c in cols) for r in rows],
        )
        dst.commit()
        dst_n_after = dst.execute("SELECT COUNT(*) FROM cross_market").fetchone()[0]
        return {"ok": True, "columns": cols, "src_rows": src_n,
                "dst_rows_before": dst_n_before, "dst_rows_after": dst_n_after,
                "copied": len(rows)}
    finally:
        src.close()
        dst.close()


def verify(db_root: Path) -> dict:
    """校验：regime.db 行数 ≥ market.db；最新 ts 一致。"""
    src = ledger.connect(db_root / "market.db", readonly=True)
    dst = ledger.connect(db_root / "regime.db", readonly=True)
    try:
        src_n = src.execute("SELECT COUNT(*) FROM cross_market").fetchone()[0]
        dst_n = dst.execute("SELECT COUNT(*) FROM cross_market").fetchone()[0]
        src_max = src.execute("SELECT MAX(ts) FROM cross_market").fetchone()[0]
        dst_max = dst.execute("SELECT MAX(ts) FROM cross_market").fetchone()[0]
        ok = dst_n >= src_n and src_max == dst_max
        return {"ok": ok, "src_rows": src_n, "dst_rows": dst_n,
                "src_max_ts": src_max, "dst_max_ts": dst_max}
    finally:
        src.close()
        dst.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="V2.0 regime 真拆迁移")
    ap.add_argument("--db-root", default=_project_path('db'))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    root = Path(args.db_root)

    import json
    res = migrate(root, dry_run=args.dry_run)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if not res.get("ok"):
        return 1
    if args.verify and not args.dry_run:
        v = verify(root)
        print("-- verify --")
        print(json.dumps(v, ensure_ascii=False, indent=2))
        return 0 if v["ok"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
