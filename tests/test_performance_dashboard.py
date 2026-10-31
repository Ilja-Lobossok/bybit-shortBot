import importlib.util
import json
from pathlib import Path
import re
from tests.performance_fixtures import Offline, seed

ROOT = Path(__file__).resolve().parents[1]


class DashboardTests(Offline):
    def dashboard(self):
        path = ROOT / "infra/grafana/performance_catalog.py"
        spec = importlib.util.spec_from_file_location("performance_catalog_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        dashboard = module.dashboard()
        self.assertEqual(dashboard, json.loads((ROOT / "infra/grafana/dashboards/trading-performance.json").read_text(encoding="utf-8")))
        return dashboard

    def test_reproducible_unique_layout_and_no_trading_resources(self):
        dashboard = self.dashboard()
        cells, ids = set(), set()
        for panel in dashboard["panels"]:
            self.assertNotIn(panel["id"], ids)
            ids.add(panel["id"])
            p = panel["gridPos"]
            occupied = {(x, y) for x in range(p["x"], p["x"] + p["w"]) for y in range(p["y"], p["y"] + p["h"])}
            self.assertFalse(occupied & cells)
            cells |= occupied
        self.assertEqual(dashboard["uid"], "bybit-trading-performance")
        terraform = (ROOT / "infra/grafana/performance.tf").read_text()
        self.assertIn('default     = true', terraform)
        self.assertNotRegex(terraform.lower(), r'(resource|provider)\s+"render')
        self.assertNotIn("provisioner", terraform)

    def test_only_existing_metrics_datasource_and_no_per_entity_labels(self):
        dashboard = self.dashboard()
        for panel in dashboard["panels"]:
            for target in panel.get("targets", []):
                expr = target.get("expr", "")
                self.assertEqual(target["datasource"], {"type": "prometheus", "uid": "__METRICS_DATASOURCE_UID__"})
                self.assertNotRegex(expr, r'\{[^}]*\b(order_id|exec_id|symbol|generation)=')
                self.assertNotIn("projection", expr)

    def test_no_loki_credentials_datasource_or_generation_setup(self):
        dashboard = self.dashboard()
        text = json.dumps(dashboard)
        self.assertNotIn("loki", text.lower())
        self.assertEqual({v["name"] for v in dashboard["templating"]["list"]}, {"scope", "window", "account"})
        terraform = (ROOT / "infra/grafana/performance.tf").read_text()
        self.assertNotIn("logs_datasource_uid", terraform)
        self.assertNotIn("performance_logs", terraform)

    def test_requested_financial_and_strategy_metrics_have_panels(self):
        text = json.dumps(self.dashboard())
        for suffix in ("wallet_balance", "equity", "exchange_cumulative_realized_pnl", "unrealized_pnl",
                       "net_closed_pnl", "wins", "losses", "win_rate", "profit_factor", "positions", "strategy_size", "decision_count"):
            self.assertIn("bot_performance_" + suffix, text)
        self.assertGreaterEqual(sum(p["type"] == "timeseries" for p in self.dashboard()["panels"]), 2)

    def test_decimal_export_is_read_only_and_never_overwrites(self):
        from scripts.performance_export import export
        seed(self.store)
        target = self.path.with_name("private-export.jsonl")
        before = self.store.db.execute("SELECT count(*) FROM facts").fetchone()[0]
        export(self.path, target, "closed_pnl")
        self.assertIn('"11.1"', target.read_text())
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM facts").fetchone()[0], before)
        with self.assertRaises(ValueError):
            export(self.path, target, "closed_pnl")

    def test_equity_export_preserves_both_timestamps(self):
        from scripts.performance_export import export
        from tests.performance_fixtures import wallet
        self.store.snapshot(wallet(time=4000), 5000)
        target = self.path.with_name("equity.jsonl")
        export(self.path, target, "equity")
        row = json.loads(target.read_text())
        self.assertEqual((row["exchange_ms"], row["received_ms"]), (4000, 5000))
