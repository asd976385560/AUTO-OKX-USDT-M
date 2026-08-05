from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for sub in (ROOT / "collectors", ROOT / "scripts"):
    if str(sub) not in sys.path:
        sys.path.insert(0, str(sub))

from core import dispatcher  # noqa: E402
import ledger  # noqa: E402
import stage_runner  # noqa: E402
import trigger_agent  # noqa: E402
import collection_monitor  # noqa: E402


CST = timezone(timedelta(hours=8))


class RuntimeRootNamespaceTests(unittest.TestCase):
    def test_custom_root_namespaces_session_message_and_status_names(self):
        cycle = "2026-08-04T12:00"
        with tempfile.TemporaryDirectory() as tmp:
            custom = Path(tmp) / "db"
            custom.mkdir()
            default_key = trigger_agent.session_key(
                "live", cycle, trigger_agent._CANONICAL_DB_ROOT
            )
            custom_key = trigger_agent.session_key("live", cycle, custom)
            self.assertEqual(default_key, "live-20260804-1200")
            self.assertNotEqual(custom_key, default_key)
            self.assertEqual(
                custom_key,
                stage_runner._stage_session_key("live", cycle, custom),
            )
            self.assertEqual(
                trigger_agent._stage_status_path("live", cycle, custom).name,
                stage_runner._status_path("live", cycle, custom).name,
            )

    def test_real_agent_launch_rejects_nondefault_root_before_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(trigger_agent, "LOG_DIR", log_dir), \
             mock.patch.object(trigger_agent, "build_cmd") as build, \
             mock.patch.object(trigger_agent.subprocess, "Popen") as popen:
                with self.assertRaisesRegex(RuntimeError, "Gateway tool DB-root"):
                    trigger_agent.fire(
                        "live", "2026-08-04T12:00", "full",
                        db_root=Path(tmp) / "isolated-db",
                    )
            self.assertFalse(log_dir.exists())
        build.assert_not_called()
        popen.assert_not_called()

    def test_dispatcher_blocks_custom_root_agents_before_latches(self):
        cycle = "2026-08-04T12:00"
        now = datetime(2026, 8, 4, 12, 5, tzinfo=CST)
        with tempfile.TemporaryDirectory() as tmp:
            db_root = Path(tmp) / "db"
            ledger_path = db_root / "ledger.db"
            with mock.patch.dict(
                os.environ, {"OKX_TRIGGER_DRYRUN": "0"}, clear=False
            ), mock.patch.object(
                dispatcher,
                "analysis_row",
                return_value={
                    "mode": "full",
                    "status": "ok",
                    "ts": "2026-08-04 12:04:00",
                },
            ), mock.patch.object(
                dispatcher, "trade_written", return_value=False
            ), mock.patch.object(
                dispatcher.ledger, "stage_dispatched"
            ) as dispatched, mock.patch.object(
                dispatcher.ledger, "try_profile_lease"
            ) as lease, mock.patch.object(
                dispatcher.ledger, "try_stage"
            ) as try_stage, mock.patch.object(dispatcher, "log"):
                result = dispatcher.dispatch_cycle(
                    db_root,
                    ledger_path,
                    cycle,
                    now=now,
                )

            dispatched.assert_not_called()
            lease.assert_not_called()
            try_stage.assert_not_called()
            self.assertIn("blocked live/demo", result[0])

    def test_direct_dispatch_custom_push_binds_selected_root_and_ledger(self):
        cycle = "2026-08-04T12:00"
        now = datetime(2026, 8, 4, 12, 5, tzinfo=CST)
        with tempfile.TemporaryDirectory() as tmp:
            db_root = Path(tmp) / "db"
            ledger_path = db_root / "ledger.db"
            con = mock.Mock()
            with mock.patch.dict(
                os.environ, {"OKX_TRIGGER_DRYRUN": "0"}, clear=False
            ), mock.patch.object(
                dispatcher,
                "analysis_row",
                return_value={
                    "mode": "full",
                    "status": "ok",
                    "ts": "2026-08-04 12:04:00",
                },
            ), mock.patch.object(
                dispatcher, "trade_written", return_value=True
            ), mock.patch.object(
                dispatcher.ledger, "stage_dispatched", return_value=False
            ) as dispatched, mock.patch.object(
                dispatcher.ledger, "try_profile_lease"
            ) as lease, mock.patch.object(
                dispatcher.ledger, "try_stage", return_value=True
            ) as try_stage, mock.patch.object(
                dispatcher.ledger, "connect", return_value=con
            ), mock.patch.object(
                trigger_agent, "_fire_push_script", return_value="isolated-push-key"
            ) as push, mock.patch.object(dispatcher, "log"):
                result = dispatcher.dispatch_cycle(
                    db_root, ledger_path, cycle, now=now
                )

            push.assert_called_once_with(cycle, db_root=db_root)
            try_stage.assert_called_once_with(ledger_path, cycle, "push")
            self.assertTrue(dispatched.call_args_list)
            self.assertTrue(all(
                call.args[0] == ledger_path for call in dispatched.call_args_list
            ))
            lease.assert_not_called()
            self.assertTrue(any("fired push" in item for item in result), result)

    def test_custom_root_agent_dry_run_remains_read_only(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(
                 os.environ, {"OKX_TRIGGER_DRYRUN": "1"}, clear=True
             ), \
             mock.patch.object(trigger_agent, "LOG_DIR", Path(tmp) / "logs"), \
             mock.patch.object(trigger_agent, "build_cmd") as build, \
             mock.patch.object(trigger_agent.subprocess, "Popen") as popen:
            key = trigger_agent.fire(
                "live", "2026-08-04T12:00", "full",
                db_root=Path(tmp) / "isolated-db",
            )
        self.assertIn("-r", key)
        build.assert_not_called()
        popen.assert_not_called()

    def test_invalid_analysis_dry_run_never_writes_skip_warn(self):
        result: list[str] = []
        with mock.patch.dict(
            os.environ, {"OKX_TRIGGER_DRYRUN": "1"}, clear=False
        ), mock.patch.object(
            dispatcher, "analysis_row", return_value={"status": "failed", "mode": "full"}
        ), mock.patch.object(
            dispatcher.ledger, "stage_dispatched"
        ) as dispatched, mock.patch.object(
            dispatcher.ledger, "try_stage"
        ) as try_stage, mock.patch.object(dispatcher, "log"):
            result = dispatcher.dispatch_cycle(
                Path("isolated"), Path("isolated") / "ledger.db",
                "2026-08-04T12:00",
            )
        dispatched.assert_not_called()
        try_stage.assert_not_called()
        self.assertIn("no latch written", result[0])

    def test_push_delivery_truth_uses_same_custom_namespace(self):
        now = datetime(2026, 8, 4, 12, 30, tzinfo=CST)
        cycle = "2026-08-04T12:00"
        dispatched_at = "2026-08-04 12:10:00"
        with tempfile.TemporaryDirectory() as tmp:
            db_root = Path(tmp) / "db"
            db_root.mkdir()
            ledger_path = db_root / "ledger.db"
            ledger.init_ledger(ledger_path)
            con = sqlite3.connect(ledger_path)
            try:
                con.execute(
                    "INSERT INTO stage_dispatch(cycle_id,stage,dispatched_at) "
                    "VALUES(?,?,?)",
                    (cycle, "push", dispatched_at),
                )
                con.commit()
            finally:
                con.close()

            namespace = dispatcher._root_namespace(db_root)
            dkey = f"push:{namespace}:{cycle}"
            skey = hashlib.sha256(f"default|{dkey}".encode("utf-8")).hexdigest()
            dedupe = db_root / "qq_push_dedupe.db"
            con = sqlite3.connect(dedupe)
            try:
                con.execute(
                    "CREATE TABLE sent(k TEXT PRIMARY KEY,status TEXT,updated_at TEXT)"
                )
                con.execute(
                    "INSERT INTO sent(k,status,updated_at) VALUES(?,?,?)",
                    (skey, "sent", dispatched_at),
                )
                con.commit()
            finally:
                con.close()

            with mock.patch.object(
                dispatcher, "_delivered_dedupe_keys", return_value=set()
            ) as delivered, mock.patch.object(dispatcher, "log") as log:
                dispatcher.verify_push_delivery(db_root, ledger_path, now=now)

            expected_event = dispatcher.PUSH_EVENT_LOG.with_name(
                f"qq_push_dedupe-{namespace}.jsonl"
            )
            self.assertEqual(delivered.call_args.args[0], expected_event)
            log.assert_not_called()

    def test_collection_alert_passes_selected_db_root(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(collection_monitor, "STATE_DIR", Path(tmp)), \
             mock.patch.object(
                 collection_monitor, "run_script", return_value=(0, "sent")
             ) as run:
            rc, _ = collection_monitor.send_alert("synthetic", Path(tmp) / "db")
        self.assertEqual(rc, 0)
        argv = run.call_args.args[1]
        self.assertEqual(
            argv[argv.index("--db-root") + 1],
            str((Path(tmp) / "db").resolve()),
        )

    @unittest.skipUnless(os.name == "nt", "Windows path casing contract")
    def test_production_root_comparison_is_windows_case_safe(self):
        swapped = str(collection_monitor.PROD_DB_ROOT).swapcase()
        self.assertTrue(collection_monitor._is_production_db_root(swapped))

    def test_manual_journal_hint_pins_db_root_and_dry_run(self):
        now = datetime(2026, 8, 4, 12, 30, tzinfo=CST)
        with tempfile.TemporaryDirectory() as tmp:
            db_root = Path(tmp) / "isolated-db"
            journal = db_root / "journal" / "exec_live.jsonl"
            journal.parent.mkdir(parents=True)
            journal.write_text("{}\n", encoding="utf-8")
            plan = {
                "plan": {
                    "2026-08-04T12:00": [{
                        "ts": "2026-08-04 12:00:00",
                        "symbol": "TEST-USDT-SWAP",
                        "action": "open",
                        "sz": "1",
                        "ordId": "ORD-ISOLATED",
                    }]
                },
                "identity_conflicts": [],
                "bad_lines": 0,
            }
            with mock.patch.object(
                collection_monitor,
                "run_script",
                return_value=(0, json.dumps(plan)),
            ):
                signals = collection_monitor.detect_journal_unaccounted(
                    str(db_root), now, dry_run=True
                )

            live = next(s for s in signals if s["key"] == "journal_unaccounted:live")
            self.assertIn(f"--db-root {db_root.resolve()}", live["detail"])
            self.assertIn("--replay-dry-run", live["detail"])
            self.assertIn("--ordid <ORD_ID>", live["detail"])
            self.assertIn("逐笔", live["detail"])

    def test_custom_audit_attribution_never_reads_canonical_sessions(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            collection_monitor, "STAGE_STATUS_DIR", Path(tmp) / "status"
        ), mock.patch.object(
            collection_monitor.sqlite3, "connect"
        ) as connect:
            result = collection_monitor._audit_attribution(
                "live", "2026-08-04T12:00", Path(tmp) / "isolated-db"
            )

        self.assertEqual(result, "no-run")
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
