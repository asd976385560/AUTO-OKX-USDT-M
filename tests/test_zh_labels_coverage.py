# -*- coding: utf-8 -*-
"""中文标签表收录完整性契约（2026-08-21 告警/战报中文化批次）。

约束：告警面会出现的 failure_kind / autoheal kind / collection status
必须在 scripts/_zh_labels.py 有中文收录；failure_kind 译法与报告工件
FAILURE_ZH 孪生一致。一律用 AST/可执行形式比对——本仓注释保留
「曾经有什么」，grep 子串会撞注释，禁用。
"""
import ast
import importlib.util
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _zh_labels  # noqa: E402

_KIND_KEY_NAMES = {"failure_kind", "upstream_failure_kind"}
_KIND_LITERAL_RE = re.compile(r"^[a-z][a-z0-9_]{3,}$")


def _load_by_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _literal_assignment(path: Path, name: str):
    # 同 test_acceptance_threshold_migration 的孪生比对手法。
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    matches = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                matches.append(ast.literal_eval(node.value))
    if len(matches) != 1:
        raise AssertionError(f"expected one assignment for {name} in {path}")
    return matches[0]


def _collect_failure_kind_literals(path: Path,
                                   also_kind_assign: bool = False) -> set:
    """收集一个文件里所有 failure_kind 位置的字符串字面量。

    覆盖三种可执行形态：dict 字面量 {"failure_kind"/"upstream_failure_kind":
    <expr>}、failure_kind = <expr> 赋值、obj["failure_kind"] = <expr> 赋值；
    stage_failure_contract 里规范化变量名为 kind，用 also_kind_assign 收编。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = set()

    def _harvest(expr):
        for sub in ast.walk(expr):
            if (isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                    and sub.value not in _KIND_KEY_NAMES
                    and _KIND_LITERAL_RE.fullmatch(sub.value)):
                found.add(sub.value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant)
                        and key.value in _KIND_KEY_NAMES):
                    _harvest(value)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                named = (isinstance(target, ast.Name)
                         and (target.id == "failure_kind"
                              or (also_kind_assign and target.id == "kind")))
                keyed = (isinstance(target, ast.Subscript)
                         and isinstance(target.slice, ast.Constant)
                         and target.slice.value == "failure_kind")
                if named or keyed:
                    _harvest(node.value)
    return found


def _collect_autoheal_kinds(path: Path) -> set:
    """收集 ledger_autoheal.py 中 {"kind": …} 的字符串字面量（含兜底）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and key.value == "kind"):
                continue
            for sub in ast.walk(value):
                if (isinstance(sub, ast.Constant)
                        and isinstance(sub.value, str)
                        and sub.value and sub.value != "kind"):
                    found.add(sub.value)
    return found


class FailureKindCoverageTests(unittest.TestCase):
    def test_stage_taxonomy_fully_mapped(self):
        observed = set()
        observed |= _collect_failure_kind_literals(SCRIPTS / "stage_runner.py")
        observed |= _collect_failure_kind_literals(
            SCRIPTS / "stage_failure_contract.py", also_kind_assign=True)
        observed |= _collect_failure_kind_literals(
            SCRIPTS / "build_push_payload.py")
        self.assertTrue(observed, "collector 必须能找到 stage 失败分类字面量")
        missing = observed - set(_zh_labels.FAILURE_KIND_ZH)
        self.assertFalse(
            missing, f"failure_kind 缺中文收录: {sorted(missing)}")

    def test_artifact_failure_translations_twin(self):
        artifact = (ROOT / "reports" / "quality"
                    / "optimization-v3-20260820-1343" / "current-2337"
                    / "build_report_artifact.py")
        if not artifact.exists():
            self.skipTest("chinese-report 轮工件生成器已退役")
        twin = _literal_assignment(artifact, "FAILURE_ZH")
        for key, zh in twin.items():
            self.assertEqual(
                _zh_labels.FAILURE_KIND_ZH.get(key), zh,
                f"{key} 译法与报告工件 FAILURE_ZH 不一致")

    def test_failure_kind_zh_lookup_contract(self):
        self.assertEqual(
            _zh_labels.failure_kind_zh("cycle_deadline_exceeded"),
            "周期截止超时硬止")
        self.assertEqual(_zh_labels.failure_kind_zh("nope"), "")
        self.assertEqual(_zh_labels.failure_kind_zh(None), "")


class AutohealKindCoverageTests(unittest.TestCase):
    def test_autoheal_kinds_fully_mapped(self):
        observed = _collect_autoheal_kinds(SCRIPTS / "ledger_autoheal.py")
        observed.add("UNKNOWN")  # trigger_agent._send_autoheal_p0_alert 兜底
        self.assertTrue(observed, "collector 必须能找到 autoheal kind 字面量")
        missing = observed - set(_zh_labels.AUTOHEAL_KIND_ZH)
        self.assertFalse(
            missing, f"autoheal kind 缺中文收录: {sorted(missing)}")

    def test_gloss_keeps_code_and_appends_zh(self):
        self.assertEqual(
            _zh_labels.autoheal_kind_gloss("GHOST-EXACT"),
            "GHOST-EXACT(幽灵仓·fills精确可补)")
        self.assertEqual(
            _zh_labels.autoheal_kind_gloss("NEW-KIND"), "NEW-KIND")
        self.assertEqual(
            _zh_labels.autoheal_kind_gloss(None), "UNKNOWN(未知类别)")


class CollectionStatusCoverageTests(unittest.TestCase):
    def test_ledger_status_families_fully_mapped(self):
        ledger = _load_by_path(
            "_ledger_for_zh_coverage", ROOT / "collectors" / "ledger.py")
        for status in tuple(ledger.DONE_STATUS) + tuple(ledger.FAIL_STATUS):
            self.assertNotEqual(
                _zh_labels.collection_status_zh(status), status,
                f"status={status} 未收录中文")
        for prefix in ledger.FAIL_STATUS_PREFIXES:
            self.assertTrue(
                _zh_labels.collection_status_zh(f"{prefix}(age=120s)")
                .startswith("过期"),
                f"前缀 {prefix} 未按过期处理")

    def test_stale_prefix_keeps_age_parameter(self):
        self.assertEqual(
            _zh_labels.collection_status_zh("stale(age=120s)"),
            "过期(age=120s)")

    def test_unknown_and_level_values_pass_through(self):
        self.assertEqual(_zh_labels.collection_status_zh("P0"), "P0")
        self.assertEqual(_zh_labels.collection_status_zh("weird"), "weird")
        self.assertEqual(_zh_labels.collection_status_zh(None), "")
        # 大写 STALE 是 dxy_zone 契约 token，不属于本函数调用面；防御断言
        # 它也不会被小写前缀分支改写。
        self.assertEqual(_zh_labels.collection_status_zh("STALE"), "STALE")


class DualImportIdentityTests(unittest.TestCase):
    def test_both_import_paths_serve_identical_tables(self):
        # scripts 内 `import _zh_labels` 与 collectors 内
        # `from scripts import _zh_labels` 是两个模块对象；表必须纯常量。
        by_path = _load_by_path("_zh_labels_by_path", SCRIPTS / "_zh_labels.py")
        self.assertEqual(
            by_path.FAILURE_KIND_ZH, _zh_labels.FAILURE_KIND_ZH)
        self.assertEqual(
            by_path.AUTOHEAL_KIND_ZH, _zh_labels.AUTOHEAL_KIND_ZH)
        self.assertEqual(
            by_path.COLLECTION_STATUS_ZH, _zh_labels.COLLECTION_STATUS_ZH)


if __name__ == "__main__":
    unittest.main()
