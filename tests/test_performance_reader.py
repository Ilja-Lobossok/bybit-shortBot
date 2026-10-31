import json
from unittest.mock import Mock

from performance.bybit_reader import Reader, Collector, ReadError, DAY
from tests.performance_fixtures import Offline, position, wallet

ENV = {"PERFORMANCE_BYBIT_API_KEY": "synthetic-key", "PERFORMANCE_BYBIT_API_SECRET": "synthetic-secret"}


class Response:
    def __init__(self, value, status=200, headers=None):
        self.value, self.status_code, self.headers = value, status, headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, _size):
        yield self.value if isinstance(self.value, bytes) else json.dumps(self.value).encode()


def reply(rows=(), cursor=""):
    return {"retCode": 0, "time": 5000, "result": {"list": list(rows), "nextPageCursor": cursor}}


class ReaderTests(Offline):
    def reader(self):
        self.session = Mock()
        self.session.get.return_value = Response(reply())
        self.sleep = Mock()
        return Reader(ENV, session=self.session, clock=lambda: 1, monotonic=lambda: 1, sleep=self.sleep)

    def test_only_get_allowlist_and_separate_credentials(self):
        reader = self.reader()
        for kind in ("positions", "orders", "executions", "closed_pnl", "transactions", "wallet"):
            reader.get(kind, {})
        for forbidden in ("place_order", "cancel_order", "transfer", "amend", "https://evil.invalid"):
            with self.assertRaises(ReadError):
                reader.get(forbidden, {})
        self.assertEqual(self.session.get.call_count, 6)
        self.session.post.assert_not_called()
        self.session.request.assert_not_called()
        with self.assertRaises(ReadError):
            Reader({"BYBIT_API_KEY": "trading-key", "BYBIT_API_SECRET": "trading-secret"})

    def test_signature_headers_timeouts_and_query(self):
        reader = self.reader()
        reader.get("orders", {"startTime": 1, "category": "linear"})
        url = self.session.get.call_args.args[0]
        options = self.session.get.call_args.kwargs
        self.assertIn("?category=linear&startTime=1", url)
        self.assertNotIn("synthetic", url)
        self.assertEqual(options["timeout"], (2, 5))
        self.assertFalse(options["allow_redirects"])
        self.assertEqual(len(options["headers"]["X-BAPI-SIGN"]), 64)

    def test_rate_limit_failure_backoff_has_no_immediate_retry(self):
        reader = self.reader()
        self.session.get.return_value = Response({"retCode": 10006, "retMsg": "synthetic-secret"})
        for _ in range(2):
            with self.assertRaises(ReadError) as error:
                reader.get("orders", {})
            self.assertNotIn("synthetic", str(error.exception))
        self.assertEqual(self.session.get.call_count, 1)

    def test_failure_hides_request_and_credentials(self):
        reader = self.reader()
        self.session.get.side_effect = RuntimeError("Authorization: synthetic-secret")
        with self.assertRaises(ReadError) as error:
            reader.get("wallet", {})
        self.assertNotIn("synthetic", str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)

    def test_response_memory_limit_and_redirects(self):
        for response in (Response(b"x" * (2 * 1024**2 + 1)), Response(reply(), status=302)):
            reader = self.reader()
            self.session.get.return_value = response
            with self.assertRaises(ReadError):
                reader.get("wallet", {})

    def test_successive_requests_are_paced(self):
        reader = self.reader()
        reader.get("wallet", {})
        reader.get("wallet", {})
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [0, 1])

    def test_history_pagination_is_restartable_and_bounded(self):
        reader = Mock()
        reader.get.side_effect = lambda kind, params: reply(cursor="page-" + str(int(params.get("cursor", "page-0").split("-")[-1]) + 1))
        collector = Collector(reader, self.store, lookback_days=30)
        collector.history(40 * DAY)
        self.assertEqual(reader.get.call_count, 16)  # max 4 pages x 4 endpoints
        self.assertFalse(self.store.covered("orders", 10 * DAY, 11 * DAY))
        saved = self.store.db.execute("SELECT cursor FROM sync_cursors WHERE kind='orders'").fetchone()[0]
        self.assertEqual(saved, "page-4")
        calls = []

        def finish(kind, params):
            calls.append((kind, params))
            return reply()
        reader.get.side_effect = finish
        collector.history(41 * DAY)
        self.assertTrue(all(params["cursor"] == "page-4" for kind, params in calls))
        self.assertIsNotNone(self.store.meta("watermark:orders"))
        self.assertTrue(all(params["endTime"] - params["startTime"] < 7 * DAY for kind, params in calls))
        tx_params = next(params for kind, params in calls if kind == "transactions")
        self.assertNotIn("category", tx_params)
        self.assertNotIn("settleCoin", next(params for kind, params in calls if kind == "closed_pnl"))

    def test_failed_position_second_page_cannot_clear_first_snapshot(self):
        self.store.replace_positions([position()], 10)
        reader = Mock()
        reader.get.return_value = wallet()

        def broken_pages(*args):
            yield [position()], False, reply([position()], "next")
            raise ReadError("Offline failure")
        reader.pages.side_effect = broken_pages
        collector = Collector(reader, self.store)
        with self.assertRaises(ReadError):
            collector.snapshots(5000)
        self.assertEqual(self.store.meta("positions_success_ms"), "10")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM positions_current").fetchone()[0], 1)

    def test_repeated_cursor_is_incomplete(self):
        reader = self.reader()
        self.session.get.return_value = Response(reply(cursor="same"))
        with self.assertRaises(ReadError):
            list(reader.pages("orders", {}))
        self.assertEqual(self.session.get.call_count, 2)
