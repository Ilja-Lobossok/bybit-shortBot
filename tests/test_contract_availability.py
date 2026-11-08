"""Only synthetic SDK errors and fake order clients; network forbidden."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from live.contract_availability import ContractAvailability, TTL, MAX_SYMBOLS
from tests.performance_fixtures import Offline
from tests.test_accounting import BotAccountingTests, close_record


class AvailabilityTests(Offline):
    def gate(self, **kwargs):
        return ContractAvailability(self.path.with_name('contract_availability.json'),clock=lambda:1_000_000,**kwargs)

    def test_only_confirmed_agreement_error_is_cached_and_persisted(self):
        gate=self.gate()
        for code in (110007,10006,110094,None):gate.record('TESTUSDT',SimpleNamespace(status_code=code))
        self.assertTrue(gate.allows_entry('TESTUSDT'))
        gate.record('TESTUSDT',SimpleNamespace(status_code=110126,request='SECRET',message='SECRET'))
        self.assertFalse(self.gate().allows_entry('TESTUSDT'))
        self.assertNotIn('SECRET',gate.path.read_text())
        self.assertFalse(self.path.with_name('state.json').exists())

    def test_agreement_rejection_expires_without_any_request(self):
        gate=self.gate();gate.record('TESTUSDT',SimpleNamespace(status_code=110126))
        gate.clock=lambda:1_000_000+TTL
        self.assertTrue(gate.allows_entry('TESTUSDT'))

    def test_recent_log_bootstrap_recognizes_exact_code_only(self):
        log=self.path.with_name('bot.log')
        stamp=datetime.fromtimestamp(999_999,timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        log.write_text(stamp+' - bot - ERROR - ❌ Ошибка открытия позиции TESTUSDT: required agreement (ErrCode: 110126)\n'+
                       stamp+' - bot - ERROR - ❌ Ошибка открытия позиции OTHERUSDT: no funds (ErrCode: 110007)\n',encoding='utf-8')
        gate=self.gate(log_path=log)
        self.assertFalse(gate.allows_entry('TESTUSDT'))
        self.assertTrue(gate.allows_entry('OTHERUSDT'))

    def test_cache_is_bounded_and_persistence_failure_still_blocks(self):
        gate=self.gate()
        with patch('live.contract_availability.os.open',side_effect=OSError('SECRET')):
            with self.assertLogs('live.contract_availability',level='WARNING') as logs:
                for i in range(MAX_SYMBOLS+5):gate.record(f'TEST{i}USDT',SimpleNamespace(status_code=110126))
        self.assertLessEqual(len(gate.blocked),MAX_SYMBOLS)
        self.assertNotIn('SECRET',str(logs.output))


class EntryGateTests(BotAccountingTests):
    def setUp(self):
        super().setUp()
        self.bot.contract_availability=ContractAvailability(self.bot.tracker.state_file.with_name('availability.json'))

    def test_blocked_symbol_has_no_api_cancel_or_order_calls(self):
        self.bot.contract_availability.record('TESTUSDT',SimpleNamespace(status_code=110126))
        self.bot.cancel_all_orders_for_symbol=Mock()
        self.bot.open_position('TESTUSDT',{'local_high':1})
        self.bot.client.assert_not_called()
        self.assertEqual(self.bot.client.mock_calls,[])
        self.bot.cancel_all_orders_for_symbol.assert_not_called()

    def test_original_rejection_is_attempted_once_then_survives_restart(self):
        self.prepare_entry()
        self.bot.client.get_positions.return_value=[]
        error=RuntimeError('synthetic agreement rejection');error.status_code=110126
        self.bot.client.place_order.side_effect=error
        self.bot.open_position('TESTUSDT',{'local_high':1})
        self.bot.contract_availability=ContractAvailability(self.bot.contract_availability.path)
        self.bot.open_position('TESTUSDT',{'local_high':1})
        self.bot.client.place_order.assert_called_once()
        self.bot.cancel_all_orders_for_symbol.assert_called_once()
        self.assertNotIn('TESTUSDT',self.bot.tracker.positions)

    def test_other_error_retains_existing_next_cycle_behavior(self):
        self.prepare_entry();self.bot.client.get_positions.return_value=[]
        error=RuntimeError('synthetic funds');error.status_code=110007
        self.bot.client.place_order.side_effect=error
        for _ in range(2):self.bot.open_position('TESTUSDT',{'local_high':1})
        self.assertEqual(self.bot.client.place_order.call_count,2)

    def test_cached_symbol_still_reconciles_existing_position_closure(self):
        self.bot.contract_availability.record('RLCUSDT',SimpleNamespace(status_code=110126))
        self.bot.client.get_positions.return_value=[]
        self.bot.client.get_closed_pnl.return_value=[close_record()]
        self.bot.check_positions()
        self.bot.notifier.send_trade_close.assert_called_once()

    def test_broken_availability_storage_does_not_stop_supported_market(self):
        self.prepare_entry()
        self.bot.contract_availability=SimpleNamespace(allows_entry=Mock(side_effect=OSError()),record=Mock())
        self.bot.client.get_positions.side_effect=[[],[dict(side='Sell',size='8.2',avgPrice='1')]]
        with patch('bot.time.sleep'):self.bot.open_position('TESTUSDT',{'local_high':1})
        self.bot.client.place_order.assert_called_once()
