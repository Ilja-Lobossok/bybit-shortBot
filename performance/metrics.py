"""Bounded aggregate gauges through the existing OTLP credential/transport code."""
import time
from uuid import uuid4

NAMES = (
    "wallet_balance", "equity", "unrealized_pnl", "exchange_cumulative_realized_pnl", "account_total_equity_usd",
    "net_closed_pnl", "win_rate", "wins", "losses", "average_win", "average_loss", "profit_factor", "cycles", "breakeven",
    "fees", "funding", "realized_cash_change", "cash_adjusted_profit", "drawdown_ratio", "max_drawdown_ratio",
    "positions", "history_complete", "pending_records", "unattributed_records", "coverage_start_timestamp_seconds",
    "coverage_end_timestamp_seconds", "balance_timestamp_seconds", "positions_timestamp_seconds",
    "strategy_timestamp_seconds", "storage_bytes", "storage_degraded", "dropped_events_total", "errors_total",
    "projection_generation", "projection_expected_rows", "projection_truncated", "projection_timestamp_seconds",
    "process_start_timestamp_seconds", "order_counts", "decision_count", "profit_factor_no_losses",
    "cash_adjusted_as_of_timestamp_seconds", "kpi_window_complete",
    "strategy_size",
)


def points(store, view, summary, runtime, now):
    result = []

    def add(name, value, **labels):
        if value is not None:
            result.append((name, float(value), labels))

    snapshot = summary["snapshot"]
    for field, name in (("walletBalance", "wallet_balance"), ("equity", "equity"),
                        ("unrealisedPnl", "unrealized_pnl"), ("cumRealisedPnl", "exchange_cumulative_realized_pnl"),
                        ("totalEquityUSD", "account_total_equity_usd")):
        add(name, snapshot.get(field), scope="account", currency="USD" if name.endswith("usd") else "USDT")
    for stats in summary["statistics"]:
        labels = {"scope": stats["scope"], "window": stats["window"], "currency": "USDT"}
        for name in ("net_pnl", "win_rate", "wins", "losses", "average_win", "average_loss", "profit_factor", "breakeven"):
            add("net_closed_pnl" if name == "net_pnl" else name, stats[name], **labels)
        add("cycles", stats["n"], **labels)
        add("profit_factor_no_losses", int(stats["profit_factor_status"] == "no_losses"), **labels)
        add("kpi_window_complete", stats["coverage_complete"], **labels)
    if summary["drawdown"]:
        add("drawdown_ratio", summary["drawdown"]["current"], scope="account", currency="USDT")
        add("max_drawdown_ratio", summary["drawdown"]["maximum"], scope="account", currency="USDT")
    for name, key in (("fees", "fees"), ("funding", "funding"), ("realized_cash_change", "realized")):
        # These are account-wide transaction components, not bot closed-cycle net.
        if view["history_status"] == "covered" and summary["transaction_totals"]["classification_complete"]:
            add(name, summary["transaction_totals"][key], scope="account", currency="USDT")
    add("cash_adjusted_profit", summary["cash_adjusted_profit"], scope="account", currency="USDT")
    add("cash_adjusted_as_of_timestamp_seconds", summary["cash_adjusted_as_of_ms"] / 1000 if summary["cash_adjusted_as_of_ms"] is not None else None)
    if store.meta("positions_success_ms") is not None:
        for scope in ("account", "bot"):
            add("positions", sum(scope == "account" or row["attribution_status"] == "bot" for row in view["positions"]), scope=scope)
    strategy_counts = store.meta("strategy_counts")
    if strategy_counts:
        import json
        for state, value in json.loads(strategy_counts).items():
            if state in ("watching", "candidate", "ready", "blocked", "stalling"):
                add("strategy_size", value, state=state)
    add("history_complete", int(view["history_status"] == "covered"))
    add("pending_records", view["pending"])
    add("unattributed_records", view["unattributed"])
    add("coverage_start_timestamp_seconds", view["coverage_start_ms"] / 1000 if view["coverage_end_ms"] else None)
    add("coverage_end_timestamp_seconds", view["coverage_end_ms"] / 1000 if view["coverage_end_ms"] else None)
    for name, key in (("balance", "balance_success_ms"), ("positions", "positions_success_ms"), ("strategy", "strategy_success_ms")):
        value = store.meta(key)
        add(name + "_timestamp_seconds", int(value) / 1000 if value else None)
    add("storage_bytes", store.size())
    for name, attr in (("storage_degraded", "degraded"), ("dropped_events_total", "dropped")):
        add(name, getattr(runtime, attr))
    add("errors_total", runtime.errors + runtime.export_errors)
    add("process_start_timestamp_seconds", runtime.started)
    ack = store.meta("projection_ack")
    if ack:
        import json
        ack = json.loads(ack)
        add("projection_generation", ack["generation"])
        add("projection_timestamp_seconds", ack["created_ms"] / 1000)
        for kind, count in ack["expected"].items():
            add("projection_expected_rows", count, kind=kind)
            add("projection_truncated", int(ack["truncated"][kind]), kind=kind)
    for reason in runtime.reasons:
        add("decision_count", int(store.meta("decision:" + reason, 0)), reason=reason)
    # Canonical counts are gauges: delayed corrections can revise them downwards.
    for scope, outcome, count in view["order_counts"]:
        add("order_counts", count, scope=scope, outcome=outcome)
    return tuple(result)


class MetricPipeline:
    def __init__(self, runtime, env, *, exporter=None, extra_readers=()):
        from opentelemetry.metrics import Observation
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from monitoring.metrics import SafeExporter
        from monitoring.otlp import CloudCredentials, create_otlp_exporter
        delegate = exporter if exporter is not None else create_otlp_exporter(CloudCredentials.from_env(env))
        safe = SafeExporter(delegate, runtime)
        reader = PeriodicExportingMetricReader(safe, export_interval_millis=60_000, export_timeout_millis=5000)
        self.provider = MeterProvider(metric_readers=[reader, *extra_readers], shutdown_on_exit=False,
                                      resource=Resource({"service.name": "bybit-performance", "service.instance.id": uuid4().hex}))
        meter = self.provider.get_meter("bybit_shortbot.performance", "4")
        common = {"bot": "bybit-shortbot", "component": "performance", "environment": env.get("MONITORING_ENVIRONMENT", "production"),
                  "account_alias": env.get("PERFORMANCE_ACCOUNT_ALIAS", "primary")}
        for name in NAMES:
            def callback(_options, name=name):
                return [Observation(value, dict(common, **labels)) for field, value, labels in runtime.cache if field == name]
            meter.create_observable_gauge("bot_performance_" + name, [callback])

    def close(self):
        self.provider.shutdown(timeout_millis=5000)
