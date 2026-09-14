# -*- coding: utf-8 -*-
"""F1 补齐｜observer 杀进程路径也必须落 analysis 占位行（2026-08-19）。

F1 首版只覆盖 `analyst_writer` 自己拒绝越界写入的那条路径。但 live 轮真正
的多数失败形态是 `stage_runner` 的 `_LiveChildObserver` 判
`analysis_deadline_exceeded:*` 后先请求同连接取消、必要时再整树终止子进程 —— 此时
analyst_writer 根本没被调用过，占位行无从谈起。实测 2026-08-19：live 派发
83 轮、analysis_runs 只有 76 行，缺的 7 轮连 error 行都没有，`query_state`
的 lost_cycles 因此恒 FAIL，dispatcher 也只能按「压根没分析」派失败战报。

本用例钉死：observer 停机 ⇒ 占位行必须落库，且不得顶掉已存在的 'ok' 终态。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "scripts", ROOT / "collectors"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import stage_runner  # noqa: E402
# stage_runner 在 import 时把 collectors/ 塞进 sys.path，它内部走的是**顶层名**
# `import analyst_writer`。这里必须用同一个顶层名取模块对象——`from collectors
# import analyst_writer` 会得到另一个模块实例，patch DB_PATH 打不到被测路径。
import analyst_writer  # noqa: E402

CST = timezone(timedelta(hours=8))

_ANALYSIS_DDL = """
CREATE TABLE analysis_runs (
    cycle_id        TEXT PRIMARY KEY,
    ts              TEXT,
    mode            TEXT,
    regime          TEXT,
    regime_stale    INTEGER,
    market_summary  TEXT,
    missing_sources TEXT,
    raw             TEXT,
    status          TEXT
);
CREATE TABLE analysis_signals (
    cycle_id TEXT, symbol TEXT, dim1 REAL, dim2 REAL, dim3 REAL, dim4 REAL,
    dim5 REAL, total REAL, action TEXT, side TEXT, confidence REAL,
    entry_hint TEXT, stop_hint TEXT, tp_hint TEXT, reasoning TEXT,
    raw TEXT, decision_card TEXT
);
"""


class LiveObserverPlaceholderTests(unittest.TestCase):
    CYCLE = "2026-08-19T20:15"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "analysis.db"
        with closing(sqlite3.connect(self.db)) as con:
            con.executescript(_ANALYSIS_DDL)
            con.commit()
        # cycle+1min：距 live 绝对截止（cycle+13:00）仍有 12 分钟预算，
        # 不会走「剩余预算不足，child 不启动」的提前返回。
        self.now = datetime(2026, 8, 19, 20, 16, tzinfo=CST)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, stop_reason: str):
        """驱动 _run_stage_child 走 observer 停机分支。

        run_guarded 被替换掉后 observer 不会被真正轮询，因此直接把停机证据
        写进 observer.evidence —— 被测分支读的正是这份证据。
        """
        captured = {}

        def fake_run_guarded(command, **kwargs):
            observer = kwargs.get("observer")
            captured["observer"] = observer
            if observer is not None and stop_reason:
                observer.evidence["stop_reason"] = stop_reason
                observer.evidence["observed_at"] = "2026-08-19 20:24:52"
            return (stage_runner._proc.RC_OBSERVED_STOP, "", "", False)

        with mock.patch.object(analyst_writer, "DB_PATH", self.db), \
                mock.patch.object(stage_runner._proc, "run_guarded",
                                  side_effect=fake_run_guarded), \
                mock.patch.object(stage_runner, "_abort_gateway_session",
                                  return_value={"ok": True}):
            return stage_runner._run_stage_child(
                "live", self.CYCLE, ["noop"], now=self.now)

    def _row(self):
        with closing(sqlite3.connect(self.db)) as con:
            return con.execute(
                "SELECT cycle_id,ts,mode,regime,market_summary,raw,status "
                "FROM analysis_runs WHERE cycle_id=?", (self.CYCLE,)).fetchone()

    def test_observer_deadline_stop_writes_placeholder_row(self):
        result = self._run("analysis_deadline_exceeded:no_timely_analysis")
        self.assertTrue(result.get("analysis_placeholder_written"))
        self.assertEqual("analysis_deadline_exceeded",
                         result.get("failure_kind"))
        row = self._row()
        self.assertIsNotNone(row, "observer 杀进程后必须留下占位行")
        self.assertEqual("error", row[6])
        self.assertEqual("full", row[2])
        # 占位行不含任何业务结论。
        self.assertIsNone(row[3])
        self.assertIsNone(row[4])
        self.assertIn("stage_runner.live_observer", row[5])
        self.assertIn("no_timely_analysis", row[5])
        self.assertIn("placeholder", row[5])

    def test_placeholder_covers_every_deadline_stop_variant(self):
        """三种 analysis_deadline_exceeded:* 停机原因都要留痕。"""
        for reason in (
            "analysis_deadline_exceeded:no_timely_analysis",
            "analysis_deadline_exceeded:late_analysis",
            "analysis_deadline_exceeded:facts_without_timely_analysis",
        ):
            with self.subTest(reason=reason):
                with closing(sqlite3.connect(self.db)) as con:
                    con.execute("DELETE FROM analysis_runs")
                    con.commit()
                result = self._run(reason)
                self.assertTrue(result.get("analysis_placeholder_written"))
                self.assertIn(reason.split(":", 1)[1], self._row()[5])

    def test_placeholder_never_overwrites_existing_ok_run(self):
        """真分析已落库、observer 才停机（迟到证据竞态）⇒ 'ok' 必须原样保留。"""
        with closing(sqlite3.connect(self.db)) as con:
            con.execute(
                "INSERT INTO analysis_runs(cycle_id,ts,mode,raw,status) "
                "VALUES(?,?,?,?,?)",
                (self.CYCLE, "real-ts", "full", '{"real":true}', "ok"))
            con.commit()
        self._run("analysis_deadline_exceeded:late_analysis")
        row = self._row()
        self.assertEqual("ok", row[6])
        self.assertEqual("real-ts", row[1])
        self.assertEqual('{"real":true}', row[5])

    def test_non_deadline_stop_reasons_do_not_write_placeholder(self):
        """业务终态/handoff 违规不是「没有分析」，不得伪造 error 行。"""
        for reason in ("business_terminal_committed",
                       "runner_terminal:committed",
                       "analysis_terminal:skipped"):
            with self.subTest(reason=reason):
                with closing(sqlite3.connect(self.db)) as con:
                    con.execute("DELETE FROM analysis_runs")
                    con.commit()
                result = self._run(reason)
                self.assertNotIn("analysis_placeholder_written", result)
                self.assertIsNone(self._row())

    def test_placeholder_failure_never_breaks_the_stage_result(self):
        """写占位失败只降级为 False，绝不把 stage 结果炸掉。"""
        with mock.patch.object(analyst_writer, "DB_PATH",
                               Path("Z:/nonexistent/analysis.db")):
            result = self._run("analysis_deadline_exceeded:no_timely_analysis")
        self.assertIn("returncode", result)
        self.assertEqual("analysis_deadline_exceeded",
                         result.get("failure_kind"))

    def test_placeholder_flag_is_surfaced_in_stage_status(self):
        """写没写必须能从 stage-status 看到。

        `stage_runner.main()` 的 status 是**逐键挑选**而非整体合并 child_result，
        2026-08-20 首次真实触发时就漏了这一键：占位行确实落了库，status 里却是
        None —— 排查的人读到 None 会误判成没触发，比不写更糟。
        """
        import ast
        src = (ROOT / "scripts" / "stage_runner.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        wired = False
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Subscript)
                            and isinstance(t.value, ast.Name)
                            and t.value.id == "status"
                            and isinstance(t.slice, ast.Constant)
                            and t.slice.value == "analysis_placeholder_written"
                            for t in node.targets)):
                wired = True
        self.assertTrue(
            wired, "stage_runner 未把 analysis_placeholder_written 写进 status")

    def test_public_entry_delegates_to_internal_writer(self):
        """公开入口只做转发，不复制第二份占位逻辑。"""
        with mock.patch.object(analyst_writer, "DB_PATH", self.db):
            analyst_writer.commit_deadline_placeholder(
                self.CYCLE, "full", {"refusal": "x"})
        self.assertEqual("error", self._row()[6])


if __name__ == "__main__":
    unittest.main()
