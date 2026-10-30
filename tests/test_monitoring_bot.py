"""Trading regressions with fake clients only; dotenv/file logging are isolated."""

import logging
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

# Importing the legacy bot normally loads dotenv and opens bot.log. Neither is
# needed in this suite. No real credentials, state directory or HTTP are used.
with patch.dict(os.environ, {"MONITORING_ENABLED": "false"}, clear=True), \
        patch("dotenv.load_dotenv"), patch("os.makedirs"), \
        patch("logging.FileHandler", return_value=logging.NullHandler()):
    import bot as trading

from live.bybit_client import BybitClient
from monitoring.runtime import DISABLED, RuntimeMonitoring
from monitoring.runtime import EVENT_CAPACITY
from tests import test_accounting as accounting
from tests.test_monitoring_otel import OfflineTestCase, TEST_ENV, TEST_TOKEN


def monitoring():
    return RuntimeMonitoring(dict(TEST_ENV, MONITORING_ENABLED="true"), "unused-test-disk")


def broken_monitoring():
    return SimpleNamespace(enabled=True,
                           api_result=Mock(side_effect=RuntimeError(TEST_TOKEN)),
                           fail_cycle=Mock(side_effect=RuntimeError(TEST_TOKEN)),
                           balance_validated=Mock(side_effect=RuntimeError(TEST_TOKEN)))


class EnabledAccountingTests(accounting.BotAccountingTests):
    """Run all existing state/order/accounting assertions with monitoring enabled."""
    def setUp(self):
        OfflineTestCase.setUp(self)
        super().setUp()
        self.bot.monitoring = monitoring()


class BrokenObserverAccountingTests(accounting.BotAccountingTests):
    def setUp(self):
        OfflineTestCase.setUp(self)
        super().setUp()
        self.bot.monitoring = broken_monitoring()


class EnabledClientTests(accounting.ClientTests):
    def setUp(self):
        OfflineTestCase.setUp(self)
        super().setUp()
        self.client.monitoring = monitoring()


class BrokenObserverClientTests(accounting.ClientTests):
    def setUp(self):
        OfflineTestCase.setUp(self)
        super().setUp()
        self.client.monitoring = broken_monitoring()


class LoopTests(OfflineTestCase):
    def make_bot(self, *, bar=1800, observer=None):
        result = trading.ShortBot.__new__(trading.ShortBot)
        result.monitoring = observer if observer is not None else monitoring()
        result.running = True
        result.last_bar_close = 900
        result.config = SimpleNamespace(wake_seconds=5)
        result.tracker = SimpleNamespace(positions={}, watchlist={})
        result.reload_from_file = Mock()
        result.refresh_balance = Mock(return_value=True)
        result.check_positions = Mock()
        result.get_current_bar_close = Mock(return_value=bar)
        result.get_all_tickers = Mock(return_value=[{"symbol": "TESTUSDT"}])
        result.check_pump_candidate = Mock(return_value=None)
        result.update_watchlist = Mock()
        result.check_stall = Mock(return_value=[])
        result.open_position = Mock()
        result.balance_summary = Mock(return_value="synthetic balance")
        return result

    def run_once(self, instance):
        def stop(_seconds):
            instance.running = False
        with patch("bot.time.sleep", side_effect=stop):
            instance.run()

    def test_waiting_updates_progress_without_a_successful_candle(self):
        instance = self.make_bot(bar=900)
        instance.monitoring.cycle_success = 100
        self.run_once(instance)
        self.assertIsNotNone(instance.monitoring.main_progress)
        self.assertEqual(instance.monitoring.cycle_success, 100)
        instance.refresh_balance.assert_not_called()
        instance.check_positions.assert_not_called()

    def test_successful_candle_keeps_original_call_order_and_counts(self):
        instance = self.make_bot()
        sequence = Mock()
        for attribute in ("refresh_balance", "check_positions", "get_all_tickers",
                          "check_pump_candidate", "check_stall", "open_position"):
            sequence.attach_mock(getattr(instance, attribute), attribute)
        self.run_once(instance)
        self.assertEqual(sequence.mock_calls, [
            call.refresh_balance(), call.check_positions(), call.get_all_tickers(),
            call.check_pump_candidate({"symbol": "TESTUSDT"}), call.check_stall(),
            call.refresh_balance(),
        ])
        self.assertIsNotNone(instance.monitoring.cycle_success)
        self.assertEqual(instance.last_bar_close, 1800)

    def test_disabled_monitoring_keeps_all_trading_calls(self):
        instance = self.make_bot(observer=DISABLED)
        self.run_once(instance)
        self.assertEqual(instance.refresh_balance.call_count, 2)
        instance.check_positions.assert_called_once_with()
        instance.get_all_tickers.assert_called_once_with()
        instance.check_stall.assert_called_once_with()
        instance.open_position.assert_not_called()

    def test_balance_failure_does_not_skip_original_processing_or_mark_success(self):
        instance = self.make_bot()
        instance.refresh_balance.side_effect = [False, True]
        self.run_once(instance)
        self.assertIsNone(instance.monitoring.cycle_success)
        instance.check_positions.assert_called_once_with()
        instance.check_stall.assert_called_once_with()
        self.assertEqual(instance.refresh_balance.call_count, 2)

    def test_suppressed_position_error_invalidates_candle_but_keeps_processing(self):
        instance = self.make_bot()
        instance.tracker.positions = {"TESTUSDT": {"qty": 1}}
        instance.client = Mock()
        instance.client.get_positions.side_effect = RuntimeError("synthetic API failure")
        instance.check_positions = trading.ShortBot.check_positions.__get__(instance)
        self.run_once(instance)
        self.assertIsNone(instance.monitoring.cycle_success)
        self.assertIn("TESTUSDT", instance.tracker.positions)
        instance.check_stall.assert_called_once_with()

    def test_suppressed_ticker_error_does_not_mark_success(self):
        instance = self.make_bot()
        instance.client = Mock()
        instance.client.get_tickers.side_effect = RuntimeError("synthetic API failure")
        instance.get_all_tickers = trading.ShortBot.get_all_tickers.__get__(instance)
        self.run_once(instance)
        self.assertIsNone(instance.monitoring.cycle_success)
        instance.check_stall.assert_called_once_with()

    def test_uncaught_stage_error_does_not_finish_the_candle(self):
        instance = self.make_bot()
        instance.check_positions.side_effect = RuntimeError("synthetic stage failure")
        self.run_once(instance)
        self.assertIsNone(instance.monitoring.cycle_success)
        instance.get_all_tickers.assert_not_called()

    def test_pending_entry_confirmation_is_not_a_complete_cycle(self):
        instance = self.make_bot()
        instance.monitoring.begin_cycle()
        instance.tracker.positions = {"TESTUSDT": {"entry_pending": True, "entry_order_id": "synthetic-id"}}
        instance.client = Mock()
        instance.client.get_positions.return_value = []
        instance.client.get_entry_fill.return_value = None
        trading.ShortBot.check_positions(instance)
        from tests.test_monitoring_runtime import complete_cycle
        complete_cycle(instance.monitoring)
        self.assertIsNone(instance.monitoring.cycle_success)
        self.assertIn("TESTUSDT", instance.tracker.positions)

    def test_balance_timestamp_requires_validation_and_successful_state_save(self):
        instance = self.make_bot()
        instance.client = Mock()
        instance.client.get_wallet_balance.return_value = accounting.wallet_response()
        instance.tracker = Mock()
        instance.tracker.risk_state = {}
        instance.risk_manager = None
        instance.account_balance = None
        self.assertTrue(trading.ShortBot.refresh_balance(instance))
        timestamp = instance.monitoring.balance_success
        self.assertIsNotNone(timestamp)
        instance.tracker.save.side_effect = OSError("synthetic disk failure")
        self.assertFalse(trading.ShortBot.refresh_balance(instance))
        self.assertEqual(instance.monitoring.balance_success, timestamp)
        self.assertIsNone(instance.account_balance)


class RestartRiskTests(OfflineTestCase):
    """Characterize existing restart guards and same-candle risk; no trading fix."""

    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_file = str(Path(self.temp.name) / "state.json")

    def make_bot(self):
        instance = trading.ShortBot.__new__(trading.ShortBot)
        instance.config = accounting.BotConfig(data_dir=self.temp.name)
        instance.monitoring = DISABLED
        instance.tracker = accounting.PositionTracker(self.state_file)
        instance.risk_manager = None
        instance.account_balance = None
        instance.running = True
        # These are the current constructor defaults on every new process.
        instance.last_bar_close = 0
        instance._last_stall_check = 0
        instance.client = Mock()
        instance.notifier = Mock()
        instance.client.get_wallet_balance.return_value = accounting.wallet_response(available="400")
        instance.client.get_positions.return_value = []
        instance.client.get_instruments.return_value = {"result": {"list": [{
            "lotSizeFilter": {"qtyStep": "0.1", "minOrderQty": "0.1", "minNotionalValue": "5"},
            "priceFilter": {"tickSize": "0.01"},
        }]}}
        instance.client.place_order.return_value = {"retCode": 0, "result": {"orderId": "fake-entry"}}
        instance.get_all_tickers = Mock(return_value=[{"symbol": "TESTUSDT", "lastPrice": "1"}])
        instance.check_pump_candidate = Mock(return_value=None)
        instance.get_klines = Mock(return_value=[[0, 0, "1", 0, "0.9"], [0, 0, "1", 0, "0.9"]])
        instance.cancel_all_orders_for_symbol = Mock()
        return instance

    def run_once(self, instance):
        def stop(_seconds):
            instance.running = False
        # The real run(), check_stall(), open_position(), tracker and balance
        # handling execute; only exchange I/O and time are synthetic.
        with patch("bot.time.time", return_value=1801), patch("bot.time.sleep", side_effect=stop):
            instance.run()

    def test_restart_same_candle_advances_stall_and_can_open_early(self):
        first = self.make_bot()
        first.tracker.watchlist["TESTUSDT"] = {
            "local_high": 1, "stall": 2, "blocked": False, "created_ts": 1801,
        }
        first.tracker.save()
        self.run_once(first)
        self.assertEqual(first.last_bar_close, 1800)
        self.assertEqual(first.tracker.watchlist["TESTUSDT"]["stall"], 3)
        first.client.place_order.assert_not_called()

        # Without a process restart the same candle is only a waiting pass.
        first.running = True
        self.run_once(first)
        self.assertEqual(first.tracker.watchlist["TESTUSDT"]["stall"], 3)
        first.client.place_order.assert_not_called()

        restarted = self.make_bot()
        restarted.client.get_positions.side_effect = [[], [{"side": "Sell", "size": "8.2", "avgPrice": "1"}]]
        self.run_once(restarted)
        self.assertEqual(restarted.last_bar_close, first.last_bar_close)
        restarted.client.place_order.assert_called_once()
        self.assertIn("TESTUSDT", restarted.tracker.positions)
        self.assertNotIn("TESTUSDT", restarted.tracker.watchlist)

    def test_saved_confirmed_or_pending_entry_blocks_another_order_after_restart(self):
        for pending in (False, True):
            with self.subTest(entry_pending=pending):
                original = self.make_bot()
                original.tracker.add_position({"symbol": "TESTUSDT", "entry_pending": pending})
                restarted = self.make_bot()
                restarted.open_position("TESTUSDT", {"local_high": 1})
                restarted.client.get_positions.assert_not_called()
                restarted.client.place_order.assert_not_called()

    def test_visible_exchange_position_blocks_order_when_local_record_is_missing(self):
        restarted = self.make_bot()
        restarted.client.get_positions.return_value = [{"side": "Sell", "size": "8.2"}]
        restarted.open_position("TESTUSDT", {"local_high": 1})
        restarted.client.get_positions.assert_called_once_with(symbol="TESTUSDT")
        restarted.client.place_order.assert_not_called()


class RequestContractTests(OfflineTestCase):
    def client(self, observer):
        result = BybitClient.__new__(BybitClient)
        result.category = "linear"
        result.monitoring = observer
        result.session = Mock()
        return result

    def test_telemetry_failure_does_not_retry_a_successful_order(self):
        client = self.client(broken_monitoring())
        response = {"retCode": 0, "result": {"orderId": "synthetic-id"}}
        client.session.place_order.return_value = response
        actual = client.place_order(symbol="TESTUSDT", qty="3", side="Sell")
        self.assertIs(actual, response)
        client.session.place_order.assert_called_once_with(symbol="TESTUSDT", qty="3", side="Sell")

    def test_full_monitoring_mailbox_does_not_change_or_duplicate_an_order(self):
        observation = monitoring()
        for _ in range(EVENT_CAPACITY):
            observation.api_result("get_positions", "success", "exchange", 0.1)
        client = self.client(observation)
        response = {"retCode": 0, "result": {"orderId": "synthetic-id"}}
        client.session.place_order.return_value = response
        self.assertIs(client.place_order(symbol="TESTUSDT", qty="3", side="Sell"), response)
        client.session.place_order.assert_called_once_with(symbol="TESTUSDT", qty="3", side="Sell")
        self.assertEqual(observation.dropped_events, 1)
        self.assertEqual(len(observation.take_events()), EVENT_CAPACITY)

    def test_order_transport_exception_is_not_replaced_or_retried(self):
        client = self.client(broken_monitoring())
        original = TimeoutError("original order timeout")
        client.session.place_order.side_effect = original
        with self.assertRaises(TimeoutError) as actual:
            client.place_order(symbol="TESTUSDT", qty="3")
        self.assertIs(actual.exception, original)
        client.session.place_order.assert_called_once_with(symbol="TESTUSDT", qty="3")

    def test_existing_read_retry_policy_is_preserved(self):
        client = self.client(monitoring())
        client.session.get_tickers.side_effect = [TimeoutError(), TimeoutError(), {"retCode": 0}]
        with patch.object(BybitClient.get_tickers.retry, "sleep") as sleep:
            self.assertEqual(client.get_tickers(), {"retCode": 0})
        self.assertEqual(client.session.get_tickers.call_count, 3)
        self.assertEqual([item.args[0] for item in sleep.call_args_list], [1.0, 2.0])
        self.assertEqual([event[1] for event in client.monitoring.take_events()], ["error", "error", "success"])

    def test_cancel_calls_and_suppressed_failures_keep_original_order(self):
        instance = trading.ShortBot.__new__(trading.ShortBot)
        instance.monitoring = monitoring()
        instance.monitoring.begin_cycle()
        instance.config = SimpleNamespace(category="linear")
        instance.client = self.client(instance.monitoring)
        instance.client.session.get_open_orders.return_value = {
            "retCode": 0, "result": {"list": [{"orderId": "one"}, {"orderId": "two"}]}}
        instance.client.session.cancel_order.side_effect = [RuntimeError("synthetic failure"), {"retCode": 0}]
        instance.cancel_all_orders_for_symbol("TESTUSDT")
        self.assertEqual(instance.client.session.mock_calls, [
            call.get_open_orders(category="linear", symbol="TESTUSDT"),
            call.cancel_order(category="linear", symbol="TESTUSDT", orderId="one"),
            call.cancel_order(category="linear", symbol="TESTUSDT", orderId="two"),
        ])
        self.assertFalse(instance.monitoring._cycle_ok)
