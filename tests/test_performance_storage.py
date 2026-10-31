import json
import sqlite3
from contextlib import closing
from unittest.mock import patch

from performance.storage import Store, BudgetExceeded
from tests.performance_fixtures import Offline, seed, position, wallet


class StorageTests(Offline):
    def test_migration_durability_and_reopen(self):
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.store.db.execute("PRAGMA synchronous").fetchone()[0], 2)
        seed(self.store)
        self.store.close()
        self.store = Store(self.path, reserve_bytes=0)
        self.assertEqual(len(self.store.facts("executions")), 4)

    def test_repeated_pages_and_observations_do_not_double_count(self):
        seed(self.store)
        seed(self.store)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM revisions").fetchone()[0], 14)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM ownership").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM strategy_events").fetchone()[0], 1)

    def test_revision_replaces_current_and_preserves_audit(self):
        values = seed(self.store)
        row = dict(values["closed_pnl"][0], closedPnl="11.2", updatedTime=6000)
        self.store.page("closed_pnl", 0, 7000, [row], 7000, final=True)
        self.assertEqual(self.store.facts("closed_pnl")[0]["closedPnl"], "11.2")
        self.store.page("closed_pnl", 0, 7000, values["closed_pnl"], 7001, final=True)
        self.assertEqual(self.store.facts("closed_pnl")[0]["closedPnl"], "11.2")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM revisions WHERE kind='closed_pnl'").fetchone()[0], 3)

    def test_bad_page_rolls_back_facts_and_checkpoint(self):
        values = seed(self.store)
        with self.assertRaises(ValueError):
            self.store.page("executions", 5001, 6000, [dict(values["executions"][0], execId="new"), {}], 6000, final=True)
        self.assertEqual(len(self.store.facts("executions")), 4)
        self.assertFalse(self.store.covered("executions", 0, 6000))

    def test_coverage_merges_overlaps_but_detects_holes(self):
        for start, end in ((100, 200), (190, 300), (302, 400)):
            self.store.page("orders", start, end, [], 500, final=True)
        self.assertTrue(self.store.covered("orders", 100, 300))
        self.assertFalse(self.store.covered("orders", 100, 400))

    def test_partial_page_persists_cursor_not_success(self):
        self.store.page("orders", 0, 5000, [], 5000, final=False, cursor="page-2")
        self.assertFalse(self.store.covered("orders", 0, 5000))
        self.assertIsNone(self.store.meta("watermark:orders"))
        self.assertEqual(self.store.db.execute("SELECT cursor FROM sync_cursors").fetchone()[0], "page-2")
        self.store.page("orders", 0, 5000, [], 5000, final=True)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM sync_cursors").fetchone()[0], 0)

    def test_failed_position_snapshot_keeps_old_portfolio(self):
        self.store.replace_positions([position()], 100)
        with self.assertRaises(ValueError):
            self.store.replace_positions([position(), {}], 200)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM positions_current").fetchone()[0], 1)
        self.assertEqual(self.store.meta("positions_success_ms"), "100")
        self.store.replace_positions([], 300)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM positions_current").fetchone()[0], 0)

    def test_money_preserved_as_original_strings(self):
        value = wallet()
        value["result"]["list"][0]["coin"][0]["walletBalance"] = "100.123456789123456789"
        self.store.snapshot(value, 100)
        self.assertEqual(self.store.latest_snapshot()[1]["walletBalance"], "100.123456789123456789")

    def test_budget_stops_writes_without_deleting_financial_facts(self):
        seed(self.store)
        self.store.max_bytes = 1
        with self.assertRaises(BudgetExceeded):
            self.store.snapshot(wallet(), 6000)
        self.assertEqual(len(self.store.facts("executions")), 4)

    def test_sqlite_busy_has_bounded_timeout(self):
        other = sqlite3.connect(self.path, timeout=0.1)
        self.addCleanup(other.close)
        other.execute("BEGIN IMMEDIATE")
        with self.assertRaises(sqlite3.OperationalError):
            self.store.snapshot(wallet(), 6000)
        other.rollback()
        self.store.snapshot(wallet(), 6000)

    def test_backup_includes_wal_and_refuses_overwrite(self):
        seed(self.store)
        target = self.path.with_name("backup.sqlite3")
        self.store.backup(target)
        with closing(sqlite3.connect(target)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM facts").fetchone()[0], 14)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        with self.assertRaises(ValueError):
            self.store.backup(target)

    def test_changing_read_key_cannot_mix_accounts(self):
        self.store.bind_account("synthetic-fingerprint-one")
        self.store.bind_account("synthetic-fingerprint-one")
        with self.assertRaises(ValueError):
            self.store.bind_account("synthetic-fingerprint-two")

    def test_payload_allowlist_discards_credentials_and_free_text(self):
        data = seed(self.store)
        row = dict(data["orders"][0], api_key="SECRET", orderLinkId="SECRET", rejectReason="SECRET", updatedTime=8000)
        self.store.page("orders", 0, 9000, [row], 9000, final=True)
        self.assertNotIn("SECRET", json.dumps(self.store.facts("orders")))

    def test_full_error_propagates_only_to_analytics_caller(self):
        with patch.object(self.store, "check_budget", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.snapshot(wallet(), 6000)
        self.assertEqual(self.store.latest_snapshot(), (None, {}))
