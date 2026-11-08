"""Storage exhaustion, sustained aggregation and restart regressions. Offline."""
import json
import sqlite3
from unittest.mock import patch
from performance.observations import Mailbox
from performance.runtime import Runtime
from performance.storage import Store, BudgetExceeded, DAY, WAL_HEADROOM
from tests.performance_fixtures import Offline, seed, wallet


class StorageRecoveryTests(Offline):
    def legacy(self, n=8000):
        payload = json.dumps({'reason':'turnover', 'old_detail':'x' * 1024})
        with self.store.db:
            self.store.db.executemany('INSERT INTO strategy_events VALUES(?,?,?,?,?,?)',
                                     [(f'legacy:{i}', i+1000, 'decision', 'TESTUSDT', 'turnover', payload) for i in range(n)])
            self.store.db.executemany('INSERT INTO observations VALUES(?,?)', [(f'legacy:{i}', i+1000) for i in range(n)])
        self.store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    def finance(self):
        return {table: [tuple(row) for row in self.store.db.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                for table in ('facts','revisions','ownership','account_snapshots','positions_current','position_runs')}

    def test_exhausted_legacy_file_recovers_without_vacuum_or_financial_deletion(self):
        seed(self.store)
        self.store.snapshot(wallet(), 5000)
        state = self.path.with_name('state.json')
        state.write_text('original-trading-state')
        self.legacy()
        before = self.finance()
        self.store.max_bytes = self.store.size() - 500_000
        with self.assertRaises(BudgetExceeded):
            self.store.check_budget()
        with patch('performance.storage.DETAIL_LIMIT',100), patch('performance.storage.OBSERVATION_LIMIT',200):
            self.store.maintenance(9*DAY)
        self.assertGreater(self.store.size(), self.store.max_bytes)  # allocated old pages still exist
        self.assertLess(self.store.used_size(), self.store.max_bytes)
        self.store.check_budget()
        self.assertEqual(self.finance(), before)
        self.assertEqual(state.read_text(), 'original-trading-state')
        self.store.snapshot(wallet(), 9*DAY)
        self.assertEqual(self.store.latest_snapshot()[0], 9*DAY)
        self.assertEqual(self.store.db.execute('PRAGMA quick_check').fetchone()[0], 'ok')

    def test_maintenance_can_run_when_write_budget_is_exceeded(self):
        self.legacy(1000)
        with patch.object(self.store,'check_budget',side_effect=BudgetExceeded()), \
                patch('performance.storage.DETAIL_LIMIT',10), patch('performance.storage.OBSERVATION_LIMIT',20):
            self.store.maintenance(100_000)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM strategy_events').fetchone()[0],10)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM observations').fetchone()[0],20)

    def test_expired_dedup_ids_do_not_recount_on_restart(self):
        self.store.observe(dict(id='old',time_ms=1000,kind='decision',body=dict(symbol='TESTUSDT',reason='ready')))
        self.store.maintenance(9*DAY)
        self.store.close()
        self.store=Store(self.path,reserve_bytes=0)
        self.store.observe(dict(id='old',time_ms=1000,kind='decision',body=dict(symbol='TESTUSDT',reason='ready')))
        self.assertEqual(self.store.meta('decision:ready'),'1')

    def test_wal_and_real_free_disk_limits_still_stop_writes(self):
        with patch.object(self.store,'size',return_value=self.store.max_bytes+WAL_HEADROOM+32769):
            with self.assertRaises(BudgetExceeded): self.store.check_budget()

    def test_long_reader_defers_cleanup_then_recovers_without_losing_money(self):
        seed(self.store);self.legacy(3000)
        before=self.finance()
        reader=sqlite3.connect(self.path)
        try:
            reader.execute('BEGIN')
            reader.execute('SELECT count(*) FROM strategy_events').fetchone()
            with patch('performance.storage.WAL_HEADROOM',128*1024), patch('performance.storage.DETAIL_LIMIT',10):
                self.store.maintenance(100_000)
                left=self.store.db.execute('SELECT count(*) FROM strategy_events').fetchone()[0]
                self.assertGreater(left,10)
            self.assertEqual(self.finance(),before)
        finally:
            reader.close()
        with patch('performance.storage.DETAIL_LIMIT',10):self.store.maintenance(100_000)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM strategy_events').fetchone()[0],10)
        self.assertEqual(self.finance(),before)
        self.store.reserve_bytes=100
        with patch('performance.storage.shutil.disk_usage') as usage:
            usage.return_value.free=99
            with self.assertRaises(BudgetExceeded): self.store.check_budget()

    def test_hot_market_rejections_do_not_crowd_out_entries(self):
        mailbox=Mailbox(capacity=2)
        for _ in range(10_000): mailbox.emit('decision',dict(symbol='TESTUSDT',reason='turnover'))
        mailbox.emit('entry',dict(symbol='TESTUSDT',order_id='entry-proof',flat_before=True))
        mailbox.emit('strategy_snapshot',dict(rows=[dict(symbol='TESTUSDT',ready=True)]))
        events=mailbox.take()
        self.assertEqual(len(events),3)
        self.assertEqual(mailbox.dropped,0)
        for event in events: self.store.observe(event)
        self.assertEqual(self.store.meta('decision:turnover'),'10000')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM ownership').fetchone()[0],1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM strategy_events').fetchone()[0],2)
        for event in events: self.store.observe(event)
        self.assertEqual(self.store.meta('decision:turnover'),'10000')

    def test_ten_days_of_scans_remain_bounded_and_counts_survive_restart(self):
        mailbox=Mailbox()
        for day in range(10):
            for scan in range(24):
                for _ in range(1000): mailbox.emit('decision',dict(symbol='TESTUSDT',reason='pump'))
                event=mailbox.take()[0]
                event['time_ms']=day*DAY+scan*3_600_000+1000
                self.store.observe(event)
            self.store.maintenance((day+1)*DAY)
        self.assertEqual(self.store.meta('decision:pump'),'240000')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM strategy_events').fetchone()[0],0)
        self.assertLessEqual(self.store.db.execute('SELECT count(*) FROM observations').fetchone()[0],168)
        self.assertLess(self.store.used_size(),2*1024**2)
        self.store.close();self.store=Store(self.path,reserve_bytes=0)
        self.assertEqual(self.store.meta('decision:pump'),'240000')

    def test_invalid_aggregate_rolls_back_its_dedup_id(self):
        for body in (dict(reason='pump',count=-1),dict(reason='pump',count=True),dict(symbol='TESTUSDT',reason='ready',count=2)):
            with self.assertRaises(ValueError):
                self.store.observe(dict(id='bad',time_ms=1000,kind='decision',body=body))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM observations').fetchone()[0],0)

    def test_readonly_history_catchup_cadence_returns_to_normal(self):
        self.assertEqual(Runtime.history_interval(self.store,2_000_000),60_000)
        with self.store.db:
            for kind in ('executions','orders','closed_pnl','transactions'):self.store.set_meta('watermark:'+kind,1_900_000)
        self.assertEqual(Runtime.history_interval(self.store,2_000_000),300_000)
