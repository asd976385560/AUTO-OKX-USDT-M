import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit_source_health  # noqa: E402


class SourceHealthAuditTests(unittest.TestCase):
    def _ledger(self, root: Path, rows: list[tuple]) -> Path:
        path = root / "ledger.db"
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE collection_runs("
            "cycle_id TEXT,source TEXT,status TEXT,ts TEXT,rows INTEGER,"
            "latency_ms INTEGER,err TEXT,PRIMARY KEY(cycle_id,source))"
        )
        connection.executemany(
            "INSERT INTO collection_runs VALUES(?,?,?,?,?,?,?)", rows)
        connection.commit()
        connection.close()
        return path

    def test_missing_scheduled_slot_is_in_denominator(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ledger = self._ledger(root, [
                ("2026-08-12T01:15", "fast", "ok", "2026-08-12 01:16:00", 1, 1, None),
                ("2026-08-12T01:30", "fast", "degraded", "2026-08-12 01:31:00", 1, 1, "partial"),
                ("2026-08-12T01:45", "fast", "error", "2026-08-12 01:46:00", 0, 1, "ssl eof"),
            ])
            result = audit_source_health.audit_source_health(
                ledger_db=ledger,
                as_of=audit_source_health._parse_cst("2026-08-12T02:07:00+08:00"),
                forward_start=audit_source_health._parse_cst("2026-08-12T01:15:00+08:00"),
                rolling_days=1,
                target_rate=0.99,
                forward_minimum_slots=4,
                grace_minutes=5,
            )
        forward = result["forward_after_remediation"]
        self.assertEqual(4, forward["expected_slots"])
        self.assertEqual(3, forward["observed_rows"])
        self.assertEqual(1, forward["missing_slots"])
        self.assertEqual(1, forward["complete_slots"])
        self.assertEqual(0.25, forward["complete_rate"])
        self.assertEqual(2, forward["available_slots"])
        self.assertEqual(0.5, forward["available_rate"])
        self.assertEqual("NOT_MET", forward["status"])
        self.assertEqual(1, forward["failure_kind_counts"]["missing_collection_run"])

    def test_exactly_99_percent_passes_after_minimum_evidence(self):
        start = audit_source_health._parse_cst("2026-08-10T00:00:00+08:00")
        end = start + timedelta(minutes=15 * 100)
        expected = audit_source_health._expected_cycles(start, end)
        records = {
            cycle: {"status": "ok", "ts": cycle, "err": None}
            for cycle in expected
        }
        records[expected[-1]] = {
            "status": "error", "ts": expected[-1], "err": "timeout"
        }
        summary = audit_source_health._summarize_window(
            start=start,
            end_exclusive=end,
            records=records,
            target_rate=0.99,
            minimum_slots=100,
        )
        self.assertEqual(0.99, summary["complete_rate"])
        self.assertEqual(0.99, summary["available_rate"])
        self.assertEqual("PASSED", summary["status"])

    def test_forward_gate_does_not_pass_before_minimum_slots(self):
        start = audit_source_health._parse_cst("2026-08-12T00:00:00+08:00")
        end = start + timedelta(minutes=15 * 4)
        records = {
            cycle: {"status": "ok", "ts": cycle, "err": None}
            for cycle in audit_source_health._expected_cycles(start, end)
        }
        summary = audit_source_health._summarize_window(
            start=start,
            end_exclusive=end,
            records=records,
            target_rate=0.99,
            minimum_slots=96,
        )
        self.assertEqual(1.0, summary["complete_rate"])
        self.assertEqual("INSUFFICIENT_EVIDENCE", summary["status"])

    def test_unaligned_forward_start_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "15-minute"):
            audit_source_health._ensure_slot_aligned(
                datetime(2026, 8, 12, 1, 46, tzinfo=audit_source_health.CST),
                "forward_start",
            )


if __name__ == "__main__":
    unittest.main()
