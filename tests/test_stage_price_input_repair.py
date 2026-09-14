# -*- coding: utf-8 -*-
"""Price-unit mistakes must be caught before analysis commits or orders run."""
import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from collectors import analyst_writer as writer
from collectors import trigger_agent
from core import decision_card
from scripts import _acceptance_thresholds as thresholds
from scripts import decision_briefing
from scripts import multitimeframe_decision_evidence as evidence
from scripts import stage_runner

CYCLE = "2026-09-07T18:30"
ACTIVATION = "2026-09-07T18:30:00+08:00"
TICK = "2026-09-07T10:30:04Z"


def payload(last=1219.97, stop=1190.0):
    return {
        "cycle_id": CYCLE, "ts": "2026-09-07 18:30:00", "mode": "full",
        "status": "ok", "decision_protocol": "minimal_decision_v2",
        "regime": "range", "regime_stale": 0, "missing_sources": [],
        "market_summary": {k: {} for k in ("macro", "news", "tech", "sentiment", "quant")},
        "signals": [{"symbol": "ZEC-USDT-SWAP", "action": "open_long", "side": "long",
                     "reasoning": "current independent market evidence", "entry_hint": last,
                     "stop_hint": stop, "tp_hint": last * 1.04, "exit_mode": "fixed_tp"}],
        "raw": {},
    }


class StagePriceInputRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.market = self.root / "market.db"
        with closing(sqlite3.connect(self.market)) as con, con:
            con.execute("CREATE TABLE tick_snapshots(ts TEXT,symbol TEXT,last REAL)")
            con.execute("INSERT INTO tick_snapshots VALUES(?,?,?)", (TICK, "ZEC-USDT-SWAP", 1219.97))
        self.manifest = {
            "schema": evidence.CANDIDATE_MANIFEST_SCHEMA, "cycle_id": CYCLE,
            "tick_ts": TICK, "candidate_count": 1,
            "candidates": [{"ordinal": 1, "symbol": "ZEC-USDT-SWAP", "side": "long",
                            "layer": "mature", "rotation_due": True, "recent_deep_dives_6h": 0,
                            "recent_rejections_6h": 0, "prior_evidence_hash": None, "last": 1219.97}],
        }
        self.save_manifest()

    def save_manifest(self):
        self.manifest.pop("manifest_sha256", None)
        self.manifest["manifest_sha256"] = hashlib.sha256(json.dumps(
            self.manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        self.manifest_path = evidence.candidate_evidence_paths(CYCLE, root=self.root)["manifest"]
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def check(self, data=None):
        with mock.patch.object(thresholds, "OPEN_PRICE_PREFLIGHT_ACTIVATION_CST", ACTIVATION):
            obj = payload() if data is None else data
            # Normalization already happened at the production writer boundary.
            obj = copy.deepcopy(obj)
            for signal in obj.get("signals") or []:
                if signal.get("action") in {"open_long", "open_short"}:
                    signal["decision_card"] = writer._lightweight_open_card(signal)
            return writer._validate_lightweight_open_prices(obj, db_root=self.root, evidence_dir=self.root)

    def test_prompt_example_prices_cannot_pass_as_live_prices(self):
        message = trigger_agent._closure_unified_live_message(CYCLE, "", candidate_bundle={})
        skeleton = json.loads(message.split("```json\n", 1)[1].split("\n```", 1)[0])
        signal = skeleton["signals"][0]
        self.assertTrue(decision_card.validate_lightweight_open_card(writer._lightweight_open_card(signal)))
        for key in ("entry_hint", "stop_hint", "tp_hint"):
            self.assertIsNone(signal[key])
        self.assertIn("last", message)
        self.assertIn("USDT绝对价格", message)

    def test_real_incident_unit_prices_are_rejected_with_exact_quote(self):
        data = payload(last=1.0, stop=0.98)
        errors = self.check(data)
        self.assertTrue(any("sl_deviation_exceeds" in e and "1219.97" in e for e in errors), errors)
        self.assertEqual(data["signals"][0]["stop_hint"], 0.98)

    def test_real_prices_pass_without_changing_payload_or_database(self):
        data = payload()
        original = copy.deepcopy(data)
        before = self.market.read_bytes()
        self.assertEqual([], self.check(data))
        self.assertEqual(original, data)
        self.assertEqual(before, self.market.read_bytes())
        self.assertFalse((self.root / "analysis.db").exists())

    def test_legitimate_one_dollar_asset_is_not_blacklisted(self):
        self.manifest["candidates"][0]["last"] = 1.0
        self.save_manifest()
        with closing(sqlite3.connect(self.market)) as con, con:
            con.execute("UPDATE tick_snapshots SET last=1.0")
        self.assertEqual([], self.check(payload(last=1.0, stop=0.98)))

    def test_stop_distance_uses_existing_thirty_percent_limit(self):
        self.assertEqual([], self.check(payload(stop=1219.97 * 0.7)))
        self.assertTrue(self.check(payload(stop=1219.97 * 0.69)))

    def test_short_prices_keep_their_direction(self):
        data = payload(stop=1250.0)
        data["signals"][0].update(action="open_short", side="short", tp_hint=1180.0)
        self.assertEqual([], self.check(data))

    def test_quote_missing_does_not_use_a_newer_snapshot(self):
        with closing(sqlite3.connect(self.market)) as con, con:
            con.execute("UPDATE tick_snapshots SET ts='2026-09-07T10:45:04Z'")
        self.assertTrue(any("price_reference_unavailable" in e for e in self.check()))

    def test_quote_drift_from_frozen_manifest_is_rejected(self):
        with closing(sqlite3.connect(self.market)) as con, con:
            con.execute("UPDATE tick_snapshots SET last=1.0")
        self.assertTrue(any("price_reference_mismatch" in e for e in self.check()))

    def test_duplicate_quote_is_not_arbitrarily_selected(self):
        with closing(sqlite3.connect(self.market)) as con, con:
            con.execute("INSERT INTO tick_snapshots VALUES(?,?,?)", (TICK, "ZEC-USDT-SWAP", 1.0))
        self.assertTrue(self.check())

    def test_manifest_tampering_and_wrong_cycle_are_rejected(self):
        original = self.manifest_path.read_text(encoding="utf-8")
        self.manifest_path.write_text(original.replace("1219.97", "1.0"), encoding="utf-8")
        self.assertTrue(self.check())
        self.manifest["cycle_id"] = "2026-09-07T18:15"
        self.save_manifest()
        self.assertTrue(self.check())

    def test_manifest_timestamp_must_belong_to_its_natural_slot(self):
        for stamp in ("2026-09-07T10:15:04Z", "2026-09-07T10:45:00Z", "invalid"):
            with self.subTest(stamp=stamp):
                self.manifest["tick_ts"] = stamp
                self.save_manifest()
                self.assertTrue(self.check())

    def test_bad_quote_values_fail_closed(self):
        for value in (None, True, "NaN", 0, -1):
            with self.subTest(value=value):
                self.manifest["candidates"][0]["last"] = value
                self.save_manifest()
                self.assertTrue(self.check())

    def test_old_cycles_and_zero_open_do_not_require_price_database(self):
        self.market.unlink()
        old = payload()
        old["cycle_id"] = "2026-09-07T18:15"
        self.assertEqual([], self.check(old))
        hold = payload()
        hold["signals"] = []
        self.assertEqual([], self.check(hold))
        close = payload()
        close["signals"] = [{"symbol": "ZEC-USDT-SWAP", "action": "close", "side": "long"}]
        self.assertEqual([], self.check(close))

    def test_writer_uses_price_preflight_before_commit(self):
        with mock.patch.object(writer, "normalize_receipt", side_effect=lambda data: data), \
             mock.patch.object(writer, "analysis_deadline_refusal", return_value=None), \
             mock.patch.object(writer, "_validate_lightweight_open_prices", return_value=["unit_price_error"]), \
             mock.patch.object(writer, "connect") as connect:
            result = writer.write_analysis(payload())
        self.assertFalse(result["ok"])
        self.assertIn("unit_price_error", str(result))
        connect.assert_not_called()

    def test_briefing_preserves_subcent_prices_and_does_not_invent_missing(self):
        self.assertIn("0.00001234", decision_briefing._candidate_price_text(0.00001234))
        self.assertIn("1219.97", decision_briefing._candidate_price_text(1219.97))
        self.assertIn("0.0000000000001234", decision_briefing._candidate_price_text(1.234e-13))
        for value in (None, True, float("nan"), float("inf"), 0, -1):
            self.assertIn("N/A", decision_briefing._candidate_price_text(value))


class StageFailureDiagnosticTests(unittest.TestCase):
    def test_validation_failure_keeps_a_bounded_reason_for_the_missing_analysis_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(writer, "VALIDATION_STATE_DIR", root / "logs/analysis-validation"):
                errors = ["sl_deviation_exceeds: last=1219.97 stop=0.98\nprice units"] + ["x" * 900] * 5
                state = writer._record_validation_failure(CYCLE, "a" * 64, errors)
                self.assertEqual(3, len(state["last_errors"]))
                self.assertLessEqual(max(map(len, state["last_errors"])), 300)
                state = writer._record_validation_failure(CYCLE, "a" * 64, errors)
                self.assertTrue(state["blocked"])
                with mock.patch.object(stage_runner, "_row_exists", return_value=(False, None)):
                    result = stage_runner.verify_business_output("live", CYCLE, "unified", root / "db")
            self.assertFalse(result["ok"])
            self.assertEqual("business_output_missing", result["failure_kind"])
            self.assertIn("sl_deviation_exceeds", "\n".join(stage_runner._failure_cause_lines(result)))

    def test_missing_analysis_does_not_borrow_another_cycles_validation_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "logs/analysis-validation"
            state_dir.mkdir(parents=True)
            (state_dir / "analysis-2026-09-07T18-30.json").write_text(json.dumps({
                "schema_version": 1, "cycle_id": "2026-09-07T18:15",
                "failed_attempts": 2, "max_failed_attempts": 2, "last_errors": ["wrong_cycle"]}), encoding="utf-8")
            with mock.patch.object(stage_runner, "_row_exists", return_value=(False, None)):
                result = stage_runner.verify_business_output("live", CYCLE, "unified", root / "db")
            self.assertFalse(result["ok"])
            self.assertNotIn("analysis_validation", result)

    def test_failed_business_check_includes_executor_reason_without_promoting_success(self):
        raw = {"cycle_id": CYCLE, "batch_status": "failed", "position_action_failures": [{
            "request": {"action": "ADD", "symbol": "PONS-USDT-SWAP", "side": "long"},
            "result": {"reject_reason": "pre_position_semantics_changed", "reject_detail": "exists expected=True actual=False"}}]}
        with mock.patch.object(stage_runner, "_row_exists", return_value=(True, {
            "decision": "error", "n_orders": 0, "ts": "2026-09-07 18:35:00", "raw": json.dumps(raw)})):
            result = stage_runner.verify_business_output("live", CYCLE, "full")
        self.assertFalse(result["ok"])
        self.assertEqual("business_verification_error", result["failure_kind"])
        self.assertIn("pre_position_semantics_changed", "\n".join(stage_runner._failure_cause_lines(result)))

    def test_wrong_cycle_diagnostics_are_not_attached(self):
        raw = {"cycle_id": "2026-09-07T18:15", "errors": ["unrelated_failure"]}
        with mock.patch.object(stage_runner, "_row_exists", return_value=(True, {
            "decision": "error", "n_orders": 0, "ts": "2026-09-07 18:35:00", "raw": json.dumps(raw)})):
            result = stage_runner.verify_business_output("live", CYCLE, "full")
        self.assertFalse(result["ok"])
        self.assertNotIn("unrelated_failure", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
