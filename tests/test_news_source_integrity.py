from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "collectors" / "sources"
COLLECTORS = ROOT / "collectors"
for path in (SOURCES, COLLECTORS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import news_collect  # noqa: E402
import news_geo  # noqa: E402
import news_mxsearch  # noqa: E402
import _mx_news_common as mx_common  # noqa: E402
import _news_http  # noqa: E402
import news_jinse  # noqa: E402
import news_odaily  # noqa: E402
import news_okx  # noqa: E402
import news_panews  # noqa: E402
import news_rss  # noqa: E402
import news_writer  # noqa: E402


class NewsSourceIntegrityTests(unittest.TestCase):
    def test_schannel_transport_keeps_proxy_tls_and_hard_timeout(self) -> None:
        completed = mock.Mock(returncode=0, stdout=b"<rss />", stderr=b"")
        with mock.patch.object(
            _news_http, "_native_curl_path", return_value=r"C:\Windows\System32\curl.exe",
        ), mock.patch.object(
            _news_http.subprocess, "run", return_value=completed,
        ) as run:
            text = _news_http._fetch_text_schannel(
                "https://publisher.example/feed",
                timeout=2.5,
                headers={"Accept": "application/rss+xml"},
                proxy="http://127.0.0.1:10080",
            )

        self.assertEqual(text, "<rss />")
        args = run.call_args.args[0]
        self.assertEqual(
            args[args.index("--proxy") + 1], "http://127.0.0.1:10080")
        self.assertNotIn("--insecure", args)
        self.assertEqual(run.call_args.kwargs["timeout"], 2.5)

    def test_alternate_http_uses_schannel_within_original_budget(self) -> None:
        request = _news_http.httpx.Request(
            "GET", "https://publisher.example/feed")
        client = mock.MagicMock()
        client.__enter__.return_value.get.side_effect = (
            _news_http.httpx.ConnectError("tls eof", request=request))
        with mock.patch.object(
            _news_http.httpx, "Client", return_value=client,
        ), mock.patch.object(
            _news_http.time, "monotonic", side_effect=[100.0, 100.25],
        ), mock.patch.object(
            _news_http, "_fetch_text_schannel", return_value="<rss />",
        ) as schannel:
            text = _news_http.fetch_text(
                "https://publisher.example/feed",
                timeout=4.0,
                headers={"Accept": "application/rss+xml"},
            )

        self.assertEqual(text, "<rss />")
        self.assertAlmostEqual(
            schannel.call_args.kwargs["timeout"], 3.75, places=6)
        self.assertEqual(
            schannel.call_args.kwargs["headers"],
            {"Accept": "application/rss+xml"},
        )
        self.assertEqual(
            schannel.call_args.kwargs["proxy"],
            _news_http.os.environ.get("OKX_PROXY_URL")
            or _news_http.os.environ.get("HTTPS_PROXY")
            or _news_http.os.environ.get("HTTP_PROXY"),
        )

    def test_alternate_http_does_not_retry_authoritative_http_status(self) -> None:
        request = _news_http.httpx.Request(
            "GET", "https://publisher.example/feed")
        response = _news_http.httpx.Response(503, request=request)
        client = mock.MagicMock()
        client.__enter__.return_value.get.return_value = response
        with mock.patch.object(
            _news_http.httpx, "Client", return_value=client,
        ), mock.patch.object(
            _news_http, "_fetch_text_schannel",
        ) as schannel:
            with self.assertRaises(_news_http.httpx.HTTPStatusError):
                _news_http.fetch_text(
                    "https://publisher.example/feed", timeout=4.0)
        schannel.assert_not_called()

    def test_news_adapters_import_through_project_package(self) -> None:
        from collectors.sources import news_panews as packaged_panews
        from collectors.sources import news_rss as packaged_rss

        self.assertTrue(callable(packaged_rss._fetch_text_httpx))
        self.assertTrue(callable(packaged_panews._fetch_text_httpx))

    def test_partial_adapter_error_is_degraded(self) -> None:
        self.assertEqual(
            news_collect._source_result_status(
                {"ok": True, "err": "one feed failed"}, 12),
            "degraded",
        )
        self.assertEqual(
            news_collect._source_result_status({"ok": True}, 12), "ok")
        self.assertEqual(
            news_collect._source_result_status({"ok": True}, 0), "ok")
        self.assertEqual(
            news_collect._source_result_status({"ok": False}, 12), "failed")

    def test_module_missing_is_failed_not_legal_cadence_skip(self) -> None:
        source = {"id": "broken_news", "adapter": "missing_adapter"}
        with (
            mock.patch.object(news_collect._registry, "load_registry",
                              return_value={"sources": [source]}),
            mock.patch.object(news_collect._registry, "enabled_sources",
                              return_value=[source]),
            mock.patch.object(news_collect, "_source_due", return_value=True),
            mock.patch.object(news_collect, "_load_adapter", return_value=None),
        ):
            result = news_collect.collect_all(r"E:\isolated", apply=False)
        self.assertEqual("failed", result["sources"][0]["status"])
        self.assertEqual("module-missing", result["sources"][0]["err"])

    def test_news_writer_rejects_empty_source_before_database_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "news.db"
            db_path.touch()
            with mock.patch.object(news_writer.ledger, "connect") as connect:
                result = news_writer.write_news(
                    [{"source": "  ", "title": "BTC update"}], db_path)
        self.assertFalse(result["ok"])
        self.assertEqual("news_source_required", result["error"])
        self.assertEqual([0], result["invalid_source_indices"])
        connect.assert_not_called()

    def test_news_writer_does_not_enforce_strict_source_enum(self) -> None:
        item = news_writer.normalize_item({
            "source": "custom:future_source", "title": "BTC update"})
        self.assertEqual("custom:future_source", item["source"])

    def test_multi_feed_outcomes_preserve_success_and_failure(self) -> None:
        xml = "<rss><channel><item><title>BTC update</title></item></channel></rss>"
        errors: list[str] = []
        outcomes: list[dict] = []
        feeds = [
            ("Good", "https://good.example/feed"),
            ("Bad", "https://bad.example/feed"),
        ]

        with mock.patch.object(
            news_rss, "_fetch",
            side_effect=[
                xml,
                urllib.error.URLError("blocked"),
                urllib.error.URLError("still blocked"),
            ],
        ), mock.patch.object(
            news_rss, "_fetch_text_httpx",
            side_effect=TimeoutError("alternate blocked"),
        ), mock.patch.object(news_rss.time, "sleep"):
            items = news_rss.fetch_rss_items(
                feeds=feeds, errors=errors, outcomes=outcomes)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "rss:good")
        self.assertEqual([row["status"] for row in outcomes], ["ok", "failed"])
        self.assertEqual(outcomes[1]["id"], "rss:bad")
        self.assertEqual(outcomes[1]["attempts"], 3)
        self.assertTrue(outcomes[1]["cold_retry_requested"])
        self.assertFalse(outcomes[1]["historical_retry"])
        self.assertFalse(outcomes[1]["unbounded_retry"])
        self.assertIn("hot=", outcomes[1]["err"])
        self.assertIn("cold=", outcomes[1]["err"])
        self.assertTrue(outcomes[1]["pre_cold_error"])
        self.assertTrue(outcomes[1]["cold_retry_error"])
        self.assertTrue(errors)

    def test_failed_rss_recovers_in_exact_delayed_cold_wave(self) -> None:
        xml = "<rss><channel><item><title>BTC update</title></item></channel></rss>"
        outcomes: list[dict] = []
        with mock.patch.object(
            news_rss, "_fetch", side_effect=urllib.error.URLError("tls eof"),
        ), mock.patch.object(
            news_rss, "_fetch_text_httpx",
            side_effect=[TimeoutError("hot eof 1"),
                         TimeoutError("hot eof 2"), xml],
        ) as httpx_fetch, mock.patch.object(
            news_rss.time, "sleep",
        ) as sleep:
            items = news_rss.fetch_rss_items(
                feeds=[("Decrypt", "https://decrypt.co/feed")],
                outcomes=outcomes,
            )

        self.assertEqual(len(items), 1)
        self.assertEqual(httpx_fetch.call_count, 3)
        self.assertEqual(outcomes[0]["status"], "ok")
        self.assertEqual(outcomes[0]["attempts"], 3)
        self.assertEqual(outcomes[0]["transport_attempts"], 5)
        self.assertTrue(outcomes[0]["recovered_after_cold_retry"])
        self.assertEqual(outcomes[0]["transport"], "httpx_cold_new_client")
        self.assertFalse(outcomes[0]["historical_retry"])
        self.assertFalse(outcomes[0]["unbounded_retry"])
        self.assertTrue(any(
            call.args and call.args[0] ==
            news_rss.RSS_COLD_RETRY_DELAY_SECONDS
            for call in sleep.call_args_list
        ))

    def test_failed_rss_urllib_recovers_via_same_url_httpx(self) -> None:
        xml = "<rss><channel><item><title>BTC update</title></item></channel></rss>"
        outcomes: list[dict] = []
        with mock.patch.object(
            news_rss, "_fetch", side_effect=urllib.error.URLError("tls eof"),
        ), mock.patch.object(
            news_rss, "_fetch_text_httpx", return_value=xml,
        ), mock.patch.object(news_rss.time, "sleep"):
            items = news_rss.fetch_rss_items(
                feeds=[("Decrypt", "https://decrypt.co/feed")],
                outcomes=outcomes,
            )

        self.assertEqual(len(items), 1)
        self.assertEqual(outcomes[0]["status"], "ok")
        self.assertEqual(outcomes[0]["transport"], "httpx")
        self.assertEqual(outcomes[0]["transport_attempts"], 3)

    def test_failed_rss_first_httpx_recovers_on_second_same_url_attempt(self) -> None:
        xml = "<rss><channel><item><title>BTC update</title></item></channel></rss>"
        outcomes: list[dict] = []
        with mock.patch.object(
            news_rss, "_fetch", side_effect=urllib.error.URLError("tls eof"),
        ), mock.patch.object(
            news_rss, "_fetch_text_httpx",
            side_effect=[TimeoutError("transient eof"), xml],
        ) as httpx_fetch, mock.patch.object(news_rss.time, "sleep"):
            items = news_rss.fetch_rss_items(
                feeds=[("Cointelegraph", "https://cointelegraph.com/rss")],
                outcomes=outcomes,
            )

        self.assertEqual(len(items), 1)
        self.assertEqual(httpx_fetch.call_count, 2)
        self.assertEqual(outcomes[0]["status"], "ok")
        self.assertEqual(outcomes[0]["transport"], "httpx")
        self.assertEqual(outcomes[0]["transport_attempts"], 4)

    def test_bitcoinist_recovers_via_same_publisher_rest_fallback(self) -> None:
        payload = json.dumps([{
            "id": 700269,
            "date_gmt": news_rss.datetime.now(
                news_rss.timezone.utc).replace(tzinfo=None).isoformat(
                    timespec="seconds"),
            "link": "https://bitcoinist.com/bnb-chain-example/",
            "title": {"rendered": "BNB Chain &amp; BTC Update"},
        }])
        outcomes: list[dict] = []
        with mock.patch.object(
            news_rss, "_fetch", side_effect=urllib.error.URLError("tls eof"),
        ), mock.patch.object(
            news_rss,
            "_fetch_text_httpx",
            side_effect=[
                TimeoutError("feed tls eof 1"),
                TimeoutError("feed tls eof 2"),
                payload,
            ],
        ), mock.patch.object(news_rss.time, "sleep"):
            items = news_rss.fetch_rss_items(
                feeds=[("Bitcoinist", "https://bitcoinist.com/feed/")],
                outcomes=outcomes,
            )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "BNB Chain & BTC Update")
        self.assertEqual(items[0]["raw"]["post_id"], 700269)
        self.assertEqual(outcomes[0]["status"], "ok")
        self.assertEqual(outcomes[0]["transport"], "official_wordpress_rest")
        self.assertTrue(outcomes[0]["recovered_after_publisher_fallback"])

    def test_wordpress_fallback_invalid_payload_is_explicit_failure(self) -> None:
        with self.assertRaises(ValueError):
            news_rss._parse_wordpress_posts("Bitcoinist", "[]", 24)

    def test_failed_feed_retries_after_other_initial_feeds(self) -> None:
        xml = "<rss><channel><item><title>BTC update</title></item></channel></rss>"
        calls: list[tuple[str, int]] = []

        def fake_fetch(url: str, timeout: int = 12) -> str:
            calls.append((url, timeout))
            if url == "https://first.example/feed" and len(calls) == 1:
                raise urllib.error.URLError("transient")
            return xml

        outcomes: list[dict] = []
        with mock.patch.object(news_rss, "_fetch", side_effect=fake_fetch), \
                mock.patch.object(news_rss.time, "sleep"):
            items = news_rss.fetch_rss_items(
                feeds=[
                    ("First", "https://first.example/feed"),
                    ("Second", "https://second.example/feed"),
                ],
                outcomes=outcomes,
            )

        self.assertEqual(
            [url for url, _timeout in calls],
            [
                "https://first.example/feed",
                "https://second.example/feed",
                "https://first.example/feed",
            ],
        )
        self.assertEqual([row["status"] for row in outcomes], ["ok", "ok"])
        self.assertTrue(outcomes[0]["recovered_after_retry"])
        self.assertEqual(outcomes[0]["attempts"], 2)
        self.assertEqual(len(items), 2)

    def test_default_feed_replaces_cryptopotato_with_cryptoslate(self) -> None:
        names = [name for name, _url in news_rss.DEFAULT_FEEDS]
        urls = [url for _name, url in news_rss.DEFAULT_FEEDS]

        self.assertIn("CryptoSlate", names)
        self.assertIn("https://cryptoslate.com/feed/", urls)
        self.assertNotIn("CryptoPotato", names)

    def test_official_news_retries_after_both_initial_endpoints(self) -> None:
        calls: list[str] = []

        def fake_okx_json(_group, kind, *_args, **_kwargs):
            calls.append(kind)
            if kind == "important" and calls.count(kind) == 1:
                raise TimeoutError("transient")
            return {"details": [{"id": kind, "title": f"{kind} update"}]}

        errors: list[str] = []
        stats: dict = {}
        with mock.patch.object(news_okx, "okx_json", side_effect=fake_okx_json), \
                mock.patch.object(news_okx.time, "sleep"):
            items = news_okx.fetch_items(errors=errors, retry_stats=stats)

        self.assertEqual(calls, ["important", "latest", "important"])
        self.assertEqual(errors, [])
        self.assertEqual(stats, {
            "initial_failed": 1,
            "recovered_after_retry": 1,
            "recovered_after_immediate_retry": 1,
            "cold_retry_requested": 0,
            "recovered_after_cold_retry": 0,
            "final_failed": 0,
            "maximum_fetch_phases_per_endpoint": 3,
            "cold_retry_delay_seconds": 3.0,
            "cold_retry_timeout_seconds": 4.0,
            "historical_retry": False,
            "unbounded_retry": False,
        })
        self.assertEqual({item["raw"]["id"] for item in items},
                         {"important", "latest"})

    def test_official_news_exact_endpoint_recovers_after_cold_delay(self) -> None:
        calls: list[str] = []

        def fake_okx_json(_group, kind, *_args, **_kwargs):
            calls.append(kind)
            if kind == "important" and calls.count(kind) < 3:
                raise TimeoutError("transient")
            return {"details": [{"id": kind, "title": f"{kind} update"}]}

        errors: list[str] = []
        stats: dict = {}
        with mock.patch.object(
            news_okx, "okx_json", side_effect=fake_okx_json,
        ), mock.patch.object(news_okx.time, "sleep") as sleep:
            items = news_okx.fetch_items(errors=errors, retry_stats=stats)

        self.assertEqual(
            calls, ["important", "latest", "important", "important"])
        self.assertEqual(errors, [])
        self.assertEqual(stats["cold_retry_requested"], 1)
        self.assertEqual(stats["recovered_after_cold_retry"], 1)
        self.assertEqual(stats["final_failed"], 0)
        self.assertFalse(stats["historical_retry"])
        self.assertFalse(stats["unbounded_retry"])
        self.assertTrue(any(
            call.args and call.args[0] == 3.0
            for call in sleep.call_args_list
        ))
        self.assertEqual({item["raw"]["id"] for item in items},
                         {"important", "latest"})

    def test_panews_retries_transient_failure_and_clears_error(self) -> None:
        xml = "<rss><channel><item><title>BTC update</title></item></channel></rss>"
        errors: list[str] = []
        stats: dict = {}
        with mock.patch.object(
            news_panews, "_fetch",
            side_effect=[urllib.error.URLError("transient"), xml],
        ), mock.patch.object(news_panews.time, "sleep"):
            items = news_panews.fetch_items(
                errors=errors, retry_stats=stats)

        self.assertEqual(len(items), 1)
        self.assertEqual(errors, [])
        self.assertEqual(stats["attempts"], 2)
        self.assertTrue(stats["recovered_after_retry"])

    def test_panews_official_page_fallback_preserves_uuid_identity(self) -> None:
        article_id = "019ff4d7-c34d-73ce-9900-cea3aad70f0c"
        published = news_panews.datetime.now(
            news_panews.timezone.utc).isoformat().replace("+00:00", "Z")
        values = [
            {"_1": 2}, "loaderData", {"_3": 4}, "routes/newsflash",
            {"_5": 6}, "payload", {"_7": 8}, "articles", [9],
            {"_10": 11, "_12": 13, "_14": 15, "_16": 17},
            "id", article_id, "title", "BTC official update",
            "publishedAt", published,
            "isImportant", True,
        ]
        stream = json.dumps(json.dumps(values))
        page = (
            "<script>window.__reactRouterContext.streamController.enqueue("
            f"{stream});</script>"
        )
        errors: list[str] = []
        stats: dict = {}
        with mock.patch.object(
            news_panews, "_fetch", side_effect=urllib.error.URLError("rss eof"),
        ), mock.patch.object(
            news_panews, "_fetch_text_httpx", return_value=page,
        ), mock.patch.object(news_panews.time, "sleep"):
            items = news_panews.fetch_items(
                errors=errors, retry_stats=stats)

        self.assertEqual(errors, [])
        self.assertEqual(len(items), 1)
        self.assertEqual(
            items[0]["url"],
            f"https://www.panewslab.com/zh/articles/{article_id}",
        )
        self.assertRegex(items[0]["dedupe_hash"], r"^[0-9a-f]{32}$")
        self.assertEqual(items[0]["raw"]["feed"], "panews_official_page")
        self.assertTrue(stats["recovered_after_fallback"])
        self.assertEqual(stats["transport"], "official_page")

    def test_panews_official_page_structure_failure_is_not_empty_success(self) -> None:
        with self.assertRaises(ValueError):
            news_panews._parse_official_page("<html></html>", 24)

    def test_invalid_odaily_payload_retries_then_fails_explicitly(self) -> None:
        errors: list[str] = []
        stats: dict = {}
        with mock.patch.object(
            news_odaily, "_fetch", return_value={"code": 200, "data": {}},
        ), mock.patch.object(
            news_odaily, "_fetch_alternate",
            return_value={"code": 200, "data": {}},
        ), mock.patch.object(news_odaily.time, "sleep"):
            items = news_odaily.fetch_items(
                errors=errors, retry_stats=stats)

        self.assertEqual(items, [])
        self.assertTrue(errors)
        self.assertTrue(stats["final_failed"])
        self.assertEqual(stats["attempts"], 2)

    def test_odaily_recovers_via_same_url_alternate_transport(self) -> None:
        errors: list[str] = []
        stats: dict = {}
        with mock.patch.object(
            news_odaily, "_fetch",
            side_effect=urllib.error.URLError("tls eof"),
        ) as primary, mock.patch.object(
            news_odaily, "_fetch_alternate",
            return_value={"code": 200, "data": {"list": []}},
        ) as alternate, mock.patch.object(news_odaily.time, "sleep"):
            items = news_odaily.fetch_items(
                errors=errors, retry_stats=stats)

        self.assertEqual(items, [])
        self.assertEqual(errors, [])
        self.assertEqual(primary.call_count, 1)
        self.assertEqual(alternate.call_count, 1)
        self.assertTrue(stats["recovered_after_retry"])
        self.assertEqual(stats["transport"], "alternate_http")

    def test_jinse_recovers_via_same_url_alternate_transport(self) -> None:
        errors: list[str] = []
        stats: dict = {}
        with mock.patch.object(
            news_jinse, "_fetch",
            side_effect=urllib.error.URLError("tls eof"),
        ) as primary, mock.patch.object(
            news_jinse, "_fetch_alternate",
            return_value=json.dumps({"list": []}),
        ) as alternate, mock.patch.object(news_jinse.time, "sleep"):
            items = news_jinse.fetch_items(
                errors=errors, retry_stats=stats)

        self.assertEqual(items, [])
        self.assertEqual(errors, [])
        self.assertEqual(primary.call_count, 1)
        self.assertEqual(alternate.call_count, 1)
        self.assertTrue(stats["recovered_after_retry"])
        self.assertEqual(stats["transport"], "alternate_http")

    def test_mx_search_retries_once_without_duplicate_success(self) -> None:
        calls: list[float] = []

        def fake_search(_query, *, key, timeout_sec):
            self.assertEqual(key, "test-key")
            calls.append(timeout_sec)
            if len(calls) == 1:
                raise TimeoutError("transient")
            return []

        errors: list[str] = []
        stats: dict = {}
        with mock.patch.object(news_mxsearch, "api_key", return_value="test-key"), \
                mock.patch.object(news_mxsearch, "search", side_effect=fake_search), \
                mock.patch.object(news_mxsearch.time, "sleep"):
            items = news_mxsearch.fetch_items(errors, stats)

        self.assertEqual(items, [])
        self.assertEqual(errors, [])
        self.assertEqual(calls, [6.0, 4.0])
        self.assertEqual(stats["attempts"], 2)
        self.assertTrue(stats["recovered_after_retry"])

    def test_geo_retries_only_failed_query(self) -> None:
        calls: list[tuple[str, float]] = []
        failed_query = news_geo.GEO_QUERIES[1]

        def fake_search(query, *, key, timeout_sec):
            self.assertEqual(key, "test-key")
            calls.append((query, timeout_sec))
            if query == failed_query and sum(
                1 for item, _timeout in calls if item == query) == 1:
                raise TimeoutError("transient")
            return []

        errors: list[str] = []
        stats: dict = {}
        with mock.patch.object(news_geo, "api_key", return_value="test-key"), \
                mock.patch.object(news_geo, "search", side_effect=fake_search), \
                mock.patch.object(news_geo.time, "sleep"):
            items = news_geo.fetch_items(errors, stats)

        self.assertEqual(items, [])
        self.assertEqual(errors, [])
        self.assertEqual(
            [query for query, _timeout in calls].count(failed_query), 2)
        self.assertEqual(len(calls), len(news_geo.GEO_QUERIES) + 1)
        self.assertEqual(stats["recovered_after_retry"], 1)
        self.assertEqual(stats["final_failed"], 0)

    def test_mx_quota_error_is_not_retried(self) -> None:
        calls: list[float] = []

        def fake_search(_query, *, key, timeout_sec):
            self.assertEqual(key, "test-key")
            calls.append(timeout_sec)
            raise mx_common.MXQuotaExceeded("MX business code=113: quota")

        errors: list[str] = []
        stats: dict = {}
        with mock.patch.object(news_mxsearch, "api_key", return_value="test-key"), \
                mock.patch.object(news_mxsearch, "search", side_effect=fake_search), \
                mock.patch.object(news_mxsearch.time, "sleep"):
            items = news_mxsearch.fetch_items(errors, stats)

        self.assertEqual(items, [])
        self.assertEqual(calls, [6.0])
        self.assertTrue(errors)
        self.assertEqual(stats["attempts"], 1)
        self.assertTrue(stats["quota_exhausted"])
        self.assertTrue(stats["retry_skipped_non_retryable"])

    def test_geo_quota_error_stops_remaining_queries_without_retry(self) -> None:
        calls: list[str] = []
        quota_query = news_geo.GEO_QUERIES[1]

        def fake_search(query, *, key, timeout_sec):
            self.assertEqual(key, "test-key")
            calls.append(query)
            if query == quota_query:
                raise mx_common.MXQuotaExceeded(
                    "MX business code=113: quota")
            return []

        errors: list[str] = []
        stats: dict = {}
        with mock.patch.object(news_geo, "api_key", return_value="test-key"), \
                mock.patch.object(news_geo, "search", side_effect=fake_search), \
                mock.patch.object(news_geo.time, "sleep"):
            items = news_geo.fetch_items(errors, stats)

        self.assertEqual(items, [])
        self.assertEqual(calls, list(news_geo.GEO_QUERIES[:2]))
        self.assertTrue(errors)
        self.assertTrue(stats["quota_exhausted"])
        self.assertEqual(stats["queries_attempted"], 2)
        self.assertEqual(stats["queries_skipped_after_quota"], 2)
        self.assertEqual(stats["final_failed"], 3)


class AnnouncementTypesCacheTests(unittest.TestCase):
    """2026-08-18 A1：types 列表缓存降级（types 单点失败不再杀全轮）。"""

    def setUp(self) -> None:
        import tempfile
        import news_okx_announcements as ann
        self.ann = ann
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_path = str(Path(self._tmp.name) / "types_cache.json")
        self._env = mock.patch.dict(
            "os.environ", {ann._TYPES_CACHE_ENV: self.cache_path})
        self._env.start()
        self._sleep = mock.patch.object(
            self.ann.time, "sleep", lambda *_a, **_k: None)
        self._sleep.start()

    def tearDown(self) -> None:
        self._sleep.stop()
        self._env.stop()
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass  # Windows 桥挂载下 TemporaryDirectory 清理偶发受限

    @staticmethod
    def _page(ann_type: str) -> dict:
        return {"details": [{
            "title": f"OKX to delist {ann_type.upper()}X/USDT perpetual",
            "url": "https://www.okx.com/help/x",
            "pTime": "1786940000000",
        }]}

    def test_types_success_persists_cache(self) -> None:
        types = [{"annType": "announcements-delistings"}]
        with mock.patch.object(
            self.ann._okx_http,
            "fetch_support_announcement_types_sync", return_value=types,
        ), mock.patch.object(
            self.ann._okx_http, "fetch_support_announcements_sync",
            side_effect=lambda t, **_k: self._page(t),
        ):
            stats: dict = {}
            out = self.ann.fetch_items(errors=[], retry_stats=stats)
        self.assertTrue(out)
        self.assertEqual(stats["types_source"], "live")
        cached = json.loads(Path(self.cache_path).read_text(encoding="utf-8"))
        self.assertEqual(cached["schema"], "okx_announcement_types_cache_v1")
        self.assertEqual(cached["types"], types)

    def test_types_double_failure_falls_back_to_cache(self) -> None:
        from datetime import datetime, timedelta, timezone
        cst = timezone(timedelta(hours=8))
        Path(self.cache_path).write_text(json.dumps({
            "schema": "okx_announcement_types_cache_v1",
            "fetched_at_cst": (
                datetime.now(cst) - timedelta(days=2)
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "types": [{"annType": "announcements-delistings"}],
        }, ensure_ascii=False), encoding="utf-8")
        errors: list[str] = []
        stats: dict = {}
        with mock.patch.object(
            self.ann._okx_http, "fetch_support_announcement_types_sync",
            side_effect=RuntimeError("transport down"),
        ), mock.patch.object(
            self.ann._okx_http, "fetch_support_announcements_sync",
            side_effect=lambda t, **_k: self._page(t),
        ):
            out = self.ann.fetch_items(errors=errors, retry_stats=stats)
        self.assertTrue(out, "cached types must keep category pages flowing")
        self.assertEqual(stats["types_source"], "cached")
        self.assertFalse(stats["types_recovered_after_cold_retry"])
        self.assertAlmostEqual(
            stats["types_cache_age_days"], 2.0, delta=0.2)
        self.assertTrue(any("fallback=types_cache" in e for e in errors))

    def test_types_double_failure_without_cache_keeps_failing_closed(
            self) -> None:
        errors: list[str] = []
        stats: dict = {}
        with mock.patch.object(
            self.ann._okx_http, "fetch_support_announcement_types_sync",
            side_effect=RuntimeError("transport down"),
        ):
            out = self.ann.fetch_items(errors=errors, retry_stats=stats)
        self.assertEqual(out, [])
        self.assertEqual(stats["types_source"], "unavailable")
        self.assertEqual(stats["final_failed"], 1)

    def test_stale_cache_is_ignored(self) -> None:
        from datetime import datetime, timedelta, timezone
        cst = timezone(timedelta(hours=8))
        Path(self.cache_path).write_text(json.dumps({
            "schema": "okx_announcement_types_cache_v1",
            "fetched_at_cst": (
                datetime.now(cst)
                - timedelta(days=self.ann._TYPES_CACHE_MAX_AGE_DAYS + 1)
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "types": [{"annType": "announcements-delistings"}],
        }, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(
            self.ann._okx_http, "fetch_support_announcement_types_sync",
            side_effect=RuntimeError("transport down"),
        ):
            out = self.ann.fetch_items(errors=[], retry_stats={})
        self.assertEqual(out, [])


class UnlockCalendarPrimaryDomainTests(unittest.TestCase):
    """2026-08-18 A3-lite：解锁日历所有者域名入一级源白名单。"""

    def setUp(self) -> None:
        collectors = ROOT / "collectors"
        if str(collectors) not in sys.path:
            sys.path.insert(0, str(collectors))
        import news_writer
        self.news_writer = news_writer

    def test_calendar_owner_domains_are_primary(self) -> None:
        for url in (
            "https://tokenomist.ai/token/zro",
            "https://defillama.com/unlocks/calendar",
            "https://www.okx.com/help/x",
        ):
            self.assertEqual(
                self.news_writer.source_grade(url, None), "primary", url)

    def test_media_and_social_links_stay_non_primary(self) -> None:
        self.assertEqual(
            self.news_writer.source_grade(
                "https://x.com/Tokenomist_ai/status/1", None), "aggregator")
        self.assertEqual(
            self.news_writer.source_grade(
                "https://coindesk.com/unlock-story", None), "secondary")


if __name__ == "__main__":
    unittest.main()
