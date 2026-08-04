# -*- coding: utf-8 -*-
"""ledger_autoheal 硬闸契约（2026-08-04）。

钉住自愈硬闸，防止逻辑日后被放宽：
Live 永久只读；Demo 只补 EXACT、受单轮上限和 runner 互斥约束，且 dry-run 不写库。
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


import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for sub in ("scripts", "collectors"):
    p = str(ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

import ledger_autoheal  # noqa: E402

CST = timezone(timedelta(hours=8))

TRADE_SCHEMA = """
CREATE TABLE trade_cycles(
  cycle_id TEXT PRIMARY KEY, ts TEXT NOT NULL, mode TEXT, decision TEXT,
  n_orders INTEGER DEFAULT 0, equity REAL, note TEXT, raw TEXT
);
CREATE TABLE trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT, ts TEXT NOT NULL,
  symbol TEXT NOT NULL, action TEXT NOT NULL, side TEXT, sz REAL,
  fill_px REAL, lev REAL, margin REAL, notional REAL, score_total INTEGER,
  reasoning TEXT, deviation TEXT, degradation TEXT, pnl REAL, raw TEXT
);
"""

SYM = "TEST-USDT-SWAP"
OPEN_TS = "2026-08-04 10:00:00"
CLOSE_MS = int(datetime(2026, 8, 4, 10, 30, tzinfo=CST).timestamp() * 1000)


def _make_db(root: Path, profile: str, opens=((1.0, 139.47),)) -> Path:
    path = root / f"{profile}_trades.db"
    con = sqlite3.connect(path)
    try:
        con.executescript(TRADE_SCHEMA)
        for sz, px in opens:
            con.execute(
                "INSERT INTO trades(cycle_id, ts, symbol, action, side, sz, "
                "fill_px, lev, pnl) VALUES(?,?,?,?,?,?,?,?,?)",
                ("2026-08-04T10:00", OPEN_TS, SYM, "open", "short", sz, px, 5.0, 0.0))
        con.commit()
    finally:
        con.close()
    return path


def _fill(ord_id: str, sz: float, px: float = 141.53, pnl: float = -2.06):
    return [{"ordId": ord_id, "fillSz": str(sz), "fillPx": str(px),
             "fillPnl": str(pnl), "fillTime": str(CLOSE_MS)}]


def _rows(db: Path, action: str | None = None):
    con = sqlite3.connect(db)
    try:
        q = "SELECT action, side, sz, pnl FROM trades"
        if action:
            q += f" WHERE action='{action}'"
        return con.execute(q).fetchall()
    finally:
        con.close()


class LedgerAutohealGateTests(unittest.TestCase):
    def _run(self, root: Path, *, venue, fills, apply=True,
             max_heals=3, self_cycle=None, runner=None, profile="demo"):
        with mock.patch.object(ledger_autoheal.rec, "venue_positions",
                               return_value=venue), \
             mock.patch.object(ledger_autoheal.rec, "fetch_reduce_fills",
                               side_effect=lambda *a, **k: fills), \
             mock.patch.object(ledger_autoheal, "active_runner",
                               return_value=runner):
            return ledger_autoheal.autoheal(profile, root, apply,
                                            max_heals, self_cycle)

    # --- 闸 1/2：EXACT 自愈，且只写 close ---
    def test_exact_ghost_is_healed_with_close_row_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = _make_db(root, "demo")
            res = self._run(root, venue={}, fills=_fill("ORD-1", 1.0))

            self.assertEqual(res["rc"], 0, res)
            self.assertEqual(len(res["healed"]), 1)
            self.assertTrue(res["healed"][0]["applied"])
            self.assertEqual(res["healed"][0]["ord_ids"], ["ORD-1"])
            # 只多出一条 close，open 行数不变、绝不新增 open
            self.assertEqual(len(_rows(db, "close")), 1)
            self.assertEqual(len(_rows(db, "open")), 1)

    # --- 闸 5：幂等，补完再跑无幽灵 ---
    def test_second_run_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = _make_db(root, "demo")
            self._run(root, venue={}, fills=_fill("ORD-1", 1.0))
            again = self._run(root, venue={}, fills=_fill("ORD-1", 1.0))

            self.assertEqual(again["exact_count"], 0)
            self.assertEqual(again["healed"], [])
            self.assertEqual(len(_rows(db, "close")), 1)

    # --- 闸 1：FUZZY 绝不写库 ---
    def test_fuzzy_ghost_is_never_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = _make_db(root, "demo")
            # fills 只有 0.3 张，对不上 1.0 幽灵 → FUZZY
            res = self._run(root, venue={}, fills=_fill("ORD-X", 0.3))

            self.assertEqual(res["healed"], [])
            self.assertEqual(res["rc"], 1)
            self.assertEqual(
                [n["kind"] for n in res["needs_human"]], ["GHOST-FUZZY"])
            self.assertEqual(len(_rows(db, "close")), 0)

    # --- 闸 2：UNRECORDED（现仓>账本）只报告，绝不补 open ---
    def test_unrecorded_is_reported_not_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = _make_db(root, "live")
            res = self._run(root, venue={(SYM, "short"): 3.0}, fills=[],
                            apply=False, profile="live")

            kinds = [n["kind"] for n in res["needs_human"]]
            self.assertIn("UNRECORDED", kinds)
            self.assertIn("NAKED-POSITION-P0", kinds)
            self.assertEqual(res["healed"], [])
            self.assertEqual(res["rc"], 2)
            self.assertEqual(len(_rows(db, "open")), 1)  # 未新增 open

    # --- 闸 3：超单轮上限则一笔都不补 ---
    def test_over_cap_heals_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "demo_trades.db"
            con = sqlite3.connect(db)
            con.executescript(TRADE_SCHEMA)
            for i in range(3):
                con.execute(
                    "INSERT INTO trades(cycle_id, ts, symbol, action, side, sz,"
                    " fill_px, lev, pnl) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("2026-08-04T10:00", OPEN_TS, f"S{i}-USDT-SWAP", "open",
                     "short", 1.0, 100.0, 5.0, 0.0))
            con.commit()
            con.close()

            res = self._run(root, venue={}, fills=_fill("ORD-1", 1.0),
                            max_heals=2)

            self.assertEqual(res["rc"], 2)
            self.assertEqual(res["healed"], [])
            self.assertIn("OVER_CAP", [n["kind"] for n in res["needs_human"]])
            self.assertEqual(len(_rows(db, "close")), 0)

    # --- 闸 4：runner 互斥；--self-cycle 放行自身 ---
    def test_active_runner_blocks_heal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = _make_db(root, "demo")
            res = self._run(root, venue={}, fills=_fill("ORD-1", 1.0),
                            runner={"cycle_id": "2026-08-04T11:00"})

            self.assertEqual(res["rc"], 3)
            self.assertEqual(res["skipped"], "demo_runner_active")
            self.assertEqual(len(_rows(db, "close")), 0)

    def test_active_runner_probe_uses_selected_db_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "isolated-db"
            root.mkdir()
            _make_db(root, "live")
            with mock.patch.object(
                ledger_autoheal, "active_runner",
                return_value={"cycle_id": "2026-08-04T11:00"},
            ) as probe:
                res = ledger_autoheal.autoheal(
                    "live", root, False, 3, None
                )

            probe.assert_called_once_with("live", root)
            self.assertEqual(res["skipped"], "live_runner_active")

    def test_self_cycle_runner_is_not_a_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = _make_db(root, "demo")
            res = self._run(root, venue={}, fills=_fill("ORD-1", 1.0),
                            runner={"cycle_id": "2026-08-04T11:00"},
                            self_cycle="2026-08-04T11:00")

            self.assertIsNone(res["skipped"])
            self.assertEqual(res["rc"], 0)
            self.assertEqual(len(_rows(db, "close")), 1)

    # --- dry-run 不写库 ---
    def test_dry_run_reports_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = _make_db(root, "live")
            res = self._run(root, venue={}, fills=_fill("ORD-1", 1.0),
                            apply=False, profile="live")

            self.assertEqual(len(res["healed"]), 1)
            self.assertFalse(res["healed"][0]["applied"])
            self.assertEqual(len(_rows(db, "close")), 0)

    def test_queue_close_uses_same_db_root_and_keeps_machine_stdout_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_db(root, "demo")
            account = root / "account.db"
            con = sqlite3.connect(account)
            con.executescript(
                """
                CREATE TABLE repair_queue(
                  id INTEGER PRIMARY KEY, ts TEXT, check_name TEXT,
                  status TEXT, issue TEXT, closed_at TEXT,
                  closed_by TEXT, resolution TEXT
                );
                """
            )
            con.execute(
                "INSERT INTO repair_queue(id,ts,check_name,status,issue) "
                "VALUES(1,'2026-08-04 10:00:00','order_executor','pending',?)",
                (f"pretrade_ledger_position_mismatch {SYM}/short",),
            )
            con.commit()
            con.close()

            captured = io.StringIO()
            with redirect_stdout(captured):
                res = self._run(root, venue={}, fills=_fill("ORD-Q", 1.0))

            self.assertEqual(res["queue_closed"], [1])
            self.assertEqual(captured.getvalue(), "")
            con = sqlite3.connect(account)
            try:
                row = con.execute(
                    "SELECT status,closed_by FROM repair_queue WHERE id=1"
                ).fetchone()
            finally:
                con.close()
            self.assertEqual(row, ("closed", "ledger_autoheal"))

    def test_queue_failure_remains_structured_after_ledger_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = _make_db(root, "demo")
            with mock.patch.object(
                ledger_autoheal, "_pending_queue_ids", return_value=[9]
            ), mock.patch.object(
                ledger_autoheal.repair_queue_tool,
                "do_close",
                side_effect=sqlite3.OperationalError("queue locked"),
            ):
                res = self._run(root, venue={}, fills=_fill("ORD-Q2", 1.0))

            self.assertTrue(res["healed"][0]["applied"])
            self.assertEqual(len(_rows(db, "close")), 1)
            queue_error = next(
                item for item in res["needs_human"]
                if item["kind"] == "QUEUE-CLOSE-ERROR"
            )
            self.assertEqual(queue_error["queue_ids"], [9])
            self.assertEqual(queue_error["sev"], "P1")
            self.assertEqual(res["rc"], 2)


class LiveAutohealReadOnlyTests(unittest.TestCase):
    """Live classification is permanently read-only at API and CLI boundaries."""

    @staticmethod
    def _account_db(root: Path) -> Path:
        account = root / "account.db"
        con = sqlite3.connect(account)
        try:
            con.execute(
                "CREATE TABLE repair_queue("
                "id INTEGER PRIMARY KEY, status TEXT, check_name TEXT, issue TEXT)"
            )
            con.execute(
                "INSERT INTO repair_queue VALUES(1,'pending','order_executor',?)",
                (f"pretrade_ledger_position_mismatch {SYM}/short",),
            )
            con.commit()
        finally:
            con.close()
        return account

    def _patch_classification(self):
        stack = mock.patch.object(
            ledger_autoheal.rec, "venue_positions", return_value={}
        )
        return stack

    def test_direct_api_blocks_live_write_flags_but_keeps_full_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = _make_db(root, "live")
            account = self._account_db(root)
            before_db = db.read_bytes()
            before_account = account.read_bytes()
            with self._patch_classification(), mock.patch.object(
                ledger_autoheal.rec, "fetch_reduce_fills",
                return_value=_fill("ORD-LIVE-BLOCK", 1.0),
            ), mock.patch.object(
                ledger_autoheal, "active_runner", return_value=None
            ), mock.patch.object(
                ledger_autoheal.repair_queue_tool, "do_close"
            ) as close_queue:
                result = ledger_autoheal.autoheal(
                    "live", root, True, 3, None,
                    enable_unrecorded=True,
                )

            self.assertEqual(result["rc"], 2)
            self.assertFalse(result["apply"])
            self.assertTrue(result["apply_requested"])
            self.assertFalse(result["enable_unrecorded"])
            self.assertTrue(result["enable_unrecorded_requested"])
            self.assertTrue(result["write_policy"]["write_request_blocked"])
            self.assertEqual(result["write_policy"]["manual_repair"]["selection"],
                             "unique_ordId")
            self.assertEqual(len(result["healed"]), 1)
            self.assertFalse(result["healed"][0]["applied"])
            self.assertEqual(result["queue_closed"], [])
            self.assertEqual(db.read_bytes(), before_db)
            self.assertEqual(account.read_bytes(), before_account)
            close_queue.assert_not_called()

    def test_direct_cli_live_apply_returns_nonzero_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = _make_db(root, "live")
            account = self._account_db(root)
            before_db = db.read_bytes()
            before_account = account.read_bytes()
            output = io.StringIO()
            argv = [
                "ledger_autoheal.py", "--profile", "live",
                "--db-root", str(root), "--apply", "--enable-unrecorded",
            ]
            with self._patch_classification(), mock.patch.object(
                ledger_autoheal.rec, "fetch_reduce_fills",
                return_value=_fill("ORD-LIVE-CLI", 1.0),
            ), mock.patch.object(
                ledger_autoheal, "active_runner", return_value=None
            ), mock.patch.object(
                sys, "argv", argv
            ), redirect_stdout(output):
                rc = ledger_autoheal.main()

            payload = json.loads(output.getvalue())
            self.assertEqual(rc, 2)
            self.assertTrue(payload["write_policy"]["write_request_blocked"])
            self.assertFalse(payload["healed"][0]["applied"])
            self.assertEqual(payload["queue_closed"], [])
            self.assertEqual(db.read_bytes(), before_db)
            self.assertEqual(account.read_bytes(), before_account)


OPEN_MS = int(datetime(2026, 8, 4, 9, 30, tzinfo=CST).timestamp() * 1000)


def _valid_card() -> dict:
    """合法六项决策卡（与 test_reconcile_hardening 同构）。"""
    return {
        "direction_evidence": ["isolated test"],
        "opposing_evidence": ["counter"],
        "execution_conditions": {"status": "ready"},
        "invalidation_point": {"condition": "invalid"},
        "risk_reward": {"summary": "bounded"},
        "portfolio_impact": {"summary": "isolated"},
        "historical_experience": {
            "matched_wins": [], "matched_losses": [],
            "missed_opportunities": [], "usage": "none",
            "reason": "no comparable sample",
        },
        "agent_judgement": "isolated test",
        "reference_overrides": [],
    }


def _open_fill(ord_id: str, sz: float, px: float = 100.0):
    """开仓腿 fill：fillPnl 必须为 0（P2 的 net 模式判据）。"""
    return [{"ordId": ord_id, "fillSz": str(sz), "fillPx": str(px),
             "fillPnl": "0", "fillTime": str(OPEN_MS), "side": "buy",
             "posSide": "long", "tradeId": f"T-{ord_id}"}]


class UnrecordedAutohealTests(unittest.TestCase):
    """Demo：账本 < OKX（交易所有仓账本无）→ 受控补 open。"""

    def _run(self, root, *, venue, open_fills, intent=None, card=None,
             sl=None, apply=True, enabled=True, max_heals=3):
        with mock.patch.object(ledger_autoheal.rec, "venue_positions",
                               return_value=venue), \
             mock.patch.object(ledger_autoheal.rec, "fetch_reduce_fills",
                               return_value=[]), \
             mock.patch.object(ledger_autoheal.rec, "fetch_open_fills",
                               side_effect=lambda *a, **k: open_fills), \
             mock.patch.object(ledger_autoheal, "_intent_for",
                               return_value=intent), \
             mock.patch.object(ledger_autoheal, "_card_for", return_value=card), \
             mock.patch.object(ledger_autoheal, "_probe_sl",
                               return_value=sl or {"has_sl": True, "n_pending": 1}), \
             mock.patch.object(ledger_autoheal, "active_runner", return_value=None):
            return ledger_autoheal.autoheal("demo", root, apply, max_heals,
                                            None, enable_unrecorded=enabled)

    def _empty_db(self, root: Path) -> Path:
        path = root / "demo_trades.db"
        con = sqlite3.connect(path)
        con.executescript(TRADE_SCHEMA)
        con.commit()
        con.close()
        return path

    def test_t1_with_intent_and_real_card_is_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = self._empty_db(root)
            intent = {"cycle_id": "2026-08-04T09:30", "ord_id": "ORD-A",
                      "state": "submitted", "reserved_at": "2026-08-04 09:29:00"}
            res = self._run(root, venue={(SYM, "long"): 2.0},
                            open_fills=_open_fill("ORD-A", 2.0),
                            intent=intent, card=_valid_card())

            healed = [h for h in res["healed"] if h.get("kind") == "UNRECORDED"]
            self.assertEqual(len(healed), 1)
            self.assertEqual(healed[0]["tier"], "T1")
            self.assertTrue(healed[0]["applied"])
            self.assertTrue(healed[0]["has_real_card"])
            self.assertEqual(healed[0]["cycle_id"], "2026-08-04T09:30")
            self.assertEqual(len(_rows(db, "open")), 1)
            self.assertEqual(len(_rows(db, "close")), 0)

    def test_t2_without_intent_is_reported_and_never_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = self._empty_db(root)
            res = self._run(root, venue={(SYM, "long"): 2.0},
                            open_fills=_open_fill("ORD-B", 2.0), intent=None)

            self.assertEqual(res["healed"], [])
            self.assertEqual(len(_rows(db, "open")), 0)
            p0 = next(n for n in res["needs_human"]
                      if n.get("tier") == "T2")
            self.assertEqual(p0["kind"], "UNRECORDED")
            self.assertEqual(p0["sev"], "P0")
            self.assertIn("禁止自动补", p0["reason"])

    def test_t3_fuzzy_fills_are_never_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = self._empty_db(root)
            # 缺口 2.0，但 fills 只有 0.5 → 对不上
            res = self._run(root, venue={(SYM, "long"): 2.0},
                            open_fills=_open_fill("ORD-C", 0.5))

            self.assertEqual([h for h in res["healed"]
                              if h.get("kind") == "UNRECORDED"], [])
            self.assertEqual(len(_rows(db, "open")), 0)
            t3 = [n for n in res["needs_human"]
                  if n.get("kind") == "UNRECORDED" and n.get("tier") == "T3"]
            self.assertEqual(len(t3), 1)

    def test_missing_or_unknown_stop_blocks_repair_and_raises_p0(self):
        for sl in (
            {"has_sl": False, "n_pending": 0},
            {"has_sl": None, "error": "probe unavailable"},
        ):
            with self.subTest(sl=sl), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                db = self._empty_db(root)
                intent = {
                    "cycle_id": "2026-08-04T09:30", "ord_id": "ORD-D",
                    "state": "submitted", "reserved_at": "2026-08-04 09:29:00",
                }
                res = self._run(
                    root, venue={(SYM, "long"): 2.0},
                    open_fills=_open_fill("ORD-D", 2.0), intent=intent, sl=sl,
                )

                naked = [n for n in res["needs_human"]
                         if n["kind"] == "NAKED-POSITION-P0"]
                self.assertEqual(len(naked), 1)
                self.assertEqual(naked[0]["sev"], "P0")
                self.assertEqual(len(_rows(db, "open")), 0)
                self.assertFalse(res["healed"][0]["applied"])
                self.assertEqual(res["queue_closed"], [])
                self.assertEqual(res["rc"], 2)

    def test_stop_probe_requires_matching_active_reduce_only_full_size(self):
        import _okxorder

        valid = {
            "instId": SYM, "algoId": "A-VALID", "state": "live",
            "posSide": "long", "side": "sell", "reduceOnly": "true",
            "slTriggerPx": "95", "sz": "2",
        }
        wrong = [
            {**valid, "algoId": "A-PAUSED", "state": "pause"},
            {**valid, "algoId": "A-FIRED", "state": "effective"},
            {**valid, "algoId": "A-SIDE", "posSide": "short"},
            {**valid, "algoId": "A-OPEN", "reduceOnly": "false"},
            {**valid, "algoId": "A-SMALL", "sz": "1.5"},
        ]
        with mock.patch.object(
            _okxorder, "get_algo_orders", return_value=[*wrong, valid]
        ):
            result = ledger_autoheal._probe_sl("live", SYM, "long", 2.0)
        self.assertTrue(result["has_sl"])
        self.assertEqual(result["algo_ids"], ["A-VALID"])
        self.assertEqual(result["rejected_candidates"], len(wrong))

        with mock.patch.object(
            _okxorder, "get_algo_orders", return_value=wrong
        ):
            result = ledger_autoheal._probe_sl("live", SYM, "long", 2.0)
        self.assertFalse(result["has_sl"])

    def test_missing_card_still_repairs_but_marks_degradation(self):
        """真卡取不到不阻断补账（不修＝继续冻结交易），但必须如实标注降级。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = self._empty_db(root)
            intent = {"cycle_id": "2026-08-04T09:30", "ord_id": "ORD-E",
                      "state": "submitted", "reserved_at": "2026-08-04 09:29:00"}
            res = self._run(root, venue={(SYM, "long"): 2.0},
                            open_fills=_open_fill("ORD-E", 2.0),
                            intent=intent, card=None)

            healed = [h for h in res["healed"] if h.get("kind") == "UNRECORDED"][0]
            self.assertTrue(healed["applied"])
            self.assertFalse(healed["has_real_card"])
            self.assertIn("decision_card_missing", healed["degradation"])
            self.assertEqual(len(_rows(db, "open")), 1)

    def test_malformed_card_degrades_instead_of_blocking_repair(self):
        """库里的卡不完整时必须降级写入，而不是整单拒绝——拒绝＝交易继续冻结。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = self._empty_db(root)
            intent = {"cycle_id": "2026-08-04T09:30", "ord_id": "ORD-BAD",
                      "state": "submitted", "reserved_at": "2026-08-04 09:29:00"}
            res = self._run(root, venue={(SYM, "long"): 2.0},
                            open_fills=_open_fill("ORD-BAD", 2.0), intent=intent,
                            card={"agent_judgement": "缺其余五项"})

            healed = [h for h in res["healed"] if h.get("kind") == "UNRECORDED"][0]
            self.assertTrue(healed["applied"], "坏卡不得阻断补账")
            self.assertIn("decision_card_missing", healed["degradation"])
            self.assertEqual(len(_rows(db, "open")), 1)

    def test_intent_ordid_mismatch_downgrades_to_t3(self):
        """有 intent 但其单号不在匹配 fills 里 → 归属存疑，绝不补。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = self._empty_db(root)
            intent = {"cycle_id": "2026-08-04T09:30", "ord_id": "ORD-OTHER",
                      "state": "submitted", "reserved_at": "2026-08-04 09:29:00"}
            res = self._run(root, venue={(SYM, "long"): 2.0},
                            open_fills=_open_fill("ORD-F", 2.0), intent=intent)

            self.assertEqual(len(_rows(db, "open")), 0)
            t3 = [n for n in res["needs_human"] if n.get("tier") == "T3"]
            self.assertEqual(len(t3), 1)
            self.assertIn("归属存疑", t3[0]["reason"])

    def test_disable_switch_reports_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = self._empty_db(root)
            res = self._run(root, venue={(SYM, "long"): 2.0},
                             open_fills=_open_fill("ORD-G", 2.0), enabled=False)

            self.assertEqual(len(_rows(db, "open")), 0)
            t2 = next(n for n in res["needs_human"] if n.get("tier") == "T2")
            self.assertEqual(t2["sev"], "P0")

    def test_disable_switch_never_writes_even_exact_t1(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = self._empty_db(root)
            intent = {"cycle_id": "2026-08-04T09:30", "ord_id": "ORD-G2",
                      "state": "submitted", "reserved_at": "2026-08-04 09:29:00"}
            res = self._run(
                root, venue={(SYM, "long"): 2.0},
                open_fills=_open_fill("ORD-G2", 2.0), intent=intent,
                card=_valid_card(), enabled=False, apply=True,
            )

            self.assertEqual(len(_rows(db, "open")), 0)
            item = next(h for h in res["healed"] if h.get("tier") == "T1")
            self.assertFalse(item["applied"])
            self.assertFalse(item["write_enabled"])
            self.assertIn("report-only", item["note"])

    def test_default_read_only_still_probes_stop_and_reports_p0(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = self._empty_db(root)
            intent = {"cycle_id": "2026-08-04T09:30", "ord_id": "ORD-G3",
                      "state": "submitted", "reserved_at": "2026-08-04 09:29:00"}
            res = self._run(
                root, venue={(SYM, "long"): 2.0},
                open_fills=_open_fill("ORD-G3", 2.0), intent=intent,
                card=_valid_card(), sl={"has_sl": False, "n_pending": 0},
                enabled=False, apply=False,
            )

            self.assertEqual(len(_rows(db, "open")), 0)
            naked = [n for n in res["needs_human"]
                     if n.get("kind") == "NAKED-POSITION-P0"]
            self.assertEqual(len(naked), 1)
            self.assertEqual(naked[0]["sev"], "P0")
            self.assertEqual(res["rc"], 2)

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = self._empty_db(root)
            intent = {"cycle_id": "2026-08-04T09:30", "ord_id": "ORD-H",
                      "state": "submitted", "reserved_at": "2026-08-04 09:29:00"}
            res = self._run(root, venue={(SYM, "long"): 2.0},
                            open_fills=_open_fill("ORD-H", 2.0),
                            intent=intent, apply=False)

            healed = [h for h in res["healed"] if h.get("kind") == "UNRECORDED"][0]
            self.assertFalse(healed["applied"])
            self.assertEqual(len(_rows(db, "open")), 0)


class MatchRuleSingleSourceTests(unittest.TestCase):
    """精确匹配规则是唯一定义源——幽灵补 close 与 UNRECORDED 补 open 共用。"""

    def _g(self, oid, sz):
        return {"ordId": oid, "sz": sz, "pnl": 0.0, "wavg_px": 1.0,
                "t_last_ms": OPEN_MS, "fills": []}

    def test_rule_a_all_groups_sum_to_target(self):
        groups = [self._g("A", 0.6), self._g("B", 0.4)]
        hit, leftover, reason = ledger_autoheal.rec.match_exact_groups(groups, 1.0)
        self.assertEqual(len(hit), 2)
        self.assertEqual(leftover, [])
        self.assertIsNone(reason)

    def test_rule_b_unique_group_equals_target(self):
        groups = [self._g("A", 1.0), self._g("B", 0.3)]
        hit, leftover, reason = ledger_autoheal.rec.match_exact_groups(groups, 1.0)
        self.assertEqual([g["ordId"] for g in hit], ["A"])
        self.assertEqual([g["ordId"] for g in leftover], ["B"])

    def test_ambiguous_returns_none(self):
        groups = [self._g("A", 1.0), self._g("B", 1.0)]
        hit, _, reason = ledger_autoheal.rec.match_exact_groups(groups, 1.0)
        self.assertIsNone(hit)
        self.assertIn("无法唯一对齐", reason)


class AutohealWiringContractTests(unittest.TestCase):
    """插入点 A/B 的接线契约。"""

    def test_pretrade_helper_returns_true_only_when_something_applied(self):
        from core import order_executor as oe

        def _fake_run(stdout):
            return mock.Mock(stdout=stdout, returncode=0)

        with mock.patch.dict("os.environ", {}, clear=False), \
             mock.patch("subprocess.run") as run, \
             mock.patch("os.path.exists", return_value=True):
            run.return_value = _fake_run('{"healed":[{"applied":true}]}')
            self.assertTrue(oe._try_autoheal_ledger("demo", _project_path('db'), "c1"))
            self.assertFalse(oe._try_autoheal_ledger("live", _project_path('db'), "c1"))

            run.return_value = _fake_run('{"healed":[{"applied":false}]}')
            self.assertFalse(oe._try_autoheal_ledger("demo", _project_path('db'), "c1"))

            run.return_value = _fake_run('{"healed":[]}')
            self.assertFalse(oe._try_autoheal_ledger("demo", _project_path('db'), "c1"))

    def test_pretrade_write_opt_ins_are_demo_only(self):
        from core import order_executor as oe

        fake = mock.Mock(stdout='{"healed":[]}', returncode=0)
        with mock.patch("subprocess.run", return_value=fake) as run, \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(oe._try_autoheal_ledger("live", _project_path('db'), "c1"))
            argv = run.call_args[0][0]
            self.assertNotIn("--apply", argv)
            self.assertNotIn("--enable-unrecorded", argv)

        with mock.patch("subprocess.run", return_value=fake) as run, \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch.dict("os.environ", {
                 "OKX_LEDGER_AUTOHEAL_APPLY": "1",
                 "OKX_LEDGER_AUTOHEAL_UNRECORDED": "1",
             }, clear=True):
            self.assertFalse(oe._try_autoheal_ledger("live", _project_path('db'), "c1"))
            argv = run.call_args[0][0]
            self.assertNotIn("--apply", argv)
            self.assertNotIn("--enable-unrecorded", argv)

        with mock.patch("subprocess.run", return_value=fake) as run, \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch.dict(
                 "os.environ",
                 {
                     "OKX_LEDGER_AUTOHEAL_APPLY": "1",
                     "OKX_LEDGER_AUTOHEAL_UNRECORDED": "1",
                 },
                 clear=True,
             ):
            self.assertFalse(oe._try_autoheal_ledger("demo", _project_path('db'), "c1"))
            argv = run.call_args[0][0]
            self.assertIn("--apply", argv)
            self.assertIn("--enable-unrecorded", argv)

    def test_pretrade_helper_swallows_failures_and_stays_fail_closed(self):
        from core import order_executor as oe

        with mock.patch("os.path.exists", return_value=True), \
             mock.patch("subprocess.run", side_effect=RuntimeError("boom")):
            self.assertFalse(oe._try_autoheal_ledger("live", _project_path('db'), "c1"))

        # 输出不是合法 JSON 也不得当成"已修"
        with mock.patch("os.path.exists", return_value=True), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(stdout="not-json", returncode=0)):
            self.assertFalse(oe._try_autoheal_ledger("live", _project_path('db'), "c1"))

    def test_pretrade_autoheal_can_be_disabled_by_env(self):
        from core import order_executor as oe

        with mock.patch.dict("os.environ",
                             {"OKX_DISABLE_LEDGER_AUTOHEAL": "1"}), \
             mock.patch("subprocess.run") as run:
            self.assertFalse(oe._try_autoheal_ledger("live", _project_path('db'), "c1"))
            run.assert_not_called()

    def test_trigger_autoheal_only_runs_for_trading_stages(self):
        import trigger_agent

        with mock.patch("subprocess.run") as run, \
             mock.patch.dict("os.environ", {}, clear=True):
            trigger_agent._autoheal_ledger("push", "2026-08-04T10:00")
            trigger_agent._autoheal_ledger("analyst", "2026-08-04T10:00")
            run.assert_not_called()

            trigger_agent._autoheal_ledger("live", "2026-08-04T10:00")
            self.assertEqual(run.call_count, 1)
            argv = run.call_args[0][0]
            self.assertNotIn("--apply", argv)
            self.assertNotIn("--enable-unrecorded", argv)
            self.assertIn("--self-cycle", argv)
            self.assertEqual(argv[argv.index("--profile") + 1], "live")

        with mock.patch("subprocess.run") as run, \
             mock.patch.dict(
                 "os.environ",
                 {
                     "OKX_LEDGER_AUTOHEAL_APPLY": "1",
                     "OKX_LEDGER_AUTOHEAL_UNRECORDED": "1",
                 },
                 clear=True,
             ):
            trigger_agent._autoheal_ledger("live", "2026-08-04T10:00")
            live_argv = run.call_args[0][0]
            self.assertNotIn("--apply", live_argv)
            self.assertNotIn("--enable-unrecorded", live_argv)

            trigger_agent._autoheal_ledger("demo", "2026-08-04T10:00")
            argv = run.call_args[0][0]
            self.assertIn("--apply", argv)
            self.assertIn("--enable-unrecorded", argv)

    def test_trigger_custom_db_root_reaches_autoheal_and_briefing(self):
        import trigger_agent

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fake = mock.Mock(stdout="{}", returncode=0)
            with mock.patch.object(trigger_agent, "LOG_DIR", root / "logs"), \
                 mock.patch.object(trigger_agent.subprocess, "run",
                                   return_value=fake) as run, \
                 mock.patch.dict("os.environ", {}, clear=True):
                trigger_agent._autoheal_ledger(
                    "live", "2026-08-04T10:00", db_root=root
                )
                autoheal_argv = run.call_args.args[0]
                self.assertEqual(
                    autoheal_argv[autoheal_argv.index("--db-root") + 1],
                    str(root),
                )

                trigger_agent._run_briefing(db_root=root)
                briefing_argv = run.call_args.args[0]
                self.assertEqual(
                    briefing_argv[briefing_argv.index("--db-root") + 1],
                    str(root),
                )

    def test_trigger_p0_alerts_and_blocks_before_agent_command(self):
        import trigger_agent

        payload = {
            "rc": 2,
            "needs_human": [{
                "kind": "NAKED-POSITION-P0",
                "sev": "P0",
                "symbol": SYM,
                "side": "long",
                "ord_ids": ["MUST-NOT-LEAK"],
            }],
        }
        heal = mock.Mock(stdout=json.dumps(payload), returncode=2)
        alert = mock.Mock(stdout="sent", returncode=0)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(trigger_agent, "LOG_DIR", Path(tmp)), \
             mock.patch.object(trigger_agent.subprocess, "run",
                               side_effect=[heal, alert]) as run, \
             mock.patch.dict("os.environ", {}, clear=True):
            result = trigger_agent._autoheal_ledger(
                "live", "2026-08-04T10:00", db_root=Path(tmp)
            )

        self.assertTrue(result["blocking"])
        self.assertTrue(result["alerted"])
        self.assertEqual(result["p0_kinds"], ["NAKED-POSITION-P0"])
        self.assertEqual(run.call_count, 2)
        alert_argv = run.call_args_list[1].args[0]
        self.assertIn("--alert", alert_argv)
        self.assertIn("--dedupe-key", alert_argv)
        self.assertEqual(
            alert_argv[alert_argv.index("--db-root") + 1],
            str(Path(tmp).resolve()),
        )
        message = alert_argv[alert_argv.index("--message") + 1]
        self.assertNotIn("MUST-NOT-LEAK", message)

        with mock.patch.object(
            trigger_agent, "_autoheal_ledger",
            return_value={
                "blocking": True,
                "alerted": True,
                "p0_kinds": ["NAKED-POSITION-P0"],
            },
        ), mock.patch.object(trigger_agent, "_analyst_briefing") as briefing, \
             mock.patch.object(trigger_agent, "_write_message_file") as write_msg:
            with self.assertRaisesRegex(RuntimeError, "P0 blocked"):
                trigger_agent.build_cmd(
                    "live", "2026-08-04T10:00", "unified", db_root=Path(tmp)
                )
        briefing.assert_not_called()
        write_msg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
