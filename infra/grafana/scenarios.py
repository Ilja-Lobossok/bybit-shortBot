"""Synthetic minute-spaced series for the real PromQL engine, never Bybit data."""
import json

import catalog

RULES = {rule["id"]: rule for rule in catalog.RULES}
BASE = {"bot": "bybit-shortbot", "component": "trading-bot", "environment": "production",
        "job": "bybit-shortbot", "instance": "test-process"}


def series(name, values, **labels):
    attrs = dict(BASE, **labels)
    labelset = ",".join(key + "=" + json.dumps(value) for key, value in sorted(attrs.items()))
    return {"series": name + "{" + labelset + "}",
            "values": " ".join("_" if value is None else str(value) for value in values)}


def healthy(minutes=45):
    timeline = range(minutes + 1)
    return [
        series("bot_main_progress_timestamp_seconds", [i * 60 for i in timeline]),
        series("bot_cycle_success_timestamp_seconds", [(i // 15) * 900 for i in timeline]),
        series("bot_balance_success_timestamp_seconds", [(i // 15) * 900 for i in timeline]),
        series("bot_process_start_timestamp_seconds", [0 for _ in timeline]),
        series("bot_disk_available_bytes", [1000000000 for _ in timeline]),
        series("bot_process_resident_memory_bytes", [50000000 for _ in timeline]),
        series("bot_process_cpu_seconds_total", [i * 0.1 for i in timeline]),
        series("bot_monitoring_export_errors_total", [0 for _ in timeline]),
        series("bot_monitoring_dropped_events_total", [0 for _ in timeline]),
        series("bot_api_requests_total", [i * 10 for i in timeline], operation="get_positions", result="success"),
        series("bot_api_requests_total", [0 for _ in timeline], operation="get_positions", result="error"),
        series("bot_api_errors_total", [0 for _ in timeline], operation="get_positions", category="exception"),
    ]


def replace(inputs, name, values=None, **labels):
    kept = [entry for entry in inputs if not entry["series"].startswith(name + "{")]
    return kept + ([] if values is None else [series(name, values, **labels)])


def alert(rule_id, minute, firing):
    rule = RULES[rule_id]
    return {"eval_time": f"{minute}m", "alertname": "BotHealth_" + rule_id.replace("-", "_"),
            "exp_alerts": ([{"exp_labels": {"bot": "bybit-shortbot", "component": "trading-bot",
                                             "environment": "production", "severity": rule["severity"]}}]
                           if firing else [])}


def expression(rule_id, minute, value):
    return {"expr": RULES[rule_id]["expr"].replace("__ENVIRONMENT__", "production"),
            "eval_time": f"{minute}m", "exp_samples": [{"labels": "{}", "value": value}]}


def scenario(name, inputs, alerts=(), expressions=()):
    return {"name": name, "interval": "1m", "input_series": inputs,
            "alert_rule_test": list(alerts), "promql_expr_test": list(expressions)}


def scenarios():
    cases = []
    quiet = healthy()
    quiet = replace(quiet, "bot_api_requests_total", [0] * 46, operation="get_positions", result="success")
    cases.append(scenario("healthy waiting; no requests is not API failure", quiet,
                          [alert(rule_id, 30, False) for rule_id in RULES]))
    stalled = replace(healthy(), "bot_main_progress_timestamp_seconds", [0] * 46)
    cases.append(scenario("stalled owner thread; honor strict threshold and pending", stalled,
                          [alert("heartbeat", 5, False), alert("heartbeat", 6, True)],
                          [expression("heartbeat", 3, 0), expression("heartbeat", 4, 1)]))
    cases.append(scenario("complete loss and never-exported bot", [],
                          [alert("heartbeat", 1, False), alert("heartbeat", 2, True)],
                          [expression("heartbeat", 1, 1)]))
    stale = replace(healthy(), "bot_main_progress_timestamp_seconds", [0, 60, 120, "stale"] + [None] * 42)
    cases.append(scenario("explicit stale marker does not create a healthy gap", stale,
                          [alert("heartbeat", 4, False), alert("heartbeat", 5, True)]))
    other = [series("bot_main_progress_timestamp_seconds", [i * 60 for i in range(46)], environment="development")]
    cases.append(scenario("other environment cannot mask missing production", other,
                          [alert("heartbeat", 2, True)]))
    no_start = replace(healthy(), "bot_process_start_timestamp_seconds")
    cases.append(scenario("heartbeat independent of failed process-start sampler", no_start,
                          [alert("heartbeat", 10, False)], [expression("heartbeat", 10, 0)]))
    old_cycle = replace(healthy(), "bot_cycle_success_timestamp_seconds", [0] * 46)
    cases.append(scenario("waiting is not candle success; old successful marker", old_cycle,
                          [alert("cycle", 25, False), alert("cycle", 26, True)],
                          [expression("cycle", 20, 0), expression("cycle", 21, 1)]))
    no_cycle = replace(healthy(), "bot_cycle_success_timestamp_seconds")
    cases.append(scenario("never-successful startup gets explicit 25m grace", no_cycle,
                          [alert("cycle", 30, False), alert("cycle", 31, True)],
                          [expression("cycle", 24, 0), expression("cycle", 26, 1)]))
    api = replace(healthy(), "bot_api_requests_total")
    api += [series("bot_api_requests_total", [i * 7 for i in range(46)], operation="get_positions", result="success"),
            series("bot_api_requests_total", [i * 3 for i in range(46)], operation="get_positions", result="error")]
    api = replace(api, "bot_api_errors_total", [i * 3 for i in range(46)], operation="get_positions", category="timeout")
    cases.append(scenario("real error count and ratio; separate pending windows", api,
                          [alert("bybit-errors", 3, True), alert("bybit-critical", 3, False), alert("bybit-critical", 4, True)],
                          [expression("bybit-critical", 1, 0), expression("bybit-critical", 2, 1)]))
    reset = replace(healthy(), "bot_api_errors_total", [5, 5, 5] + [0] * 43, operation="get_positions", category="exception")
    reset = replace(reset, "bot_api_requests_total", [100, 100, 100] + [10 * i for i in range(43)], operation="get_positions", result="success")
    cases.append(scenario("counter reset and initial nonzero batch are not new errors", reset,
                          [alert("bybit-errors", 10, False), alert("bybit-critical", 10, False)],
                          [expression("bybit-errors", 10, 0)]))
    disk = replace(healthy(), "bot_disk_available_bytes", [104857599] * 46)
    cases.append(scenario("disk below 100MiB; respect five-minute pending", disk,
                          [alert("disk", 4, False), alert("disk", 5, True)]))
    boundary = replace(healthy(), "bot_disk_available_bytes", [104857600] * 46)
    cases.append(scenario("disk exactly on boundary is not below it", boundary,
                          [alert("disk", 10, False)], [expression("disk", 10, 0)]))
    export = replace(healthy(), "bot_monitoring_export_errors_total", list(range(46)))
    cases.append(scenario("export failures; accumulated count and pending", export,
                          [alert("export", 4, False), alert("export", 5, True)]))
    dropped = replace(healthy(), "bot_monitoring_dropped_events_total", [0, 0] + [1] * 44)
    cases.append(scenario("mailbox drop; pending before warning", dropped,
                          [alert("dropped", 3, False), alert("dropped", 4, True)]))
    starts = []
    for index in range(4):
        values = [None] * (index * 10) + [index * 600] * 10 + ["stale"]
        values += [None] * (46 - len(values))
        starts.append(series("bot_process_start_timestamp_seconds", values, instance=f"process-{index}"))
    cases.append(scenario("instance churn; restart observation across series", starts,
                          [alert("restarts", 29, False), alert("restarts", 30, True)],
                          [expression("restarts", 29, 0), expression("restarts", 30, 1)]))
    overlap = healthy()
    overlap = replace(overlap, "bot_process_resident_memory_bytes", [999000000] * 46)
    overlap += [series("bot_process_start_timestamp_seconds", [None] * 10 + [600] * 36, instance="new-process"),
                series("bot_process_resident_memory_bytes", [None] * 10 + [50000000] * 36, instance="new-process")]
    memory_expr = next(panel["targets"][0]["expr"] for panel in catalog.dashboard()["panels"] if panel["id"] == 16)
    cases.append(scenario("old higher RSS cannot mask the newest process", overlap, expressions=[{
        "expr": memory_expr.replace("__ENVIRONMENT__", "production"), "eval_time": "12m",
        "exp_samples": [{"labels": "{}", "value": 50000000}]}]))
    return {"rule_files": ["rules.yaml"], "evaluation_interval": "1m", "tests": cases}
