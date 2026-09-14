# -*- coding: utf-8 -*-
import time
import unittest
from unittest import mock

from core.lib import _okxorder as ox

SYMBOL = "ARB-USDT-SWAP"


def quote(*, symbol=SYMBOL, price="0.1654", age_ms=0, ts=None):
    return {"code": "0", "data": [{"instId": symbol, "instType": "SWAP", "markPx": price,
            "ts": str(int(time.time() * 1000) - age_ms) if ts is None else ts}]}


class MarkPriceReadRecoveryTests(unittest.TestCase):
    def test_wrong_instrument_is_never_used(self):
        with mock.patch.object(ox, "okx_json", return_value=quote(symbol="BTC-USDT-SWAP")), \
             mock.patch.object(ox, "_read_mark_http", side_effect=TimeoutError(), create=True):
            self.assertIsNone(ox.get_mark_price(SYMBOL, "live"))

    def test_api_error_with_price_is_not_accepted(self):
        data = quote()
        data["code"] = "50011"
        with mock.patch.object(ox, "okx_json", return_value=data), \
             mock.patch.object(ox, "_read_mark_http", side_effect=TimeoutError(), create=True):
            self.assertIsNone(ox.get_mark_price(SYMBOL, "live"))

    def test_cli_failure_gets_one_fresh_official_read(self):
        with mock.patch.object(ox, "okx_json", side_effect=TimeoutError("diagnostic must not copy secrets")) as cli, \
             mock.patch.object(ox, "_read_mark_http", return_value=quote()) as http:
            value = ox.get_mark_price(SYMBOL, "live")
        self.assertEqual(0.1654, value)
        cli.assert_called_once()
        self.assertEqual(0, cli.call_args.kwargs["retries"])
        http.assert_called_once()
        evidence = ox.get_mark_price_evidence(SYMBOL, "live")
        self.assertEqual("public_http", evidence["source"])
        self.assertTrue(evidence["recovered"])
        self.assertEqual(2, len(evidence["attempts"]))
        self.assertNotIn("secrets", str(evidence))

    def test_healthy_cli_has_no_duplicate_public_read(self):
        with mock.patch.object(ox, "okx_json", return_value=quote()) as cli, \
             mock.patch.object(ox, "_read_mark_http") as http:
            self.assertEqual(0.1654, ox.get_mark_price(SYMBOL, "live"))
        cli.assert_called_once()
        http.assert_not_called()

    def test_both_paths_failed_remain_failed_and_do_not_use_last_good_value(self):
        with mock.patch.object(ox, "okx_json", return_value=quote()):
            self.assertEqual(0.1654, ox.get_mark_price(SYMBOL, "live"))
        with mock.patch.object(ox, "okx_json", side_effect=TimeoutError()), \
             mock.patch.object(ox, "_read_mark_http", side_effect=ConnectionError()) as http:
            self.assertIsNone(ox.get_mark_price(SYMBOL, "live"))
        http.assert_called_once()
        self.assertEqual("failed", ox.get_mark_price_evidence(SYMBOL, "live")["status"])

    def test_invalid_and_stale_quotes_fail_closed_on_both_paths(self):
        bad_values = [quote(price=value) for value in ("NaN", "Infinity", 0, -1, True, None)]
        bad_values += [quote(age_ms=60_000), quote(age_ms=-60_000), quote(ts=None)]
        bad_values[-1]["data"][0].pop("ts")
        duplicate = quote()
        duplicate["data"].append(dict(duplicate["data"][0]))
        bad_values += [duplicate, {"code": "0", "data": []}]
        for data in bad_values:
            with self.subTest(data=data), mock.patch.object(ox, "okx_json", return_value=data), \
                 mock.patch.object(ox, "_read_mark_http", return_value=data):
                self.assertIsNone(ox.get_mark_price(SYMBOL, "live"))

    def test_deadline_exhaustion_stops_another_request(self):
        with mock.patch.object(ox, "okx_json", side_effect=TimeoutError()) as cli, \
             mock.patch.object(ox, "_read_mark_http") as http, \
             mock.patch.object(ox.time, "monotonic", side_effect=[0.0, 0.0, 16.0, 16.0]):
            self.assertIsNone(ox.get_mark_price(SYMBOL, "live"))
        cli.assert_called_once()
        http.assert_not_called()

    def test_late_success_is_not_accepted(self):
        with mock.patch.object(ox, "okx_json", return_value=quote()), \
             mock.patch.object(ox, "_read_mark_http") as http, \
             mock.patch.object(ox.time, "monotonic", side_effect=[0.0, 0.0, 16.0, 16.0, 16.0]):
            self.assertIsNone(ox.get_mark_price(SYMBOL, "live"))
        http.assert_not_called()

    def test_diagnostic_cannot_be_borrowed_by_another_symbol_profile_or_call(self):
        with mock.patch.object(ox, "okx_json", return_value=quote()), \
                mock.patch.object(ox.time, "monotonic", return_value=100.0):
            self.assertEqual(0.1654, ox.get_mark_price(SYMBOL, "live"))
        self.assertIsNone(ox.get_mark_price_evidence("BTC-USDT-SWAP", "live"))
        self.assertIsNone(ox.get_mark_price_evidence(SYMBOL, "demo"))
        self.assertIsNotNone(ox.get_mark_price_evidence(SYMBOL, "live", since_monotonic=100.0))
        self.assertIsNone(ox.get_mark_price_evidence(SYMBOL, "live", since_monotonic=100.1))


if __name__ == "__main__":
    unittest.main()
