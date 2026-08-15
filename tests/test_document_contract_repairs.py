# -*- coding: utf-8 -*-
"""只读文档契约回归。

不连接交易所、不访问生产数据库、不启动 Agent、不发送消息。
"""
from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def load_script(relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_doc_checker():
    path = ROOT / "scripts" / "check_doc_versions.py"
    spec = importlib.util.spec_from_file_location("check_doc_versions_contract", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DocumentHeaderTests(unittest.TestCase):
    def test_current_document_surface_has_version_headers(self):
        checker = load_doc_checker()
        self.assertEqual(len(checker.COVERAGE_DOCS), 17)
        for relative in checker.COVERAGE_DOCS:
            with self.subTest(relative=relative):
                info = checker.parse_doc_header(ROOT / relative)
                self.assertNotIn(info["version"], {"?", "MISSING", "ERROR"})
                self.assertRegex(info["updated"], r"^\d{4}-\d{2}-\d{2}$")

    def test_db_apply_is_non_destructive_and_keeps_static_coverage_separate(self):
        checker = load_doc_checker()
        source = read("scripts/check_doc_versions.py")
        self.assertEqual(checker.DB_TRACKED_DOCS, ("skill.md",))
        self.assertNotIn("DELETE FROM doc_versions", source)
        self.assertIn("--static-only", source)


class TemplateContractTests(unittest.TestCase):
    def test_analysis_template_requires_five_sections_and_news_events(self):
        text = read("templates/analysis_template.md")
        for section in ("macro", "news", "tech", "sentiment", "quant"):
            self.assertIn(section, text)
        self.assertIn('"events"', text)
        self.assertNotIn('"top_events"', text)
        self.assertIn("强制校验", text)

    def test_trade_template_requires_context_atomic_commit_and_no_fake_open_fill(self):
        text = read("templates/trade_template.md")
        self.assertIn("receipt_context", text)
        self.assertIn("commit_receipt", text)
        self.assertIn("同一确定性 Python 进程", text)
        self.assertNotIn("approx_agg", text)
        self.assertNotIn("拉不到回退 mark_px", text)
        self.assertIn("OPEN 缺失即拒绝", text)

    def test_daily_contract_uses_its_own_validation_surface(self):
        template = read("templates/daily_template.md")
        reviewer = read("agents/reviewer.md")
        self.assertIn("独立日报校验", template)
        self.assertIn("独立日报 validator", reviewer)
        self.assertIn("不得调用 15M", template)
        self.assertNotIn("echo '{", template)
        self.assertNotIn("echo '{", reviewer)
        self.assertIn("weekly-<本周一日期>.md", template)
        self.assertIn("订单标识允许随日报外发", template)

    def test_push_contract_matches_validator_and_archives_before_send(self):
        template = read("templates/push_template.md")
        validator = load_script("scripts/validate_push_format.py")
        section_names = [name for _pattern, name in validator.REQUIRED_SECTIONS]
        non_anchor_names = {
            "轮次", "耗时", "动作", "资金字段",
            "累计收益字段", "BTC 行情", "ETH 行情",
        }
        anchor_count = sum(
            name not in non_anchor_names for name in section_names
        )
        self.assertEqual(len(section_names), 16)
        self.assertEqual(anchor_count, 9)
        self.assertNotIn("三周期判断", section_names)
        self.assertEqual(
            validator.MULTITIMEFRAME_REPORT_REQUIRED_FROM,
            "2026-08-12T20:00",
        )
        self.assertIn("16 项", template)
        self.assertIn("9 个 emoji 锚点", template)
        self.assertIn("版本化硬校验", template)
        self.assertLess(template.index("-> `push_archive`"), template.index("-> `qq_push`"))
        self.assertNotRegex(
            template,
            r"(?i)(?:target|group|c2c)[^\r\n]{0,80}\b[0-9]{8,20}\b",
        )


class CrossDocumentFactTests(unittest.TestCase):
    def test_public_map_covers_latest_runtime_contracts(self):
        skill = read("skill.md")
        readme = read("README.md")
        for text in (skill, readme):
            self.assertIn("execution_intents", text)
            self.assertIn("stage_runner", text)
        self.assertIn("全集合核对", skill)
        self.assertIn("账仓", readme)
        self.assertIn("macro_observations", skill)
        self.assertIn("OKX_ROOT", readme)
        self.assertNotIn("当前生产固定根", skill)

    def test_tests_and_release_scope_describe_current_state(self):
        skill = read("skill.md")
        readme = read("README.md")
        public_release = read("PUBLIC_RELEASE.md")
        self.assertNotIn("项目当前没有完整 `tests/` 目录", skill)
        self.assertIn("不是完整 money-path", skill)
        self.assertIn("不代表完整 money-path", readme)
        self.assertIn("complete money-path", public_release)
        self.assertNotIn("openclaw_host_upgrade_runbook.md", checker_text := read("docs/README.md"))
        self.assertNotIn("quality-optimization-plan", checker_text)

    def test_push_pipeline_archive_precedes_send(self):
        source = read("scripts/push_pipeline.py")
        archive_call = source.index(r'r".\scripts\push_archive.py"')
        send_call = source.index(r'r".\scripts\qq_push.py"')
        self.assertLess(archive_call, send_call)


if __name__ == "__main__":
    unittest.main()
