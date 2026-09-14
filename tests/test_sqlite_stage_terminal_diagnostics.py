# -*- coding: utf-8 -*-
import json
import sqlite3
import tempfile
import unittest
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from scripts import stage_runner

CYCLE = "2026-09-07T20:45"
KEY = "agent:okx-live-trader:live-20260907-2045"


class SqliteStageTerminalDiagnosticsTests(unittest.TestCase):
    def inspect(self, messages, *, key=KEY, version=19):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "agents/okx-live-trader/agent/openclaw-agent.sqlite"
            path.parent.mkdir(parents=True)
            with closing(sqlite3.connect(path)) as con, con:
                con.executescript("""
                    CREATE TABLE session_nodes(session_key TEXT,current_session_id TEXT,entry_valid INTEGER);
                    CREATE TABLE session_windows(session_id TEXT,session_key TEXT);
                    CREATE TABLE transcript_events(session_id TEXT,seq INTEGER,event_json TEXT);
                """)
                con.execute(f"PRAGMA user_version={version}")
                con.execute("INSERT INTO session_nodes VALUES(?,?,1)", (key, "session-1"))
                con.execute("INSERT INTO session_windows VALUES(?,?)", ("session-1", key))
                for seq, message in enumerate(messages):
                    con.execute("INSERT INTO transcript_events VALUES(?,?,?)", ("session-1", seq, json.dumps({"message": message})))
            return stage_runner.detect_agent_terminal_failure("live", CYCLE, root)

    def idle(self):
        return {"role": "assistant", "stopReason": "aborted", "errorMessage": "LLM idle timeout (300s): no response from model",
                "content": [{"type": "thinking", "thinking": "must never be included in diagnostics"}]}

    def test_idle_timeout_survives_framework_final_publication(self):
        result = self.inspect([self.idle(), {"role": "assistant", "provider": "openclaw", "model": "gateway-publication",
                               "stopReason": "stop", "content": [{"type": "text", "text": "next invoke validate-only"}]}])
        self.assertIsNotNone(result)
        self.assertEqual("agent_idle_timeout", result["failure_kind"])
        self.assertEqual("sqlite", result["source_format"])
        self.assertNotIn("thinking", json.dumps(result))

    def test_later_real_assistant_recovery_does_not_inherit_old_timeout(self):
        self.assertIsNone(self.inspect([self.idle(), {"role": "assistant", "stopReason": "toolUse",
                            "content": [{"type": "toolCall", "name": "exec", "arguments": {}}]},
                            {"role": "assistant", "stopReason": "stop",
                            "content": [{"type": "text", "text": "work completed"}]}]))

    def test_text_only_unfinished_summary_does_not_erase_idle_timeout(self):
        result = self.inspect([self.idle(), {"role": "assistant", "stopReason": "stop",
                              "content": [{"type": "text", "text": "next invoke validate-only"}]}])
        self.assertEqual("agent_idle_timeout", result["failure_kind"])
        self.assertTrue(result["later_text_only_terminal_observed"])

    def test_wrong_session_and_unsupported_schema_do_not_supply_evidence(self):
        self.assertIsNone(self.inspect([self.idle()], key=KEY + "-wrong"))
        self.assertIsNone(self.inspect([self.idle()], version=99))

    def test_length_and_empty_output_have_minimal_diagnostics(self):
        result = self.inspect([{"role": "assistant", "stopReason": "length", "content": [], "usage": {"totalTokens": 123}}])
        self.assertEqual("model_output_length", result["failure_kind"])
        result = self.inspect([{"role": "assistant", "stopReason": "stop", "content": [], "usage": {"output": 0}}])
        self.assertEqual("model_empty_output", result["failure_kind"])

    def test_other_abort_is_not_falsely_classified_as_idle_timeout(self):
        self.assertIsNone(self.inspect([{"role": "assistant", "stopReason": "aborted",
                                        "errorMessage": "cancelled by user", "content": []}]))

    def first_event_timeout(self, *, stop="aborted"):
        return {"role": "assistant", "stopReason": stop, "content": [],
                "errorMessage": "responses HTTP stream opened but did not deliver a first SSE event "
                                "within 300000ms after streaming headers (first-event timeout). "
                                "provider=private-provider model=private-model"}

    def test_first_event_timeout_survives_textual_fake_tool_finalization(self):
        for stop in ("aborted", "error"):
            with self.subTest(stop=stop):
                result = self.inspect([self.first_event_timeout(stop=stop),
                    {"role": "assistant", "stopReason": "stop", "content": [
                        {"type": "text", "text": '<tool_call>write(path="analysis.json")</tool_call>'}]}])
                self.assertEqual("agent_first_event_timeout", result["failure_kind"])
                self.assertEqual("first_event", result["timeout_phase"])
                self.assertEqual(300, result["timeout_seconds"])
                self.assertTrue(result["later_text_only_terminal_observed"])
                self.assertNotIn("private-", json.dumps(result))
                self.assertNotIn("errorMessage", result)

    def test_first_event_timeout_is_cleared_by_real_tool_continuation(self):
        self.assertIsNone(self.inspect([self.first_event_timeout(),
            {"role": "assistant", "stopReason": "toolUse", "content": [
                {"type": "toolCall", "name": "exec", "arguments": {}}]},
            {"role": "assistant", "stopReason": "stop", "content": [
                {"type": "text", "text": "completed"}]}]))

    def test_quoted_timeout_and_cancel_are_not_provider_timeout_evidence(self):
        message = self.first_event_timeout(stop="stop")
        self.assertIsNone(self.inspect([message]))
        message["stopReason"] = "aborted"
        message["errorMessage"] = "user cancelled after discussing first-event timeout"
        self.assertIsNone(self.inspect([message]))

    def test_first_event_timeout_obeys_current_session_schema_boundary(self):
        self.assertIsNone(self.inspect([self.first_event_timeout()], key=KEY + "-wrong"))
        self.assertIsNone(self.inspect([self.first_event_timeout()], version=99))

    def test_framework_publication_cannot_clear_first_event_timeout(self):
        result = self.inspect([self.first_event_timeout(), {
            "role": "assistant", "provider": "openclaw", "model": "gateway-publication",
            "stopReason": "stop", "content": [{"type": "text", "text": "completed"}]}])
        self.assertEqual("agent_first_event_timeout", result["failure_kind"])

    def test_latest_unrecovered_timeout_is_reported(self):
        result = self.inspect([self.idle(), self.first_event_timeout()])
        self.assertEqual("agent_first_event_timeout", result["failure_kind"])
        result = self.inspect([self.first_event_timeout(), self.idle()])
        self.assertEqual("agent_idle_timeout", result["failure_kind"])


if __name__ == "__main__":
    unittest.main()
