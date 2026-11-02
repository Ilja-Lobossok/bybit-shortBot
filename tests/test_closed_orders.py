"""Offline regressions for delayed protective closes and journal boundaries."""
import json
from unittest.mock import Mock

from performance.bybit_reader import Collector, DAY
from performance.contracts import timestamp
from performance.reconciliation import reconcile, summaries
from performance.runtime import Runtime
from performance.storage import Store
from tests.performance_fixtures import Offline, seed
from tests.test_performance_reader import reply


class ClosedOrdersTests(Offline):
    def stats(self, scope='account'):
        view = reconcile(self.store, 130_000)
        return next(x for x in summaries(self.store, view, 130_000)['close_statistics']
                    if x['scope'] == scope and x['window'] == 'all')

    def test_partial_exit_orders_are_not_mislabeled_as_position_cycles(self):
        seed(self.store)
        view = reconcile(self.store, 130_000)
        self.assertEqual(len(view['trades']), 1)
        self.assertEqual(self.stats()['n'], 2)
        self.assertEqual(self.stats()['net_pnl'], '14.5')
        self.assertTrue(all(x['unit'] == 'closing_order' and x['money_status'] == 'exchange_reported'
                            for x in view['close_records']))

    def test_old_entry_missing_does_not_hide_exchange_reported_win_or_loss(self):
        rows = seed(self.store, owner=False)
        with self.store.db:
            self.store.db.execute("DELETE FROM facts WHERE kind IN ('executions','orders','transactions')")
        loss = dict(rows['closed_pnl'][1], closedPnl='-20', updatedTime=4500)
        self.store.page('closed_pnl', 0, 5000, [loss], 6000, final=True)
        stats = self.stats()
        self.assertEqual((stats['n'], stats['wins'], stats['losses'], stats['net_pnl']), (2, 1, 1, '-8.9'))
        self.assertEqual(self.stats('bot')['n'], 0)
        self.assertEqual(reconcile(self.store, 130_000)['trades'], [])

    def test_revisions_duplicates_and_restart_count_each_close_once(self):
        rows = seed(self.store)
        revised = dict(rows['closed_pnl'][1], closedPnl='-20', updatedTime=4500)
        for _ in range(2):
            self.store.page('closed_pnl', 0, 5000, rows['closed_pnl'] + [revised], 6000, final=True)
        before = self.stats()
        self.store.close()
        self.store = Store(self.path, reserve_bytes=0)
        self.assertEqual(self.stats(), before)
        self.assertEqual(before['net_pnl'], '-8.9')

    def test_update_time_selects_period_not_protective_order_creation_time(self):
        rows = seed(self.store)
        with self.store.db:
            self.store.set_meta('closed_history_origin_ms', 3500)
        revised = dict(rows['closed_pnl'][1], createdTime=1, updatedTime=4500, closedPnl='-20')
        self.store.page('closed_pnl', 0, 5000, [revised], 6000, final=True)
        self.assertEqual(timestamp('closed_pnl', revised), 4500)
        self.assertEqual((self.stats()['n'], self.stats()['net_pnl']), (1, '-20'))

    def test_imported_tracker_evidence_uses_ids_not_risk_money_or_symbol_alone(self):
        rows = seed(self.store, owner=False)
        state = self.path.with_name('state.json')
        state.write_text(json.dumps(dict(positions={}, risk_state=dict(trades_history=[
            dict(symbol='TESTUSDT', order_ids=['exit-a'], pnl_usdt=999999, token='SECRET'),
            dict(symbol='TESTUSDT', pnl_usdt=999999)]))))
        original = state.read_bytes()
        self.store.refresh_tracker_evidence(state, 6000)
        self.store.refresh_tracker_evidence(state, 6000)
        stats = self.stats('bot')
        self.assertEqual((stats['n'], stats['net_pnl']), (1, '11.1'))
        self.assertEqual(state.read_bytes(), original)
        owners = list(self.store.db.execute('SELECT * FROM ownership'))
        self.assertEqual(len(owners), 1)
        self.assertNotIn('SECRET', str([tuple(x) for x in owners]))
        self.assertEqual(len(self.store.facts('closed_pnl')), 2)

    def test_confirmed_close_proves_complete_cycle_without_fabricated_entry_id(self):
        seed(self.store, owner=False)
        state = self.path.with_name('state.json')
        state.write_text(json.dumps(dict(risk_state=dict(trades_history=[
            dict(symbol='TESTUSDT', order_ids=['exit-a', 'exit-b'])]))))
        self.store.refresh_tracker_evidence(state, 6000)
        view = reconcile(self.store, 130_000)
        self.assertEqual(view['trades'][0]['attribution_status'], 'bot')
        self.assertEqual(view['trades'][0]['money_status'], 'reconciled')
        self.assertEqual(self.stats('bot')['n'], 2)

    def test_mismatched_symbol_attestation_does_not_claim_foreign_close(self):
        seed(self.store, owner=False)
        state = self.path.with_name('state.json')
        state.write_text(json.dumps(dict(risk_state=dict(trades_history=[
            dict(symbol='OTHERUSDT', order_ids=['exit-a', 'exit-b'])]))))
        self.store.refresh_tracker_evidence(state, 6000)
        self.assertEqual(self.stats('bot')['n'], 0)

    def test_stop_loss_cycle_reconciles_fees_funding_and_preserves_negative_money(self):
        rows = seed(self.store)
        close = dict(rows['closed_pnl'][1], createdTime=100, avgExitPrice='20', closedPnl='-40.6', updatedTime=4500)
        execution = dict(rows['executions'][-1], execPrice='20')
        tx = dict(rows['transactions'][-1], cashFlow='-40', change='-40.4')
        order = dict(rows['orders'][-1], stopOrderType='StopLoss', updatedTime=4500)
        for kind, row in [('closed_pnl', close), ('executions', execution), ('transactions', tx), ('orders', order)]:
            self.store.page(kind, 0, 5000, [row], 6000, final=True)
        trade = reconcile(self.store, 130_000)['trades'][0]
        self.assertEqual((trade['money_status'], trade['net_pnl'], trade['funding']), ('reconciled', '-29.5', '0.5'))
        self.assertEqual(self.stats()['losses'], 1)
        self.assertEqual(self.stats()['average_loss'], '-40.6')

    def test_runtime_exports_closed_money_without_adding_any_trade_calls(self):
        seed(self.store)
        runtime = Runtime({}, self.path.parent)
        runtime.tick(self.store, None, None, 130_000)
        values = {name: value for name, value, labels in runtime.cache
                  if labels.get('scope') == 'account' and labels.get('window') == 'all'}
        self.assertEqual(values['closed_orders'], 2)
        self.assertEqual(values['closed_order_net_pnl'], 14.5)
        self.assertEqual(values['closed_order_losses'], 0)

    def test_backfill_has_independent_cursor_and_never_regresses_recent_watermark(self):
        with self.store.db:
            self.store.set_meta('watermark:closed_pnl', 39 * DAY)
        reader = Mock()
        reader.get.side_effect = lambda kind, params: reply(cursor='p' + str(int(params.get('cursor', 'p0')[1:]) + 1))
        collector = Collector(reader, self.store)
        collector.closed_backfill(40 * DAY)
        self.assertEqual(reader.get.call_count, 4)
        self.assertEqual(self.store.meta('watermark:closed_pnl'), str(39 * DAY))
        cursor = self.store.db.execute("SELECT cursor FROM sync_cursors WHERE kind='closed_pnl_backfill'").fetchone()[0]
        self.assertEqual(cursor, 'p4')
        reader.get.side_effect = None
        reader.get.return_value = reply()
        collector.closed_backfill(40 * DAY)
        self.assertEqual(reader.get.call_args.kwargs, {})
        self.assertEqual(reader.get.call_args_list[4].args[1]['cursor'], 'p4')
        self.assertEqual(self.store.meta('watermark:closed_pnl'), str(39 * DAY))

    def test_backfill_reaches_old_negative_and_marks_coverage_honestly(self):
        rows = seed(self.store, owner=False)
        old = dict(rows['closed_pnl'][0], closedPnl='-21', createdTime=1, updatedTime=12 * DAY)
        reader = Mock()
        reader.get.return_value = reply([old])
        collector = Collector(reader, self.store)
        collector.closed_backfill(40 * DAY)
        stats = self.stats()
        self.assertEqual(stats['coverage_complete'], 0)
        self.assertEqual(len(self.store.facts('closed_pnl')), 2)  # revision, not extra money
        self.assertEqual(next(x for x in self.store.facts('closed_pnl') if x['orderId'] == old['orderId'])['closedPnl'], '-21')

    def test_invalid_or_missing_state_cannot_replace_financial_facts(self):
        seed(self.store)
        state = self.path.with_name('state.json')
        self.store.refresh_tracker_evidence(state, 6000)
        state.write_text('{invalid')
        with self.assertRaises(ValueError):
            self.store.refresh_tracker_evidence(state, 6000)
        self.assertEqual(self.stats()['net_pnl'], '14.5')
