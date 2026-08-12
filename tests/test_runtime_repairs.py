# -*- coding: utf-8 -*-
"""2026-07-26 运行修复最小回归。

只覆盖本轮修复的确定性行为；不连接交易所、不写生产数据库、不触发 Agent。
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "collectors", ROOT / "scripts", ROOT / "collectors" / "sources"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import _proc  # noqa: E402
import fast_collect  # noqa: E402
import build_push_payload  # noqa: E402
import collect_slow  # noqa: E402
import collect_market_features  # noqa: E402
import daily_maintenance  # noqa: E402
import news_collect  # noqa: E402
import public_macro  # noqa: E402
import query_state  # noqa: E402
import reconcile_exchange_closes  # noqa: E402
import render_push_report  # noqa: E402
import slow_collect  # noqa: E402
import source_freshness  # noqa: E402
import validate_push_format  # noqa: E402
import _okx_http  # noqa: E402
from core import order_executor  # noqa: E402


class SlowCollectRegressionTests(unittest.TestCase):
    def test_collect_slow_connections_support_named_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "regime.db"
            seed = sqlite3.connect(db_path)
            seed.execute("CREATE TABLE sample(observation_date TEXT, value REAL)")
            seed.execute("INSERT INTO sample VALUES('2026-07-28', 1.0)")
            seed.commit()
            seed.close()

            connection = collect_slow.open_db(Path(tmp), "regime.db")
            try:
                row = connection.execute(
                    "SELECT observation_date,value FROM sample"
                ).fetchone()
                self.assertEqual(row["observation_date"], "2026-07-28")
                self.assertEqual(row["value"], 1.0)
            finally:
                connection.close()

    def test_retry_cycle_stays_on_hour_slot(self):
        now = datetime(2026, 7, 26, 23, 36, 58, tzinfo=slow_collect.CST)
        self.assertEqual(slow_collect._hour_cycle_id(now), "2026-07-26T23:00")

    def test_slow_kline_partial_batch_is_degraded_and_bounded(self):
        class FakeConnection:
            def __init__(self):
                self.rows = []
                self.committed = False

            def executemany(self, _sql, rows):
                self.rows.extend(rows)

            def commit(self):
                self.committed = True

        candle = [
            "1785196800000", "100", "101", "99", "100.5",
            "0", "0", "1234",
        ]
        connection = FakeConnection()
        with (
            mock.patch.object(collect_slow, "SLOW_TIMEFRAMES", {"1H": "1H"}),
            mock.patch.object(
                collect_slow,
                "fetch_candles_batch_sync",
                return_value={
                    "AAA-USDT-SWAP": [candle],
                    "BBB-USDT-SWAP": [],
                },
            ) as fetch,
        ):
            count, incomplete = collect_slow.collect_slow_klines(
                connection,
                ["AAA-USDT-SWAP", "BBB-USDT-SWAP"],
            )
        self.assertEqual(count, 1)
        self.assertEqual(incomplete, ["1H:1/2"])
        self.assertTrue(connection.committed)
        self.assertLessEqual(
            fetch.call_args.kwargs["batch_timeout_s"],
            collect_slow.SLOW_KLINE_TF_TIMEOUT_S,
        )

    def test_okx_batch_deadline_stops_before_request(self):
        client = mock.Mock()
        with self.assertRaises(TimeoutError):
            _okx_http._get_data(
                client,
                "/api/v5/market/candles",
                {"instId": "BTC-USDT-SWAP"},
                deadline=time.monotonic() - 1,
            )
        client.get.assert_not_called()

    def test_okx_request_without_batch_deadline_keeps_client_timeout(self):
        client = mock.Mock()
        response = client.get.return_value
        response.json.return_value = {"code": "0", "data": []}
        self.assertEqual(
            _okx_http._get_data(
                client,
                "/api/v5/market/candles",
                {"instId": "BTC-USDT-SWAP"},
                retries=0,
            ),
            [],
        )
        self.assertNotIn("timeout", client.get.call_args.kwargs)

    def test_okx_default_domains_use_recommended_primary_and_legacy_fallback(self):
        self.assertEqual(
            (
                "https://openapi.okx.com",
                "https://www.okx.com",
            ),
            _okx_http._resolve_base_urls(None, None),
        )

    def test_okx_explicit_regional_domain_has_no_implicit_global_fallback(self):
        self.assertEqual(
            ("https://us.okx.com",),
            _okx_http._resolve_base_urls("https://us.okx.com/", None),
        )

    def test_okx_network_retry_rotates_domains_inside_existing_budget(self):
        client = mock.Mock()
        response = mock.Mock()
        response.json.return_value = {"code": "0", "data": [{"instId": "BTC-USDT-SWAP"}]}
        client.get.side_effect = [RuntimeError("ssl eof"), response]
        domains = ("https://openapi.okx.com", "https://www.okx.com")
        with (
            mock.patch.object(_okx_http, "_BASE_URLS", domains),
            mock.patch.object(_okx_http.time, "sleep"),
        ):
            result = _okx_http._get_data(
                client,
                "/api/v5/market/tickers",
                {"instType": "SWAP"},
                retries=1,
            )
        self.assertEqual([{"instId": "BTC-USDT-SWAP"}], result)
        self.assertEqual(2, client.get.call_count)
        self.assertEqual(
            "https://openapi.okx.com/api/v5/market/tickers",
            client.get.call_args_list[0].args[0],
        )
        self.assertEqual(
            "https://www.okx.com/api/v5/market/tickers",
            client.get.call_args_list[1].args[0],
        )

    @unittest.skipUnless(os.name == "nt", "Windows process-tree behavior")
    def test_timeout_returns_before_outer_cron_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "sleepy.py"
            script.write_text(
                "import time\n"
                "print('[WARN] partial timeout evidence', flush=True)\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            started = time.monotonic()
            result = slow_collect.run_step("sleepy", script, [], timeout=1)
            elapsed = time.monotonic() - started
        self.assertEqual(result["rc"], 124)
        self.assertIn("process tree terminated", result["stderr_tail"])
        self.assertIn("partial timeout evidence", result["warn_tail"][0])
        self.assertLess(elapsed, 8)


class ProcGuardTests(unittest.TestCase):
    """`_proc.run_guarded` 的共用契约（2026-08-05 抽出）。

    这些用例存在的理由：同一个「超时只杀直接子进程」缺陷 2026-07-28 在
    slow_collect 修过一次却没回移到 fast_collect，又漏了 8 次、丢了两轮数据。
    共用实现 + 共用用例，避免再出现「改了一处漏了另一处」。
    """

    def _wrapper_shape(self, tmp: str, body: str) -> list[str]:
        """还原生产真实形状：pwsh -File <ps1> -> python <script>（有孙进程）。"""
        script = Path(tmp) / "child.py"
        script.write_text(body, encoding="utf-8")
        ps1 = Path(tmp) / "wrap.ps1"
        ps1.write_text(f"& '{sys.executable}' '{script}'\nexit $LASTEXITCODE\n",
                       encoding="utf-8")
        return ["pwsh", "-NoProfile", "-File", str(ps1)]

    @unittest.skipUnless(os.name == "nt", "Windows process-tree behavior")
    def test_timeout_kills_grandchild_and_returns_promptly(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = self._wrapper_shape(tmp, "import time\ntime.sleep(40)\n")
            started = time.monotonic()
            rc, _out, err, timed_out = _proc.run_guarded(cmd, timeout=3)
            elapsed = time.monotonic() - started
        self.assertTrue(timed_out)
        self.assertEqual(rc, _proc.RC_TIMEOUT)
        self.assertIn("process tree terminated", err)
        self.assertLess(elapsed, 12,
                        "超时未生效——孙进程仍持有管道拖住 communicate()")

    @unittest.skipUnless(os.name == "nt", "Windows process-tree behavior")
    def test_normal_completion_passes_through_rc_and_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = self._wrapper_shape(
                tmp, "import sys\nprint('hello')\nsys.exit(3)\n")
            rc, out, _err, timed_out = _proc.run_guarded(cmd, timeout=60)
        self.assertFalse(timed_out)
        self.assertEqual(rc, 3)
        self.assertIn("hello", out)

    def test_stdin_is_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "echo.py"
            script.write_text("import sys\nsys.stdout.write(sys.stdin.read())\n",
                              encoding="utf-8")
            rc, out, _err, _to = _proc.run_guarded(
                [sys.executable, str(script)], timeout=60, input_text="ping")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "ping")

    def test_wrapper_callers_no_longer_use_bare_subprocess_timeout(self):
        """契约：经 wrapper 起子进程的调用方一律走 run_guarded。

        直接 `subprocess.run(..., timeout=)` 在这些文件里会静默退化回旧缺陷。
        """
        for rel in ("scripts/push_pipeline.py", "scripts/collection_monitor.py",
                    "scripts/reconcile_daily.py"):
            src = (ROOT / rel).read_text(encoding="utf-8")
            with self.subTest(file=rel):
                self.assertIn("_proc.run_guarded", src)
                self.assertNotIn("subprocess.run(", src)


class FastCollectRegressionTests(unittest.TestCase):
    def test_budget_reservation_keeps_required_steps_ahead_of_enrichment(self):
        calls = []

        def fake_run(name, script, sargs, timeout):
            calls.append((name, timeout))
            return {
                "name": name, "ok": True, "rc": 0, "dur_s": 0.0,
                "payload": {}, "stderr_tail": "",
            }

        with tempfile.TemporaryDirectory() as tmp:
            argv = [
                "fast_collect.py", "--db-root", str(Path(tmp) / "db"),
                "--cycle", "2026-08-12T14:15",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(fast_collect, "run_step", side_effect=fake_run),
                mock.patch.object(fast_collect.ledger, "init_ledger"),
                mock.patch.object(fast_collect.ledger, "record_collection"),
                mock.patch.object(fast_collect, "_nudge_mod", None),
                mock.patch.object(sys, "stdout", io.StringIO()),
            ):
                self.assertEqual(0, fast_collect.main())

        self.assertEqual(
            [
                "collect_data", "live_account_check",
                "contract_statistics", "market_features",
            ],
            [name for name, _timeout in calls],
        )
        self.assertIn(calls[0][1], (149, 150))
        self.assertEqual([25, 75, 60], [timeout for _, timeout in calls[1:]])

    def test_budget_helper_never_borrows_reserved_seconds(self):
        self.assertEqual(
            150,
            fast_collect._bounded_step_timeout(
                deadline=320.9,
                requested=150,
                reserve_after=105,
                now=0.0,
            ),
        )
        self.assertEqual(
            12,
            fast_collect._bounded_step_timeout(
                deadline=20.9,
                requested=40,
                reserve_after=8,
                now=0.0,
            ),
        )
        self.assertIsNone(
            fast_collect._bounded_step_timeout(
                deadline=19.9,
                requested=40,
                reserve_after=8,
                now=0.0,
            )
        )

    def test_full_universe_shadow_runs_three_fixed_slots_only(self):
        self.assertTrue(fast_collect.full_universe_shadow_due("2026-08-11T00:00"))
        self.assertTrue(fast_collect.full_universe_shadow_due("2026-08-11T08:00"))
        self.assertTrue(fast_collect.full_universe_shadow_due("2026-08-11T16:00"))
        self.assertFalse(fast_collect.full_universe_shadow_due("2026-08-11T08:15"))
        self.assertFalse(fast_collect.full_universe_shadow_due("bad-cycle"))

    def test_frozen_model_shadow_runs_each_natural_hour_only(self):
        self.assertTrue(
            fast_collect.frozen_model_shadow_due("2026-08-11T00:00")
        )
        self.assertTrue(
            fast_collect.frozen_model_shadow_due("2026-08-11T09:00")
        )
        self.assertTrue(
            fast_collect.frozen_model_shadow_due("2026-08-11T23:00")
        )
        self.assertFalse(
            fast_collect.frozen_model_shadow_due("2026-08-11T09:15")
        )
        self.assertFalse(fast_collect.frozen_model_shadow_due("bad-cycle"))

    def test_frozen_model_evaluation_runs_on_hourly_half_hour_only(self):
        self.assertTrue(
            fast_collect.frozen_model_shadow_evaluation_due(
                "2026-08-11T08:30")
        )
        self.assertTrue(
            fast_collect.frozen_model_shadow_evaluation_due(
                "2026-08-11T23:30")
        )
        self.assertFalse(
            fast_collect.frozen_model_shadow_evaluation_due(
                "2026-08-11T08:00")
        )
        self.assertFalse(
            fast_collect.frozen_model_shadow_evaluation_due(
                "2026-08-11T08:15")
        )
        self.assertFalse(fast_collect.frozen_model_shadow_evaluation_due("bad"))

    def test_full_universe_shadow_path_isolated_from_production_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_root = Path(tmp) / "db"
            path = fast_collect.full_universe_shadow_path(
                db_root, "2026-08-11T08:00"
            )
            self.assertTrue(str(path).startswith(str(Path(tmp))))
            self.assertIn("2026-08-11", str(path))

    def test_frozen_model_shadow_path_isolated_from_production_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_root = Path(tmp) / "db"
            path = fast_collect.frozen_model_shadow_path(
                db_root, "2026-08-11T08:00"
            )
            self.assertTrue(str(path).startswith(str(Path(tmp))))
            self.assertIn("model-shadow", str(path))
            self.assertIn("forward", str(path))

    def test_frozen_model_evaluation_paths_are_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_root = Path(tmp) / "db"
            shadow_root, receipt, labels = (
                fast_collect.frozen_model_shadow_evaluation_paths(db_root)
            )
            quality = Path(tmp) / "reports" / "quality"
            self.assertEqual(quality / "model-shadow" / "forward", shadow_root)
            self.assertEqual(quality / "model-shadow-evaluation.json", receipt)
            self.assertEqual(quality / "model-shadow-labels.csv", labels)
            self.assertEqual(
                quality / "model-shadow-label-quality-audit.json",
                fast_collect.frozen_model_shadow_quality_path(db_root),
            )

    def test_multitimeframe_coverage_path_isolated_from_production_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_root = Path(tmp) / "db"
            path = fast_collect.multitimeframe_coverage_path(db_root)
            self.assertEqual(
                Path(tmp) / "reports" / "quality" /
                "multitimeframe-coverage-audit.json",
                path,
            )

    def test_fast_due_cycle_runs_multitimeframe_audit_as_diagnostic(self):
        source = (ROOT / "collectors" / "fast_collect.py").read_text(
            encoding="utf-8")
        self.assertIn('"multitimeframe_coverage_audit"', source)
        self.assertIn("audit_multitimeframe_coverage.py", source)
        self.assertIn('coverage_step["diagnostic_only"] = True', source)

    def test_fast_due_cycle_settles_frozen_model_labels_as_diagnostic(self):
        source = (ROOT / "collectors" / "fast_collect.py").read_text(
            encoding="utf-8")
        self.assertIn('"frozen_model_shadow_evaluation"', source)
        self.assertIn("evaluate_multitimeframe_model_shadow.py", source)
        self.assertIn(
            'model_evaluation_step["diagnostic_only"] = True', source
        )
        self.assertIn('"frozen_model_shadow_label_quality"', source)
        self.assertIn("audit_model_shadow_label_quality.py", source)
        self.assertIn('model_quality_step["diagnostic_only"] = True', source)

    def test_half_hour_evaluation_and_quality_are_diagnostic_without_shadow_scoring(self):
        calls = []

        def fake_run(name, script, sargs, timeout):
            calls.append((name, script, sargs, timeout))
            return {
                "name": name,
                "ok": True,
                "rc": 0,
                "dur_s": 0.0,
                "payload": {},
                "stderr_tail": "",
            }

        with tempfile.TemporaryDirectory() as tmp:
            db_root = Path(tmp) / "db"
            stdout = io.StringIO()
            argv = [
                "fast_collect.py",
                "--db-root", str(db_root),
                "--cycle", "2026-08-12T08:30",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(fast_collect, "run_step", side_effect=fake_run),
                mock.patch.object(fast_collect.ledger, "init_ledger"),
                mock.patch.object(
                    fast_collect.ledger, "record_collection"
                ) as record,
                mock.patch.object(fast_collect, "_nudge_mod", None),
                mock.patch.object(sys, "stdout", stdout),
            ):
                self.assertEqual(0, fast_collect.main())

            names = [item[0] for item in calls]
            self.assertNotIn("frozen_model_shadow", names)
            self.assertNotIn("universe_judgment_shadow", names)
            self.assertLess(
                names.index("frozen_model_shadow_evaluation"),
                names.index("frozen_model_shadow_label_quality"),
            )
            evaluation_args = next(
                item[2]
                for item in calls
                if item[0] == "frozen_model_shadow_evaluation"
            )
            arg_map = dict(zip(evaluation_args[::2], evaluation_args[1::2]))
            quality = Path(tmp) / "reports" / "quality"
            self.assertEqual(
                quality / "model-shadow" / "forward",
                Path(arg_map["--shadow-root"]),
            )
            self.assertEqual(
                quality / "model-shadow-evaluation.json",
                Path(arg_map["--json-out"]),
            )
            self.assertEqual(
                quality / "model-shadow-labels.csv",
                Path(arg_map["--labels-out"]),
            )
            self.assertEqual("ok", record.call_args.args[3])
            output = json.loads(stdout.getvalue().strip().splitlines()[-1])
            self.assertEqual("ok", output["status"])
            self.assertFalse(any(
                "frozen_model_shadow" in warning
                for warning in output["warnings"]
            ))

    def test_failed_half_hour_evaluation_never_audits_stale_outputs(self):
        calls = []

        def fake_run(name, script, sargs, timeout):
            calls.append(name)
            ok = name != "frozen_model_shadow_evaluation"
            return {
                "name": name, "ok": ok, "rc": 0 if ok else 1,
                "dur_s": 0.0, "payload": {},
                "stderr_tail": "" if ok else "simulated evaluation failure",
            }

        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            argv = [
                "fast_collect.py", "--db-root", str(Path(tmp) / "db"),
                "--cycle", "2026-08-12T08:30",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(fast_collect, "run_step", side_effect=fake_run),
                mock.patch.object(fast_collect.ledger, "init_ledger"),
                mock.patch.object(
                    fast_collect.ledger, "record_collection"
                ) as record,
                mock.patch.object(fast_collect, "_nudge_mod", None),
                mock.patch.object(sys, "stdout", stdout),
            ):
                self.assertEqual(0, fast_collect.main())
        self.assertIn("frozen_model_shadow_evaluation", calls)
        self.assertNotIn("frozen_model_shadow_label_quality", calls)
        self.assertEqual("ok", record.call_args.args[3])
        output = json.loads(stdout.getvalue().strip().splitlines()[-1])
        self.assertTrue(any(
            "frozen_model_shadow_evaluation: simulated evaluation failure"
            in warning for warning in output["warnings"]
        ))
        self.assertTrue(any(
            "frozen_model_shadow_label_quality: prerequisite" in warning
            for warning in output["warnings"]
        ))

    def test_full_shadow_slot_scores_model_without_premature_evaluation(self):
        calls = []

        def fake_run(name, script, sargs, timeout):
            calls.append((name, script, sargs, timeout))
            return {
                "name": name, "ok": True, "rc": 0, "dur_s": 0.0,
                "payload": {}, "stderr_tail": "",
            }

        with tempfile.TemporaryDirectory() as tmp:
            argv = [
                "fast_collect.py", "--db-root", str(Path(tmp) / "db"),
                "--cycle", "2026-08-12T08:00",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(fast_collect, "run_step", side_effect=fake_run),
                mock.patch.object(fast_collect.ledger, "init_ledger"),
                mock.patch.object(fast_collect.ledger, "record_collection"),
                mock.patch.object(fast_collect, "_nudge_mod", None),
                mock.patch.object(sys, "stdout", io.StringIO()),
            ):
                self.assertEqual(0, fast_collect.main())
        names = [item[0] for item in calls]
        self.assertIn("universe_judgment_shadow", names)
        self.assertIn("frozen_model_shadow", names)
        self.assertNotIn("frozen_model_shadow_evaluation", names)
        self.assertNotIn("frozen_model_shadow_label_quality", names)

    def test_non_anchor_hour_scores_model_without_full_universe_snapshot(self):
        calls = []

        def fake_run(name, script, sargs, timeout):
            calls.append((name, script, sargs, timeout))
            return {
                "name": name, "ok": True, "rc": 0, "dur_s": 0.0,
                "payload": {}, "stderr_tail": "",
            }

        with tempfile.TemporaryDirectory() as tmp:
            argv = [
                "fast_collect.py", "--db-root", str(Path(tmp) / "db"),
                "--cycle", "2026-08-12T09:00",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(fast_collect, "run_step", side_effect=fake_run),
                mock.patch.object(fast_collect.ledger, "init_ledger"),
                mock.patch.object(fast_collect.ledger, "record_collection"),
                mock.patch.object(fast_collect, "_nudge_mod", None),
                mock.patch.object(sys, "stdout", io.StringIO()),
            ):
                self.assertEqual(0, fast_collect.main())
        names = [item[0] for item in calls]
        self.assertIn("frozen_model_shadow", names)
        self.assertNotIn("universe_judgment_shadow", names)
        self.assertNotIn("multitimeframe_coverage_audit", names)
        self.assertNotIn("frozen_model_shadow_evaluation", names)

    @unittest.skipUnless(os.name == "nt", "Windows process-tree behavior")
    def test_timeout_returns_before_outer_cron_budget(self):
        """2026-08-05：步级 timeout 必须真的生效。

        原实现经 pwsh wrapper 起 collect_data，TimeoutExpired 只 TerminateProcess
        杀掉 pwsh，python 孙进程存活并持有 stdout 管道 → 二次 communicate() 阻塞到
        孙进程自然退出。后果是 --total-budget 形同虚设，快采反复撞满 cron 480s
        并整轮丢数据（7/16-8/5 共 22 次）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "sleepy.py"
            script.write_text(
                "import time\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            started = time.monotonic()
            result = fast_collect.run_step("sleepy", script, [], timeout=1)
            elapsed = time.monotonic() - started
        self.assertEqual(result["rc"], 124)
        self.assertFalse(result["ok"])
        self.assertIn("process tree terminated", result["stderr_tail"])
        self.assertLess(elapsed, 8, "超时未生效——孙进程仍在拖住 communicate()")

    @unittest.skipUnless(os.name == "nt", "Windows process-tree behavior")
    def test_timeout_keeps_partial_stdout_json_for_attribution(self):
        """超时也要保住已刷出的末行 JSON，否则底层根因只剩 rc=124。"""
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "noisy.py"
            script.write_text(
                "import time\n"
                'print(\'{"error": "upstream stalled"}\', flush=True)\n'
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            result = fast_collect.run_step("noisy", script, [], timeout=2)
        self.assertEqual(result["rc"], 124)
        self.assertEqual(result["payload"], {"error": "upstream stalled"})
        self.assertEqual(
            fast_collect._step_error(result), "noisy: upstream stalled"
        )

    def test_steps_no_longer_spawn_through_pwsh_wrapper(self):
        """契约：内层直起 Python。回退到 pwsh 会让上面两条超时保证再次失效。"""
        src = (ROOT / "collectors" / "fast_collect.py").read_text(encoding="utf-8")
        self.assertIn("cmd = [sys.executable, str(script), *sargs]", src)
        # 只钉 argv 形状——注释里提到 wrapper 是合法的（本脚本正是由它启动）。
        self.assertNotIn('"pwsh", "-NoProfile"', src)

    def test_payload_error_is_preserved(self):
        step = {
            "name": "collect_data",
            "ok": False,
            "rc": 1,
            "payload": {"error": "TimeoutError: upstream unavailable"},
            "stderr_tail": "",
        }
        self.assertEqual(
            fast_collect._step_error(step),
            "collect_data: TimeoutError: upstream unavailable",
        )

    def test_stderr_fallback_is_preserved(self):
        step = {
            "name": "market_features",
            "ok": False,
            "rc": 2,
            "payload": None,
            "stderr_tail": "schema mismatch",
        }
        self.assertEqual(
            fast_collect._step_error(step),
            "market_features: schema mismatch",
        )

    def test_positioning_runs_only_on_hour_slot_by_default(self):
        self.assertTrue(
            collect_market_features.positioning_due("2026-07-27T20:00", "auto")
        )
        self.assertFalse(
            collect_market_features.positioning_due("2026-07-27T20:15", "auto")
        )
        self.assertTrue(
            collect_market_features.positioning_due("2026-07-27T20:15", "always")
        )

    def test_contract_statistics_runs_each_standard_quarter_hour(self):
        for minute in ("00", "15", "30", "45"):
            self.assertTrue(collect_market_features.contract_statistics_due(
                f"2026-08-12T04:{minute}", "auto"))
        self.assertFalse(collect_market_features.contract_statistics_due(
            "2026-08-12T04:07", "auto"))
        self.assertFalse(collect_market_features.contract_statistics_due(
            "2026-08-12T04:15", "off"))

    def test_contract_statistics_requires_same_closed_source_bucket(self):
        row = collect_market_features.contract_statistics_row(
            [["1786477500000", "100", "10", "250000"]],
            [["1786477500000", "2000", "3000"]],
            "2026-08-12T04:15",
            "2026-08-11T20:16:00Z",
            "BTC-USDT-SWAP",
        )
        self.assertEqual(row[0], "2026-08-11T19:45:00Z")
        self.assertEqual(row[3], "BTC-USDT-SWAP")
        self.assertEqual(row[4], "15m")
        self.assertEqual(row[5:10], (100.0, 10.0, 250000.0, 2000.0, 3000.0))
        self.assertAlmostEqual(row[10], 0.6)
        self.assertEqual(row[12], "okx_rest_contract_oi_taker_15m")
        aligned = collect_market_features.contract_statistics_row(
            [
                ["1786477500000", "100", "10", "250000"],
                ["1786476600000", "90", "9", "225000"],
            ],
            [["1786476600000", "1000", "3000"]],
            "2026-08-12T04:15",
            "2026-08-11T20:16:00Z",
            "BRKB-USDT-SWAP",
        )
        self.assertEqual(aligned[0], "2026-08-11T19:30:00Z")
        self.assertEqual(aligned[5], 90.0)
        self.assertAlmostEqual(aligned[10], 0.75)
        with self.assertRaisesRegex(ValueError, "timestamp mismatch"):
            collect_market_features.contract_statistics_row(
                [["1786477500000", "100", "10", "250000"]],
                [["1786476600000", "2000", "3000"]],
                "2026-08-12T04:15",
                "2026-08-11T20:16:00Z",
                "BTC-USDT-SWAP",
            )

    def test_contract_statistics_fallback_requires_three_source_reconciliation(self):
        cycle = "2026-08-12T04:15"
        start_ms, end_ms = (
            collect_market_features.contract_statistics_bucket_window_ms(cycle)
        )
        oi = {
            "instId": "DKNG-USDT-SWAP",
            "ts": str(end_ms + 30_000),
            "oi": "100",
            "oiCcy": "10",
            "oiUsd": "250000",
        }
        trades = [
            {"tradeId": "1", "ts": str(start_ms + 60_000),
             "side": "buy", "sz": "2", "px": "10"},
            {"tradeId": "2", "ts": str(start_ms + 120_000),
             "side": "sell", "sz": "3", "px": "12"},
        ]
        candle = [[
            str(start_ms), "10", "12", "9", "11", "5",
            "0", "0", "1",
        ]]
        row = collect_market_features.contract_statistics_fallback_row(
            oi,
            trades,
            {"ok": True, "error_type": None},
            candle,
            cycle,
            collect_market_features.ms_to_iso(end_ms + 60_000),
            "DKNG-USDT-SWAP",
            0.1,
        )
        self.assertEqual(
            collect_market_features.ms_to_iso(start_ms), row[0])
        self.assertAlmostEqual(3.6, row[8])
        self.assertAlmostEqual(2.0, row[9])
        self.assertAlmostEqual(2.0 / 5.6, row[10])
        self.assertEqual(
            "official_public_oi_trades_candle_reconciled_fallback",
            json.loads(row[11])["method"],
        )
        bad_candle = [list(candle[0])]
        bad_candle[0][5] = "6"
        with self.assertRaisesRegex(ValueError, "do not reconcile"):
            collect_market_features.contract_statistics_fallback_row(
                oi,
                trades,
                {"ok": True},
                bad_candle,
                cycle,
                collect_market_features.ms_to_iso(end_ms + 60_000),
                "DKNG-USDT-SWAP",
                0.1,
            )

    def test_contract_statistics_replaces_stale_primary_with_verified_fallback(self):
        symbol = "DKNG-USDT-SWAP"
        cycle = "2026-08-12T04:15"
        start_ms, end_ms = (
            collect_market_features.contract_statistics_bucket_window_ms(cycle)
        )
        stale_ms = start_ms - 2 * 60 * 60 * 1000
        oi_row = [str(stale_ms), "100", "10", "250000"]
        taker_row = [str(stale_ms), "2000", "3000"]
        public_oi = {symbol: {
            "instId": symbol,
            "ts": str(end_ms + 30_000),
            "oi": "101",
            "oiCcy": "10.1",
            "oiUsd": "252500",
        }}
        trade = {
            "tradeId": "gap-1", "ts": str(start_ms + 60_000),
            "side": "buy", "sz": "2", "px": "10",
        }
        candle = {symbol: [[
            str(start_ms), "10", "10", "10", "10", "2",
            "0", "0", "1",
        ]]}

        def recent_trades(_symbols, _limit, _timeout, *, outcomes):
            outcomes[symbol] = {"ok": True, "error_type": None}
            return {symbol: [trade]}

        with (
            mock.patch.object(
                collect_market_features,
                "fetch_contract_open_interest_history_batch_sync",
                return_value={symbol: [oi_row]},
            ),
            mock.patch.object(
                collect_market_features,
                "fetch_contract_taker_volumes_batch_sync",
                return_value={symbol: [taker_row]},
            ),
            mock.patch.object(
                collect_market_features,
                "fetch_open_interest_all_sync",
                return_value=public_oi,
            ),
            mock.patch.object(
                collect_market_features,
                "fetch_recent_trades_batch_sync",
                side_effect=recent_trades,
            ),
            mock.patch.object(
                collect_market_features,
                "fetch_candles_batch_sync",
                return_value=candle,
            ),
        ):
            rows, errors = collect_market_features.fetch_contract_statistics_rows(
                [symbol],
                cycle,
                collect_market_features.ms_to_iso(end_ms + 60_000),
                {symbol: 0.1},
            )
        self.assertEqual([], errors)
        self.assertEqual(1, len(rows))
        self.assertEqual(collect_market_features.ms_to_iso(start_ms), rows[0][0])
        self.assertEqual(101.0, rows[0][5])
        self.assertEqual(
            "official_public_oi_trades_candle_reconciled_fallback",
            json.loads(rows[0][11])["method"],
        )

    def test_okx_cli_top_long_short_payload_is_parsed(self):
        payload = [{
            "data": [{
                "instId": "BTC-USDT-SWAP",
                "timeframes": {
                    "1H": {
                        "indicators": {
                            "TOPLONGSHORT": [{
                                "ts": "1785153600000",
                                "values": {
                                    "longRatio": "0.54",
                                    "shortRatio": "0.46",
                                    "longShortRatio": "1.18",
                                },
                            }]
                        }
                    }
                },
            }]
        }]
        row = collect_market_features.positioning_row(
            payload, "2026-07-27T20:00", "2026-07-27T12:10:00Z",
            "BTC-USDT-SWAP",
        )
        self.assertEqual(row[3], "BTC-USDT-SWAP")
        self.assertEqual(row[4], "1H")
        self.assertAlmostEqual(row[5], 0.54)
        self.assertAlmostEqual(row[7], 1.18)

    def test_okx_rest_contract_ratio_derives_account_shares(self):
        row = collect_market_features.rest_positioning_row(
            [["1786471200000", "1.5"]],
            "2026-08-12T02:00",
            "2026-08-11T18:42:00Z",
            "BTC-USDT-SWAP",
        )
        self.assertEqual(row[0], "2026-08-11T18:00:00Z")
        self.assertEqual(row[3], "BTC-USDT-SWAP")
        self.assertAlmostEqual(row[5], 0.6)
        self.assertAlmostEqual(row[6], 0.4)
        self.assertAlmostEqual(row[7], 1.5)
        self.assertEqual(row[9], "okx_rest_contract_long_short_ratio")

    def test_contract_ratio_http_uses_official_path_and_symbol_rate_key(self):
        expected = {"BTC-USDT-SWAP": [["1786471200000", "1.5"]]}
        with mock.patch.object(_okx_http, "_batch", return_value=expected) as batch:
            actual = _okx_http.fetch_contract_long_short_ratios_batch_sync(
                ["BTC-USDT-SWAP"], period="1H", limit=1,
                batch_timeout_s=30,
            )
        self.assertEqual(actual, expected)
        path_fn, params_fn = batch.call_args.args[1:3]
        self.assertEqual(
            path_fn("BTC-USDT-SWAP"),
            "/api/v5/rubik/stat/contracts/long-short-account-ratio-contract",
        )
        self.assertEqual(params_fn("BTC-USDT-SWAP"), {
            "instId": "BTC-USDT-SWAP", "period": "1H", "limit": "1",
        })
        self.assertEqual(
            batch.call_args.kwargs["throttle_key_fn"]("BTC-USDT-SWAP"),
            "BTC-USDT-SWAP",
        )
        self.assertEqual(batch.call_args.kwargs["batch_timeout_s"], 30)

    def test_contract_statistics_http_contracts_use_symbol_rate_keys(self):
        expected = {"BTC-USDT-SWAP": [["1786477500000", "1", "2", "3"]]}
        with mock.patch.object(_okx_http, "_batch", return_value=expected) as batch:
            actual = _okx_http.fetch_contract_open_interest_history_batch_sync(
                ["BTC-USDT-SWAP"], period="15m", limit=1,
                batch_timeout_s=30,
            )
        self.assertEqual(actual, expected)
        path_fn, params_fn = batch.call_args.args[1:3]
        self.assertEqual(
            path_fn("BTC-USDT-SWAP"),
            "/api/v5/rubik/stat/contracts/open-interest-history",
        )
        self.assertEqual(params_fn("BTC-USDT-SWAP"), {
            "instId": "BTC-USDT-SWAP", "period": "15m", "limit": "1",
        })
        self.assertEqual(
            batch.call_args.kwargs["throttle_key_fn"]("BTC-USDT-SWAP"),
            "BTC-USDT-SWAP",
        )
        self.assertEqual(batch.call_args.kwargs["request_retries"], 2)
        self.assertIsNone(batch.call_args.kwargs["outcomes"])
        self.assertEqual(
            batch.call_args.kwargs["workers"],
            max(1, min(64, _okx_http._CONTRACT_STATS_WORKERS)),
        )

        expected = {"BTC-USDT-SWAP": [["1786477500000", "2", "3"]]}
        with mock.patch.object(_okx_http, "_batch", return_value=expected) as batch:
            actual = _okx_http.fetch_contract_taker_volumes_batch_sync(
                ["BTC-USDT-SWAP"], period="15m", unit="2", limit=1,
                batch_timeout_s=30,
            )
        self.assertEqual(actual, expected)
        path_fn, params_fn = batch.call_args.args[1:3]
        self.assertEqual(
            path_fn("BTC-USDT-SWAP"),
            "/api/v5/rubik/stat/taker-volume-contract",
        )
        self.assertEqual(params_fn("BTC-USDT-SWAP"), {
            "instId": "BTC-USDT-SWAP", "period": "15m", "unit": "2",
            "limit": "1",
        })
        self.assertEqual(
            batch.call_args.kwargs["throttle_key_fn"]("BTC-USDT-SWAP"),
            "BTC-USDT-SWAP",
        )
        self.assertEqual(batch.call_args.kwargs["request_retries"], 2)
        self.assertIsNone(batch.call_args.kwargs["outcomes"])
        self.assertEqual(
            batch.call_args.kwargs["workers"],
            max(1, min(64, _okx_http._CONTRACT_STATS_WORKERS)),
        )

    def test_okx_batch_outcome_keeps_bounded_error_detail(self):
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.get.side_effect = RuntimeError("ssl eof detail")
        outcomes = {}
        with (
            mock.patch.object(_okx_http, "_client", return_value=client),
            mock.patch.object(_okx_http, "_throttle"),
        ):
            result = _okx_http._batch(
                ["BTC-USDT-SWAP"],
                lambda _symbol: "/test",
                lambda symbol: {"instId": symbol},
                lambda data: data,
                request_retries=0,
                outcomes=outcomes,
            )
        self.assertEqual(result, {"BTC-USDT-SWAP": []})
        self.assertEqual(
            outcomes["BTC-USDT-SWAP"]["error_type"], "RuntimeError")
        self.assertIn("ssl eof detail", outcomes["BTC-USDT-SWAP"]["error"])

    def test_contract_statistics_retries_only_failed_symbols_once(self):
        symbol = "BTC-USDT-SWAP"
        oi_row = ["1786477500000", "100", "10", "250000"]
        taker_row = ["1786477500000", "2000", "3000"]
        with (
            mock.patch.object(
                collect_market_features,
                "fetch_contract_open_interest_history_batch_sync",
                side_effect=[{symbol: []}, {symbol: [oi_row]}],
            ) as oi_fetch,
            mock.patch.object(
                collect_market_features,
                "fetch_contract_taker_volumes_batch_sync",
                side_effect=[{symbol: [taker_row]}, {symbol: [taker_row]}],
            ) as taker_fetch,
        ):
            rows, errors = collect_market_features.fetch_contract_statistics_rows(
                [symbol],
                "2026-08-12T04:15",
                "2026-08-11T20:16:00Z",
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(errors, [])
        self.assertEqual(oi_fetch.call_count, 2)
        self.assertEqual(taker_fetch.call_count, 2)
        self.assertEqual(oi_fetch.call_args_list[1].args[0], [symbol])
        self.assertEqual(taker_fetch.call_args_list[1].args[0], [symbol])
        self.assertEqual(
            oi_fetch.call_args_list[0].kwargs["request_retries"], 0)
        self.assertEqual(
            taker_fetch.call_args_list[0].kwargs["request_retries"], 0)
        self.assertEqual(
            oi_fetch.call_args_list[1].kwargs["request_retries"], 1)
        self.assertEqual(
            taker_fetch.call_args_list[1].kwargs["request_retries"], 1)

    def test_contract_statistics_systemic_retry_all_then_fallback_uncovered(self):
        symbols = [f"S{i:03d}-USDT-SWAP" for i in range(100)]
        uncovered = symbols[-1]
        carryable = set(symbols[:-1])
        cycle = "2026-08-12T11:30"
        start_ms, end_ms = (
            collect_market_features.contract_statistics_bucket_window_ms(cycle)
        )
        collected = collect_market_features.ms_to_iso(end_ms + 60_000)
        public_oi = {uncovered: {
            "instId": uncovered,
            "ts": str(end_ms + 30_000),
            "oi": "101",
            "oiCcy": "10.1",
            "oiUsd": "252500",
        }}
        trade = {
            "tradeId": "systemic-1", "ts": str(start_ms + 60_000),
            "side": "buy", "sz": "2", "px": "10",
        }
        candle = {uncovered: [[
            str(start_ms), "10", "10", "10", "10", "2",
            "0", "0", "1",
        ]]}

        def recent_trades(selected, _limit, _timeout, *, outcomes):
            self.assertEqual(selected, [uncovered])
            outcomes[uncovered] = {"ok": True, "error_type": None}
            return {uncovered: [trade]}

        with (
            mock.patch.object(
                collect_market_features,
                "fetch_contract_open_interest_history_batch_sync",
                return_value={symbol: [] for symbol in symbols},
            ) as oi_fetch,
            mock.patch.object(
                collect_market_features,
                "fetch_contract_taker_volumes_batch_sync",
                return_value={symbol: [] for symbol in symbols},
            ) as taker_fetch,
            mock.patch.object(
                collect_market_features,
                "fetch_open_interest_all_sync",
                return_value=public_oi,
            ),
            mock.patch.object(
                collect_market_features,
                "fetch_recent_trades_batch_sync",
                side_effect=recent_trades,
            ) as trades_fetch,
            mock.patch.object(
                collect_market_features,
                "fetch_candles_batch_sync",
                return_value=candle,
            ) as candles_fetch,
        ):
            rows, errors = collect_market_features.fetch_contract_statistics_rows(
                symbols,
                cycle,
                collected,
                {symbol: 0.1 for symbol in symbols},
                carryable_symbols=carryable,
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], uncovered)
        self.assertEqual(oi_fetch.call_args_list[1].args[0], symbols)
        self.assertEqual(taker_fetch.call_args_list[1].args[0], symbols)
        self.assertEqual(trades_fetch.call_args.args[0], [uncovered])
        self.assertEqual(candles_fetch.call_args.args[0], [uncovered])
        self.assertTrue(any(
            "contract_statistics_systemic_primary_failure:failed=100/100:"
            "strict_fallback_prioritized=1:fresh_direct_origins=99" in error
            for error in errors
        ))

    def test_contract_statistics_cycle_requires_exact_quarter_boundary(self):
        with self.assertRaisesRegex(ValueError, "15m boundary"):
            collect_market_features.contract_statistics_bucket_window_ms(
                "2026-08-12T10:35")

    def test_contract_statistics_batch_gate_accepts_99pct_direct_tail(self):
        self.assertTrue(
            collect_market_features.contract_statistics_batch_passed(
                availability_coverage_rate=427 / 429,
                direct_coverage_rate=427 / 429,
                written_rows=427,
                completed_rows=427,
            )
        )
        self.assertFalse(
            collect_market_features.contract_statistics_batch_passed(
                availability_coverage_rate=1.0,
                direct_coverage_rate=424 / 429,
                written_rows=429,
                completed_rows=429,
            )
        )
        self.assertFalse(
            collect_market_features.contract_statistics_batch_passed(
                availability_coverage_rate=1.0,
                direct_coverage_rate=1.0,
                written_rows=428,
                completed_rows=429,
            )
        )

    def test_contract_statistics_bounded_carry_preserves_source_age_and_values(self):
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE market_contract_statistics("
            "ts TEXT,collected_ts TEXT,cycle_id TEXT,symbol TEXT,timeframe TEXT,"
            "oi_contracts REAL,oi_ccy REAL,oi_usd REAL,taker_sell_usd REAL,"
            "taker_buy_usd REAL,taker_buy_ratio REAL,raw TEXT,source TEXT)"
        )
        direct = (
            "2026-08-12T00:00:00Z", "2026-08-12T00:16:00Z",
            "2026-08-12T08:15", "BTC-USDT-SWAP", "15m",
            100.0, 10.0, 1000.0, 40.0, 60.0, 0.6,
            json.dumps({"open_interest_row": [1], "taker_volume_row": [2]}),
            collect_market_features.CONTRACT_STATS_SOURCE,
        )
        con.execute(
            "INSERT INTO market_contract_statistics VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            direct,
        )
        rows, quality, errors = (
            collect_market_features.complete_contract_statistics_with_previous_batch(
                con, [], ["BTC-USDT-SWAP"], "2026-08-12T08:30",
                available_at="2026-08-12T00:31:00Z",
            )
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)
        carried = rows[0]
        raw = json.loads(carried[11])
        self.assertEqual(carried[0], direct[0])
        self.assertEqual(carried[1], "2026-08-12T00:31:00Z")
        self.assertEqual(carried[2], "2026-08-12T08:30")
        self.assertEqual(carried[5:11], direct[5:11])
        self.assertEqual(
            raw["method"],
            collect_market_features.CONTRACT_STATS_CARRY_METHOD,
        )
        self.assertEqual(raw["carried_from_cycle_id"], "2026-08-12T08:15")
        self.assertEqual(raw["origin_cycle_id"], "2026-08-12T08:15")
        self.assertEqual(raw["carry_count"], 1)
        self.assertEqual(raw["source_age_seconds"], 1860.0)
        self.assertTrue(quality["carry_forward_excluded_from_model_features"])
        self.assertEqual(quality["direct_coverage_rate"], 0.0)
        self.assertEqual(quality["availability_coverage_rate"], 1.0)
        con.close()

    def test_contract_statistics_recarry_references_direct_origin_without_resetting_age(self):
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE market_contract_statistics("
            "ts TEXT,collected_ts TEXT,cycle_id TEXT,symbol TEXT,timeframe TEXT,"
            "oi_contracts REAL,oi_ccy REAL,oi_usd REAL,taker_sell_usd REAL,"
            "taker_buy_usd REAL,taker_buy_ratio REAL,raw TEXT,source TEXT)"
        )
        direct = (
            "2026-08-12T00:00:00Z", "2026-08-12T00:16:00Z",
            "2026-08-12T08:15", "BTC-USDT-SWAP", "15m",
            100.0, 10.0, 1000.0, 40.0, 60.0, 0.6, "{}",
            collect_market_features.CONTRACT_STATS_SOURCE,
        )
        con.execute(
            "INSERT INTO market_contract_statistics VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            direct,
        )
        first, _, first_errors = (
            collect_market_features.complete_contract_statistics_with_previous_batch(
                con, [], ["BTC-USDT-SWAP"], "2026-08-12T08:30",
                available_at="2026-08-12T00:31:00Z",
            )
        )
        self.assertEqual(first_errors, [])
        con.execute(
            "INSERT INTO market_contract_statistics VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            first[0],
        )
        second, _, second_errors = (
            collect_market_features.complete_contract_statistics_with_previous_batch(
                con, [], ["BTC-USDT-SWAP"], "2026-08-12T09:00",
                available_at="2026-08-12T01:01:00Z",
            )
        )
        self.assertEqual(second_errors, [])
        second_raw = json.loads(second[0][11])
        self.assertEqual(second[0][0], direct[0])
        self.assertEqual(second_raw["carry_count"], 1)
        self.assertEqual(second_raw["origin_cycle_id"], "2026-08-12T08:15")
        self.assertEqual(second_raw["carried_from_cycle_id"], "2026-08-12T08:15")
        self.assertEqual(second_raw["source_age_seconds"], 3660.0)
        con.execute(
            "INSERT INTO market_contract_statistics VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            second[0],
        )
        expired, _, expired_errors = (
            collect_market_features.complete_contract_statistics_with_previous_batch(
                con, [], ["BTC-USDT-SWAP"], "2026-08-12T10:00",
                available_at="2026-08-12T01:31:00Z",
            )
        )
        self.assertEqual(expired, [])
        self.assertTrue(any(
            "stale_source_time" in error for error in expired_errors
        ))
        con.close()

    def test_contract_statistics_current_carry_input_is_never_counted_as_direct(self):
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE market_contract_statistics("
            "ts TEXT,collected_ts TEXT,cycle_id TEXT,symbol TEXT,timeframe TEXT,"
            "oi_contracts REAL,oi_ccy REAL,oi_usd REAL,taker_sell_usd REAL,"
            "taker_buy_usd REAL,taker_buy_ratio REAL,raw TEXT,source TEXT)"
        )
        carried_input = (
            "2026-08-12T00:00:00Z", "2026-08-12T00:31:00Z",
            "2026-08-12T08:30", "BTC-USDT-SWAP", "15m",
            100.0, 10.0, 1000.0, 40.0, 60.0, 0.6,
            json.dumps({"method": collect_market_features.CONTRACT_STATS_CARRY_METHOD}),
            collect_market_features.CONTRACT_STATS_SOURCE,
        )
        rows, quality, errors = (
            collect_market_features.complete_contract_statistics_with_previous_batch(
                con, [carried_input], ["BTC-USDT-SWAP"],
                "2026-08-12T08:30", available_at="2026-08-12T00:31:00Z",
            )
        )
        self.assertEqual(rows, [])
        self.assertEqual(quality["direct_valid_symbols"], 0)
        self.assertIn(
            "current_batch_carry_input_disallowed",
            quality["invalid_current_symbols"]["BTC-USDT-SWAP"],
        )
        self.assertTrue(any("previous_row_missing" in error for error in errors))
        con.close()

    def test_contract_statistics_invalid_prior_row_never_carries(self):
        con = sqlite3.connect(":memory:")
        con.execute(
            "CREATE TABLE market_contract_statistics("
            "ts TEXT,collected_ts TEXT,cycle_id TEXT,symbol TEXT,timeframe TEXT,"
            "oi_contracts REAL,oi_ccy REAL,oi_usd REAL,taker_sell_usd REAL,"
            "taker_buy_usd REAL,taker_buy_ratio REAL,raw TEXT,source TEXT)"
        )
        con.execute(
            "INSERT INTO market_contract_statistics VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "2026-08-12T00:00:00Z", "2026-08-12T00:16:00Z",
                "2026-08-12T08:15", "BTC-USDT-SWAP", "15m",
                100.0, 10.0, 1000.0, 40.0, 60.0, 0.5, "{}",
                collect_market_features.CONTRACT_STATS_SOURCE,
            ),
        )
        rows, _, errors = (
            collect_market_features.complete_contract_statistics_with_previous_batch(
                con, [], ["BTC-USDT-SWAP"], "2026-08-12T08:30",
                available_at="2026-08-12T00:31:00Z",
            )
        )
        self.assertEqual(rows, [])
        self.assertTrue(any("taker_ratio_algebra" in error for error in errors))
        con.close()


class DailyInspectionRegressionTests(unittest.TestCase):
    def test_macro_ratio_is_rendered_as_percent(self):
        self.assertEqual(build_push_payload._ratio_pct(-0.0007659), -0.08)
        self.assertIsNone(build_push_payload._ratio_pct(None))

    def test_daily_maintenance_help_never_executes_steps(self):
        with (
            mock.patch.object(daily_maintenance.subprocess, "run") as run,
            mock.patch("sys.stdout", new=io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            daily_maintenance.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        run.assert_not_called()

    def test_collection_and_cron_failures_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = sqlite3.connect(root / "ledger.db")
            ledger.execute(
                "CREATE TABLE collection_runs("
                "cycle_id TEXT,source TEXT,status TEXT,ts TEXT,err TEXT)"
            )
            now_cst = datetime.now(query_state.CST).strftime("%Y-%m-%d %H:%M:%S")
            ledger.execute(
                "INSERT INTO collection_runs VALUES(?,?,?,?,?)",
                ("2026-07-26T23:00", "fast", "error", now_cst, "network timeout"),
            )
            ledger.commit()
            ledger.close()

            openclaw_db = root / "openclaw.sqlite"
            cron = sqlite3.connect(openclaw_db)
            cron.execute(
                "CREATE TABLE cron_jobs("
                "store_key TEXT,job_id TEXT,name TEXT,last_run_status TEXT,"
                "last_error TEXT,consecutive_errors INTEGER)"
            )
            cron.execute(
                "CREATE TABLE cron_run_logs("
                "store_key TEXT,job_id TEXT,ts INTEGER,status TEXT,error TEXT,"
                "run_at_ms INTEGER,duration_ms INTEGER)"
            )
            now_ms = int(time.time() * 1000)
            cron.execute(
                "INSERT INTO cron_jobs VALUES(?,?,?,?,?,?)",
                ("cron", "slow", "okx-collect-hourly", "error", "timed out", 2),
            )
            cron.execute(
                "INSERT INTO cron_run_logs VALUES(?,?,?,?,?,?,?)",
                ("cron", "slow", now_ms, "error", "timed out", now_ms, 480000),
            )
            cron.commit()
            cron.close()

            results = []
            with mock.patch.dict(
                os.environ, {"OPENCLAW_STATE_DB": str(openclaw_db)}, clear=False
            ):
                query_state.check_collection_failures(
                    str(root), stale_min=15, hh01_only=False, results=results
                )

        self.assertEqual(results[0]["status"], "WARN")
        self.assertEqual(len(results[0]["collection_errors"]), 1)
        self.assertEqual(len(results[0]["cron_errors"]), 1)
        self.assertEqual(len(results[0]["active_cron_errors"]), 1)

    def test_regime_check_prefers_authoritative_public_macro_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            con = sqlite3.connect(root / "regime.db")
            con.row_factory = sqlite3.Row
            con.executescript(public_macro.TABLE_DDL)
            public_macro.upsert_observations(
                con,
                [
                    {
                        "metric": public_macro.METRIC_DXY_ECB,
                        "observation_date": "2026-07-27",
                        "source": public_macro.SOURCE_ECB_DXY,
                        "status": "official_inputs_calculated",
                        "value": 101.25,
                    },
                    {
                        "metric": public_macro.METRIC_FEAR_GREED,
                        "observation_date": "2026-07-27",
                        "source": public_macro.SOURCE_ALTERNATIVE,
                        "status": "official_primary",
                        "value": 30,
                        "label": "Fear",
                    },
                ],
            )
            con.commit()
            con.close()
            now_utc = datetime.now(query_state.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            stale_cross_market = {
                "ts": now_utc,
                "regime": "neutral",
                "dxy": 120.0,
                "vix": 18.0,
                "spx": 6500.0,
                "btc_mcap_chg_24h_usd": 1.0,
                "btc_etf_net_flow_usd": None,
                "dxy_calc_ecb": None,
                "fear_greed": None,
                "fear_greed_label": None,
            }
            results = []
            with (
                mock.patch(
                    "_regime_read.latest_cross_market",
                    return_value=stale_cross_market,
                ),
                mock.patch("_regime_read.latest_source", return_value="regime.db"),
            ):
                query_state.check_regime(
                    str(root), stale_min=15, hh01_only=False, results=results
                )

        self.assertEqual(results[0]["dxy_calc_ecb"], 101.25)
        self.assertEqual(results[0]["fear_greed"], 30)
        self.assertEqual(results[0]["fear_greed_label"], "Fear")


class ExecutionAndPushContractTests(unittest.TestCase):
    @staticmethod
    def _render_without_authoritative_overrides(payload):
        with (
            mock.patch.object(
                render_push_report,
                "authoritative_cycle_count",
                return_value=None,
            ),
            mock.patch.object(
                render_push_report,
                "authoritative_cycle_duration",
                return_value=None,
            ),
            mock.patch.object(
                render_push_report,
                "authoritative_equity",
                return_value=None,
            ),
            mock.patch.object(
                render_push_report,
                "authoritative_cum_pnl",
                return_value=None,
            ),
            mock.patch.object(
                render_push_report,
                "authoritative_position_count",
                return_value=None,
            ),
        ):
            return render_push_report.render(payload)

    def test_push_renders_live_portfolio_imr_contract(self):
        rendered = self._render_without_authoritative_overrides({
            "cycle_id": "TEST-PORTFOLIO-IMR",
            "cycle_count": 0,
            "action_taken": "HOLD",
            "assets": {
                "live": {"equity": 1000, "availBal": 400, "positions": 1},
                "demo": {"equity": 1000, "availBal": 900, "positions": 0},
            },
            "market": {"btc": 65000},
            "positions": [],
            "risk": {
                "current_portfolio_imr_ratio": 0.50,
                "projected_portfolio_imr_ratio": 0.61,
                "max_portfolio_imr_ratio": 0.666,
                "portfolio_imr_ratio_unit": "fraction",
                "lev": 5,
                "status": "PASS",
            },
        })

        self.assertIn(
            "Live组合保证金 当前 50.0% | 预计 61.0% / 66.6%",
            rendered["content"],
        )
        self.assertNotIn("Live单笔保证金", rendered["content"])

    def test_push_labels_legacy_single_order_margin_as_history_only(self):
        rendered = self._render_without_authoritative_overrides({
            "cycle_id": "TEST-LEGACY-MARGIN",
            "cycle_count": 0,
            "action_taken": "HOLD",
            "assets": {
                "live": {"equity": 1000, "availBal": 400, "positions": 0},
                "demo": {"equity": 1000, "availBal": 900, "positions": 0},
            },
            "market": {"btc": 65000},
            "positions": [],
            "risk": {"margin_pct": 2.5, "status": "PASS"},
        })

        self.assertIn(
            "Live组合保证金 当前 - / 66.6% | "
            "旧payload单笔字段 2.5000%（历史只读）",
            rendered["content"],
        )

    def test_close_receipt_always_carries_dispatched_cycle_id(self):
        cycle_id = "2026-07-27T21:45"
        receipt_context = {
            "cycle_id": cycle_id,
            "status": "ok",
            "decision_protocol": "decision_card_v1",
            "decision_card": {
                "direction_evidence": ["close receipt regression"],
                "opposing_evidence": ["no open position may remain"],
                "execution_conditions": {"status": "test fixture"},
                "invalidation_point": {"condition": "context mismatch"},
                "risk_reward": {"summary": "no order expected"},
                "portfolio_impact": {"summary": "no position change expected"},
                "historical_experience": {
                    "matched_wins": [],
                    "matched_losses": [],
                    "missed_opportunities": [],
                    "usage": "none",
                    "reason": "cycle propagation regression only",
                },
                "agent_judgement": "preserve dispatched cycle identity",
                "reference_overrides": [],
            },
        }
        with mock.patch.object(
            order_executor, "fetch_open_positions", return_value=[]
        ):
            receipt = order_executor.close_position(
                "WLD-USDT-SWAP",
                profile="live",
                pos_side="short",
                db_root=Path("<PROJECT_ROOT>/db"),
                cycle_id=cycle_id,
                receipt_context=receipt_context,
            )
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["note"], "no_open_position")
        self.assertEqual(receipt["cycle_id"], cycle_id)

    def test_oversized_push_keeps_every_required_section(self):
        long_text = "超长决策证据" * 180
        positions = [
            {
                "profile": "live" if index < 10 else "demo",
                "symbol": f"TEST{index}-USDT-SWAP",
                "side": "long" if index % 2 == 0 else "short",
                "sz": 100 + index,
                "avgPx": 123.45 + index,
                "lev": 5,
                "margin": 25.0,
                "upl": 1.0,
            }
            for index in range(14)
        ]
        payload = {
            "cycle_id": "TEST-2026-07-27T22:00",
            "cycle_count": 1,
            "cycle_duration_s": 10,
            "hhmm": "22:00",
            "action_taken": "OPEN_SHORT",
            "symbol": "TEST",
            "action_summary": long_text,
            "assets": {
                "live": {"equity": 1000, "availBal": 700, "pnl": 1},
                "demo": {"equity": 70000, "availBal": 60000, "pnl": 2},
            },
            "positions": positions,
            "risk": {
                "margin_pct": 2.5,
                "lev": 5,
                "side_pct": 50,
                "position_count": 10,
                "status": "PASS",
            },
            "market": {
                "btc": 65000,
                "btc_chg24h": 1.2,
                "eth": 1900,
                "eth_chg24h": 2.3,
                "regime": "range",
                "dxy": 120,
            },
            "decision": {
                "decision_protocol": "decision_card_v1",
                "summary": long_text,
                "reason": long_text,
                "decision_card": {
                    "direction_evidence": [long_text],
                    "opposing_evidence": [long_text],
                    "execution_conditions": {"status": long_text},
                    "invalidation_point": {"condition": long_text},
                    "risk_reward": {"rr": long_text},
                    "portfolio_impact": {"summary": long_text},
                    "historical_experience": {
                        "matched_wins": [],
                        "matched_losses": [],
                        "missed_opportunities": [],
                        "usage": "partial",
                        "reason": long_text,
                    },
                },
            },
            "execution": {
                "result": long_text,
                "db_rows_live": 1,
                "db_rows_demo": 0,
            },
            "timeline": {"next_hh01_min": 46, "next_review_time": "08:05"},
            "exceptions": [{"name": "test", "detail": long_text}],
            "is_hh01": True,
            "macro": {
                "enabled": True,
                "dxy": 120,
                "dxy_d1": -0.08,
                "dxy_calc_ecb": 101.34,
                "dxy_calc_ecb_d1": -0.08,
                "fear_greed": 30,
                "fear_greed_label": "Fear",
                "btc_etf_net_flow_usd": "-240.1M provisional",
                "btc_etf_flow_status": "provisional_single_source",
                "btc_etf_flow_as_of": "2026-07-24",
            },
        }
        patches = (
            mock.patch.object(
                render_push_report, "authoritative_cycle_count", return_value=None
            ),
            mock.patch.object(
                render_push_report,
                "authoritative_cycle_duration",
                return_value=None,
            ),
            mock.patch.object(
                render_push_report, "authoritative_equity", return_value=None
            ),
            mock.patch.object(
                render_push_report, "authoritative_cum_pnl", return_value=None
            ),
            mock.patch.object(
                render_push_report,
                "authoritative_position_count",
                return_value=None,
            ),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            rendered = render_push_report.render(payload)
        validation = validate_push_format.validate(rendered["content"])
        self.assertLessEqual(
            rendered["char_count"], render_push_report.MAX_CONTENT_CHARS
        )
        self.assertTrue(validation["ok"], validation)

    def test_reconcile_prefers_matching_execution_journal_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_root = Path(tmp) / "db"
            journal_dir = db_root / "journal"
            journal_dir.mkdir(parents=True)
            db_path = db_root / "live_trades.db"
            db_path.touch()
            journal_record = {
                "ts": "2026-07-27 21:53:38",
                "action_taken": "CLOSE_SHORT",
                "trade": {
                    "symbol": "WLD-USDT-SWAP",
                    "action": "close",
                    "side": "short",
                    "ordId": "3780175823831326720",
                    "reason": "Agent 主动平仓",
                    "raw": {"ord_ids": ["3780175823831326720"]},
                },
            }
            (journal_dir / "exec_live.jsonl").write_text(
                json.dumps(journal_record, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            matched = reconcile_exchange_closes.find_journal_close(
                db_path,
                "live",
                "WLD-USDT-SWAP",
                ["3780175823831326720"],
            )

        self.assertIsNotNone(matched)
        self.assertEqual(matched["line_no"], 1)
        self.assertEqual(matched["trade"]["reason"], "Agent 主动平仓")


class ScopeAndSourceRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = json.loads(
            (ROOT / "collectors" / "sources" / "registry.json").read_text(
                encoding="utf-8"
            )
        )
        cls.sources = {row["id"]: row for row in cls.registry["sources"]}

    def test_mx_shared_daily_budget_is_72(self):
        mx = self.sources["mx_search"]
        geo = self.sources["geo_political"]
        calls = 1440 // mx["poll_interval_min"]
        calls += (1440 // geo["poll_interval_min"]) * 4
        self.assertEqual(mx["poll_interval_min"], 60)
        self.assertEqual(calls, 72)
        self.assertEqual(calls * 2, 144)
        self.assertLess(calls * 2, 150)
        self.assertTrue(news_collect._source_due(mx, "2026-07-27T01:00"))
        self.assertFalse(news_collect._source_due(mx, "2026-07-27T01:30"))
        self.assertTrue(news_collect._source_due(geo, "2026-07-27T02:00"))
        self.assertFalse(news_collect._source_due(geo, "2026-07-27T01:00"))

    def test_contract_statistics_sources_are_optional_official_15m_inputs(self):
        open_interest = self.sources["okx_contract_open_interest_history"]
        taker_volume = self.sources["okx_contract_taker_volume"]
        for source in (open_interest, taker_volume):
            self.assertEqual(source["native_cadence"], "15m")
            self.assertFalse(source["required"])
            self.assertTrue(source["enabled"])
            self.assertEqual(source["timeout_sec"], 75)
        self.assertIn(
            "/api/v5/rubik/stat/contracts/open-interest-history",
            open_interest["endpoint"],
        )
        self.assertIn(
            "/api/v5/rubik/stat/taker-volume-contract",
            taker_volume["endpoint"],
        )
        self.assertIn("unit=2", taker_volume["endpoint"])

    def test_contract_statistics_freshness_uses_source_ts_not_carried_collection_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            con = sqlite3.connect(root / "market.db")
            con.execute(
                "CREATE TABLE market_contract_statistics("
                "ts TEXT,collected_ts TEXT,source TEXT)"
            )
            con.execute(
                "INSERT INTO market_contract_statistics VALUES(?,?,?)",
                (
                    "2026-08-12T01:15:00Z",
                    "2026-08-12T03:02:14Z",
                    "okx_rest_contract_oi_taker_15m",
                ),
            )
            con.commit()
            con.close()

            last_seen = source_freshness.derive_last_seen(root)

        expected = "2026-08-12 09:15:00"
        self.assertEqual(
            last_seen["okx_contract_open_interest_history"], expected)
        self.assertEqual(last_seen["okx_contract_taker_volume"], expected)
        self.assertNotEqual(
            last_seen["okx_contract_open_interest_history"],
            "2026-08-12 11:02:14",
        )

    def test_public_macro_sources_use_independent_observation_dates(self):
        timestamps = source_freshness._macro_source_timestamps(
            "2026-07-26 23:00:00",
            public_dates={
                "macro_dxy_calc_ecb": "2026-07-24",
                "macro_etf_flow": "2026-07-24",
                "macro_fear_greed": "2026-07-27",
            },
        )
        self.assertEqual(
            timestamps["macro_dxy_calc_ecb"], "2026-07-24 23:59:59"
        )
        self.assertEqual(timestamps["macro_etf_flow"], "2026-07-24 23:59:59")
        self.assertEqual(
            timestamps["macro_fear_greed"], "2026-07-27 23:59:59"
        )
        self.assertTrue(self.sources["macro_fear_greed"]["enabled"])
        self.assertTrue(self.sources["macro_etf_flow"]["enabled"])
        self.assertTrue(self.sources["macro_dxy_calc_ecb"]["enabled"])
        self.assertTrue(self.sources["macro_btc_mcap_change"]["enabled"])

    def test_dxy_uses_its_source_as_of_not_shared_row_time(self):
        timestamps = source_freshness._macro_source_timestamps(
            "2026-07-27 21:00:00",
            json.dumps({"dxy": {"source": "fred", "source_as_of": "2026-07-17"}}),
        )
        self.assertEqual(
            timestamps["macro_dxy_vix_spx"], "2026-07-17 23:59:59"
        )

    def test_okx_first_and_authoritative_supplement_contract(self):
        self.assertTrue(self.sources["okx_top_long_short"]["enabled"])
        self.assertEqual(
            self.sources["okx_top_long_short"]["native_cadence"], "hourly"
        )
        self.assertTrue(self.sources["macro_economic_calendar"]["enabled"])
        supplement = self.sources["x_authoritative_supplement"]
        self.assertTrue(supplement["enabled"])
        self.assertEqual(supplement["adapter"], "EXTERNAL_SCOUT")
        self.assertEqual(supplement["native_cadence"], "daily")
        scout = (ROOT / "agents" / "news_scout.md").read_text(encoding="utf-8")
        self.assertIn("OKX CLI 专用结构化接口 > X 官方/权威账号", scout)
        self.assertIn("verification_status=cross_checked", scout)
        self.assertIn("Alternative.me API 直采", scout)
        self.assertIn("DXY 计算值已由 ECB 官方汇率直采", scout)
        macro = self.sources["macro_dxy_vix_spx"]
        self.assertIn("DTWEXBGS", macro["endpoint"])
        self.assertIn("非ICE DXY", macro["note"])
        inspection = (ROOT / "scripts" / "query_state.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("BTC_ETF_NET_FLOW_USD", inspection)
        self.assertNotIn(" | ETF={etf}", inspection)
        rendered = (ROOT / "scripts" / "render_push_report.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("USD_BROAD(DTWEXBGS)", rendered)
        self.assertNotIn("BTC ETF proxy", rendered)

    def test_all_linear_usdt_swaps_are_in_scope(self):
        live = (ROOT / "agents" / "live_trader.md").read_text(encoding="utf-8")
        self.assertIn("无资产类别排除", live)
        self.assertNotIn("排除股票代币", live)


class TmpStdlibShadowTests(unittest.TestCase):
    """2026-08-06：tmp 根下与标准库同名的 .py 会让 trader 当轮执行脚本炸在 import
    （sys.path[0] = 脚本自身目录）。实证：13:34 落下的 bisect.py 埋了 6.4h，
    因期间双盘全是 HOLD（回执走 collectors/，绕开该路径）才没引爆。"""

    def test_detects_only_stdlib_names_at_tmp_root(self):
        import tmp_cleanup

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bisect.py").write_text("# 调试残留", encoding="utf-8")
            (root / "inspect.py").write_text("# 调试残留", encoding="utf-8")
            # 正常的当轮执行脚本/回执，绝不能误报
            (root / "_run_open_2026-08-06T04-00.py").write_text("x=1", encoding="utf-8")
            (root / "_receipt_live_2026-08-06T19-45.json").write_text("{}", encoding="utf-8")
            (root / "bisect.txt").write_text("非 .py 不算", encoding="utf-8")
            # archive/ 子目录不在 sys.path[0] 上，不该误报
            (root / "archive").mkdir()
            (root / "archive" / "json.py").write_text("# 归档", encoding="utf-8")

            hits = [p.name for p in tmp_cleanup.find_stdlib_shadows(root)]
        self.assertEqual(hits, ["bisect.py", "inspect.py"])

    def test_missing_tmp_root_is_not_an_error(self):
        import tmp_cleanup

        self.assertEqual(
            tmp_cleanup.find_stdlib_shadows(Path(r"<PROJECT_ROOT>\tmp\__does_not_exist__")),
            [])

    def test_trigger_preflight_warns_but_never_blocks(self):
        """派发层只告警——在这里阻断会把本来能完成的 HOLD 轮一起杀掉
        （同 2026-08-05 autoheal 拍板的边界）。"""
        import trigger_agent

        with (
            mock.patch.object(trigger_agent, "_send_tmp_shadow_alert",
                              return_value=True) as alert,
            mock.patch("tmp_cleanup.find_stdlib_shadows",
                       return_value=[Path(r"<PROJECT_ROOT>\tmp\bisect.py")]),
            mock.patch("sys.stderr", new=io.StringIO()) as err,
        ):
            names = trigger_agent._check_tmp_stdlib_shadow("live", "2026-08-06T19:45")
        self.assertEqual(names, ["bisect.py"])
        alert.assert_called_once()
        self.assertIn("遮蔽标准库", err.getvalue())

    def test_trigger_preflight_survives_probe_failure(self):
        """探测本身出错绝不能拖垮起棒。"""
        import trigger_agent

        with (
            mock.patch("tmp_cleanup.find_stdlib_shadows",
                       side_effect=OSError("boom")),
            mock.patch("sys.stderr", new=io.StringIO()),
        ):
            self.assertEqual(
                trigger_agent._check_tmp_stdlib_shadow("live", "2026-08-06T19:45"),
                [])


if __name__ == "__main__":
    unittest.main()
