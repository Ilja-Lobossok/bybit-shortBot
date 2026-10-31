from copy import deepcopy
from performance.reconciliation import reconcile, summaries
from performance.calculations import statistics, transaction_totals, drawdown
from tests.performance_fixtures import Offline, seed, wallet, transaction


class ReconciliationTests(Offline):
    def test_partials_fees_funding_and_one_cycle(self):
        seed(self.store)
        view = reconcile(self.store, 5000)
        self.assertEqual(len(view["trades"]), 1)
        trade = view["trades"][0]
        self.assertEqual((trade["net_pnl"], trade["entry"], trade["exit"], trade["qty"]), ("14.5", "10", "8.4", "10"))
        self.assertEqual((trade["money_status"], trade["attribution_status"], trade["reason"]), ("reconciled", "bot", "tp"))
        self.assertEqual(trade["fees"], {"USDT": "2.0"})
        self.assertEqual(trade["funding"], "0.5")
        self.assertEqual(view["pending"], 0)

    def test_replay_after_restart_same_result(self):
        seed(self.store)
        first = reconcile(self.store, 5000)
        self.store.close()
        from performance.storage import Store
        self.store = Store(self.path, reserve_bytes=0)
        seed(self.store)
        self.assertEqual(reconcile(self.store, 6000), first)

    def test_foreign_trade_never_becomes_bot_result(self):
        seed(self.store, owner=False)
        view = reconcile(self.store, 5000)
        self.assertEqual(view["trades"][0]["attribution_status"], "unknown")
        stats = summaries(self.store, view, 5000)["statistics"]
        self.assertEqual(next(x for x in stats if x["scope"] == "account" and x["window"] == "all")["n"], 1)
        self.assertEqual(next(x for x in stats if x["scope"] == "bot" and x["window"] == "all")["n"], 0)

    def test_incomplete_page_or_missing_history_excludes_stats(self):
        seed(self.store, final=False)
        view = reconcile(self.store, 5000)
        self.assertEqual(view["history_status"], "incomplete")
        self.assertEqual(view["trades"][0]["money_status"], "incomplete")
        self.assertEqual(statistics(view["trades"])["n"], 0)

    def test_delayed_close_recovers_without_double_profit(self):
        rows = seed(self.store)
        with self.store.db:
            self.store.db.execute("DELETE FROM facts WHERE kind='closed_pnl' AND id='TESTUSDT:exit-b'")
        self.assertEqual(reconcile(self.store, 5000)["trades"][0]["money_status"], "incomplete")
        self.store.page("closed_pnl", 0, 5000, rows["closed_pnl"], 6000, final=True)
        self.assertEqual(reconcile(self.store, 6000)["trades"][0]["net_pnl"], "14.5")

    def test_changed_closed_pnl_cannot_stay_reconciled_against_old_cash(self):
        rows = seed(self.store)
        corrected = dict(rows["closed_pnl"][0], closedPnl="12.1", updatedTime=7000)
        self.store.page("closed_pnl", 0, 8000, [corrected], 8000, final=True)
        trade = reconcile(self.store, 8000)["trades"][0]
        self.assertEqual(trade["net_pnl"], "15.5")
        self.assertEqual(trade["money_status"], "incomplete")

    def test_incomplete_entry_fills_excluded(self):
        seed(self.store)
        with self.store.db:
            self.store.db.execute("DELETE FROM facts WHERE kind='executions' AND id='e2'")
        self.assertEqual(statistics(reconcile(self.store, 5000)["trades"])["n"], 0)

    def test_hedge_mode_does_not_guess_funding_allocation(self):
        rows = seed(self.store)
        self.store.page("orders", 0, 5000, [dict(x, positionIdx=2, updatedTime=6000) for x in rows["orders"]], 6000, final=True)
        trade = reconcile(self.store, 6000)["trades"][0]
        self.assertEqual(trade["money_status"], "incomplete")
        self.assertIsNone(trade["funding"])

    def test_no_price_based_reason_guess(self):
        rows = seed(self.store)
        self.store.page("orders", 0, 5000, [{k: v for k, v in dict(x, updatedTime=6000).items() if k != "stopOrderType"}
                                           for x in rows["orders"]], 6000, final=True)
        self.assertEqual(reconcile(self.store, 6000)["trades"][0]["reason"], "unknown")

    def test_exit_price_disagreement_excludes_cycle(self):
        rows = seed(self.store)
        self.store.page("closed_pnl", 0, 5000, [dict(rows["closed_pnl"][0], avgExitPrice="7", updatedTime=6000)], 6000, final=True)
        self.assertEqual(reconcile(self.store, 6000)["trades"][0]["money_status"], "incomplete")

    def test_missing_first_position_evidence_is_not_flat(self):
        rows = seed(self.store, owner=False)
        self.store.page("transactions", 0, 5000, [dict(rows["transactions"][0], size="-20")], 6000, final=True)
        self.assertEqual(reconcile(self.store, 6000)["trades"][0]["money_status"], "incomplete")

    def test_reversal_and_ioc_partial_cancel_are_not_extra_cycles(self):
        rows = seed(self.store)
        # A terminal IOC partial fill/cancel with fully recovered actual fills is valid.
        self.store.page("orders", 0, 5000, [dict(rows["orders"][0], qty="20", orderStatus="PartiallyFilledCanceled", updatedTime=6000)], 6000, final=True)
        self.assertEqual(reconcile(self.store, 6000)["trades"][0]["money_status"], "reconciled")
        # Mixed open+close execution is a reversal, not a guessed flat-to-flat split.
        self.store.page("executions", 0, 5000, [dict(rows["executions"][2], execQty="20", closedSize="6")], 6001, final=True)
        self.assertEqual(statistics(reconcile(self.store, 6001)["trades"])["n"], 0)

    def test_one_entry_two_exit_orders_not_four_fill_trades(self):
        seed(self.store)
        view = reconcile(self.store, 5000)
        counts = {(scope, outcome): value for scope, outcome, value in view["order_counts"]}
        self.assertEqual(counts["bot", "entry_filled"], 1)
        self.assertEqual(counts["bot", "exit_filled"], 2)
        self.assertEqual(counts["bot", "partial_exit_orders"], 1)
        self.assertEqual(counts["bot", "tp"], 2)

    def test_mixed_fee_currency_not_summed_as_usdt(self):
        rows = seed(self.store)
        self.store.page("executions", 0, 5000, [dict(rows["executions"][0], feeCurrency="BTC")], 6000, final=True)
        trade = reconcile(self.store, 6000)["trades"][0]
        self.assertIn("BTC", trade["fees"])
        self.assertEqual(trade["money_status"], "incomplete")

    def test_deposit_is_not_realized_profit(self):
        rows = seed(self.store)
        deposit = transaction("deposit", time=4500, type="TRANSFER_IN", cashFlow="1000", change="1000")
        totals = transaction_totals(rows["transactions"] + [deposit], start=0, end=5000)
        self.assertEqual(totals["realized"], "14.5")
        self.assertEqual(totals["nontrading"], "1000")
        self.assertEqual(totals["fees"], "2.0")

    def test_unknown_cash_type_and_spot_trade_do_not_pretend_complete(self):
        for row in (transaction("x", type="NEW_UNKNOWN"), transaction("x", category="spot")):
            self.assertFalse(transaction_totals([row], start=0, end=5000)["classification_complete"])

    def test_statistics_zero_breakeven_no_losses_and_pf(self):
        self.assertIsNone(statistics([])["net_pnl"])
        self.assertIsNone(statistics([])["win_rate"])
        trades = [{"net_pnl": v, "money_status": "reconciled"} for v in ("10", "-5", "0")]
        self.assertEqual(statistics(trades)["profit_factor"], "2")
        self.assertEqual(statistics(trades)["breakeven"], 1)
        self.assertEqual(statistics(trades[:1])["profit_factor_status"], "no_losses")

    def test_observed_drawdown_does_not_use_wallet_peak(self):
        self.assertEqual(drawdown(["100", "120", "90", "108"]), {"current": "0.1", "maximum": "0.25"})
        self.assertEqual(drawdown([]), {})

    def test_equity_history_never_backfilled_from_closes(self):
        seed(self.store)
        self.assertEqual(summaries(self.store, reconcile(self.store, 5000), 5000)["snapshot"], {})

    def test_transfer_adjusted_profit_requires_matching_wallet_delta_and_coverage(self):
        self.store.snapshot(wallet("100", "100", 0), 0)
        self.store.snapshot(wallet("1100", "1100", 5000), 5000)
        self.store.page("transactions", 0, 5000, [transaction("deposit", time=4000, type="TRANSFER_IN", cashFlow="1000", change="1000")], 5000, final=True)
        view = reconcile(self.store, 5000)
        self.assertEqual(summaries(self.store, view, 5000)["cash_adjusted_profit"], "0")
        self.store.snapshot(wallet("1200", "1200", 6000), 6000)
        self.assertEqual(summaries(self.store, view, 6000)["cash_adjusted_as_of_ms"], 5000)
        self.store.page("transactions", 0, 6000, [], 6000, final=True)
        self.assertIsNone(summaries(self.store, reconcile(self.store, 6000), 6000)["cash_adjusted_profit"])
