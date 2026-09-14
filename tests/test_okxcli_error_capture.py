# -*- coding: utf-8 -*-
"""_okxcli failure capture (2026-09-12): no CLI update notice, no silently lost stream.

Offline: subprocess.run is mocked; no exchange calls.
"""
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for folder in ("scripts", "core/lib"):
    sys.path.insert(0, str(ROOT / folder))
import _okxcli as cli  # noqa: E402
import _okxorder as ox  # noqa: E402

NOTICE = ("\nUpdate available for @okx_ai/okx-trade-cli: 1.4.4 -> 1.4.6\n"
          "Run: npm install -g @okx_ai/okx-trade-cli\n\n")
REJECT_BODY = ('{"code":"1","msg":"All operations failed","data":[{"algoId":"",'
               '"sCode":"51279","sMsg":"TP trigger price cannot be lower than the last price"}]}')


def proc(rc=0, out="[]", err=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


class CliEnvTests(unittest.TestCase):
    def test_update_check_is_forced_off_and_proxies_still_stripped(self):
        with mock.patch.dict(cli.os.environ, {"OKX_UPDATE_CHECK": "true",
                                              "HTTPS_PROXY": "http://127.0.0.1:1"}):
            env = cli._subprocess_env()
        self.assertEqual("false", env["OKX_UPDATE_CHECK"])
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertEqual("*", env["NO_PROXY"])


class FailureStreamTests(unittest.TestCase):
    def setUp(self):
        for patch in (mock.patch.object(cli, "_base_cmd", return_value=["node", "offline-cli"]),
                      mock.patch.object(cli, "_throttle")):
            patch.start()
            self.addCleanup(patch.stop)

    def _fail(self, out, err):
        with mock.patch.object(cli.subprocess, "run", return_value=proc(1, out, err)) as run, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as diag:
            with self.assertRaises(RuntimeError) as ctx:
                cli.okx_json("swap", "algo", "place", "--instId", "X-USDT-SWAP")
        self.assertEqual(1, run.call_count)
        return ctx.exception, diag.getvalue()

    def test_dropped_stdout_is_kept_without_changing_the_message(self):
        exc, diag = self._fail(REJECT_BODY, NOTICE)
        self.assertEqual(f"okx CLI rc=1 after 1 attempts: {NOTICE.strip()}", str(exc))
        self.assertIn('"sCode":"51279"', exc.cli_stdout)
        self.assertIn("Update available", exc.cli_stderr)
        self.assertIn("[okx-cli-error] command=swap/algo/place rc=1", diag)
        self.assertIn("51279", diag)

    def test_business_code_and_s2b_ambiguity_still_come_from_the_message_only(self):
        with mock.patch.object(cli.subprocess, "run", return_value=proc(1, REJECT_BODY, NOTICE)), \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            res = ox._call("swap", "algo", "place", profile="live")
        self.assertFalse(res["ok"])
        self.assertIsNone(res["sCode"])
        self.assertNotIn("51279", res["error"])
        with mock.patch.object(cli.subprocess, "run", return_value=proc(1, REJECT_BODY, "")):
            self.assertEqual("51279", ox._call("swap", "algo", "place", profile="live")["sCode"])

    def test_single_stream_failures_keep_their_old_text_and_print_nothing(self):
        for out, err, expected in (("", "Error: network error", "Error: network error"),
                                   (REJECT_BODY, "", REJECT_BODY)):
            with self.subTest(out=bool(out), err=bool(err)):
                exc, diag = self._fail(out, err)
                self.assertEqual(f"okx CLI rc=1 after 1 attempts: {expected}", str(exc))
                self.assertEqual("", diag)

    def test_captured_streams_are_bounded(self):
        exc, diag = self._fail("x" * 5000, "y" * 5000)
        self.assertEqual(600, len(exc.cli_stdout))
        self.assertEqual(600, len(exc.cli_stderr))
        self.assertLess(len(diag), 900)


if __name__ == "__main__":
    unittest.main()
