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
        self.assertIn('default     = false', terraform)
        self.assertNotRegex(terraform.lower(), r'(resource|provider)\s+"render')
        self.assertNotIn("provisioner", terraform)

    def test_no_per_entity_metric_labels_or_loki_index_labels(self):
        dashboard = self.dashboard()
        for panel in dashboard["panels"]:
            for target in panel.get("targets", []):
                expr = target.get("expr", "")
                if target.get("datasource", {}).get("type") == "prometheus":
                    self.assertNotRegex(expr, r'\{[^}]*\b(order_id|exec_id|symbol|generation)=')
                if target.get("datasource", {}).get("type") == "loki":
                    self.assertTrue(expr.startswith('{service_name="bybit-performance"}') or expr.startswith("count("))
                    self.assertIn("generation=${projection}", expr)

    def test_projection_variable_uses_scalar_not_vector_sample_timestamp(self):
        variable = next(v for v in self.dashboard()["templating"]["list"] if v["name"] == "projection")
        self.assertIn("query_result(scalar(", variable["query"]["query"])
        regex = variable["regex"].strip("/")
        self.assertEqual(re.match(regex, "1793400000123").group(1), "1793400000123")
        self.assertIsNone(re.match(regex, "{} 123 1793400000123"))
        self.assertIsNone(re.match(regex, "NaN"))

    def test_integrity_panels_check_counts_freshness_and_truncation(self):
        dashboard = self.dashboard()
        for panel in dashboard["panels"]:
            if panel["id"] in (26, 27, 28):
                text = json.dumps(panel)
                for marker in ("projection_expected_rows", "projection_truncated", "projection_timestamp_seconds", "storage_degraded", "$A == $B"):
                    self.assertIn(marker, text)

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
