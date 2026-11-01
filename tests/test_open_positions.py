import copy
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from performance.contracts import decimal as D
from performance.metrics import points
from performance.open_positions import summarize, risk_points, prune, GRAPH_LIMIT
from performance.reconciliation import reconcile, summaries
from performance.runtime import Runtime
from performance.storage import Store
from tests.performance_fixtures import Offline, position, seed, wallet


def pos(**changes):
    return dict(position(), curRealisedPnl='-2', createdTime=1000, **changes)


class OpenPositionTests(Offline):
    def test_costs_partial_realization_and_no_double_fee_subtraction(self):
        rows = [dict(position(), curRealisedPnl='-2'),
                dict(position(), symbol='OTHERUSDT', unrealisedPnl='-10', curRealisedPnl='3')]
        summary = summarize(rows)
        self.assertEqual(summary['unrealized'], D(10))
        self.assertEqual(summary['realized'], D(1))
        self.assertEqual(summary['combined'], D(11))
        self.assertEqual((summary['positive'], summary['negative']), (1, 1))
        self.assertEqual(summary['average_loss'], D(-7))
        self.assertEqual(summary['worst_loss'], D(-7))

    def test_missing_realized_is_unknown_and_not_a_zero_expense(self):
        result = summarize([position()])
        self.assertEqual(result['complete'], 0)
        self.assertIsNone(result['combined'])
        self.assertIsNone(result['negative'])
        self.assertEqual(result['unrealized'], D(20))

    def test_complete_empty_portfolio_and_no_losers(self):
        empty = summarize([])
        self.assertEqual((empty['count'], empty['combined'], empty['average_loss']), (0, D(0), D(0)))
        self.assertEqual(summarize([pos()])['worst_loss'], D(0))
        row = pos()
        row.update(unrealisedPnl='2')
        self.assertEqual(summarize([row])['breakeven'], 1)

    def test_restart_replay_and_partial_size_change_preserve_observed_minimum(self):
        row = pos()
        self.store.replace_positions([row], 10_000)
        row.update(unrealisedPnl='-8', curRealisedPnl='-3')
        self.store.replace_positions([row], 70_000)
        self.store.close()
        self.store = Store(self.path, reserve_bytes=0)
        # A delayed older page cannot reset current positions or risk.
        row.update(unrealisedPnl='100')
        self.store.replace_positions([row], 60_000)
        row.update(size='4', unrealisedPnl='1', curRealisedPnl='5')
        self.store.replace_positions([row], 130_000)
        risk = self.store.db.execute('SELECT * FROM position_runs').fetchone()
        self.assertEqual((risk['min_pnl'], risk['last_pnl'], risk['samples'], risk['gaps']), ('-11', '6', 3, 0))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM position_samples').fetchone()[0], 1)

    def test_close_reentry_and_direction_change_do_not_reuse_risk_cycle(self):
        self.store.replace_positions([pos()], 10_000)
        self.store.replace_positions([], 70_000)
        again = pos()
        again.update(createdTime=100_000, unrealisedPnl='-8')
        self.store.replace_positions([again], 130_000)
        # A close/reopen entirely between samples is detected if the API reports a new createdTime.
        again.update(createdTime=150_000, unrealisedPnl='1')
        self.store.replace_positions([again], 190_000)
        again.update(side='Buy')
        self.store.replace_positions([again], 250_000)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM position_runs').fetchone()[0], 4)
        result = risk_points(self.store, [again], 250_000)
        active = {(p[2]['direction'], p[1]) for p in result if p[0] == 'position_active'}
        self.assertEqual(active, {('long', 1), ('short', 0)})
        self.assertEqual({p[1] for p in result if p[0] == 'position_run_start_timestamp_seconds'}, {250.0})

    def test_gap_is_recorded_and_failed_snapshot_does_not_close_positions(self):
        self.store.replace_positions([pos()], 10_000)
        with self.assertRaises(ValueError):
            self.store.replace_positions([{}], 70_000)
        self.store.replace_positions([pos()], 300_000)
        row = self.store.db.execute('SELECT * FROM position_runs').fetchone()
        self.assertEqual((row['gaps'], row['closed_ms']), (1, None))
        self.assertEqual(row['first_ms'], 10_000)

    def test_missing_money_never_produces_invented_observed_zero(self):
        self.store.replace_positions([position()], 10_000)
        self.assertFalse(any(p[0] in ('position_combined_pnl', 'position_worst_observed_loss')
                             for p in risk_points(self.store, [position()], 10_000)))

    def test_graph_cardinality_cap_is_explicit_and_ids_are_not_labels(self):
        rows = [dict(pos(), symbol=f'PAIR{i:03}USDT') for i in range(GRAPH_LIMIT + 1)]
        self.store.replace_positions(rows, 10_000)
        output = risk_points(self.store, rows, 10_000)
        self.assertEqual(sum(name == 'position_combined_pnl' for name, _, _ in output), GRAPH_LIMIT)
        self.assertEqual(next(value for name, value, _ in output if name == 'position_graph_truncated'), 1)
        for name, value, labels in output:
            self.assertFalse({'order_id', 'exec_id', 'run_id', 'entity_key', 'generation'} & labels.keys())
            if 'symbol' in labels:
                self.assertEqual(set(labels), {'scope', 'currency', 'symbol', 'direction'})

    def test_sample_retention_never_deletes_financial_facts_or_run_minima(self):
        seed(self.store)
        self.store.replace_positions([pos()], 10_000)
        row = pos()
        row.update(unrealisedPnl='-10')
        self.store.replace_positions([row], 400_000)
        with self.store.db, patch('performance.open_positions.SAMPLE_MAX_ROWS', 1):
            prune(self.store, 500_000)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM position_samples').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute('SELECT min_pnl FROM position_runs').fetchone()[0], '-12')
        self.assertEqual(len(self.store.facts('executions')), 4)

    def test_v1_upgrade_preserves_existing_journal_and_does_not_fabricate_position_history(self):
        target = self.path.with_name('old.sqlite3')
        connection = sqlite3.connect(target)
        sql = (Path(__file__).parents[1] / 'performance/migrations/001_initial.sql').read_text()
        connection.executescript(sql + '\nINSERT INTO schema_migrations VALUES(1,1); PRAGMA user_version=1;')
        connection.execute('INSERT INTO positions_current VALUES(?,?,?)', ('TESTUSDT:0', 5000, json.dumps(pos())))
        connection.execute('INSERT INTO meta VALUES(?,?)', ('existing', 'preserved'))
        connection.commit()
        connection.close()
        migrated = Store(target, reserve_bytes=0)
        try:
            self.assertEqual(migrated.db.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertEqual(migrated.meta('existing'), 'preserved')
            self.assertEqual(migrated.db.execute('SELECT count(*) FROM positions_current').fetchone()[0], 1)
            self.assertEqual(migrated.db.execute('SELECT count(*) FROM position_runs').fetchone()[0], 0)
            migrated.replace_positions([pos()], 10_000)
            self.assertEqual(migrated.db.execute('SELECT first_ms FROM position_runs').fetchone()[0], 10_000)
        finally:
            migrated.close()

    def test_portfolio_points_do_not_depend_on_history_or_ownership(self):
        self.store.replace_positions([pos()], 10_000)
        runtime = Runtime({}, self.path.parent)
        runtime.tick(self.store, None, None, 10_000)
        account = {name: value for name, value, labels in runtime.cache if labels.get('scope') == 'account' and 'symbol' not in labels}
        bot = {name: value for name, value, labels in runtime.cache if labels.get('scope') == 'bot' and 'symbol' not in labels}
        self.assertEqual((account['positions'], account['open_combined_pnl']), (1, 18))
        self.assertEqual(bot['positions'], 0)
        self.assertEqual(account['cost_breakdown_complete'], 0)
        self.assertNotIn('fees', account)

    def test_covered_cost_decomposition_is_not_deducted_from_position_money(self):
        seed(self.store)
        self.store.replace_positions([pos()], 10_000)
        runtime = Runtime({}, self.path.parent)
        runtime.tick(self.store, None, None, 10_000)
        account = {name: value for name, value, labels in runtime.cache if labels.get('scope') == 'account' and 'symbol' not in labels}
        self.assertEqual(account['open_combined_pnl'], 18)
        self.assertEqual(account['fees'], 2)
        self.assertEqual(account['funding'], .5)
        self.assertEqual(account['realized_price_change'], 16)
        self.assertEqual(account['cost_breakdown_complete'], 1)

    def test_paid_funding_remains_negative_and_does_not_get_deducted_twice(self):
        from tests.performance_fixtures import transaction
        seed(self.store)
        self.store.page('transactions', 0, 5000,
                        [transaction('paid-funding', time=2501, type='SETTLEMENT', funding='-2', change='-2')], 5000, final=True)
        self.store.replace_positions([dict(pos(), curRealisedPnl='-4')], 10_000)
        runtime = Runtime({}, self.path.parent)
        runtime.tick(self.store, None, None, 10_000)
        account = {name: value for name, value, labels in runtime.cache if labels.get('scope') == 'account' and 'symbol' not in labels}
        self.assertEqual(account['funding'], -1.5)
        self.assertEqual(account['open_combined_pnl'], 16)

    def test_newer_funding_outside_common_watermark_is_not_presented_as_covered(self):
        from tests.performance_fixtures import transaction
        seed(self.store)
        self.store.page('transactions', 0, 6000,
                        [transaction('paid-funding', time=5500, type='SETTLEMENT', funding='-2', change='-2')], 6000, final=True)
        runtime = Runtime({}, self.path.parent)
        runtime.tick(self.store, None, None, 10_000)
        account = {name: value for name, value, labels in runtime.cache if labels.get('scope') == 'account' and 'symbol' not in labels}
        self.assertEqual(account['funding'], .5)
        self.assertEqual(next(value for name, value, _ in runtime.cache if name == 'coverage_end_timestamp_seconds'), 5)
