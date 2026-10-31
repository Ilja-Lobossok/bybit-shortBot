"""Offline IaC checks. Requires initialized provider; never runs init/plan/apply."""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "infra/grafana"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--terraform", default="terraform")
    parser.add_argument("--promtool", default="promtool")
    args = parser.parse_args()
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("GRAFANA_", "TF_VAR_")) and key not in ("TF_LOG", "TF_LOG_PATH")}
    env.update(CHECKPOINT_DISABLE="1", TF_IN_AUTOMATION="1", GRAFANA_AUTH="offline-mock-only")

    def run(command):
        subprocess.run([str(part) for part in command], cwd=ROOT, env=env, check=True)

    output = MODULE / ".test-output"
    run([sys.executable, MODULE / "build.py", "--check", "--promtool-dir", output])
    run([sys.executable, "-m", "unittest", "tests.test_dashboard_as_code", "-q"])
    run([args.terraform, f"-chdir={MODULE}", "fmt", "-check", "-recursive"])
    run([args.terraform, f"-chdir={MODULE}", "validate", "-no-color"])
    # Do not silently accept zero tests (Windows uses backslashes in -filter).
    safety = MODULE / "tests/safety.tftest.hcl"
    if set(MODULE.rglob("*.tftest.hcl")) != {safety}:
        raise SystemExit("Unexpected Terraform test files; review them before execution")
    expected_runs = len(re.findall(r'^run "', safety.read_text(), re.MULTILINE))
    result = subprocess.run([args.terraform, f"-chdir={MODULE}", "test", "-no-color"],
                            cwd=ROOT, env=env, text=True, capture_output=True, check=True)
    print(result.stdout)
    if f"Success! {expected_runs} passed, 0 failed." not in result.stdout:
        raise SystemExit("Terraform did not execute all expected mock plan tests")
    run([args.promtool, "check", "rules", output / "rules.yaml"])
    run([args.promtool, "test", "rules", output / "tests.yaml"])
    print("Offline Grafana IaC checks passed. No apply, cloud API or trading calls.")


if __name__ == "__main__":
    main()
