# -*- coding: utf-8 -*-
"""okx_announcements adapter 契约回归：类型映射 / symbol 提取 / 失败隔离 / writer 落库。"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "collectors" / "sources"
COLLECTORS = ROOT / "collectors"
for _p in (str(SOURCES), str(COLLECTORS), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import news_okx_announcements as ann  # noqa: E402
import news_writer  # noqa: E402


def _make_news_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE news_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, source TEXT, hash TEXT UNIQUE,
            level TEXT CHECK(level IN ('A','B','C')),
            symbol TEXT, title TEXT, url TEXT, sentiment TEXT, raw TEXT,
            ingested_at TEXT, event_time TEXT, severity TEXT, tags TEXT
        );
        CREATE TABLE news_events_index (
            symbol TEXT, ts TEXT, news_id INTEGER,
            PRIMARY KEY (symbol, ts, news_id)
        );
        """
    )
    con.commit()
    con.close()


class ClassifyAnnTypeTests(unittest.TestCase):
    def test_wanted_types_map_to_expected_severity(self) -> None:
        self.assertEqual(
            ann.classify_ann_type("announcements-delistings"),
            ("delisting", "high", "A"))
        self.assertEqual(
            ann.classify_ann_type("announcements-new-listings"),
            ("listing", "medium", "B"))
        # 地域变体（如 trading-updates-us-aus）也按关键词命中
        self.assertEqual(
            ann.classify_ann_type("trading-updates-us-aus"),
            ("trading_update", "medium", "B"))
        self.assertEqual(
            ann.classify_ann_type("announcements-api"),
            ("api", "low", "C"))

    def test_unwanted_or_empty_types_are_skipped(self) -> None:
        self.assertIsNone(ann.classify_ann_type("announcements-p2p-trading"))
        self.assertIsNone(ann.classify_ann_type(""))
        self.assertIsNone(ann.classify_ann_type(None))


class ExtractSymbolsTests(unittest.TestCase):
    def test_pair_pattern_and_verb_pattern(self) -> None:
        self.assertEqual(
            ann.extract_symbols(
                "OKX to list GRVT/USDT (Grvt) for spot trading", "listing"),
            ["GRVT-USDT-SWAP"])
        self.assertEqual(
            ann.extract_symbols(
                "OKX will delist FTM perpetual contracts", "delisting"),
            ["FTM-USDT-SWAP"])

    def test_stopwords_and_low_confidence_titles_yield_empty(self) -> None:
        # USDT/OKX 等停用词不得被当作币
        self.assertEqual(
            ann.extract_symbols("OKX will suspend USDT deposits", "delisting"),
            [])
        # 活动类公告不提取（噪音大，宁缺勿假）
        self.assertEqual(
            ann.extract_symbols("Trade BTC/USDT to win rewards", "activity"),
            [])
        self.assertEqual(ann.extract_symbols("", "listing"), [])

    def test_item_to_news_shapes_writer_contract(self) -> None:
        item = {
            "annType": "announcements-new-listings",
            "title": "OKX to list GRVT/USDT (Grvt) for spot trading",
            "url": "https://www.okx.com/en-us/help/okx-to-list-grvt",
            "pTime": "1785380409432",
            "businessPTime": "1785380400000",
        }
        news = ann._item_to_news(item, "listing", "medium", "B")
        self.assertEqual(news["source"], "okx_announcements")
        self.assertEqual(news["severity"], "medium")
        self.assertEqual(news["level"], "B")
        self.assertEqual(news["tags"], ["okx_announcement", "listing"])
        self.assertEqual(news["symbols"], ["GRVT-USDT-SWAP"])
        # pTime(ms) → UTC+8 字符串；businessPTime 只留 raw
        self.assertRegex(news["event_time"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(news["raw"]["businessPTime"], "1785380400000")

    def test_missing_ptime_keeps_event_time_null(self) -> None:
        news = ann._item_to_news(
            {"title": "OKX system maintenance", "url": None},
            "maintenance", "medium", "B")
        self.assertIsNone(news["event_time"])


class SupportTransportFallbackTests(unittest.TestCase):
    def test_transport_failure_uses_one_exact_url_schannel_fallback(self) -> None:
        support = ann._okx_http
        httpx = support._okx_http.httpx
        context = mock.MagicMock()
        client = mock.Mock()
        context.__enter__.return_value = client
        request = httpx.Request(
            "GET", "https://openapi.okx.com/api/v5/support/announcement-types")
        client.get.side_effect = [
            httpx.ConnectError("tls eof", request=request),
            httpx.ConnectError("tls eof", request=request),
            httpx.ConnectError("tls eof", request=request),
        ]
        stats: dict = {}
        native_body = (
            '{"code":"0","data":[{"annType":'
            '"announcements-new-listings"}]}'
        )
        domains = ("https://openapi.okx.com", "https://www.okx.com")
        with (
            mock.patch.object(support, "_support_throttle"),
            mock.patch.object(support._okx_http, "_client", return_value=context),
            mock.patch.object(support._okx_http, "_BASE_URLS", domains),
            mock.patch.object(support._okx_http.time, "sleep"),
            mock.patch.object(
                support._news_http,
                "_fetch_text_schannel",
                return_value=native_body,
            ) as native,
        ):
            rows = support.fetch_support_announcement_types_sync(
                request_timeout_s=8.0,
                transport_stats=stats,
            )
        self.assertEqual(
            rows, [{"annType": "announcements-new-listings"}])
        self.assertEqual(client.get.call_count, 3)
        native.assert_called_once()
        self.assertEqual(
            native.call_args.args[0],
            "https://openapi.okx.com/api/v5/support/announcement-types",
        )
        self.assertEqual(stats["schannel_fallback_successes"], 1)

    def test_http_status_does_not_use_schannel_fallback(self) -> None:
        support = ann._okx_http
        httpx = support._okx_http.httpx
        context = mock.MagicMock()
        client = mock.Mock()
        context.__enter__.return_value = client
        request = httpx.Request(
            "GET", "https://openapi.okx.com/api/v5/support/announcement-types")
        response = mock.Mock()
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "service unavailable", request=request,
            response=httpx.Response(503, request=request),
        )
        client.get.return_value = response
        with (
            mock.patch.object(support, "_support_throttle"),
            mock.patch.object(support._okx_http, "_client", return_value=context),
            mock.patch.object(support._okx_http.time, "sleep"),
            mock.patch.object(
                support._news_http, "_fetch_text_schannel") as native,
        ):
            with self.assertRaises(RuntimeError):
                support.fetch_support_announcement_types_sync(
                    request_timeout_s=8.0,
                )
        native.assert_not_called()


class FetchAndCollectTests(unittest.TestCase):
    def _types(self):
        return [
            {"annType": "announcements-new-listings", "annTypeDesc": "New listings"},
            {"annType": "announcements-delistings", "annTypeDesc": "Delistings"},
            {"annType": "announcements-p2p-trading", "annTypeDesc": "P2P"},
        ]

    def test_single_type_failure_keeps_other_types(self) -> None:
        def pages(ann_type, page=1, request_timeout_s=None,
                  transport_stats=None):
            if "delist" in ann_type:
                raise TimeoutError("slow")
            return {"details": [{
                "annType": ann_type,
                "title": "OKX to list AEON/USDT for spot trading",
                "url": "https://www.okx.com/help/x",
                "pTime": "1785121210459",
            }], "totalPage": "1"}

        errors: list[str] = []
        with mock.patch.object(
            ann._okx_http, "fetch_support_announcement_types_sync",
            return_value=self._types(),
        ), mock.patch.object(
            ann._okx_http, "fetch_support_announcements_sync",
            side_effect=pages,
        ), mock.patch.object(ann.time, "sleep"):
            items = ann.fetch_items(errors=errors)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["symbols"], ["AEON-USDT-SWAP"])
        self.assertTrue(errors and "delist" in errors[0])

    def test_types_endpoint_failure_returns_failed_result(self) -> None:
        with mock.patch.object(
            ann._okx_http, "fetch_support_announcement_types_sync",
            side_effect=RuntimeError("okx GET failed"),
        ), mock.patch.object(ann.time, "sleep"):
            res = ann.collect("unused-db-path", apply=False)
        self.assertFalse(res["ok"])
        self.assertEqual(res["fetched"], 0)
        self.assertIn("types:", res["err"])

    def test_types_endpoint_recovers_with_one_cold_new_call(self) -> None:
        stats: dict = {}
        with mock.patch.object(
            ann._okx_http, "fetch_support_announcement_types_sync",
            side_effect=[RuntimeError("tls eof"), self._types()],
        ) as types_fetch, mock.patch.object(
            ann._okx_http, "fetch_support_announcements_sync",
            return_value={"details": [], "totalPage": "1"},
        ), mock.patch.object(ann.time, "sleep") as sleep:
            items = ann.fetch_items(errors=[], retry_stats=stats)
        self.assertEqual(items, [])
        self.assertEqual(types_fetch.call_count, 2)
        self.assertEqual(stats["types_attempts"], 2)
        self.assertTrue(stats["types_recovered_after_cold_retry"])
        self.assertEqual(stats["final_failed"], 0)
        self.assertEqual(stats["maximum_network_budget_seconds"], 75.0)
        self.assertFalse(stats["historical_retry"])
        self.assertFalse(stats["unbounded_retry"])
        sleep.assert_called_once_with(3.0)

    def test_single_category_recovers_in_exact_cold_wave(self) -> None:
        calls: list[str] = []

        def pages(ann_type, page=1, request_timeout_s=None,
                  transport_stats=None):
            calls.append(ann_type)
            if "delist" in ann_type and calls.count(ann_type) == 1:
                raise TimeoutError("tls eof")
            return {"details": [], "totalPage": "1"}

        stats: dict = {}
        with mock.patch.object(
            ann._okx_http, "fetch_support_announcement_types_sync",
            return_value=self._types(),
        ), mock.patch.object(
            ann._okx_http, "fetch_support_announcements_sync",
            side_effect=pages,
        ), mock.patch.object(ann.time, "sleep") as sleep:
            ann.fetch_items(errors=[], retry_stats=stats)
        delist = "announcements-delistings"
        listing = "announcements-new-listings"
        self.assertEqual(calls, [listing, delist, delist])
        self.assertEqual(stats["category_initial_failed"], 1)
        self.assertEqual(stats["category_recovered_after_cold_retry"], 1)
        self.assertEqual(stats["final_failed"], 0)
        self.assertEqual(stats["maximum_network_budget_seconds"], 75.0)
        sleep.assert_called_once_with(3.0)

    def test_budget_exhaustion_does_not_issue_cold_category_request(self) -> None:
        ticks = iter([0.0, 0.0, 0.1, 75.1, 75.1, 75.1, 75.1])
        errors: list[str] = []
        stats: dict = {}
        with mock.patch.object(
            ann.time, "monotonic", side_effect=lambda: next(ticks, 75.1),
        ), mock.patch.object(
            ann.time, "sleep",
        ), mock.patch.object(
            ann._okx_http, "fetch_support_announcement_types_sync",
            return_value=self._types(),
        ), mock.patch.object(
            ann._okx_http, "fetch_support_announcements_sync",
            side_effect=TimeoutError("initial timeout"),
        ) as pages:
            ann.fetch_items(errors=errors, retry_stats=stats)
        self.assertEqual(pages.call_count, 1)
        self.assertGreaterEqual(stats["final_failed"], 1)
        self.assertTrue(any("total budget exhausted" in error
                            for error in errors))

    def test_collect_apply_writes_through_news_writer_and_dedupes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "news.db"
            _make_news_db(db)
            details = [{
                "annType": "announcements-delistings",
                "title": "OKX will delist FTM perpetual contracts (Aug 20)",
                "url": "https://www.okx.com/help/delist-ftm",
                "pTime": "1785121210459",
            }]
            with mock.patch.object(
                ann._okx_http, "fetch_support_announcement_types_sync",
                return_value=self._types(),
            ), mock.patch.object(
                ann._okx_http, "fetch_support_announcements_sync",
                side_effect=lambda t, page=1, request_timeout_s=None,
                                   transport_stats=None: {
                    "details": list(details) if "delist" in t else [],
                    "totalPage": "1",
                },
            ):
                first = ann.collect(str(db), apply=True)
                second = ann.collect(str(db), apply=True)

            self.assertTrue(first["ok"])
            self.assertEqual(first["inserted"], 1)
            self.assertTrue(second["ok"])
            self.assertEqual(second["inserted"], 0)
            self.assertEqual(second["deduped"], 1)

            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT source, severity, level, symbol, tags, event_time "
                "FROM news_items").fetchone()
            idx = con.execute(
                "SELECT symbol FROM news_events_index").fetchall()
            con.close()
            self.assertEqual(row["source"], "okx_announcements")
            self.assertEqual(row["severity"], "high")
            self.assertEqual(row["level"], "A")
            self.assertEqual(row["symbol"], "FTM-USDT-SWAP")
            self.assertIn("delisting", row["tags"])
            self.assertEqual(idx[0]["symbol"], "FTM-USDT-SWAP")

    def test_registry_lists_adapter_and_news_collect_can_load_it(self) -> None:
        import json as _json
        reg = _json.loads(
            (SOURCES / "registry.json").read_text(encoding="utf-8"))
        entry = next(
            s for s in reg["sources"] if s["id"] == "okx_announcements")
        self.assertEqual(entry["adapter"], "news_okx_announcements")
        self.assertFalse(entry["required"])
        self.assertTrue(entry["enabled"])
        self.assertTrue(
            (SOURCES / f"{entry['adapter']}.py").exists(),
            "adapter module must exist for news_collect to iterate it")
        self.assertTrue(hasattr(ann, "collect"))


if __name__ == "__main__":
    unittest.main()
