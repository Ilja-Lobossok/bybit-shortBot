"""Explicit offline suite. Never discovers tests/test_api.py or runs the bot."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MODULES = ("tests.test_monitoring_otel", "tests.test_monitoring_runtime", "tests.test_monitoring_bot", "tests.test_accounting",
           "tests.test_dashboard_as_code", "tests.test_performance_storage", "tests.test_performance_reconciliation",
           "tests.test_performance_reader", "tests.test_performance_exporters", "tests.test_performance_observations",
           "tests.test_performance_dashboard")


def main():
    env = {key: value for key, value in os.environ.items() if not key.startswith(("BYBIT_", "PERFORMANCE_", "GRAFANA_", "OTEL_", "TELEGRAM_"))}
    env.update(MONITORING_ENABLED="false", PERFORMANCE_ENABLED="false", PYTHONIOENCODING="utf-8")
    subprocess.run([sys.executable, "-m", "unittest", *MODULES, "-q"], cwd=ROOT, env=env, check=True)
    subprocess.run([sys.executable, "infra/grafana/build.py", "--check"], cwd=ROOT, env=env, check=True)


if __name__ == "__main__":
    main()
