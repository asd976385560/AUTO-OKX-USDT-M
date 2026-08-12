# -*- coding: utf-8 -*-
"""Regression tests for bounded, UTF-8 agent evidence output."""
from __future__ import annotations

import io
import copy
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
COLLECTORS = ROOT / "collectors"
for path in (SCRIPTS, COLLECTORS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import _okxcli  # noqa: E402
import find_similar_experience  # noqa: E402
import stage_runner  # noqa: E402
import trigger_agent  # noqa: E402
from core.experience_contract import validate_contract  # noqa: E402


class SimilarExperienceOutputTests(unittest.TestCase):
    def test_same_symbol_statistics_are_separate_from_cross_symbol_analogues(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            con = sqlite3.connect(root / "account.db")
            try:
                con.execute(
                    "CREATE TABLE trade_experiences("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "cycle_id TEXT,ts TEXT,profile TEXT,symbol TEXT,side TEXT,"
                    "action TEXT,regime TEXT,regime_stale INTEGER,"
                    "score_total REAL,confidence REAL,playbook_ref TEXT,"
                    "experience_vector TEXT,pnl_pct REAL,hold_hours REAL,"
                    "is_gross_profit_close INTEGER,raw TEXT,experience_summary TEXT,"
                    "status TEXT,closed_at TEXT)"
                )
                query_vec = {
                    "v": 2,
                    "features": (
                        find_similar_experience._simutil
                        .experience_features_v2({
                            "asset_class": "crypto", "side": "long",
                            "regime": "range", "action": "open",
                        })),
                    "legacy_v1": None,
                }
                for index, symbol in enumerate(
                        ["GOOGL-USDT-SWAP"] * 3 + ["ETH-USDT-SWAP"] * 3):
                    con.execute(
                        "INSERT INTO trade_experiences VALUES("
                        "NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            f"c{index}", "2026-07-30 12:00:00", "demo",
                            symbol, "long", "open", "range", 0, None, None,
                            None, json.dumps(query_vec),
                            1.0 if index % 2 == 0 else -1.0,
                            1.0, 0, "{}", "lesson", "closed",
                            "2026-07-30 13:00:00",
                        ),
                    )
                con.commit()
            finally:
                con.close()

            result = find_similar_experience.find_similar_experience(
                "GOOGL",
                "long",
                "range",
                "open",
                db_root=root,
                now=datetime(2026, 7, 31, 12, 0,
                             tzinfo=find_similar_experience.CST),
            )

        self.assertEqual(result["summary"]["n"], 3)
        self.assertEqual(result["exact_setup_summary"]["n"], 3)
        self.assertEqual(result["exact_setup_summary"]["wins"], 2)
        self.assertEqual(result["exact_setup_summary"]["losses"], 1)
        self.assertEqual(result["cross_summary"]["n"], 3)
        self.assertEqual(result["query"]["symbol"], "GOOGL-USDT-SWAP")
        self.assertEqual(
            validate_contract(
                result["evidence_contract"],
                expected_symbol="GOOGL-USDT-SWAP",
                expected_side="long",
                expected_regime="range",
                expected_action="open",
                expected_profile="all",
                expected_as_of="2026-07-31 12:00:00",
            ),
            [],
        )
        tampered = copy.deepcopy(result["evidence_contract"])
        tampered["summaries"]["exact_setup"]["wins"] = 99
        self.assertTrue(validate_contract(tampered))
        self.assertTrue(all(
            item["symbol"] == "GOOGL-USDT-SWAP"
            for item in result["matched_wins"] + result["matched_losses"]
        ))
        self.assertTrue(all(
            item["symbol"] == "ETH-USDT-SWAP"
            for item in (
                result["cross_symbol_wins"]
                + result["cross_symbol_losses"]
            )
        ))

    def test_compact_output_keeps_decision_evidence_without_raw_payloads(self):
        result = find_similar_experience.compact_result({
            "summary": {"n": 4, "credibility": 0.25},
            "matches": [{"raw": "duplicate"}],
            "query_vec": [1, 2, 3],
            "matched_wins": [{
                "sim": 0.9,
                "pnl_pct": 1.2,
                "cycle_id": "c1",
                "profile": "live",
                "symbol": "BTC-USDT-SWAP",
                "outcome": "win",
                "lesson": "x" * 300,
                "raw_snippet": "must be omitted",
            }],
            "matched_losses": [],
            "cross_summary": {"n": 2, "sufficient": False},
            "cross_symbol_wins": [{
                "sim": 0.8,
                "pnl_pct": 0.5,
                "cycle_id": "c2",
                "profile": "demo",
                "symbol": "ETH-USDT-SWAP",
                "outcome": "win",
                "lesson": "analogue",
            }],
            "cross_symbol_losses": [],
            "query_symbol": "BTC-USDT-SWAP",
            "missed_opportunities": [{
                "ts": "2026-07-28 00:00:00",
                "symbol": "BTC-USDT-SWAP",
                "actual_4h_pct": -1.0,
                "notes": "y" * 300,
            }],
        })

        self.assertNotIn("matches", result)
        self.assertNotIn("query_vec", result)
        self.assertNotIn(
            "raw_snippet", result["matched_wins"][0])
        self.assertEqual(len(result["matched_wins"][0]["lesson"]), 240)
        self.assertEqual(
            result["matched_wins"][0]["symbol"], "BTC-USDT-SWAP")
        self.assertEqual(
            result["cross_symbol_wins"][0]["symbol"], "ETH-USDT-SWAP")
        self.assertEqual(result["cross_summary"]["n"], 2)
        self.assertEqual(
            len(result["missed_opportunities"][0]["notes"]), 240)

    def test_as_of_excludes_experience_closed_after_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            con = sqlite3.connect(root / "account.db")
            try:
                con.execute(
                    "CREATE TABLE trade_experiences("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "cycle_id TEXT,ts TEXT,profile TEXT,symbol TEXT,side TEXT,"
                    "action TEXT,regime TEXT,regime_stale INTEGER,"
                    "score_total REAL,confidence REAL,playbook_ref TEXT,"
                    "experience_vector TEXT,pnl_pct REAL,hold_hours REAL,"
                    "is_gross_profit_close INTEGER,raw TEXT,experience_summary TEXT,"
                    "status TEXT,closed_at TEXT)"
                )
                vector = {
                    "v": 2,
                    "features": (
                        find_similar_experience._simutil
                        .experience_features_v2({
                            "asset_class": "crypto", "side": "short",
                            "regime": "range", "action": "open",
                        })),
                    "legacy_v1": None,
                }
                for cycle, closed_at in (
                    ("old", "2026-08-09 10:00:00"),
                    ("future", "2026-08-10 11:17:02"),
                ):
                    con.execute(
                        "INSERT INTO trade_experiences VALUES("
                        "NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            cycle, "2026-08-09 08:00:00", "live",
                            "HYPE-USDT-SWAP", "short", "open", "range", 0,
                            None, None, None, json.dumps(vector), -1.0, 2.0, 0,
                            "{}", "lesson", "closed", closed_at,
                        ),
                    )
                con.commit()
            finally:
                con.close()

            lcon = sqlite3.connect(root / "lessons.db")
            try:
                lcon.execute(
                    "CREATE TABLE missed_opportunities("
                    "id INTEGER PRIMARY KEY,ts TEXT,symbol TEXT,regime TEXT,"
                    "direction_hint TEXT,actual_4h_pct REAL,would_hit_1r_fixed2pct INTEGER,"
                    "notes TEXT)"
                )
                lcon.executemany(
                    "INSERT INTO missed_opportunities("
                    "ts,symbol,regime,direction_hint,actual_4h_pct,would_hit_1r_fixed2pct,notes) "
                    "VALUES(?,?,?,?,?,?,?)",
                    [
                        (
                            "2026-08-10 07:45:00", "HYPE-USDT-SWAP", "range",
                            "short", -0.5, 0, "available before cycle",
                        ),
                        (
                            "2026-08-10 08:15:00", "HYPE-USDT-SWAP", "range",
                            "short", 1.0, 1, "future leakage",
                        ),
                    ],
                )
                lcon.commit()
            finally:
                lcon.close()

            result = find_similar_experience.find_similar_experience(
                "HYPE",
                "short",
                "range",
                "open",
                profile_filter="live",
                db_root=root,
                now=datetime(2026, 8, 10, 8, 0,
                             tzinfo=find_similar_experience.CST),
            )

        self.assertEqual(result["exact_setup_summary"]["n"], 1)
        self.assertEqual(
            result["evidence_contract"]["query"]["as_of"],
            "2026-08-10 08:00:00",
        )
        self.assertEqual(
            [item["notes"] for item in result["missed_opportunities"]],
            ["available before cycle"],
        )

    def test_out_file_is_atomic_utf8_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.json"
            size = find_similar_experience._atomic_write_json(
                path, {"text": "中文", "ok": True}, pretty=True)
            value = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(value["text"], "中文")
            self.assertEqual(size, len(path.read_bytes()))
            self.assertEqual(list(path.parent.glob("*.tmp")), [])


class AgentCommandAndAlertContractTests(unittest.TestCase):
    def test_unified_prompt_separates_route_mode_from_writer_mode(self):
        message = trigger_agent._unified_live_message(
            "2026-08-11T18:30", "briefing marker")

        self.assertIn("dispatch_mode=unified", message)
        self.assertIn("analysis_receipt_mode=full", message)
        self.assertIn("顶层 mode 必须固定为 full", message)
        self.assertIn("market_summary 必须直接包含", message)
        self.assertIn("signals[].decision_card 必须直接包含", message)
        self.assertIn("HOLD/WAIT 同样必须给完整卡", message)
        self.assertIn("不得改名为 rationale/final_judgement/overrides", message)
        self.assertIn("【trade writer 契约】", message)
        self.assertIn("cycle 顶层 decision_card 不是摘要容器", message)
        self.assertIn("禁止改成 summary/open_candidates/hold_positions", message)
        self.assertIn("回执应省略 live_facts", message)
        self.assertIn("multitimeframe_decision_evidence.py", message)
        self.assertIn("relative_rank_1_among_15m_1H_4H_not_calibrated", message)
        self.assertIn("confidence_claim_allowed=false", message)
        self.assertIn("【交易阶段终止契约】", message)
        self.assertIn("禁止再读取 trades_writer.py 源码", message)
        self.assertIn("trade_cycles 成功终态存在前", message)
        self.assertIn("禁止无内容 stop", message)
        self.assertIn("零成交，HOLD 也必须先落库", message)
        self.assertIn("2026-08-11T18:30", message)
        self.assertIn("briefing marker", message)

    def test_trader_prompts_use_deterministic_live_facts_entrypoint(self):
        trigger = (ROOT / "collectors" / "trigger_agent.py").read_text(
            encoding="utf-8")
        live = (ROOT / "agents" / "live_trader.md").read_text(encoding="utf-8")
        executor = (ROOT / "core" / "order_executor.py").read_text(
            encoding="utf-8")

        for text in (trigger, live):
            self.assertIn("run_okx_python.ps1", text)
            self.assertIn("scripts/live_decision_facts.py", text)
            self.assertIn("--cycle-id", text)
            self.assertIn("--out-file", text)
            self.assertIn("trade_cycles", text)
            self.assertIn("禁止", text)
        self.assertIn("禁止空内容 `stop`", live)
        self.assertIn("禁止再读 `collectors/trades_writer.py` 源码", live)
        self.assertNotIn("OKX API 现仓/余额：okx --profile", trigger)
        self.assertNotIn("`okx --profile live", live)
        self.assertNotIn('fix = f"okx --profile', executor)
        self.assertIn("repair_{profile}_{symbol}_fills.json", executor)

    def test_trigger_message_contains_complete_cycle_scoped_cli_commands(self):
        with mock.patch.object(trigger_agent, "_ro_db",
                               side_effect=OSError("isolated test")), \
                mock.patch.object(trigger_agent, "_briefing_for_traders",
                                  return_value=""):
            message = trigger_agent._trader_preload(
                "2026-07-29T00:15", "live")

        facts = "<PROJECT_ROOT>/tmp/live_facts_2026-07-29T00-15.json"
        self.assertIn(
            "<PROJECT_ROOT>/scripts/live_decision_facts.py --profile live "
            f"--cycle-id 2026-07-29T00:15 --out-file {facts}",
            message,
        )
        self.assertIn(f"read {facts}", message)
        self.assertIn("禁止自行换算", message)
        self.assertIn("--facts-file", message)
        self.assertIn("portfolio_margin_state", message)
        self.assertIn("既有仓位不扣减", message)
        self.assertIn("evidence_contract", message)
        self.assertIn("截断样例数组禁止计数", message)
        self.assertIn("multitimeframe_decision_evidence.py", message)
        self.assertIn("mtf_2026-07-29T00-15_<symbol>.json", message)

    def test_trigger_preload_states_live_imr_capacity_rule(self):
        """原为 live/demo 容量口径隔离用例；2026-08-06 demo 全量下线后只剩 live，
        断言收敛为「预载必须给出 live 的 IMR 口径，且不得出现已删除的 max-size 口径」。"""
        with mock.patch.object(trigger_agent, "_ro_db",
                               side_effect=OSError("isolated test")), \
                mock.patch.object(trigger_agent, "_briefing_for_traders",
                                  return_value=""):
            live_message = trigger_agent._trader_preload(
                "2026-07-29T00:15", "live")

        self.assertIn("account.balance.imr/totalEq", live_message)
        self.assertIn("66.6%", live_message)
        self.assertNotIn("account max-size", live_message)
        self.assertNotIn("Demo", live_message)
        self.assertNotIn("AGENTS.md §2", live_message)

    def test_okx_cli_out_file_preserves_long_json_atomically(self):
        rows = [{"instId": f"TEST-{i}", "detail": "中" * 200}
                for i in range(20)]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(_okxcli, "okx_json", return_value=rows):
            path = Path(tmp) / "positions.json"
            rc = _okxcli.main([
                "--profile", "demo", "--compact",
                "--out-file", str(path),
                "account", "positions", "--instType", "SWAP",
            ])

            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), rows)
            self.assertGreater(len(path.read_bytes()), 2000)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_okx_cli_stdout_is_never_sliced_into_invalid_json(self):
        rows = [{"instId": f"TEST-{i}", "detail": "x" * 300}
                for i in range(20)]
        with mock.patch.object(_okxcli, "okx_json", return_value=rows), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = _okxcli.main([
                "--profile", "demo", "account", "positions",
            ])

        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue()), rows)
        self.assertGreater(len(out.getvalue()), 2000)

    def test_stage_alert_status_does_not_copy_channel_identifiers(self):
        leaked = json.dumps({
            "messageId": "secret-message-id",
            "payload": {"to": "secret-target"},
        })
        proc = SimpleNamespace(returncode=0, stdout=leaked, stderr="")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(stage_runner, "STATUS_DIR", Path(tmp)), \
                mock.patch.object(stage_runner.subprocess, "run",
                                  return_value=proc):
            result = stage_runner._send_failure_alert(
                "demo", "2026-07-29T00:00", 86,
                Path(tmp) / "demo-status.json",
                {"failure_kind": "business_output_missing"},
            )

        serialized = json.dumps(result)
        self.assertEqual(result, {"rc": 0, "delivered": True})
        self.assertNotIn("secret-message-id", serialized)
        self.assertNotIn("secret-target", serialized)

    def test_failed_stage_alert_status_also_omits_raw_output(self):
        proc = SimpleNamespace(
            returncode=1,
            stdout='{"messageId":"secret-message-id"}',
            stderr="failed for secret-target",
        )
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(stage_runner, "STATUS_DIR", Path(tmp)), \
                mock.patch.object(stage_runner.subprocess, "run",
                                  return_value=proc):
            result = stage_runner._send_failure_alert(
                "demo", "2026-07-29T00:15", 86,
                Path(tmp) / "demo-status.json",
            )

        serialized = json.dumps(result)
        self.assertEqual(result["rc"], 1)
        self.assertFalse(result["delivered"])
        self.assertNotIn("secret-message-id", serialized)
        self.assertNotIn("secret-target", serialized)


if __name__ == "__main__":
    unittest.main()
