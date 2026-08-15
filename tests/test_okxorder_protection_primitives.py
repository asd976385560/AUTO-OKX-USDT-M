# -*- coding: utf-8 -*-
"""保护单原语（amend/cancel/TP）命令装配回归（2026-08-13）。

断言下发的 CLI 参数逐字匹配 OKX CLI 1.4.2 `swap algo --help`（主人 2026-08-13 核实）：
  amend  --instId <id> --algoId <id> [--newSz] [--newSlTriggerPx] [--newSlOrdPx] [--newTpTriggerPx] [--newTpOrdPx]
  cancel --instId <id> --algoId <id>
  place  --ordType conditional --tpTriggerPx ... --tpOrdPx ... --tpTriggerPxType ...

重点守两条历史坑：① `-1` 值必须走 `--flag=-1` 等号形式（空格分隔会被 commander.js
当成另一个 flag，2026-07-02 曾让带 SL 的下单全部失败）；② DRYRUN 下变更类命令绝不真发。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "core" / "lib"), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _okxorder as ox  # noqa: E402


class _Captured:
    def __init__(self):
        self.args = None
        self.kwargs = None

    def __call__(self, *args, **kwargs):
        self.args = list(args)
        self.kwargs = kwargs
        return {"code": "0", "data": [{"sCode": "0", "algoId": "A1"}]}


class AmendCommandTests(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict("os.environ", {"OKX_EXECUTOR_DRYRUN": "0"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_amend_sl_only_matches_cli_help(self) -> None:
        cap = _Captured()
        with mock.patch.object(ox, "okx_json", cap):
            r = ox.amend_algo_protection(
                "LINK-USDT-SWAP", "A1", "live", new_sl_trigger_px=8.60)
        self.assertTrue(r["ok"])
        self.assertEqual(cap.args, [
            "swap", "algo", "amend", "--instId", "LINK-USDT-SWAP",
            "--algoId", "A1", "--newSlTriggerPx", "8.6", "--newSlOrdPx=-1"])
        self.assertEqual(cap.kwargs["global_args"], ["--profile", "live"])

    def test_amend_resize_and_tp_together(self) -> None:
        cap = _Captured()
        with mock.patch.object(ox, "okx_json", cap):
            ox.amend_algo_protection(
                "CRV-USDT-SWAP", "A2", "live",
                new_sl_trigger_px=0.27, new_tp_trigger_px=0.31, new_sz=7800)
        # newSz 必须排在触发价之前（与 help 的参数顺序一致，便于人工核对日志）
        self.assertEqual(cap.args, [
            "swap", "algo", "amend", "--instId", "CRV-USDT-SWAP",
            "--algoId", "A2", "--newSz", "7800",
            "--newSlTriggerPx", "0.27", "--newSlOrdPx=-1",
            "--newTpTriggerPx", "0.31", "--newTpOrdPx=-1"])

    def test_ord_px_minus_one_uses_equals_form(self) -> None:
        """-1 必须是 `--newSlOrdPx=-1`，绝不能是 ['--newSlOrdPx', '-1']。"""
        cap = _Captured()
        with mock.patch.object(ox, "okx_json", cap):
            ox.amend_algo_protection("X-USDT-SWAP", "A3", "live",
                                     new_sl_trigger_px=1.0)
        self.assertIn("--newSlOrdPx=-1", cap.args)
        self.assertNotIn("-1", cap.args)  # 裸 -1 不得作为独立 token 出现

    def test_empty_amend_is_refused_without_calling_cli(self) -> None:
        cap = _Captured()
        with mock.patch.object(ox, "okx_json", cap):
            r = ox.amend_algo_protection("X-USDT-SWAP", "A4", "live")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "amend_no_change_requested")
        self.assertIsNone(cap.args)  # 空改单绝不发命令

    def test_cli_failure_does_not_raise(self) -> None:
        def boom(*a, **k):
            raise RuntimeError("okx exited 1: 51277 slTriggerPx invalid")
        with mock.patch.object(ox, "okx_json", boom):
            r = ox.amend_algo_protection("X-USDT-SWAP", "A5", "live",
                                         new_sl_trigger_px=1.0)
        self.assertFalse(r["ok"])
        self.assertEqual(r["sCode"], "51277")  # sCode 抠出来供调用方分支


class CancelCommandTests(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict("os.environ", {"OKX_EXECUTOR_DRYRUN": "0"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_cancel_matches_cli_help(self) -> None:
        cap = _Captured()
        with mock.patch.object(ox, "okx_json", cap):
            r = ox.cancel_algo_order("LINK-USDT-SWAP", "A9", "live")
        self.assertTrue(r["ok"])
        self.assertEqual(cap.args, [
            "swap", "algo", "cancel", "--instId", "LINK-USDT-SWAP",
            "--algoId", "A9"])


class PlaceTpCommandTests(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict("os.environ", {"OKX_EXECUTOR_DRYRUN": "0"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_tp_algo_mirrors_sl_shape_with_close_side(self) -> None:
        cap = _Captured()
        with mock.patch.object(ox, "okx_json", cap):
            ox.place_algo_tp("LINK-USDT-SWAP", "long", 100, 9.50, "live")
        self.assertEqual(cap.args, [
            "swap", "algo", "place", "--instId", "LINK-USDT-SWAP",
            "--side", "sell", "--sz", "100", "--ordType", "conditional",
            "--tpTriggerPx", "9.5", "--tpOrdPx=-1",
            "--tpTriggerPxType", "mark", "--posSide", "long",
            "--tdMode", "cross", "--reduceOnly"])

    def test_short_position_closes_with_buy(self) -> None:
        cap = _Captured()
        with mock.patch.object(ox, "okx_json", cap):
            ox.place_algo_tp("X-USDT-SWAP", "short", 10, 1.0, "live")
        self.assertEqual(cap.args[5:7], ["--side", "buy"])
        self.assertIn("--reduceOnly", cap.args)

    def test_combined_replacement_uses_oco_not_conditional(self) -> None:
        cap = _Captured()
        with mock.patch.object(ox, "okx_json", cap):
            ox.place_algo_protection(
                "LINK-USDT-SWAP", "long", 100, 8.25, "live",
                tp_trigger_px=9.50)
        index = cap.args.index("--ordType")
        self.assertEqual(cap.args[index + 1], "oco")
        self.assertIn("--slTriggerPx", cap.args)
        self.assertIn("--tpTriggerPx", cap.args)


class DryrunTests(unittest.TestCase):
    def test_mutations_never_reach_cli_in_dryrun(self) -> None:
        def boom(*a, **k):
            raise AssertionError("DRYRUN 下不得发出变更命令")
        with mock.patch.dict("os.environ", {"OKX_EXECUTOR_DRYRUN": "1"}), \
                mock.patch.object(ox, "okx_json", boom):
            self.assertTrue(ox.amend_algo_protection(
                "X-USDT-SWAP", "A1", "live", new_sl_trigger_px=1.0)["dryrun"])
            self.assertTrue(ox.cancel_algo_order(
                "X-USDT-SWAP", "A1", "live")["dryrun"])
            self.assertTrue(ox.place_algo_tp(
                "X-USDT-SWAP", "long", 1, 2.0, "live")["dryrun"])
            self.assertTrue(ox.place_algo_protection(
                "X-USDT-SWAP", "long", 1, 0.8, "live",
                tp_trigger_px=1.2)["dryrun"])

    def test_dryrun_amend_echoes_requested_change(self) -> None:
        with mock.patch.dict("os.environ", {"OKX_EXECUTOR_DRYRUN": "1"}):
            r = ox.amend_algo_protection("X-USDT-SWAP", "A1", "live",
                                         new_sl_trigger_px=8.6, new_sz=200)
        self.assertEqual(r["amended"], {"sl": 8.6, "tp": None, "sz": 200})


if __name__ == "__main__":
    unittest.main()
