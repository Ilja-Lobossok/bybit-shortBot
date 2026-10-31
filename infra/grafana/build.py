"""Generate checked-in JSON and offline promtool inputs; no network or bot imports."""
import argparse
import json
from pathlib import Path

import catalog
import performance_catalog

ROOT = Path(__file__).resolve().parent


def encoded(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def resolve(expr, environment="production"):
    return (expr.replace("__ENVIRONMENT__", environment)
            .replace("${operation:regex}", ".*").replace("$__range", "30m")
            .replace("${account}", "primary").replace("${scope}", "bot").replace("${window}", "all")
            .replace("${projection}", "12345"))


def promtool_rules():
    # Prometheus alerts fire on any returned sample, even 0. Grafana instead
    # evaluates the A (0/1) query through B > 0.5. Reproduce that filter exactly.
    threshold = catalog.alerts()["condition_threshold"]
    alerts = [{"alert": "BotHealth_" + rule["id"].replace("-", "_"),
               "expr": "(" + resolve(rule["expr"]) + ") > " + str(threshold),
               "for": rule["pending"], "labels": {"bot": "bybit-shortbot", "environment": "production",
                                                    "component": "trading-bot", "severity": rule["severity"]}}
              for rule in catalog.RULES]
    queries = [{"record": f"test:panel_{panel['id']}_{target['refId']}", "expr": resolve(target["expr"])}
               for panel in catalog.dashboard()["panels"] for target in panel.get("targets", [])]
    queries.extend({"record": f"test:performance_{panel['id']}_{target['refId']}", "expr": resolve(target["expr"])}
                   for panel in performance_catalog.dashboard()["panels"] for target in panel.get("targets", [])
                   if target.get("datasource", {}).get("type") == "prometheus")
    return {"groups": [{"name": "bot-health", "interval": "1m", "rules": alerts},
                       {"name": "dashboard-query-syntax", "interval": "1m", "rules": queries}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Detect JSON drift; do not write")
    parser.add_argument("--promtool-dir", type=Path, help="Write synthetic rule tests into this local directory")
    args = parser.parse_args()
    generated = {ROOT / "dashboards/bot-health.json": catalog.dashboard(),
                 ROOT / "alerts/bot-health.rules.json": catalog.alerts(),
                 ROOT / "dashboards/trading-performance.json": performance_catalog.dashboard()}
    for path, content in generated.items():
        expected = encoded(content)
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != expected:
                parser.exit(1, "Generated JSON differs: " + str(path) + "\n")
        elif not args.promtool_dir:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(expected, encoding="utf-8")
    if args.promtool_dir:
        from scenarios import scenarios
        args.promtool_dir.mkdir(parents=True, exist_ok=True)
        # JSON is a YAML subset understood by promtool; no PyYAML dependency.
        (args.promtool_dir / "rules.yaml").write_text(encoded(promtool_rules()), encoding="utf-8")
        (args.promtool_dir / "tests.yaml").write_text(encoded(scenarios()), encoding="utf-8")
    print("Grafana JSON check OK" if args.check else "Grafana artifacts generated locally")


if __name__ == "__main__":
    main()
