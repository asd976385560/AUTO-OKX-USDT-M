from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit_periodic_report_completeness as audit  # noqa: E402


class PeriodicReportCompletenessAuditTests(unittest.TestCase):
    def _inputs(self, temporary: str) -> dict:
        root = Path(temporary)
        weekly = root / "weekly"
        monthly = root / "monthly"
        weekly.mkdir()
        monthly.mkdir()
        for day in ("2026-08-03", "2026-08-10"):
            (weekly / f"weekly-{day}.md").write_text(
                f"weekly {day}", encoding="utf-8")
        event_log = root / "events.jsonl"
        events = []
        for day, kind in (
            ("2026-08-03", "weekly"),
            ("2026-08-10", "weekly"),
            ("2026-08-01", "monthly"),
        ):
            path = (
                weekly / f"weekly-{day}.md"
                if kind == "weekly" else monthly / f"monthly-{day}.md")
            content_hash = (
                hashlib.sha256(path.read_bytes()).hexdigest()
                if path.is_file()
                else hashlib.sha256(b"raw json").hexdigest())
            key = f"reviewer:{day}:{kind}"
            events.extend([
                {
                    "event": "claim", "dedupe_key": key,
                    "target": "default", "content_hash": content_hash,
                },
                {
                    "event": "mark", "status": "sent", "exit_code": 0,
                    "dedupe_key": key, "target": "default",
                },
            ])
        event_log.write_text(
            "".join(json.dumps(item) + "\n" for item in events),
            encoding="utf-8",
        )
        dbs = {}
        for name in ("account", "live", "ledger", "lessons"):
            dbs[name] = root / f"{name}.db"
            dbs[name].touch()
        return {
            "weekly_dir": weekly,
            "monthly_dir": monthly,
            "event_log": event_log,
            "account_db": dbs["account"],
            "live_trades_db": dbs["live"],
            "ledger_db": dbs["ledger"],
            "lessons_db": dbs["lessons"],
        }

    @staticmethod
    def _validator(**kwargs) -> dict:
        day = kwargs["report_path"].stem.rsplit("-", 3)[-3:]
        key_day = "-".join(day)
        return {
            "ok": True,
            "report_key": f"{key_day} 00:00:00",
            "errors": [],
            "checks": ["authoritative_facts"],
        }

    def test_missing_monthly_artifact_stays_in_denominator(self):
        with tempfile.TemporaryDirectory() as temporary:
            payload = audit.audit_periodic_reports(
                as_of=datetime(2026, 8, 13, tzinfo=timezone.utc),
                weekly_start=date(2026, 8, 3),
                monthly_start=date(2026, 8, 1),
                forward_weekly_start=date(2026, 8, 17),
                forward_monthly_start=date(2026, 9, 1),
                forward_weekly_minimum=12,
                forward_monthly_minimum=6,
                validator=self._validator,
                **self._inputs(temporary),
            )
        weekly = payload["historical"]["weekly"]
        monthly = payload["historical"]["monthly"]
        self.assertEqual(weekly["status"], "PASSED")
        self.assertEqual(weekly["valid"], 2)
        self.assertEqual(monthly["expected"], 1)
        self.assertEqual(monthly["valid"], 0)
        self.assertEqual(monthly["delivery_confirmed"], 0)
        self.assertEqual(monthly["delivered_report_complete"], 0)
        self.assertTrue(monthly["rows"][0]["identity_marked_sent"])
        self.assertEqual(monthly["status"], "NOT_MET")
        self.assertEqual(payload["historical"]["status"], "NOT_MET")
        self.assertEqual(
            payload["overall_status"], "PENDING_FORWARD_EVIDENCE")

    def test_corrupt_delivery_log_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            inputs = self._inputs(temporary)
            with inputs["event_log"].open("a", encoding="utf-8") as handle:
                handle.write("{bad\n")
            payload = audit.audit_periodic_reports(
                as_of=datetime(2026, 8, 13, tzinfo=timezone.utc),
                weekly_start=date(2026, 8, 3),
                monthly_start=date(2026, 8, 1),
                forward_weekly_start=date(2026, 8, 17),
                forward_monthly_start=date(2026, 9, 1),
                forward_weekly_minimum=12,
                forward_monthly_minimum=6,
                validator=self._validator,
                **inputs,
            )
        self.assertEqual(
            payload["delivery_evidence"]["integrity_status"], "NOT_MET")
        self.assertEqual(payload["historical"]["weekly"]["status"], "NOT_MET")

    def test_boundary_contract_rejects_non_monday_or_non_first(self):
        as_of = datetime(2026, 8, 13, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ValueError, "Monday"):
            audit._boundaries("weekly", date(2026, 8, 4), as_of)
        with self.assertRaisesRegex(ValueError, "month day 1"):
            audit._boundaries("monthly", date(2026, 8, 2), as_of)


if __name__ == "__main__":
    unittest.main()
