from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_release_version.py"


def load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_release_version_contract", SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


checker = load_checker()


def write_contract(root: Path, version: str, releases: list[tuple[str, str]]) -> None:
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    sections = ["# Changelog", "", "## [Unreleased]", ""]
    for release, released_on in releases:
        sections.extend([
            f"## [{release}] - {released_on}",
            "",
            "- Test release.",
            "",
        ])
    (root / "CHANGELOG.md").write_text(
        "\n".join(sections), encoding="utf-8"
    )


class ReleaseVersionContractTests(unittest.TestCase):
    def test_current_repository_contract_matches_version_source(self):
        expected = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        result = checker.validate_release_contract(ROOT)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["version"], expected)

    def test_matching_tag_passes_and_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_contract(root, "1.0.0", [("1.0.0", "2026-08-04")])
            self.assertTrue(
                checker.validate_release_contract(root, "v1.0.0")["ok"]
            )
            mismatch = checker.validate_release_contract(root, "v1.0.1")
            self.assertFalse(mismatch["ok"])
            self.assertTrue(any("does not match" in item for item in mismatch["errors"]))

    def test_noncanonical_versions_and_build_metadata_are_rejected(self):
        for value in (
            "v1.0.0",
            "01.0.0",
            "1.0",
            "1.0.0+build.1",
            "1.0.٠",
            "1.0.0-1١",
        ):
            with self.subTest(version=value), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                write_contract(root, value, [(value, "2026-08-04")])
                result = checker.validate_release_contract(root)
                self.assertFalse(result["ok"])
                self.assertTrue(any("VERSION" in item for item in result["errors"]))

    def test_version_file_rejects_multiple_trailing_blank_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_contract(root, "1.0.0", [("1.0.0", "2026-08-04")])
            (root / "VERSION").write_bytes(b"1.0.0\n\n")
            result = checker.validate_release_contract(root)
            self.assertFalse(result["ok"])
            self.assertTrue(any("exactly one" in item for item in result["errors"]))

    def test_prerelease_versions_are_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_contract(
                root,
                "1.1.0-beta.1",
                [
                    ("1.1.0-beta.1", "2026-08-05"),
                    ("1.0.0", "2026-08-04"),
                ],
            )
            result = checker.validate_release_contract(root, "v1.1.0-beta.1")
            self.assertTrue(result["ok"], result["errors"])
            self.assertTrue(result["prerelease"])

    def test_changelog_current_entry_must_be_unique_newest_and_dated(self):
        cases = [
            ([("0.9.0", "2026-08-03")], "exactly one [1.0.0]"),
            (
                [("1.0.0", "2026-08-04"), ("1.0.0", "2026-08-03")],
                "exactly one [1.0.0]",
            ),
            (
                [("1.0.0", "2026-08-04"), ("1.1.0", "2026-08-05")],
                "newest-to-oldest",
            ),
            ([("1.0.0", "not-a-date")], "valid YYYY-MM-DD"),
        ]
        for releases, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                write_contract(root, "1.0.0", releases)
                result = checker.validate_release_contract(root)
                self.assertFalse(result["ok"])
                self.assertTrue(
                    any(expected in item for item in result["errors"]),
                    result["errors"],
                )

    def test_public_docs_separate_release_and_internal_versions(self):
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        for relative in ("README.md", "README.en.md", "PUBLIC_RELEASE.md"):
            with self.subTest(path=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn(version, text)
                self.assertIn("V2.0", text)
                self.assertIn("VERSION", text)
                self.assertIn("CHANGELOG.md", text)

    def test_release_workflow_is_tag_gated_and_reuses_ci(self):
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        for expected in (
            'tags:',
            '"v*.*.*"',
            "uses: ./.github/workflows/ci.yml",
            "git cat-file -t",
            "git merge-base --is-ancestor",
            "check_release_version.py --tag",
            '"release", "create"',
            "--verify-tag",
            "--generate-notes",
        ):
            self.assertIn(expected, release)
        self.assertNotIn("git tag ", release)
        self.assertIn("workflow_call:", ci)
        self.assertIn("check_release_version.py --json", ci)
        self.assertIn("a26af69be951a213d495a4c3e4e4022e16d87065", release)
        self.assertIn("a26af69be951a213d495a4c3e4e4022e16d87065", ci)


if __name__ == "__main__":
    unittest.main()
