from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit_news_source_health as audit  # noqa: E402
import audit_source_health as scheduled  # noqa: E402


class NewsSourceHealthAuditTests(unittest.TestCase):
    def test_overall_status_uses_all_enabled_sources(self) -> None:
        self.assertEqual("PASSED", audit._overall_status("PASSED"))
        self.assertEqual(
            "PENDING_FORWARD_EVIDENCE",
            audit._overall_status("INSUFFICIENT_EVIDENCE"),
        )
        self.assertEqual("NOT_MET", audit._overall_status("NOT_MET"))

    def test_due_cycles_honor_hourly_schedule(self) -> None:
        start = datetime(2026, 8, 12, 0, 0, tzinfo=scheduled.CST)
        end = datetime(2026, 8, 12, 2, 0, tzinfo=scheduled.CST)

        cycles = audit._due_cycles(start, end, 60)

        self.assertEqual(cycles, ["2026-08-12T00:00", "2026-08-12T01:00"])

    def test_strict_completeness_rejects_degraded_and_missing(self) -> None:
        spec = audit.NewsSourceSpec(
            source="rss_en", interval_minutes=15, role="required",
            endpoint="test", historical_eligible=True)
        start = datetime(2026, 8, 12, 0, 0, tzinfo=scheduled.CST)
        end = datetime(2026, 8, 12, 1, 0, tzinfo=scheduled.CST)
        records = {
            "2026-08-12T00:00": {"status": "ok"},
            "2026-08-12T00:15": {"status": "degraded", "err": "partial"},
            "2026-08-12T00:30": {"status": "ok"},
        }

        result = audit._summarize_source(
            spec, start=start, end_exclusive=end, records=records,
            target_rate=0.99, minimum_slots=4)

        self.assertEqual(result["expected_slots"], 4)
        self.assertEqual(result["complete_slots"], 2)
        self.assertEqual(result["missing_slots"], 1)
        self.assertEqual(result["strict_complete_rate"], 0.5)
        self.assertEqual(result["available_rate"], 0.75)
        self.assertEqual(result["status"], "NOT_MET")

    def test_new_source_forward_start_narrows_denominator(self) -> None:
        """2026-08-13 新接入源：诞生前槽不加责，但年轻窗只 INSUFFICIENT 不 PASSED。"""
        import tempfile
        import sqlite3
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "registry.json"
            registry.write_text(_json.dumps({"sources": [
                {"id": "rss_en", "type": "news", "endpoint": "e",
                 "native_cadence": "15m", "required": True, "enabled": True,
                 "adapter": "news_rss"},
                {"id": "okx_news", "type": "news", "endpoint": "e",
                 "native_cadence": "15m", "required": False, "enabled": True,
                 "adapter": "news_okx"},
                {"id": "okx_announcements", "type": "news", "endpoint": "e",
                 "native_cadence": "15m", "required": False, "enabled": True,
                 "adapter": "news_okx_announcements",
                 "poll_interval_min": 30,
                 "audit_forward_start_cst": "2026-08-14T00:00:00+08:00"},
            ]}), encoding="utf-8")
            ledger = Path(tmp) / "ledger.db"
            con = sqlite3.connect(ledger)
            con.execute(
                "CREATE TABLE collection_runs (cycle_id TEXT, source TEXT, "
                "status TEXT, ts TEXT, rows INTEGER, latency_ms INTEGER, "
                "err TEXT, PRIMARY KEY (cycle_id, source))")
            # 新源诞生后 4 个 30min 槽全 ok（00:00/00:30/01:00/01:30）
            for hh, mm in ((0, 0), (0, 30), (1, 0), (1, 30)):
                con.execute(
                    "INSERT INTO collection_runs VALUES (?,?,?,?,0,0,NULL)",
                    (f"2026-08-14T{hh:02d}:{mm:02d}", "okx_announcements",
                     "ok", f"2026-08-14 {hh:02d}:{mm:02d}:30"))
            con.commit()
            con.close()

            pre_activation_report = audit.audit_news_source_health(
                ledger_db=ledger,
                registry_path=registry,
                as_of=datetime(
                    2026, 8, 13, 10, 55, tzinfo=scheduled.CST),
                forward_start=datetime(
                    2026, 8, 12, 16, 15, tzinfo=scheduled.CST),
            )
            report = audit.audit_news_source_health(
                ledger_db=ledger,
                registry_path=registry,
                as_of=datetime(2026, 8, 14, 2, 0, tzinfo=scheduled.CST),
                forward_start=datetime(
                    2026, 8, 12, 16, 15, tzinfo=scheduled.CST),
            )

        pre_rows = {
            row["source"]: row
            for row in pre_activation_report["forward_after_remediation"][
                "sources"]
        }
        pre_ann = pre_rows["okx_announcements"]
        self.assertEqual(pre_ann["expected_slots"], 0)
        self.assertEqual(pre_ann["complete_slots"], 0)
        self.assertEqual(pre_ann["missing_slots"], 0)
        self.assertEqual(pre_ann["status"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(
            pre_ann["start_cst"], "2026-08-14T00:00:00+08:00")
        self.assertEqual(
            pre_ann["end_exclusive_cst"],
            "2026-08-14T00:00:00+08:00",
        )

        rows = {row["source"]: row
                for row in report["forward_after_remediation"]["sources"]}
        ann = rows["okx_announcements"]
        # 分母只从本源预注册起点起算（4 槽），诞生前 8-12/8-13 槽零加责
        self.assertEqual(ann["expected_slots"], 4)
        self.assertEqual(ann["complete_slots"], 4)
        self.assertEqual(ann["missing_slots"], 0)
        # 但未攒满 24h 最小窗，只能 INSUFFICIENT_EVIDENCE，不得 PASSED
        self.assertEqual(ann["status"], "INSUFFICIENT_EVIDENCE")
        # 既有源分母仍从全局前向起点起算，不受新源影响
        self.assertEqual(
            rows["rss_en"]["start_cst"], "2026-08-12T16:15:00+08:00")

    def test_short_forward_window_is_insufficient_not_passed(self) -> None:
        spec = audit.NewsSourceSpec(
            source="rss:cryptoslate", interval_minutes=15,
            role="required_subsource", endpoint="test",
            historical_eligible=False)
        start = datetime(2026, 8, 12, 0, 0, tzinfo=scheduled.CST)
        end = datetime(2026, 8, 12, 0, 30, tzinfo=scheduled.CST)
        records = {
            "2026-08-12T00:00": {"status": "ok"},
            "2026-08-12T00:15": {"status": "ok"},
        }

        result = audit._summarize_source(
            spec, start=start, end_exclusive=end, records=records,
            target_rate=0.99, minimum_slots=96)

        self.assertEqual(result["strict_complete_rate"], 1.0)
        self.assertEqual(result["status"], "INSUFFICIENT_EVIDENCE")


if __name__ == "__main__":
    unittest.main()
