"""Offline accounting regressions. No credentials or exchange calls are used."""

import json
import logging
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from bot import ShortBot
from config import BotConfig
from live.accounting import AccountBalance, ClosedTrade
from live.bybit_client import BybitClient
from live.position_tracker import PositionTracker
from live.risk_manager import RiskManager
from live.telegram_notifier import TelegramNotifier


def wallet_response(wallet="410", equity="400", available="100"):
    return {"retCode": 0, "result": {"list": [{
        "accountType": "UNIFIED", "totalAvailableBalance": available,
        "coin": [{"coin": "USDT", "walletBalance": wallet, "equity": equity,
                  "unrealisedPnl": "-10", "usdValue": equity}],
    }]}}


def close_record(**overrides):
    row = dict(symbol="RLCUSDT", side="Buy", orderId="close-1", execType="Trade",
               closedSize="6.7", avgEntryPrice="1.0745", avgExitPrice="0.8953",
               closedPnl="1.1933", openFee="0.00395954", closeFee="0.00329919",
               createdTime="2000", updatedTime="3000")
    row.update(overrides)
    return row


class AccountBalanceTests(unittest.TestCase):
    def test_exchange_wallet_equity_and_available_are_distinct(self):
        balance = AccountBalance.from_response(wallet_response())
        self.assertEqual((balance.wallet, balance.equity, balance.unrealised_pnl, balance.available),
                         (410, 400, -10, 100))

    def test_usdt_is_selected_by_name_not_coin_order(self):
        response = wallet_response()
        response['result']['list'][0]['coin'].insert(0, {"coin": "BTC"})
        self.assertEqual(AccountBalance.from_response(response).wallet, 410)

    def test_empty_usdt_balance_is_zero_not_1000(self):
        response = wallet_response()
        response['result']['list'][0]['coin'] = []
        self.assertEqual(AccountBalance.from_response(response).wallet, 0)

    def test_borrowed_usdt_is_excluded_and_available_is_capped(self):
        response = wallet_response(available="999")
        response['result']['list'][0]['coin'][0]['spotBorrow'] = "20"
        balance = AccountBalance.from_response(response)
        self.assertEqual((balance.wallet, balance.available), (390, 390))

    def test_usd_available_is_converted_to_usdt(self):
        response = wallet_response()
        response['result']['list'][0]['coin'][0]['usdValue'] = "200"
        self.assertEqual(AccountBalance.from_response(response).available, 200)

    def test_isolated_margin_uses_coin_fields(self):
        response = wallet_response(available="")
        response['result']['list'][0]['coin'][0].update(
            totalPositionIM="200", totalOrderIM="20", locked="5", bonus="10")
        self.assertEqual(AccountBalance.from_response(response).available, 175)

    def test_error_or_nan_is_rejected(self):
        for response in ({"retCode": 33004}, wallet_response(wallet="NaN")):
            with self.subTest(response=response), self.assertRaises(ValueError):
                AccountBalance.from_response(response)


class ClosedTradeTests(unittest.TestCase):
    def setUp(self):
        self.position = dict(qty=6.7, entry_price=1.0745)

    def aggregate(self, rows):
        return ClosedTrade.from_records("RLCUSDT", self.position, rows, 1000, 4000)

    def test_rlc_uses_net_pnl_not_whole_position_value(self):
        trade = self.aggregate([close_record()])
        self.assertAlmostEqual(trade.pnl, 1.1933)
        self.assertEqual(trade.exit, 0.8953)
        # Bybit's net amount already includes fees and funding; no second subtraction.
        self.assertNotAlmostEqual(trade.pnl, 6.7 * 1.0745)

    def test_partial_closes_are_aggregated_and_duplicates_count_once(self):
        rows = [close_record(closedSize="3", closedPnl="0.4"),
                close_record(orderId="close-2", closedSize="3.7", closedPnl="0.7933")]
        trade = self.aggregate(rows + [deepcopy(rows[0])])
        self.assertAlmostEqual(trade.pnl, 1.1933)
        self.assertEqual(len(trade.order_ids), 2)

    def test_missing_partial_or_ambiguous_history_waits(self):
        for rows in ([], [close_record(closedSize="3")], [close_record(closedSize="10")],
                     [close_record(side="Sell")], [close_record(createdTime="999")],
                     [close_record(updatedTime="5000")], [close_record(avgEntryPrice="2")]):
            with self.subTest(rows=rows):
                self.assertIsNone(self.aggregate(rows))

    def test_zero_or_invalid_exit_is_never_reported(self):
        for value in ("0", "NaN", "-1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.aggregate([close_record(avgExitPrice=value)])

    def test_funding_and_fees_in_net_loss_are_preserved(self):
        self.assertEqual(self.aggregate([close_record(closedPnl="-0.2")]).pnl, -0.2)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = BybitClient.__new__(BybitClient)
        self.client.category = "linear"
        self.client.session = Mock()

    def test_position_api_error_propagates(self):
        self.client.session.get_positions.return_value = {"retCode": 33004, "retMsg": "Expired"}
        with self.assertRaises(RuntimeError):
            self.client.get_positions("RLCUSDT")

    def test_transport_failure_propagates(self):
        self.client.session.get_positions.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.client.get_positions("RLCUSDT")

    def test_successful_empty_position_is_distinct_from_error(self):
        self.client.session.get_positions.return_value = {"retCode": 0, "result": {"list": []}}
        self.assertEqual(self.client.get_positions("RLCUSDT"), [])

    def test_invalid_position_size_is_rejected(self):
        self.client.session.get_positions.return_value = {"retCode": 0, "result": {"list": [
            dict(symbol="RLCUSDT", side="Sell", size="NaN")]}}
        with self.assertRaises(ValueError):
            self.client.get_positions("RLCUSDT")

    def test_history_has_pagination_and_seven_day_windows(self):
        week = 7 * 24 * 60 * 60 * 1000
        self.client.session.get_closed_pnl.side_effect = [
            {"retCode": 0, "result": {"list": [1], "nextPageCursor": "next"}},
            {"retCode": 0, "result": {"list": [2]}},
            {"retCode": 0, "result": {"list": [3]}},
        ]
        self.assertEqual(self.client.get_closed_pnl("RLCUSDT", 0, week), [1, 2, 3])
        calls = self.client.session.get_closed_pnl.call_args_list
        self.assertEqual(calls[0].kwargs['endTime'], week - 1)
        self.assertEqual(calls[1].kwargs['cursor'], "next")
        self.assertEqual(calls[2].kwargs['startTime'], week)

    def test_repeated_cursor_is_rejected(self):
        self.client.session.get_closed_pnl.return_value = {
            "retCode": 0, "result": {"list": [], "nextPageCursor": "same"}}
        with self.assertRaises(ValueError):
            self.client.get_closed_pnl("RLCUSDT", 0, 10)

    def test_entry_fill_handles_partial_execution_and_cancellation(self):
        row = dict(symbol='RLCUSDT', orderId='entry-1', orderStatus='PartiallyFilledCanceled',
                   cumExecQty='3', avgPrice='1.0745')
        self.client.session.get_order_history.return_value = {'retCode': 0, 'result': {'list': [row]}}
        self.assertEqual(self.client.get_entry_fill('RLCUSDT', 'entry-1'), (3, 1.0745))
        row.update(orderStatus='Cancelled', cumExecQty='0', avgPrice='')
        self.assertEqual(self.client.get_entry_fill('RLCUSDT', 'entry-1'), (0, 0))

    def test_closed_pnl_error_does_not_return_empty_history(self):
        self.client.session.get_closed_pnl.return_value = {'retCode': 33004, 'retMsg': 'Expired'}
        with self.assertRaises(RuntimeError):
            self.client.get_closed_pnl('RLCUSDT', 0, 10)


class BotAccountingTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.bot = ShortBot.__new__(ShortBot)
        self.bot.config = BotConfig(data_dir=self.temp.name)
        self.bot.client = Mock()
        self.bot.notifier = Mock()
        self.bot.tracker = PositionTracker(str(Path(self.temp.name) / 'state.json'))
        self.bot.risk_manager = RiskManager(410)
        self.bot.account_balance = None
        self.bot.client.get_wallet_balance.return_value = wallet_response()
        self.bot.tracker.add_position(dict(symbol="RLCUSDT", qty=6.7, entry_price=1.0745,
                                           open_time=1, opened_at_ms=1000))

    def test_error_does_not_delete_or_notify(self):
        self.bot.client.get_positions.side_effect = RuntimeError("Expired")
        self.bot.check_positions()
        self.assertIn('RLCUSDT', self.bot.tracker.positions)
        self.bot.notifier.send_trade_close.assert_not_called()

    def test_zero_size_avg_price_zero_waits_for_history(self):
        self.bot.client.get_positions.return_value = [dict(size="0", avgPrice="0")]
        self.bot.client.get_closed_pnl.return_value = []
        self.bot.check_positions()
        self.assertIn('RLCUSDT', self.bot.tracker.positions)
        self.bot.notifier.send_trade_close.assert_not_called()

    def test_history_error_does_not_delete_or_notify(self):
        self.bot.client.get_positions.return_value = []
        self.bot.client.get_closed_pnl.side_effect = TimeoutError()
        self.bot.check_positions()
        self.assertIn('RLCUSDT', self.bot.tracker.positions)
        self.bot.notifier.send_trade_close.assert_not_called()

    def test_confirmed_close_persists_once_across_restart(self):
        self.bot.client.get_positions.return_value = [dict(size="0", avgPrice="0")]
        self.bot.client.get_closed_pnl.return_value = [close_record()]
        self.bot.check_positions()
        self.bot.check_positions()
        self.bot.notifier.send_trade_close.assert_called_once()
        self.assertEqual(self.bot.notifier.send_trade_close.call_args.args[3], 1.1933)
        self.assertEqual(self.bot.risk_manager.current_capital, 410)  # no double-counted PnL
        reloaded = PositionTracker(self.bot.tracker.state_file)
        self.assertEqual(reloaded.positions, {})
        restored = RiskManager.from_state(reloaded.risk_state, 410)
        self.assertEqual(len(restored.trades_history), 1)
        self.assertEqual(restored.coin_stats['RLCUSDT']['trades'], 1)

    def test_failed_atomic_save_keeps_position_and_accounting(self):
        self.bot.client.get_positions.return_value = []
        self.bot.client.get_closed_pnl.return_value = [close_record()]
        with patch.object(self.bot.tracker, 'save', side_effect=OSError("Disk full")):
            self.bot.check_positions()
        self.assertIn('RLCUSDT', self.bot.tracker.positions)
        self.assertEqual(self.bot.risk_manager.trades_history, [])
        self.bot.notifier.send_trade_close.assert_not_called()

    def test_wallet_failure_invalidates_stale_balance_and_blocks_order(self):
        self.bot.refresh_balance()
        self.bot.client.get_wallet_balance.side_effect = RuntimeError("Expired")
        self.bot.open_position('TESTUSDT', {'local_high': 1})
        self.assertIsNone(self.bot.account_balance)
        self.bot.client.place_order.assert_not_called()

    def test_startup_without_balance_stays_unavailable_then_recovers(self):
        self.bot.risk_manager = None
        self.bot.client.get_wallet_balance.side_effect = RuntimeError("Expired")
        self.assertFalse(self.bot.refresh_balance())
        self.assertIsNone(self.bot.risk_manager)
        self.bot.client.get_wallet_balance.side_effect = None
        self.assertTrue(self.bot.refresh_balance())
        self.assertEqual(self.bot.risk_manager.current_capital, 410)

    def test_failed_position_check_blocks_new_entry(self):
        self.bot.client.get_positions.side_effect = TimeoutError()
        self.bot.open_position('TESTUSDT', {'local_high': 1})
        self.bot.client.place_order.assert_not_called()

    def prepare_entry(self):
        self.bot.risk_manager.current_capital = 666.39
        self.bot.client.get_wallet_balance.return_value = wallet_response(available="400")
        self.bot.client.get_tickers.return_value = {"result": {"list": [dict(symbol='TESTUSDT', lastPrice="1")]}}
        self.bot.client.get_instruments.return_value = {"result": {"list": [{
            "lotSizeFilter": {"qtyStep": "0.1", "minOrderQty": "0.1", "minNotionalValue": "5"},
            "priceFilter": {"tickSize": "0.01"},
        }]}}
        self.bot.client.place_order.return_value = {"retCode": 0, "result": {"orderId": "entry-1"}}
        self.bot.cancel_all_orders_for_symbol = Mock()

    def test_entry_sizing_uses_wallet_not_old_internal_capital(self):
        self.prepare_entry()
        self.bot.client.get_positions.side_effect = [[], [dict(side='Sell', size="8.2", avgPrice="1")]]
        with patch('bot.time.sleep'):
            self.bot.open_position('TESTUSDT', {'local_high': 1})
        self.assertEqual(self.bot.client.place_order.call_args.kwargs['qty'], "8.2")
        self.assertFalse(self.bot.tracker.positions['TESTUSDT']['entry_pending'])

    def test_hedge_mode_second_row_prevents_duplicate(self):
        self.bot.client.get_positions.return_value = [dict(size="0"), dict(size="5", side="Sell")]
        self.bot.open_position('TESTUSDT', {'local_high': 1})
        self.bot.client.place_order.assert_not_called()

    def test_accepted_order_is_kept_when_fill_query_fails(self):
        self.prepare_entry()
        self.bot.client.get_positions.side_effect = [[], TimeoutError()]
        with patch('bot.time.sleep'):
            self.bot.open_position('TESTUSDT', {'local_high': 1})
        self.assertTrue(self.bot.tracker.positions['TESTUSDT']['entry_pending'])
        self.bot.open_position('TESTUSDT', {'local_high': 1})
        self.bot.client.place_order.assert_called_once()

    def test_pending_order_recovers_after_restart(self):
        position = self.bot.tracker.positions['RLCUSDT']
        position.update(entry_pending=True, entry_order_id='entry-1', multiplier=0.5,
                        tp_price=0.6, sl_price=4)
        self.bot.tracker.save()
        self.bot.tracker = PositionTracker(self.bot.tracker.state_file)
        self.bot.client.get_positions.return_value = [dict(size='6.7', side='Sell')]
        self.bot.client.get_entry_fill.return_value = (6.7, 1.0745)
        self.bot.check_positions()
        self.assertFalse(self.bot.tracker.positions['RLCUSDT']['entry_pending'])
        self.bot.notifier.send_trade_open.assert_called_once()
        self.bot.notifier.send_trade_close.assert_not_called()

    def test_empty_cancelled_entry_has_no_close_profit(self):
        self.bot.tracker.positions['RLCUSDT'].update(entry_pending=True, entry_order_id='entry-1')
        self.bot.client.get_positions.return_value = []
        self.bot.client.get_entry_fill.return_value = (0, 0)
        self.bot.check_positions()
        self.assertNotIn('RLCUSDT', self.bot.tracker.positions)
        self.bot.notifier.send_trade_close.assert_not_called()

    def test_minimum_notional_cannot_exceed_available_funds(self):
        self.prepare_entry()
        self.bot.client.get_positions.return_value = []
        self.bot.client.get_wallet_balance.return_value = wallet_response(wallet='100', available='3')
        self.bot.open_position('TESTUSDT', {'local_high': 1})
        self.bot.client.place_order.assert_not_called()

    def test_zero_wallet_and_zero_peak_do_not_allow_entries(self):
        self.bot.client.get_wallet_balance.return_value = wallet_response(wallet='0', equity='0', available='0')
        self.bot.risk_manager = None
        self.bot.open_position('TESTUSDT', {'local_high': 1})
        self.bot.client.place_order.assert_not_called()

    def test_yesterdays_daily_report_has_exchange_balances_and_verified_pnl(self):
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        self.bot.risk_manager.on_trade_result(1.1933, 16, 'RLCUSDT', yesterday)
        self.bot.send_daily_stats()
        values = self.bot.notifier.send_daily_stats.call_args.kwargs
        self.assertEqual(values['date'], yesterday.date().isoformat())
        self.assertEqual(values['pnl'], 1.1933)
        self.assertEqual((values['balance'], values['equity'], values['unrealised_pnl']), (410, 400, -10))


class RiskAndStateTests(unittest.TestCase):
    def test_losses_survive_restart_and_do_not_add_to_wallet(self):
        manager = RiskManager(410)
        for _ in range(3):
            manager.on_trade_result(-1, -1, 'RLCUSDT')
        restored = RiskManager.from_state(manager.to_state(), 407)
        self.assertFalse(restored.can_trade_today(datetime.now(timezone.utc).date())[0])
        self.assertEqual(restored.current_capital, 407)
        self.assertEqual(restored.today_pnl, -3)

    def test_delayed_yesterdays_trade_does_not_affect_daily_pnl(self):
        manager = RiskManager(410)
        manager.on_trade_result(-1, -1, 'RLCUSDT', datetime.now(timezone.utc) - timedelta(days=1))
        self.assertEqual(manager.today_pnl, 0)

    def test_late_history_uses_close_time_for_consecutive_losses(self):
        manager = RiskManager(410)
        now = datetime.now(timezone.utc)
        manager.on_trade_result(1, 1, 'NEWUSDT', now)
        manager.on_trade_result(-1, -1, 'OLDUSDT', now - timedelta(minutes=5))
        self.assertEqual(manager.consecutive_losses, 0)

    def test_legacy_state_preserves_existing_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text(json.dumps(dict(positions={'RLCUSDT': {'qty': 6.7}}, watchlist={'A': {}}, cooldowns={'B': 1})))
            tracker = PositionTracker(path)
            tracker.save()
            restored = PositionTracker(path)
            self.assertEqual(restored.positions, {'RLCUSDT': {'qty': 6.7}})
            self.assertEqual(restored.watchlist, {'A': {}})
            self.assertEqual(restored.risk_state, {})

    def test_corrupt_state_stops_instead_of_overwriting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text('{broken')
            with self.assertRaises(ValueError):
                PositionTracker(path)
            self.assertEqual(path.read_text(), '{broken')

    def test_atomic_replace_failure_preserves_saved_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            tracker = PositionTracker(path)
            tracker.add_position(dict(symbol='RLCUSDT', qty=6.7))
            before = path.read_bytes()
            with patch('live.position_tracker.os.replace', side_effect=OSError('Disk full')):
                with self.assertRaises(OSError):
                    tracker.close_position('RLCUSDT', RiskManager(410).to_state())
            self.assertEqual(path.read_bytes(), before)
            self.assertIn('RLCUSDT', tracker.positions)


class TelegramTests(unittest.TestCase):
    def test_daily_message_separates_current_balances_from_closed_pnl(self):
        notifier = TelegramNotifier('test-token', 'test-chat')
        notifier.send_message = Mock()
        notifier.send_daily_stats('2026-10-29', 1, 1, 1.1933, 410, 400, -10)
        message = notifier.send_message.call_args.args[0]
        self.assertIn('1.1933', message)
        self.assertIn('equity сейчас: 400.00', message)
        self.assertIn('Плавающий PnL сейчас: -10.00', message)


if __name__ == '__main__':
    unittest.main()
