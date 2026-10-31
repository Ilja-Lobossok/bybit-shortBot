"""Static IaC contracts and reproducibility; all network blocked, no bot imports."""
import importlib.util
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "infra/grafana"


def load_catalog():
    spec = importlib.util.spec_from_file_location("health_catalog", MODULE / "catalog.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DashboardAsCodeTests(unittest.TestCase):
    def setUp(self):
        for guard in (patch.object(socket, "getaddrinfo", side_effect=AssertionError("Network forbidden")),
                      patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden"))):
            guard.start()
            self.addCleanup(guard.stop)
        self.catalog = load_catalog()
        self.dashboard = json.loads((MODULE / "dashboards/bot-health.json").read_text(encoding="utf-8"))
        self.alerts = json.loads((MODULE / "alerts/bot-health.rules.json").read_text(encoding="utf-8"))

    def test_dashboard_json_is_reproducible(self):
        self.assertEqual(self.dashboard, self.catalog.dashboard())

    def test_alert_json_is_reproducible(self):
        self.assertEqual(self.alerts, self.catalog.alerts())

    def test_stable_uid_and_sixty_second_refresh(self):
        self.assertEqual(self.dashboard["uid"], "bybit-bot-health")
        self.assertEqual(self.dashboard["refresh"], "1m")
        self.assertFalse(self.dashboard["editable"])

    def test_panels_have_unique_ids_and_no_overlap(self):
        occupied, ids = set(), set()
        for panel in self.dashboard["panels"]:
            self.assertNotIn(panel["id"], ids)
            ids.add(panel["id"])
            pos = panel["gridPos"]
            self.assertLessEqual(pos["x"] + pos["w"], 24)
            cells = {(x, y) for x in range(pos["x"], pos["x"] + pos["w"])
                     for y in range(pos["y"], pos["y"] + pos["h"])}
            self.assertFalse(cells & occupied)
            occupied |= cells

    def test_all_fourteen_production_metric_names_are_used(self):
        expressions = " ".join(target["expr"] for panel in self.dashboard["panels"] for target in panel.get("targets", []))
        self.assertEqual(set(re.findall(r"\bbot_[a-z_]+", expressions)), set(self.catalog.METRICS))

    def test_every_query_uses_existing_datasource_and_bounded_scope(self):
        for panel in self.dashboard["panels"]:
            for target in panel.get("targets", []):
                self.assertEqual(target["datasource"], self.catalog.DS)
                self.assertIn('environment="__ENVIRONMENT__"', target["expr"])
                self.assertIn('bot="bybit-shortbot"', target["expr"])
                self.assertIn('component="trading-bot"', target["expr"])

    def test_null_measurements_are_not_shown_as_zero(self):
        for panel in self.dashboard["panels"]:
            if panel["type"] != "text":
                self.assertEqual(panel["fieldConfig"]["defaults"]["noValue"], "No data")
                for target in panel.get("targets", []):
                    self.assertNotIn("vector(0)", target["expr"])
        status = self.dashboard["panels"][0]
        self.assertIn("vector(-1)", status["targets"][0]["expr"])
        self.assertEqual(status["fieldConfig"]["defaults"]["mappings"][0]["options"]["-1"]["text"], "NO DATA")

    def test_histogram_preserves_le_and_averages_sum_over_count(self):
        by_id = {panel["id"]: panel for panel in self.dashboard["panels"]}
        self.assertIn("sum by (le, operation) (rate(", by_id[13]["targets"][0]["expr"])
        self.assertIn("bot_api_duration_seconds_sum", by_id[14]["targets"][0]["expr"])
        self.assertIn("bot_api_duration_seconds_count", by_id[14]["targets"][0]["expr"])

    def test_rate_precedes_aggregation_for_restart_safe_counters(self):
        expr = next(panel["targets"][0]["expr"] for panel in self.dashboard["panels"] if panel["id"] == 11)
        self.assertIn("(rate(bot_api_requests_total", expr)
        for rule in self.alerts["rules"]:
            self.assertNotRegex(rule["expr"], r"(?:rate|increase)\(\s*sum")

    def test_restarts_are_labelled_as_observed_estimate(self):
        panel = next(panel for panel in self.dashboard["panels"] if panel["id"] == 6)
        self.assertIn("Observed", panel["title"])
        self.assertIn("Lower-bound", panel["description"])
        self.assertIn("changes((max(", panel["targets"][0]["expr"])

    def test_eight_rules_have_stable_bounded_ids_and_dashboard_links(self):
        panel_ids = {panel["id"] for panel in self.dashboard["panels"]}
        rules = self.alerts["rules"]
        self.assertEqual(len(rules), 8)
        self.assertEqual(len({rule["id"] for rule in rules}), 8)
        for rule in rules:
            self.assertRegex(rule["id"], r"^[a-z-]+$")
            self.assertIn(rule["panel_id"], panel_ids)
            self.assertIn(rule["severity"], ("critical", "warning"))

    def test_no_notification_policy_contact_credentials_or_execution_resources(self):
        terraform = "\n".join(path.read_text() for path in MODULE.glob("*.tf"))
        self.assertEqual(set(re.findall(r'resource\s+"([^"]+)"', terraform)),
                         {"grafana_folder", "grafana_dashboard", "grafana_rule_group"})
        for forbidden in ('provisioner "', 'resource "grafana_data_source"', 'resource "grafana_contact_point"',
                          'resource "grafana_notification_policy"', 'cloud_access_policy_token ='):
            self.assertNotIn(forbidden, terraform)
        self.assertNotRegex(terraform, r'\bauth\s*=')

    def test_default_paused_and_explicit_contact_guard_are_wired(self):
        variables = (MODULE / "variables.tf").read_text()
        alerts = (MODULE / "alerts.tf").read_text()
        self.assertRegex(variables, r'variable "alerts_enabled"\s*\{[^}]*default\s*=\s*false')
        self.assertIn("is_paused      = !var.alerts_enabled", alerts)
        self.assertIn('!var.alerts_enabled || trimspace(var.alert_contact_point_name) != ""', alerts)
        self.assertIn('contact_point   = notification_settings.value', alerts)

    def test_mock_tests_are_plan_only(self):
        tests = (MODULE / "tests/safety.tftest.hcl").read_text()
        self.assertIn('mock_provider "grafana"', tests)
        self.assertEqual(len(re.findall(r'run "', tests)), tests.count("command = plan"))
        self.assertNotIn("command = apply", tests)

    def test_runbook_has_a_section_for_every_rule(self):
        text = (ROOT / "docs/monitoring-stage3-runbook.md").read_text(encoding="utf-8")
        for rule in self.alerts["rules"]:
            self.assertRegex(text, r"(?m)^## " + re.escape(rule["id"]) + r"$")

    def test_git_tracks_json_but_ignores_credentials_state_and_plans(self):
        paths = {
            "infra/grafana/dashboards/bot-health.json": False,
            "infra/grafana/alerts/bot-health.rules.json": False,
            "infra/grafana/.terraform.lock.hcl": False,
            "infra/grafana/terraform.tfvars.example": False,
            "infra/grafana/terraform.tfvars": True,
            "infra/grafana/secret.auto.tfvars.json": True,
            "infra/grafana/terraform.tfstate": True,
            "infra/grafana/bot-health.tfplan": True,
            "infra/grafana/.terraform/providers/example": True,
            "infra/grafana/.test-output/rules.yaml": True,
        }
        for path, ignored in paths.items():
            with self.subTest(path=path):
                result = subprocess.run(["git", "check-ignore", "--no-index", "-q", path], cwd=ROOT)
                self.assertIn(result.returncode, (0, 1))
                self.assertEqual(result.returncode == 0, ignored)

    def test_progress_age_does_not_depend_on_process_resource_sampling(self):
        panel = next(panel for panel in self.dashboard["panels"] if panel["id"] == 2)
        self.assertNotIn("bot_process_start_timestamp_seconds", panel["targets"][0]["expr"])

    def test_renderer_works_with_no_credentials_or_network(self):
        # The subprocess only reads source and writes temporary synthetic fixtures.
        with tempfile.TemporaryDirectory() as output:
            result = subprocess.run([sys.executable, str(MODULE / "build.py"), "--check", "--promtool-dir", output],
                                    env={"SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")},
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(output) / "rules.yaml").is_file())
            self.assertTrue((Path(output) / "tests.yaml").is_file())


if __name__ == "__main__":
    unittest.main()
