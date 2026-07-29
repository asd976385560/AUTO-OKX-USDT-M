# -*- coding: utf-8 -*-
"""Regression tests for bounded, UTF-8 agent evidence output."""
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
import sys
import tempfile
import unittest
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


class SimilarExperienceOutputTests(unittest.TestCase):
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
                "outcome": "win",
                "lesson": "x" * 300,
                "raw_snippet": "must be omitted",
            }],
            "matched_losses": [],
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
            len(result["missed_opportunities"][0]["notes"]), 240)

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
    def test_trader_prompts_use_runnable_okx_cli_entrypoint(self):
        trigger = (ROOT / "collectors" / "trigger_agent.py").read_text(
            encoding="utf-8")
        live = (ROOT / "agents" / "live_trader.md").read_text(encoding="utf-8")
        demo = (ROOT / "agents" / "demo_trader.md").read_text(encoding="utf-8")
        executor = (ROOT / "core" / "order_executor.py").read_text(
            encoding="utf-8")

        for text in (trigger, live, demo):
            self.assertIn("run_okx_python.ps1", text)
            self.assertIn("--compact", text)
            self.assertIn("--out-file", text)
        for text in (live, demo):
            self.assertIn("scripts/_okxcli.py", text)
        self.assertIn('"_okxcli.py"', trigger)
        self.assertIn("_project_path(", trigger)
        self.assertNotIn("OKX API 现仓/余额：okx --profile", trigger)
        self.assertNotIn("`okx --profile live", live)
        self.assertNotIn("`okx --profile demo", demo)
        self.assertNotIn('fix = f"okx --profile', executor)
        self.assertIn("repair_{profile}_{symbol}_fills.json", executor)

    def test_trigger_message_contains_complete_cycle_scoped_cli_commands(self):
        with mock.patch.object(trigger_agent, "_ro_db",
                               side_effect=OSError("isolated test")), \
                mock.patch.object(trigger_agent, "_briefing_for_traders",
                                  return_value=""):
            message = trigger_agent._trader_preload(
                "2026-07-29T00:15", "demo")

        positions = (
            _project_path('tmp', 'okx_demo_2026-07-29T00-15_positions.json'))
        balance = _project_path('tmp', 'okx_demo_2026-07-29T00-15_balance.json')
        self.assertIn(
            f"--profile demo --compact --out-file {positions} "
            "account positions --instType SWAP",
            message,
        )
        self.assertIn(
            f"--profile demo --compact --out-file {balance} account balance",
            message,
        )
        self.assertIn(f"read {positions}", message)
        self.assertIn(f"read {balance}", message)
        self.assertNotIn("<PROJECT_ROOT>", message)
        self.assertIn(
            _project_path("scripts", "run_okx_python.ps1"), message)
        self.assertIn(_project_path("scripts", "_okxcli.py"), message)

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
