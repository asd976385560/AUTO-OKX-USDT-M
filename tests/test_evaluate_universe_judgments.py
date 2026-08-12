from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import evaluate_universe_judgments as evaluator  # noqa: E402


class EvaluateUniverseJudgmentsTests(unittest.TestCase):
    def test_entry_is_after_generation_and_all_horizons_are_labeled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshots = root / "snapshots"
            snapshots.mkdir()
            generated = "2026-08-11T00:02:00Z"
            artifact = {
                "artifact_type": "full_universe_shadow_judgment",
                "cycle_id": "2026-08-11T08:00",
                "generated_at_utc": generated,
                "records": [
                    {
                        "symbol": "LONG-USDT-SWAP",
                        "judgment": "long_bias",
                        "uncalibrated_alignment_score": 0.75,
                        "execution_readiness": "shadow_candidate",
                    },
                    {
                        "symbol": "SHORT-USDT-SWAP",
                        "judgment": "short_bias",
                        "uncalibrated_alignment_score": -0.75,
                        "execution_readiness": "shadow_candidate",
                    },
                ],
            }
            (snapshots / "one.json").write_text(
                json.dumps(artifact), encoding="utf-8"
            )
            db = root / "market.db"
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE tick_snapshots(ts TEXT,symbol TEXT,last REAL)"
            )
            rows = []
            for symbol, entry, future in (
                ("LONG-USDT-SWAP", 100.0, 101.0),
                ("SHORT-USDT-SWAP", 100.0, 99.0),
            ):
                # Pre-generation price must not be used as entry.
                rows.append(("2026-08-11T00:00:00Z", symbol, 50.0))
                rows.extend([
                    ("2026-08-11T00:05:00Z", symbol, entry),
                    ("2026-08-11T00:20:00Z", symbol, future),
                    ("2026-08-11T01:05:00Z", symbol, future),
                    ("2026-08-11T04:05:00Z", symbol, future),
                ])
            con.executemany("INSERT INTO tick_snapshots VALUES(?,?,?)", rows)
            con.commit()
            con.close()

            payload, labels = evaluator.evaluate(
                snapshots,
                db,
                as_of_utc=datetime(2026, 8, 11, 5, tzinfo=timezone.utc),
                cost_bps=20,
                min_sample=100,
            )
            self.assertEqual(len(labels), 6)
            self.assertTrue(all(row["entry_tick_ts_utc"] == "2026-08-11T00:05:00Z" for row in labels))
            self.assertTrue(all(row["after_cost_hit"] for row in labels))
            self.assertEqual(payload["credibility_gate"]["status"], "NOT_MEASURABLE")
            self.assertFalse(payload["credibility_gate"]["production_threshold_change_allowed"])
            daily = payload["daily_throughput"]["latest_day"]
            self.assertEqual(daily["date"], "2026-08-11")
            self.assertEqual(daily["snapshots"], 1)
            self.assertEqual(daily["judgment_records"], 2)
            self.assertEqual(daily["unique_symbols"], 2)
            self.assertFalse(daily["daily_target_met"])
            self.assertTrue(payload["daily_throughput"]["real_fills_are_not_a_throughput_target"])
            for item in payload["horizons"]:
                self.assertEqual(item["after_cost_precision_pct"], 100.0)
                self.assertFalse(item["minimum_sample_met"])

    def test_main_writes_json_and_csv_without_business_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "snapshots").mkdir()
            db = root / "market.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE tick_snapshots(ts TEXT,symbol TEXT,last REAL)")
            con.commit()
            con.close()
            json_out = root / "out" / "evaluation.json"
            csv_out = root / "out" / "labels.csv"
            rc = evaluator.main([
                "--snapshot-root", str(root / "snapshots"),
                "--market-db", str(db),
                "--json-out", str(json_out),
                "--labels-out", str(csv_out),
                "--as-of", "2026-08-11T05:00:00Z",
            ])
            self.assertEqual(rc, 0)
            result = json.loads(json_out.read_text(encoding="utf-8"))
            self.assertEqual(result["snapshots_loaded"], 0)
            self.assertFalse(result["production_mutation"])
            self.assertTrue(csv_out.exists())


if __name__ == "__main__":
    unittest.main()
