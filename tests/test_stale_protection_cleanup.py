# -*- coding: utf-8 -*-
"""残留保护单清理（2026-09-12）：只撤交易所已证无仓一侧的 reduceOnly 平仓方向单。

交易所侧 SL 成交平仓后，开仓后另挂的独立 reduceOnly 固定 TP 仍然 live；同
symbol/posSide 日后新开的仓会继承它（09-12 00:00 普查 34 张；UNI 23:39 新仓继承
22:08 那单的 TP）。本文件锁定：
  1. 账户级列表把读失败和空列表分开，且从不传 CLI 会吞掉的翻页参数；
  2. 开仓前清理只撤现仓读证明无仓那一侧、且早于该证明的行；
  3. 普查默认只报告；交易所现仓与账本轧差都无仓的一侧才算无仓；apply 时逐标的
     复读、撤前再读现仓，有仓一侧的保护单（CGNX 多 1 的 SL 形态）永远不撤，
     任一读失败都不撤；
  4. 运维 CLI 在 live runner 在飞时拒跑。
只 mock 交易所边界（`ox._call` 一律设绊线），不连网、不写生产库。
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "core", ROOT / "core" / "lib", ROOT / "scripts",
           ROOT / "collectors"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core import order_executor as oe  # noqa: E402
import cancel_flat_side_protection as cli  # noqa: E402

HOUR_MS = 3_600_000
CGNX_SL = "3913749330030174208"   # CGNX 多 1 在用的 SL @62.06（09-12 只读核实）


class _ExchangeBoundary(unittest.TestCase):
    """交易所只经显式 mock；`ox._call` 设绊线；repair 入队被截获；哨兵指向临时目录。"""

    def setUp(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.db_root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.sentinel = self.db_root / "stale_protection_cleanup.off"
        stack.enter_context(mock.patch.object(
            oe, "STALE_PROTECTION_CLEANUP_OFF", self.sentinel))
        stack.enter_context(mock.patch.object(
            oe.ox, "is_dryrun", return_value=False))
        stack.enter_context(mock.patch.object(
            oe.ox, "_call",
            side_effect=AssertionError("unexpected exchange I/O")))
        self.repair = stack.enter_context(
            mock.patch.object(oe, "_enqueue_repair"))
        self.cancel = stack.enter_context(mock.patch.object(
            oe.ox, "cancel_algo_order",
            return_value={"ok": True, "data": [{"sCode": "0"}]}))
        self.now_ms = time.time() * 1000

    def algo(self, symbol: str, algo_id: str, pos_side: str, *,
             age_ms: float = 48 * HOUR_MS, sl=None, tp=None, sz: str = "1",
             reduce_only: str = "true", state: str = "live") -> dict:
        return {
            "instId": symbol, "algoId": algo_id, "ordType": "conditional",
            "posSide": pos_side,
            "side": "sell" if pos_side == "long" else "buy",
            "reduceOnly": reduce_only, "state": state, "sz": sz,
            "slTriggerPx": "" if sl is None else str(sl),
            "tpTriggerPx": "" if tp is None else str(tp),
            "cTime": str(int(self.now_ms - age_ms)),
        }

    def cancelled(self) -> list[tuple]:
        return [call.args for call in self.cancel.call_args_list]


class ListAlgoOrdersTests(_ExchangeBoundary):
    def test_listing_keeps_the_read_status_and_never_pages(self):
        calls = []

        def fake_call(*args, profile, timeout_sec=45.0):
            calls.append(args)
            self.assertEqual(profile, "live")
            if "--instId" in args:
                return {"ok": False, "error": "okx CLI rc=1", "data": []}
            return {"ok": True, "data": [{"algoId": "A"}]}

        with mock.patch.object(oe.ox, "_call", side_effect=fake_call):
            wide = oe.ox.list_algo_orders("live")
            typed = oe.ox.list_algo_orders("live", ord_type="oco")
            one = oe.ox.list_algo_orders("live", inst_id="TAO-USDT-SWAP")

        self.assertEqual(calls, [
            ("swap", "algo", "orders"),
            ("swap", "algo", "orders", "--ordType", "oco"),
            ("swap", "algo", "orders", "--instId", "TAO-USDT-SWAP"),
        ])
        self.assertEqual((wide["ok"], wide["data"]), (True, [{"algoId": "A"}]))
        self.assertTrue(typed["ok"])
        self.assertFalse(one["ok"])          # 读失败 ≠ 空列表
        for args in calls:
            self.assertFalse({"--after", "--before", "--limit"} & set(args))


class PreOpenLeftoverTests(_ExchangeBoundary):
    SYMBOL = "UNI-USDT-SWAP"

    def run_helper(self, rows):
        with mock.patch.object(oe.ox, "get_algo_orders",
                               return_value=rows) as listing:
            result = oe._cancel_flat_side_leftovers(
                self.SYMBOL, "long", "live", self.db_root)
        return result, listing

    def test_only_rows_older_than_the_flat_proof_are_cancelled(self):
        rows = [
            self.algo(self.SYMBOL, "OLD-TP", "long", tp=6.672, sz="35"),
            self.algo(self.SYMBOL, "OLD-SL", "long", sl=6.4, sz="35"),
            self.algo(self.SYMBOL, "JUST-PLACED", "long", tp=6.7,
                      age_ms=5_000),
            dict(self.algo(self.SYMBOL, "NO-CTIME", "long", tp=6.8),
                 cTime=""),
            self.algo(self.SYMBOL, "SHORT-SIDE-SL", "short", sl=7.1),
        ]
        result, listing = self.run_helper(rows)

        listing.assert_called_once_with(self.SYMBOL, "live")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["cancel_requested"], ["OLD-TP", "OLD-SL"])
        self.assertEqual(sorted(result["kept_recent"]),
                         ["JUST-PLACED", "NO-CTIME"])
        self.assertEqual(self.cancelled(), [
            (self.SYMBOL, "OLD-TP", "live"), (self.SYMBOL, "OLD-SL", "live")])
        self.repair.assert_not_called()

    def test_unreadable_protection_is_left_alone(self):
        unreadable = oe.ProtectionRows(read_error="RuntimeError: timeout")
        with mock.patch.object(oe, "_live_protection_rows",
                               return_value=unreadable):
            result = oe._cancel_flat_side_leftovers(
                self.SYMBOL, "long", "live", self.db_root)
        self.assertFalse(result["ok"])
        self.assertEqual(result["read_error"], "RuntimeError: timeout")
        self.cancel.assert_not_called()
        self.repair.assert_not_called()

    def test_cancel_failure_is_queued_for_repair(self):
        self.cancel.return_value = {"ok": False, "error": "okx CLI rc=1"}
        result, _ = self.run_helper(
            [self.algo(self.SYMBOL, "OLD-TP", "long", tp=6.672)])
        self.assertFalse(result["ok"])
        self.assertEqual(result["cancel_failed"], ["OLD-TP"])
        self.assertEqual(self.repair.call_args.args[3],
                         "pre_open_stale_protection_cancel_failed:OLD-TP")

    def test_sentinel_disables_every_exchange_call(self):
        self.sentinel.write_text("off", encoding="utf-8")
        with mock.patch.object(oe.ox, "get_algo_orders") as listing:
            result = oe._cancel_flat_side_leftovers(
                self.SYMBOL, "long", "live", self.db_root)
        self.assertEqual(result["disabled"], self.sentinel.name)
        self.assertEqual(result["cancel_requested"], [])
        listing.assert_not_called()
        self.cancel.assert_not_called()


class SweepFlatSideProtectionTests(_ExchangeBoundary):
    HELD = [{"symbol": "CGNX-USDT-SWAP", "side": "long", "sz": 1.0}]

    def account_rows(self) -> list[dict]:
        return [
            self.algo("CGNX-USDT-SWAP", CGNX_SL, "long", sl=62.06,
                      age_ms=2 * HOUR_MS),
            self.algo("TAO-USDT-SWAP", "TAO-TP", "short", tp=209.1, sz="76",
                      age_ms=9 * 24 * HOUR_MS),
            self.algo("CGNX-USDT-SWAP", "CGNX-SHORT-TP", "short", tp=55.0,
                      age_ms=30 * HOUR_MS),
            self.algo("UNI-USDT-SWAP", "UNI-TP", "long", tp=6.672, sz="35",
                      age_ms=26 * HOUR_MS),
            self.algo("ETH-USDT-SWAP", "ETH-FRESH-TP", "long", tp=2700.0,
                      age_ms=60_000),
            self.algo("SOL-USDT-SWAP", "SOL-NOT-REDUCE-ONLY", "long",
                      tp=150.0, reduce_only="false"),
            dict(self.algo("XRP-USDT-SWAP", "XRP-NO-POSSIDE", "short",
                           tp=0.5), posSide=""),
        ]

    def sweep(self, *, apply: bool, rows=None, per_symbol=None,
              positions=None, account_error=None, ledger=None, **kwargs):
        rows = self.account_rows() if rows is None else rows
        per_symbol = per_symbol or {}

        def listing(profile, ord_type=None, inst_id=None):
            self.assertEqual((profile, ord_type), ("live", None))
            if inst_id is None:
                if account_error:
                    return {"ok": False, "error": account_error, "data": []}
                return {"ok": True, "data": [dict(row) for row in rows]}
            if inst_id in per_symbol:
                return per_symbol[inst_id]
            return {"ok": True, "data": [dict(row) for row in rows
                                         if row["instId"] == inst_id]}

        reads = list(positions) if positions is not None else [
            self.HELD, self.HELD]
        with mock.patch.object(oe.ox, "list_algo_orders",
                               side_effect=listing) as lister, \
                mock.patch.object(oe, "fetch_open_positions",
                                  side_effect=reads) as pos, \
                mock.patch.object(
                    oe, "_read_trade_ledger_positions",
                    side_effect=ledger if isinstance(ledger, Exception) else None,
                    return_value=ledger if isinstance(ledger, dict) else {}):
            report = oe.sweep_flat_side_protection(
                "live", self.db_root, apply=apply, **kwargs)
        return report, lister, pos

    def test_dry_run_lists_old_flat_side_rows_and_changes_nothing(self):
        report, lister, pos = self.sweep(apply=False)

        self.assertTrue(report["ok"], report)
        self.assertEqual([row["algoId"] for row in report["candidates"]],
                         ["TAO-TP", "CGNX-SHORT-TP", "UNI-TP"])
        self.assertEqual(report["selected"],
                         ["TAO-TP", "CGNX-SHORT-TP", "UNI-TP"])
        self.assertEqual([row["algoId"] for row in report["recent_kept"]],
                         ["ETH-FRESH-TP"])
        self.assertEqual(report["held_side_rows"], 1)
        self.assertNotIn(CGNX_SL, json.dumps(report["candidates"]))
        # 列表未撞满：不逐标的探查；dry-run 不复读、不再读现仓。
        self.assertEqual((lister.call_count, pos.call_count), (1, 1))
        self.cancel.assert_not_called()
        self.repair.assert_not_called()

    def test_apply_cancels_reconfirmed_rows_and_never_a_held_side_sl(self):
        report, lister, pos = self.sweep(apply=True)

        self.assertTrue(report["ok"], report)
        self.assertEqual(self.cancelled(), [
            ("TAO-USDT-SWAP", "TAO-TP", "live"),
            ("CGNX-USDT-SWAP", "CGNX-SHORT-TP", "live"),
            ("UNI-USDT-SWAP", "UNI-TP", "live"),
        ])
        self.assertEqual(report["cancel_requested"],
                         ["TAO-TP", "CGNX-SHORT-TP", "UNI-TP"])
        self.assertNotIn(CGNX_SL, [args[1] for args in self.cancelled()])
        self.assertEqual(pos.call_count, 2)       # 发现时一次、撤单前一次
        self.assertEqual(
            sorted(call.kwargs.get("inst_id")
                   for call in lister.call_args_list[1:]),
            ["CGNX-USDT-SWAP", "TAO-USDT-SWAP", "UNI-USDT-SWAP"])
        self.repair.assert_not_called()

    def test_any_failed_read_cancels_nothing(self):
        down = oe.PositionsUnavailable("positions api failed")
        for label, kwargs, error, reads in (
                ("account listing unreadable",
                 {"account_error": "okx CLI timeout"},
                 "algo_listing_failed:okx CLI timeout", 0),
                ("positions unreadable", {"positions": [down]},
                 "positions_unavailable:positions api failed", 1),
                ("positions recheck unreadable",
                 {"positions": [self.HELD, down]},
                 "positions_recheck_unavailable:positions api failed", 2),
                ("ledger unreadable",
                 {"ledger": oe.TradeLedgerUnavailable("live_trades.db:missing")},
                 "ledger_unavailable:live_trades.db:missing", 1)):
            with self.subTest(label):
                self.cancel.reset_mock()
                report, _, pos = self.sweep(apply=True, **kwargs)
                self.assertFalse(report["ok"])
                self.assertEqual(report["error"], error)
                self.assertEqual(pos.call_count, reads)
                self.cancel.assert_not_called()

    def test_sides_the_ledger_still_holds_are_left_for_the_next_run(self):
        # 09-12 02:03 DOGE/NEAR 多的形态：SL 已在交易所平仓，autoheal 尚未补账。
        report, _, _ = self.sweep(
            apply=True, ledger={("UNI-USDT-SWAP", "long"): 35.0})

        self.assertTrue(report["ok"], report)
        self.assertEqual([row["algoId"] for row in report["ledger_open_rows"]],
                         ["UNI-TP"])
        self.assertNotIn("UNI-TP", report["selected"])
        self.assertEqual([args[1] for args in self.cancelled()],
                         ["TAO-TP", "CGNX-SHORT-TP"])

    def test_unreadable_symbol_is_skipped_and_reported(self):
        report, _, _ = self.sweep(apply=True, per_symbol={
            "TAO-USDT-SWAP": {"ok": False, "error": "okx CLI rc=1",
                              "data": []}})
        self.assertFalse(report["ok"])
        self.assertEqual(report["confirm_errors"],
                         {"TAO-USDT-SWAP": "okx CLI rc=1"})
        self.assertEqual([args[1] for args in self.cancelled()],
                         ["CGNX-SHORT-TP", "UNI-TP"])

    def test_rows_or_sides_that_changed_since_discovery_are_left_alone(self):
        seen_late = self.algo("TAO-USDT-SWAP", "TAO-SEEN-ONLY-AT-CONFIRM",
                              "short", tp=200.0, age_ms=10 * 24 * HOUR_MS)
        tao_now = [row for row in self.account_rows()
                   if row["instId"] == "TAO-USDT-SWAP"] + [seen_late]
        reopened = self.HELD + [
            {"symbol": "CGNX-USDT-SWAP", "side": "short", "sz": 2.0}]
        report, _, _ = self.sweep(
            apply=True,
            per_symbol={"UNI-USDT-SWAP": {"ok": True, "data": []},
                        "TAO-USDT-SWAP": {"ok": True, "data": tao_now}},
            positions=[self.HELD, reopened])

        self.assertTrue(report["ok"], report)
        self.assertEqual([args[1] for args in self.cancelled()], ["TAO-TP"])
        self.assertEqual(report["vanished"], ["UNI-TP"])
        self.assertEqual(report["side_reopened"], ["CGNX-USDT-SWAP:short"])

    def test_bounds_cap_cancels_and_symbols_per_run(self):
        report, _, _ = self.sweep(apply=True, max_cancels=2)
        self.assertEqual(report["cancel_requested"],
                         ["TAO-TP", "CGNX-SHORT-TP"])
        self.assertEqual(report["skipped_bounds"], ["UNI-TP"])

        self.cancel.reset_mock()
        report, _, _ = self.sweep(apply=True, max_symbols=1)
        self.assertEqual(report["cancel_requested"], ["TAO-TP"])
        self.assertEqual(report["skipped_bounds"], ["CGNX-SHORT-TP", "UNI-TP"])
        self.assertEqual([args[1] for args in self.cancelled()], ["TAO-TP"])

    def test_full_account_page_probes_ledger_symbols_for_older_rows(self):
        full = [self.algo("CGNX-USDT-SWAP", f"CGNX-SL-{index}", "long",
                          sl=62.0, age_ms=HOUR_MS) for index in range(100)]
        old_tao = [self.algo("TAO-USDT-SWAP", "TAO-TP", "short", tp=209.1,
                             age_ms=9 * 24 * HOUR_MS)]
        report, lister, _ = self.sweep(
            apply=False, rows=full,
            per_symbol={"TAO-USDT-SWAP": {"ok": True, "data": old_tao}},
            symbols=("TAO-USDT-SWAP", "CGNX-USDT-SWAP"))

        self.assertEqual(report["listing"]["page_full"], ["conditional"])
        self.assertEqual(report["listing"]["probed"],
                         ["CGNX-USDT-SWAP", "TAO-USDT-SWAP"])
        self.assertEqual(report["selected"], ["TAO-TP"])
        self.assertEqual(report["held_side_rows"], 100)

        report, lister, _ = self.sweep(apply=False, symbols=("TAO-USDT-SWAP",))
        self.assertEqual(report["listing"]["page_full"], [])
        self.assertEqual(lister.call_count, 1)

    def test_sentinel_refuses_apply_but_still_allows_the_report(self):
        self.sentinel.write_text("off", encoding="utf-8")
        report, lister, pos = self.sweep(apply=True)
        self.assertEqual(report["error"],
                         "disabled_by_sentinel:stale_protection_cleanup.off")
        self.assertEqual((lister.call_count, pos.call_count), (0, 0))

        report, _, _ = self.sweep(apply=False)
        self.assertTrue(report["ok"], report)
        self.cancel.assert_not_called()


class CancelFlatSideProtectionCliTests(_ExchangeBoundary):
    def write_ledger(self, rows) -> None:
        con = sqlite3.connect(self.db_root / "live_trades.db")
        try:
            con.execute("CREATE TABLE trades("
                        "id INTEGER PRIMARY KEY, ts TEXT, symbol TEXT)")
            con.executemany("INSERT INTO trades(ts, symbol) VALUES(?,?)", rows)
            con.commit()
        finally:
            con.close()

    def run_cli(self, argv, *, runner=None, sweep_result=None):
        sweep = mock.Mock(return_value=dict(sweep_result or {"ok": True}))
        out = io.StringIO()
        with mock.patch.object(cli, "active_runner", return_value=runner), \
                mock.patch.object(cli.oe, "sweep_flat_side_protection",
                                  sweep), \
                redirect_stdout(out):
            rc = cli.main(["--db-root", str(self.db_root), *argv])
        return rc, sweep, json.loads(out.getvalue())

    def test_refuses_while_a_live_runner_is_in_flight(self):
        rc, sweep, printed = self.run_cli(
            ["--apply"], runner={"cycle_id": "2026-09-12T01:15"})
        self.assertEqual(rc, 3)
        self.assertEqual(printed["refused"], "live_runner_active")
        sweep.assert_not_called()

    def test_recent_ledger_symbols_feed_a_default_dry_run(self):
        now = datetime.now(cli.CST)

        def ts(days):
            return (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

        self.write_ledger([(ts(1), "UNI-USDT-SWAP"), (ts(9), "TAO-USDT-SWAP"),
                           (ts(2), "UNI-USDT-SWAP"), (ts(40), "OLD-USDT-SWAP")])
        report_path = self.db_root / "sweep.json"
        rc, sweep, printed = self.run_cli(["--json-out", str(report_path)])

        self.assertEqual(rc, 0)
        args, kwargs = sweep.call_args
        self.assertEqual(args[0], "live")
        self.assertFalse(kwargs["apply"])
        self.assertEqual(kwargs["symbols"], ("TAO-USDT-SWAP", "UNI-USDT-SWAP"))
        self.assertEqual((kwargs["max_symbols"], kwargs["max_cancels"]),
                         (8, 12))
        self.assertEqual(printed["ledger_symbols"], 2)
        self.assertEqual(
            json.loads(report_path.read_text(encoding="utf-8")), printed)

        rc, sweep, _ = self.run_cli(["--apply", "--max-cancels", "3"],
                                    sweep_result={"ok": False})
        self.assertEqual(rc, 1)
        self.assertTrue(sweep.call_args.kwargs["apply"])
        self.assertEqual(sweep.call_args.kwargs["max_cancels"], 3)

    def test_unreadable_ledger_stops_before_the_exchange(self):
        rc, sweep, printed = self.run_cli([])
        self.assertEqual(rc, 2)
        self.assertTrue(printed["error"].startswith("ledger_unreadable:"))
        sweep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
