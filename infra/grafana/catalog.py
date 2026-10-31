"""Dashboard and alert source of truth. Stdlib only; never imports trading code."""

METRICS = (
    "bot_main_progress_timestamp_seconds", "bot_cycle_success_timestamp_seconds",
    "bot_balance_success_timestamp_seconds", "bot_api_requests_total",
    "bot_api_errors_total", "bot_api_duration_seconds_bucket",
    "bot_api_duration_seconds_count", "bot_api_duration_seconds_sum",
    "bot_process_cpu_seconds_total", "bot_process_resident_memory_bytes",
    "bot_disk_available_bytes", "bot_process_start_timestamp_seconds",
    "bot_monitoring_dropped_events_total", "bot_monitoring_export_errors_total",
)
OPERATIONS = (
    "get_instruments_info", "get_tickers", "get_kline", "get_wallet_balance",
    "place_order", "get_positions", "get_order_history", "get_closed_pnl",
    "get_open_orders", "cancel_order", "other",
)
UID = "bybit-bot-health"
DS = {"type": "prometheus", "uid": "__METRICS_DATASOURCE_UID__"}
SELECTOR = '{bot="bybit-shortbot",component="trading-bot",environment="__ENVIRONMENT__"}'


def metric(name, extra=""):
    return name + SELECTOR[:-1] + ("," + extra if extra else "") + "}"


START = metric("bot_process_start_timestamp_seconds")
LATEST = f"({START} == on() group_left() max({START}))"


def current(name):
    # Grafana Cloud promotes service.name / service.instance.id to job / instance.
    # Filter gauges/totals to the latest process during the old series' lookback.
    return f"({metric(name)} and on(job, instance) {LATEST})"


def age(name):
    # Progress/success do not depend on successful psutil sampling of process start.
    return f"time() - max({metric(name)})"


ERRORS = f'sum(increase({metric("bot_api_errors_total")}[15m]))'
REQUESTS = f'sum(increase({metric("bot_api_requests_total")}[15m]))'
RESTARTS_30M = f"changes((max({START}))[30m:1m])"

RULES = [
    dict(id="heartbeat", name="Bot heartbeat stalled or missing", severity="critical", panel_id=1,
         expr=f'({age("bot_main_progress_timestamp_seconds")} > bool 180) or on() absent(max({metric("bot_main_progress_timestamp_seconds")}))',
         pending="2m", no_data="Alerting",
         summary="Bot progress is stale or telemetry is unavailable.",
         description="Progress age >180s or no current marker, pending 2m. Inspect Render and Cloud; do not restart or send orders automatically."),
    dict(id="cycle", name="No successful trading candle", severity="warning", panel_id=3,
         expr=f'({age("bot_cycle_success_timestamp_seconds")} > bool 1200) or on() ((time() - max({START}) > bool 1500) * on() absent(max({current("bot_cycle_success_timestamp_seconds")}))) or on() vector(0)',
         pending="5m", no_data="OK",
         summary="No recent fully successful candle.",
         description="Success age >20m; if never successful, allow 25m after process start, then 5m pending. Waiting is not candle success."),
    dict(id="bybit-errors", name="Bybit API errors observed", severity="warning", panel_id=12,
         expr=f"({ERRORS} > bool 0) or on() vector(0)", pending="2m", no_data="OK",
         summary="Existing Bybit SDK calls returned errors.",
         description="New errors within 15m; inspect operation/category. Do not retry an order manually."),
    dict(id="bybit-critical", name="Bybit API critical error rate", severity="critical", panel_id=12,
         expr=f"(({ERRORS} >= bool 5) * on() ({ERRORS} / {REQUESTS} >= bool 0.25)) or on() vector(0)",
         pending="2m", no_data="OK", summary="At least five API errors and >=25% errors over 15m.",
         description="Read-call retries are counted as existing SDK invocations. No traffic alone is not an API failure."),
    dict(id="disk", name="Persistent disk critically low", severity="critical", panel_id=17,
         expr=f'max({current("bot_disk_available_bytes")}) < bool 104857600',
         pending="5m", no_data="NoData", summary="Less than 100 MiB free on DATA_DIR filesystem.",
         description="Inspect disk usage read-only. Never delete state.json, backups or logs automatically."),
    dict(id="export", name="Telemetry exports failing", severity="warning", panel_id=18,
         expr=f'(sum(increase({metric("bot_monitoring_export_errors_total")}[15m])) >= bool 3) or on() vector(0)',
         pending="2m", no_data="OK", summary="At least three failed telemetry exports over 15m.",
         description="Errors may arrive only after delivery recovers. Missing delivery is covered by the heartbeat rule."),
    dict(id="dropped", name="Telemetry events dropped", severity="warning", panel_id=18,
         expr=f'(sum(increase({metric("bot_monitoring_dropped_events_total")}[15m])) > bool 0) or on() vector(0)',
         pending="2m", no_data="OK", summary="Bounded telemetry mailbox dropped observations.",
         description="Technical counters/latency may undercount. Trading remains fail-open; inspect CPU/RAM and Cloud delivery."),
    dict(id="restarts", name="Repeated observed process starts", severity="warning", panel_id=6,
         expr=f"({RESTARTS_30M} > bool 2) or on() vector(0)", pending="0s", no_data="OK",
         summary="More than two observed start-time changes in 30m.",
         description="Lower-bound estimate sampled every minute, not a Render restart counter. Do not restart again to test this alert."),
]


def target(expr, *, legend="", instant=False, ref="A"):
    return {"datasource": DS, "editorMode": "code", "expr": expr,
            "refId": ref, "legendFormat": legend, "instant": instant, "range": not instant}


def panel(number, title, expr, x, y, w, h, *, unit="short", description="", stat=False,
          thresholds=None, mappings=None, legend="", extra_targets=()):
    defaults = {"unit": unit, "noValue": "No data", "decimals": 2,
                "color": {"mode": "thresholds" if stat else "palette-classic"},
                "mappings": mappings or [],
                "thresholds": {"mode": "absolute", "steps": thresholds or [{"color": "blue", "value": None}]}}
    return {"id": number, "type": "stat" if stat else "timeseries", "title": title,
            "description": description, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": ({"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                         "colorMode": "background", "graphMode": "none", "textMode": "auto",
                         "orientation": "auto"} if stat else
                        {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["lastNotNull"]},
                         "tooltip": {"mode": "multi", "sort": "desc"}}),
            "targets": [target(expr, legend=legend, instant=stat), *extra_targets]}


def dashboard():
    op = 'operation=~"${operation:regex}"'
    api = metric("bot_api_requests_total", op)
    errors = metric("bot_api_errors_total", op)
    hist = metric("bot_api_duration_seconds_bucket", op)
    duration_sum = metric("bot_api_duration_seconds_sum", op)
    duration_count = metric("bot_api_duration_seconds_count", op)
    red = lambda limit: [{"color": "green", "value": None}, {"color": "red", "value": limit}]
    state_maps = [{"type": "value", "options": {
        "-1": {"text": "NO DATA", "color": "gray"},
        "0": {"text": "STALE", "color": "red"},
        "1": {"text": "PROGRESSING", "color": "green"}}}]
    panels = [
        panel(1, "Bot progress", f'({age("bot_main_progress_timestamp_seconds")} <= bool 180) or on() vector(-1)', 0, 0, 4, 5, stat=True, mappings=state_maps,
              description="Main-thread progress only. Green does not assert a successful candle or profitable trading."),
        panel(2, "Main progress age", age("bot_main_progress_timestamp_seconds"), 4, 0, 4, 5, stat=True, unit="s", thresholds=red(180)),
        panel(3, "Successful candle age", age("bot_cycle_success_timestamp_seconds"), 8, 0, 4, 5, stat=True, unit="s", thresholds=red(1200),
              description="15-minute work schedule. Waiting every 5s is not success; absent before first complete clean candle."),
        panel(4, "Last successful candle", f'max({metric("bot_cycle_success_timestamp_seconds")}) * 1000', 12, 0, 4, 5, stat=True, unit="dateTimeAsIso"),
        panel(5, "Process uptime", f"time() - max({START})", 16, 0, 4, 5, stat=True, unit="s"),
        panel(6, "Observed restarts / selected range", f"changes((max({START}))[$__range:1m])", 20, 0, 4, 5, stat=True,
              description="Lower-bound estimate of changes in the newest process start timestamp. Processes missed between 60s exports/subquery steps cannot be counted; not an exact Render counter."),
        panel(7, "Validated balance age", age("bot_balance_success_timestamp_seconds"), 0, 5, 6, 5, stat=True, unit="s", thresholds=red(1200)),
        panel(8, "Last validated balance", f'max({metric("bot_balance_success_timestamp_seconds")}) * 1000', 6, 5, 6, 5, stat=True, unit="dateTimeAsIso"),
        panel(9, "Process start", f"max({START}) * 1000", 12, 5, 6, 5, stat=True, unit="dateTimeAsIso"),
        panel(10, "Heartbeat sample age", f'time() - max(timestamp({metric("bot_main_progress_timestamp_seconds")}))', 18, 5, 6, 5, stat=True, unit="s", thresholds=red(180),
              description="Age of the ingested sample, distinct from the timestamp value measuring main-thread progress."),
        panel(11, "Bybit SDK requests / s (15m window)", f"sum by (operation, result) (rate({api}[15m]))", 0, 10, 12, 8,
              unit="reqps", legend="{{operation}} / {{result}}", description="Existing SDK invocations, not physical HTTP retries. Quiet intervals between candles are normal."),
        panel(12, "Bybit errors (15m increase)", f"sum by (operation, category) (increase({errors}[15m]))", 12, 10, 12, 8,
              legend="{{operation}} / {{category}}"),
        panel(13, "Bybit p95 latency (15m)", f"histogram_quantile(0.95, sum by (le, operation) (rate({hist}[15m])))", 0, 18, 12, 8,
              unit="s", legend="{{operation}}", description="No observations means No data, not zero latency; sparse-request quantiles are approximate."),
        panel(14, "Bybit mean latency (15m)", f"sum by (operation) (rate({duration_sum}[15m])) / sum by (operation) (rate({duration_count}[15m]))", 12, 18, 12, 8, unit="s", legend="{{operation}}"),
        panel(15, "Process CPU / one core", f'sum(rate({metric("bot_process_cpu_seconds_total")}[5m]))', 0, 26, 12, 8,
              unit="percentunit", description="100% = one fully used CPU core. This is not a percentage of a confirmed Render container CPU limit."),
        panel(16, "Process RSS", f'max({current("bot_process_resident_memory_bytes")})', 12, 26, 12, 8, unit="bytes"),
        panel(17, "DATA_DIR filesystem available", f'max({current("bot_disk_available_bytes")})', 0, 34, 12, 8, unit="bytes",
              thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 104857600}, {"color": "green", "value": 209715200}],
              description="Free bytes in the filesystem containing persistent DATA_DIR. No total/percentage is invented."),
        panel(18, "Telemetry failures / drops (15m increase)", f'sum(increase({metric("bot_monitoring_export_errors_total")}[15m]))', 12, 34, 12, 8, legend="Export failures",
              extra_targets=[target(f'sum(increase({metric("bot_monitoring_dropped_events_total")}[15m]))', ref="B", legend="Dropped events")],
              description="Export errors can be received only after delivery recovers; use heartbeat loss for total outages."),
        panel(19, "Export errors / current process", f'sum({current("bot_monitoring_export_errors_total")})', 0, 42, 6, 5, stat=True, thresholds=red(1)),
        panel(20, "SDK calls / current process", f'sum({current("bot_api_requests_total")})', 6, 42, 6, 5, stat=True,
              description="Cumulative counter, including the first exported batch. API operation filter applies to charts, not these whole-process totals."),
        panel(21, "API errors / current process", f'sum({current("bot_api_errors_total")})', 12, 42, 6, 5, stat=True, thresholds=red(1)),
        panel(22, "CPU time / current process", f'max({current("bot_process_cpu_seconds_total")})', 18, 42, 6, 5, stat=True, unit="s"),
        {"id": 23, "type": "text", "title": "How to read this dashboard", "gridPos": {"x": 0, "y": 47, "w": 24, "h": 5},
         "options": {"mode": "markdown", "content": "Metrics export about every **60s**; candle processing is about every **15m**. **No data** is not healthy/zero. Quiet API intervals and missing latency before observations are normal. Restart count is an observed estimate. This dashboard shows technical health, not equity or P&L. Alerts never restart the worker or place orders."}},
    ]
    return {"uid": UID, "title": "Bybit ShortBot · Bot Health", "schemaVersion": 39,
            "version": 1, "editable": False, "timezone": "browser", "refresh": "1m",
            "tags": ["bybit-shortbot", "monitoring-stage3", "managed-by-terraform"],
            "time": {"from": "now-6h", "to": "now"}, "links": [], "annotations": {"list": []},
            "templating": {"list": [
                {"name": "environment", "type": "constant", "query": "__ENVIRONMENT__", "hide": 0,
                 "current": {"text": "__ENVIRONMENT__", "value": "__ENVIRONMENT__"}},
                {"name": "operation", "label": "API operation (charts)", "type": "custom", "query": ",".join(OPERATIONS),
                 "includeAll": True, "allValue": ".*", "multi": True,
                 "current": {"text": ["All"], "value": ["$__all"], "selected": True}, "options": []},
            ]}, "panels": panels}


def alerts():
    return {"schema_version": 1, "evaluation_interval_seconds": 60,
            "condition_threshold": 0.5, "rules": RULES}
