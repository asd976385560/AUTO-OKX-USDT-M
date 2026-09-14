# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = ROOT / "collectors"
if str(COLLECTORS) not in sys.path:
    sys.path.insert(0, str(COLLECTORS))

import trades_writer  # noqa: E402


class CtValUnknownTests(unittest.TestCase):
    def test_missing_ctval_stays_unknown_instead_of_falling_back_to_one(self):
        with tempfile.TemporaryDirectory() as td:
            market = Path(td) / "market.db"
            with closing(sqlite3.connect(market)) as con:
                con.execute(
                    "CREATE TABLE instruments_cache(instId TEXT,ctVal REAL)")
                con.commit()
            trades_writer._CTVAL_CACHE.clear()
            with mock.patch.dict(
                os.environ, {"OKX_MARKET_DB": str(market)}, clear=False):
                value = trades_writer._ctval_for("UNKNOWN-USDT-SWAP")
        self.assertIsNone(value)
        self.assertEqual({}, trades_writer._CTVAL_CACHE)


if __name__ == "__main__":
    unittest.main()
