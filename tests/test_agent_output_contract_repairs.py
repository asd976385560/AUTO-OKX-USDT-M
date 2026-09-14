# -*- coding: utf-8 -*-
"""Regression tests for bounded, UTF-8 agent evidence output."""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


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
from core.experience_contract import (  # noqa: E402
    setup_from_prices,
    validate_contract,
)


class SimilarExperienceOutputTests(unittest.TestCase):
    def test_price_geometry_uses_fractional_distance_and_one_canonical_hash(self):
        setup = setup_from_prices(0.0698, 0.0705, 0.0685)

        self.assertEqual(setup["stop_distance_pct"], 0.01002865)
        self.assertEqual(setup["planned_rr"], 1.85714286)
        with tempfile.TemporaryDirectory() as tmp:
            result = find_similar_experience.find_similar_experience(
                "DOGE-USDT-SWAP",
                "short",
                "range",
                "open",
                profile_filter="live",
                db_root=Path(tmp),
                now=datetime(2026, 8, 14, 0, 30,
                             tzinfo=find_similar_experience.CST),
                entry=0.0698,
                stop=0.0705,
                target=0.0685,
            )

        self.assertEqual(result["evidence_contract"]["query"]["setup"], setup)
        self.assertEqual(
            result["query"]["query_features"]["stop_distance_pct"],
            setup["stop_distance_pct"],
        )
        self.assertEqual(
            result["query"]["query_features"]["planned_rr"],
            setup["planned_rr"],
        )

    def test_price_geometry_rejects_partial_or_mixed_setup_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            common = {
                "symbol": "DOGE-USDT-SWAP",
                "side": "short",
                "regime": "range",
                "action": "open",
                "db_root": Path(tmp),
                "now": datetime(2026, 8, 14, 0, 30,
                                tzinfo=find_similar_experience.CST),
            }
            with self.assertRaisesRegex(ValueError, "supplied together"):
                find_similar_experience.find_similar_experience(
                    **common, entry=0.0698, stop=0.0705)
            with self.assertRaisesRegex(ValueError, "not both"):
                find_similar_experience.find_similar_experience(
                    **common,
                    entry=0.0698,
                    stop=0.0705,
                    target=0.0685,
                    stop_distance_pct=0.01002865,
                    planned_rr=1.85714286,
                )

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
                    "v": 3,
                    "feature_epoch": (
                        find_similar_experience._simutil.FEATURE_EPOCH_V3),
                    "features": (
                        find_similar_experience._simutil
                        .experience_features_v3({
                            "asset_class": "crypto", "side": "long",
                            "regime": "range", "action": "open",
                        })),
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
                    "v": 3,
                    "feature_epoch": (
                        find_similar_experience._simutil.FEATURE_EPOCH_V3),
                    "features": (
                        find_similar_experience._simutil
                        .experience_features_v3({
                            "asset_class": "crypto", "side": "short",
                            "regime": "range", "action": "open",
                        })),
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
    def assertPortableIn(self, expected, actual):
        self.assertIn(expected.replace(chr(92), "/"), actual.replace(chr(92), "/"))

    def test_unified_gateway_timeout_is_bounded_by_cycle_clock(self):
        timeout = trigger_agent._unified_live_timeout_seconds(
            "2026-08-15T09:00",
            now=datetime(2026, 8, 15, 9, 4, 22,
                         tzinfo=trigger_agent.CST),
        )
        expired = trigger_agent._unified_live_timeout_seconds(
            "2026-08-15T09:00",
            now=datetime(2026, 8, 15, 9, 14, 30,
                         tzinfo=trigger_agent.CST),
        )

        self.assertEqual(578, timeout)
        self.assertEqual(1, expired)

    def test_unified_prompt_separates_route_mode_from_writer_mode(self):
        message = trigger_agent._unified_live_message(
            "2026-08-11T18:30", "briefing marker",
            now=datetime(2026, 8, 11, 18, 31,
                         tzinfo=trigger_agent.CST))

        self.assertIn("dispatch_mode=unified", message)
        self.assertIn("analysis_receipt_mode=full", message)
        self.assertIn("顶层 mode 必须固定为 full", message)
        self.assertIn("market_summary 必须直接包含", message)
        self.assertIn("五段禁止写成 string", message)
        self.assertIn("summary/stance/key_points", message)
        self.assertIn("signals[].decision_card 必须直接包含", message)
        self.assertIn("HOLD/WAIT 若写入同样必须给完整卡", message)
        self.assertIn("side=long/short 且与 action 一致", message)
        self.assertIn("JSON list[string]", message)
        self.assertIn(
            "historical_experience.evidence_contract",
            message,
        )
        self.assertIn("尤其不得把 query.as_of/instrument_context.as_of", message)
        self.assertIn("改一个字符就会拒写", message)
        self.assertIn("禁止 Get-ChildItem/ls/dir", message)
        self.assertIn("只能读取本轮命令刚生成的具名 out-file", message)
        self.assertIn("multitimeframe_analysis 本身必须是", message)
        self.assertIn("立即淘汰该 OPEN 候选", message)
        self.assertIn("每个拟保留的 OPEN 候选都必须读取", message)
        self.assertIn("计数只允许由 writer 从契约注入 scope_counts", message)
        self.assertIn("无法满足就淘汰候选，不得猜数", message)
        self.assertIn("不得改名为 rationale/final_judgement/overrides", message)
        self.assertIn("【trade writer 契约】", message)
        self.assertIn("cycle 顶层 decision_card 不是摘要容器", message)
        self.assertIn("禁止改成 summary/open_candidates/hold_positions", message)
        self.assertIn("回执应省略 live_facts", message)
        self.assertIn("live_position_action_runner.py", message)
        self.assertIn("position_plan_2026-08-11T18-30.json", message)
        self.assertIn("actions=[] 即 HOLD", message)
        self.assertIn("【逐仓显式复核格式】", message)
        self.assertIn("完整 instId 与动作词之间不得插入逗号、句号或分号", message)
        self.assertIn("actions=[] 的 HOLD 同样适用", message)
        self.assertIn("不论是否包含 OPEN/ADD", message)
        self.assertIn("target_stop_risk_pct_equity", message)
        self.assertIn("禁止重抄或四舍五入 equity", message)
        self.assertIn("live_facts.balance.totalEq 注入唯一权威值", message)
        self.assertIn("下一条工具调用必须直接执行上述 runner 命令", message)
        self.assertIn("plan 落盘不等于业务完成", message)
        self.assertNotIn("card/equity/regime", message)
        self.assertIn("plan 后立即 runner/30s 机器闸", message)
        self.assertIn("runner_in_progress=true", message)
        self.assertNotIn("无 OPEN/ADD 时", message)
        self.assertNotIn("含 OPEN/ADD 时", message)
        self.assertIn("runner 不替你判断，也不设退出阈值", message)
        self.assertIn("不得在 receipt_context 手抄或缩写 decision_card", message)
        self.assertIn("analysis.db writer 验证卡为基底", message)
        self.assertIn("agent_judgement/position_reviews", message)
        self.assertIn("纯持仓/HOLD plan 则仍须提供完整周期 decision_card", message)
        self.assertIn("batch_status=partial|failed", message)
        self.assertIn("multitimeframe_decision_evidence.py", message)
        self.assertIn("relative_rank_1_among_15m_1H_4H_not_calibrated", message)
        self.assertIn("confidence_claim_allowed=false", message)
        self.assertIn(
            "当前前向校准门通过且主人另行风险批准前",
            message,
        )
        self.assertIn("不得把 relative rank/未校准分值写成校准可信度", message)
        self.assertNotIn("独立前瞻验证达到 90%", message)
        self.assertNotIn("90% 可信度", message)
        self.assertIn("【周期内吞吐契约】", message)
        self.assertIn("前 11 分钟内冻结 analysis", message)
        self.assertIn("预留 3 分钟", message)
        self.assertIn("动态目标区间为 3..8 个", message)
        self.assertIn("扩大分析范围不是交易配额", message)
        self.assertIn("本轮为 15 分钟槽", message)
        self.assertIn("【轮换】候选充足时", message)
        self.assertIn("『近6h未深挖』的候选", message)
        self.assertIn("淘汰 ≥3 次时应降权", message)
        self.assertIn("只允许 0..3 项", message)
        self.assertIn("依次顺延至第 4 个及其后", message)
        self.assertIn("本轮候选深挖区间据此收敛为 3..5 个", message)
        self.assertIn("本轮证据深挖下限=3", message)
        self.assertIn("仅凭 briefing 的涨跌幅、RSI、催化或结构摘要直接淘汰不算深挖", message)
        self.assertIn("raw.candidates_deep_dived", message)
        self.assertIn("raw.candidate_evidence_shortfall", message)
        self.assertIn("证据下限不是开仓下限", message)
        self.assertIn("距 analysis 绝对闸尚余 510 秒", message)
        self.assertIn("禁止调用 session_status，禁止读取 analysis_template", message)
        self.assertNotIn("session_status、memory_search", message)
        self.assertIn("允许读取 MEMORY.md 并调用 memory_search", message)
        self.assertIn("两者合计≤2 次", message)
        self.assertIn("一律以后者为准", message)
        self.assertIn("禁止先建 Python/PowerShell生成器", message)
        self.assertIn("validate-only 返回 ok:true 后", message)
        self.assertIn("下一条工具调用必须直接用完全同一文件", message)
        self.assertIn("validate-only 不等于 analysis 已落库", message)
        self.assertIn("2026-08-11 18:39:30 UTC+8", message)
        self.assertIn("2026-08-11 18:43:00 UTC+8", message)
        self.assertIn("2026-08-11 18:44:00 UTC+8", message)
        self.assertIn("不设最低开仓数、多空配额或强制交易", message)
        self.assertIn("不得单独成为停止候选求证", message)
        self.assertIn("没有最终开仓候选就写 signals=[]", message)
        self.assertIn("【零开仓双writer不变量】", message)
        self.assertIn("主动淘汰全部 OPEN 候选只是分析结论", message)
        self.assertIn("actions=[] 的完整 position plan", message)
        self.assertIn("上述两个 writer 都成功前禁止最终答复或 stop", message)
        self.assertIn("严禁把‘准备写 analysis’", message)
        self.assertIn("下一次响应必须直接是最终 analysis 文件的 write 工具调用", message)
        self.assertIn("未入选候选不得展开成 WAIT/HOLD signal", message)
        self.assertIn("不得用来跳过 gate", message)
        self.assertIn("【交易阶段终止契约】", message)
        self.assertIn("禁止再读取 trades_writer.py 源码", message)
        self.assertIn("历史 _receipt_live_*.json", message)
        self.assertIn("writer 返回 ok:true 后，严禁再调用 query_db", message)
        self.assertIn("stage_runner 会独立核验", message)
        self.assertIn("禁止无内容 stop", message)
        self.assertIn("零成交，HOLD 也必须先落库", message)
        self.assertIn("2026-08-11T18:30", message)
        self.assertIn("briefing marker", message)

    def test_late_hourly_prompt_reduces_only_candidate_tool_budget(self):
        message = trigger_agent._unified_live_message(
            "2026-08-11T18:30", "briefing marker",
            now=datetime(2026, 8, 11, 18, 35, 40,
                         tzinfo=trigger_agent.CST))
        urgent = trigger_agent._unified_live_message(
            "2026-08-11T18:30", "briefing marker",
            now=datetime(2026, 8, 11, 18, 38,
                         tzinfo=trigger_agent.CST))

        self.assertIn("距 analysis 绝对闸尚余 230 秒", message)
        self.assertIn("本轮候选深挖区间据此收敛为 2..2 个", message)
        self.assertIn("硬上限=2", message)
        self.assertIn("不是方向、开仓数或仓位约束", message)
        self.assertIn("完整 300+ 宇宙判断", message)
        self.assertIn("距 analysis 绝对闸尚余 90 秒", urgent)
        self.assertIn("本轮候选深挖区间据此收敛为 1..1 个", urgent)
        self.assertIn("本轮证据深挖下限=1", urgent)

    def test_candidate_bundle_shadow_and_consume_messages_are_mutually_exclusive(self):
        base = {
            "status": "PASSED",
            "candidate_count": 16,
            "screened_count": 16,
            "ready_count": 6,
            "briefing_sha256": "a" * 64,
            "bundle_sha256": "b" * 64,
            "bundle_path": _public_project_path('logs', 'candidate-evidence', 'bundle.json'),
        }
        shadow = trigger_agent._unified_live_message(
            "2026-08-11T18:30",
            "briefing marker",
            now=datetime(2026, 8, 11, 18, 31, tzinfo=trigger_agent.CST),
            candidate_bundle={**base, "phase": "shadow"},
        )
        self.assertIn("候选bundle阶段=shadow", shadow)
        self.assertIn("严禁用bundle替代逐币决策取证", shadow)
        self.assertIn("candidates_deep_dived_v2", shadow)
        self.assertIn("本轮动态目标=5", shadow)
        consume = trigger_agent._unified_live_message(
            "2026-08-11T18:30",
            "briefing marker",
            now=datetime(2026, 8, 11, 18, 31, tzinfo=trigger_agent.CST),
            candidate_bundle={**base, "phase": "consume"},
        )
        self.assertIn("候选bundle阶段=consume", consume)
        self.assertIn("只允许读取具名bundle", consume)
        self.assertIn("禁止对bundle内健康项重复运行--symbol", consume)
        self.assertNotIn("严禁用bundle替代逐币决策取证", consume)

        manifest_only = trigger_agent._unified_live_message(
            "2026-09-01T04:00", "briefing marker",
            now=datetime(2026, 9, 1, 4, 1, tzinfo=trigger_agent.CST),
            candidate_bundle={
                "phase": "rollback", "status": "MANIFEST_ONLY",
                "manifest_valid": True, "candidate_count": 14,
                "manifest_path": _public_project_path('logs', 'candidate-evidence', 'manifest.json'),
                "ready_pool_path": _public_project_path('logs', 'candidate-evidence', 'pool.json'),
                "ready_pool_status": "PASSED", "full_ready_count": 65,
                "briefing_sha256": "c" * 64,
            },
        )
        self.assertIn("候选漏斗独立生效", manifest_only)
        self.assertIn("--candidate-id <cand_...>", manifest_only)
        self.assertIn("bundle_status(PASSED|DEGRADED|NOT_APPLICABLE)",
                      manifest_only)

        degraded_exact = trigger_agent._unified_live_message(
            "2026-09-01T04:00", "briefing marker",
            now=datetime(2026, 9, 1, 4, 1, tzinfo=trigger_agent.CST),
            candidate_bundle={
                "phase": "shadow", "status": "DEGRADED",
                "manifest_valid": True, "candidate_count": 14,
                "manifest_path": _public_project_path('logs', 'candidate-evidence', 'manifest.json'),
                "briefing_sha256": "d" * 64,
                "error": "bundle timeout",
            },
        )
        self.assertIn("exact manifest仍有效", degraded_exact)
        self.assertIn("--candidate-id <cand_...>", degraded_exact)
        self.assertNotIn("--symbol <完整instId>", degraded_exact)

        v4_regular = trigger_agent._unified_live_message(
            "2026-08-22T03:45", "briefing marker",
            now=datetime(2026, 8, 22, 3, 46, 25,
                         tzinfo=trigger_agent.CST))
        v4_late = trigger_agent._unified_live_message(
            "2026-08-22T04:00", "briefing marker",
            now=datetime(2026, 8, 22, 4, 4, 31,
                         tzinfo=trigger_agent.CST))
        self.assertIn("距 分析+判断+交易终态闸尚余 785 秒", v4_regular)
        self.assertIn("Gateway turn 实际上限尚余 755 秒", v4_regular)
        self.assertIn("候选工具预算为 575 秒", v4_regular)
        self.assertIn("本轮候选深挖区间据此收敛为 3..7 个", v4_regular)
        self.assertIn("距 分析+判断+交易终态闸尚余 599 秒", v4_late)
        self.assertIn("Gateway turn 实际上限尚余 569 秒", v4_late)
        self.assertIn("候选工具预算为 329 秒", v4_late)
        self.assertIn("本轮候选深挖区间据此收敛为 3..3 个", v4_late)
        self.assertIn("本轮为整点槽", v4_late)
        self.assertIn("硬上限=3", v4_late)

    def test_trader_prompts_use_deterministic_live_facts_entrypoint(self):
        trigger = (ROOT / "collectors" / "trigger_agent.py").read_text(
            encoding="utf-8")
        live = (ROOT / "agents" / "live_trader.md").read_text(encoding="utf-8")
        executor = (ROOT / "core" / "order_executor.py").read_text(
            encoding="utf-8")

        self.assertIn("new_sl_trigger_px", live)
        self.assertIn("禁止写入 ADJUST_PROTECTION plan", live)
        self.assertIn(
            '"action":"ADJUST_PROTECTION"', live)

        for text in (trigger, live):
            self.assertIn("run_okx_python.ps1", text)
            self.assertIn("scripts/live_decision_facts.py", text)
            self.assertIn("trade_cycles", text)
            self.assertIn("禁止", text)
            self.assertIn("live_position_action_runner.py", text)
            self.assertIn("target_stop_risk_pct_equity", text)
            self.assertIn("不论是否包含 OPEN/ADD", text)
            self.assertIn("side=long|short", text)
        self.assertIn("no_three_period_no_six_card_v1", live)
        self.assertIn("minimal_decision_v2", live)
        self.assertIn("eligible_sides=[long,short]", live)
        self.assertIn("lightweight_open_v1", live)
        self.assertIn("顶层 `signals` 必须直接是JSON list", live)
        self.assertIn("不再要求旧ENTRY_READY veto结构", live)
        self.assertIn("不是方向或OPEN数量限制", live)
        self.assertIn("禁止 `memory_search`", live)
        self.assertNotIn("`signals` 只允许 0..3 项", live)
        self.assertNotIn("不能只放 `evidence_contract`", live)
        self.assertNotIn("confidence_claim_allowed=false", live)
        self.assertIn("scripts/_acceptance_thresholds.py", live)
        self.assertIn("cycle+870 秒", live)
        self.assertNotIn(
            "依次约束 analysis 冻结、Agent 业务终态、落记录+账实对账完成",
            live,
        )
        self.assertNotIn("六项决策卡完整率", live)
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

        facts = _public_project_path('tmp', 'live_facts_2026-07-29T00-15.json')
        self.assertPortableIn(
            ("<PROJECT_ROOT>/scripts/live_decision_facts.py --profile live "
            f"--cycle-id 2026-07-29T00:15 --out-file {facts}").replace('<PROJECT_ROOT>', _public_project_path()),
            message,
        )
        self.assertPortableIn(f"read {facts}", message)
        self.assertPortableIn(
            "analysis_authority_required_before_live_facts", message)
        self.assertPortableIn("不得重试facts", message)
        self.assertPortableIn("禁止自行换算", message)
        self.assertPortableIn("--facts-file", message)
        self.assertPortableIn("不得出现在同一条assistant响应", message)
        self.assertPortableIn("禁止并行", message)
        self.assertPortableIn(
            "position_exit_2026-07-29T00-15.json", message)
        self.assertPortableIn("portfolio_margin_state", message)
        self.assertPortableIn("既有仓位不扣减", message)
        self.assertPortableIn("evidence_contract", message)
        self.assertPortableIn("截断样例数组禁止计数", message)
        self.assertPortableIn("multitimeframe_decision_evidence.py", message)
        self.assertPortableIn("mtf_2026-07-29T00-15_<symbol>.json", message)
        self.assertPortableIn(
            "position_plan_2026-07-29T00-15.json", message)
        self.assertPortableIn(
            "_receipt_live_2026-07-29T00-15.json", message)

    def test_trigger_message_preserves_pre_boundary_and_activates_v3_clock_stop(self):
        before = trigger_agent._unified_live_message(
            "2026-08-20T17:45", "",
            now=datetime(2026, 8, 20, 17, 46, tzinfo=trigger_agent.CST),
        )
        after = trigger_agent._unified_live_message(
            "2026-08-20T18:00", "",
            now=datetime(2026, 8, 20, 18, 1, tzinfo=trigger_agent.CST),
        )
        self.assertIn("保持边界前历史停表口径", before)
        self.assertIn("Push+事后对账", before)
        self.assertNotIn("Push 不计入完整周期", before)
        self.assertIn("账实屏障最迟 2026-08-20 18:14:00", after)
        self.assertIn("Push 不计入完整周期", after)
        self.assertIn("analysis 最迟 2026-08-20 18:10:00", after)

        v4 = trigger_agent._unified_live_message(
            "2026-08-21T18:00", "",
            now=datetime(2026, 8, 21, 18, 1, tzinfo=trigger_agent.CST),
        )
        self.assertIn("870 秒只验两道事实闸", v4)
        self.assertIn("必需采集源已完成", v4)
        self.assertIn("分析+判断+交易", v4)
        self.assertIn("写库、报告、日志、Push 均不计入 870 秒", v4)
        self.assertNotIn("账实屏障最迟", v4)

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
