# -*- coding: utf-8 -*-
"""F5③ 配套｜简报末尾的自足断言必须与实际渲染的段落一致。

那句「本简报已含本轮决策所需的主要库内数据（…）」是给 Agent 的**免查询承诺**：
读到它就不再去 sqlite3 重查所列各段。所以当某段自我声明不可用时（playbook 陈旧
超 `PLAYBOOK_MAX_AGE_DAYS` 被收起），承诺里再列它就是自相矛盾——既告诉 Agent
「playbook 我给你了，别查」，又在段里说「本轮不展示条目」。这正是 F5 要消灭的
那类伪信息，只是换了个位置。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import decision_briefing as db  # noqa: E402


class SelfSufficiencyClaimTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(db._SECTION_STATE)
        db._SECTION_STATE.clear()

    def tearDown(self):
        db._SECTION_STATE.clear()
        db._SECTION_STATE.update(self._saved)

    def _claim(self) -> str:
        """复算断言串（与 decision_briefing 末尾同一组装规则）。"""
        covered = ["宏观", "行情", "技术面", "衍生品", "情绪", "关键新闻", "持仓"]
        if db._SECTION_STATE.get("playbook_shown", True):
            covered.append("playbook")
        covered += ["历史表现", "教训"]
        return "/".join(covered)

    def test_playbook_listed_when_section_rendered(self):
        """默认（无收起标记）仍列 playbook —— 回填刷新后自然复活。"""
        self.assertIn("playbook", self._claim())

    def test_playbook_dropped_when_section_collapsed(self):
        db._SECTION_STATE["playbook_shown"] = False
        claim = self._claim()
        self.assertNotIn("playbook", claim)
        # 其余段一个都不能被误删。
        for name in ("宏观", "行情", "技术面", "衍生品", "情绪",
                     "关键新闻", "持仓", "历史表现", "教训"):
            self.assertIn(name, claim)

    def test_source_wires_the_flag_in_executable_form(self):
        """收起分支必须**以可执行形式**置位。

        本仓注释里刻意保留「曾经有什么」，所以用 AST 找赋值语句，
        不用子串——子串会撞上自己的注释（2026-08-09 审计教训）。
        """
        import ast
        tree = ast.parse(
            (SCRIPTS / "decision_briefing.py").read_text(encoding="utf-8"))
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "_SECTION_STATE"
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value == "playbook_shown"
                        and isinstance(node.value, ast.Constant)
                        and node.value.value is False):
                    found = True
        self.assertTrue(
            found, "找不到 _SECTION_STATE['playbook_shown'] = False 的可执行赋值")

    def test_claim_line_is_assembled_not_hardcoded(self):
        """断言串不得再硬编码 playbook —— 否则两处口径会再次分叉。"""
        src = (SCRIPTS / "decision_briefing.py").read_text(encoding="utf-8")
        self.assertNotIn(
            "关键新闻/持仓/playbook/历史表现/教训", src,
            "自足断言仍是硬编码字符串，段收起时会继续撒谎")
        self.assertIn('_SECTION_STATE.get("playbook_shown", True)', src)


if __name__ == "__main__":
    unittest.main()
