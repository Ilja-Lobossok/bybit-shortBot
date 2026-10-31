"""Trading Performance JSON source. Fixed scalar scopes; no per-order metric series."""
DS = {"type": "prometheus", "uid": "__METRICS_DATASOURCE_UID__"}
LOGS = {"type": "loki", "uid": "__LOGS_DATASOURCE_UID__"}
BASE = 'bot="bybit-shortbot",component="performance",environment="__ENVIRONMENT__",account_alias="${account}"'


def metric(name, labels=""):
    return 'bot_performance_' + name + '{' + BASE + (',' + labels if labels else '') + '}'


def current(name, labels=""):
    start = metric("process_start_timestamp_seconds")
    return '(' + metric(name, labels) + ' and on(job,instance) (' + start + ' == on() group_left() max(' + start + ')))'


def query(name, labels="", aggregate="max"):
    return aggregate + '(' + current(name, labels) + ')'


def log_query(kind, *, range_query=False):
    selector = '{service_name="bybit-performance"}'
    # environment/account_alias remain structured metadata by default in Cloud;
    # explicitly filter their resource values after JSON, rather than assume indexing.
    pipeline = (selector + ' | environment="__ENVIRONMENT__" | account_alias="${account}" | json'
                + ' | __error__="" | generation=${projection} | kind="' + kind + '"')
    if range_query:
        # Count unique body identities, not duplicate retry log lines.
        return 'count(sum by (entity_key) (count_over_time(' + pipeline + '[15m]))) or vector(0)'
    return pipeline


def panel(ident, title, expr, *, kind="stat", unit="short", description="", x=0, y=0, w=6, h=5):
    return {"id": ident, "title": title, "type": kind, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "description": description,
            "targets": [{"refId": "A", "expr": expr, "datasource": DS, "instant": kind != "timeseries", "range": kind == "timeseries"}],
            "fieldConfig": {"defaults": {"unit": unit, "noValue": "unknown / incomplete"}, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "legend": {"displayMode": "list", "placement": "bottom"}}}


def dashboard():
    panels = []
    overview = [
        ("History endpoint coverage (not a completeness guarantee)", "history_complete", "short"),
        ("Pending / incomplete records", "pending_records", "short"),
        ("Unattributed records (excluded from bot KPI)", "unattributed_records", "short"),
        ("Balance age", None, "s"),
    ]
    for i, (title, name, unit) in enumerate(overview, 1):
        expr = query(name) if name else 'time() - ' + query("balance_timestamp_seconds")
        panels.append(panel(i, title, expr, unit=unit, x=(i - 1) * 6, y=0,
                            description="0 coverage / stale data / pending records means incomplete. Financial KPI use reconciled closed cycles only."))
        if i == 1:
            panels[-1]["fieldConfig"]["defaults"].update(thresholds={"mode": "absolute", "steps": [{"value": None, "color": "red"}, {"value": 1, "color": "green"}]})
        elif i in (2, 3):
            panels[-1]["fieldConfig"]["defaults"].update(thresholds={"mode": "absolute", "steps": [{"value": None, "color": "green"}, {"value": 1, "color": "orange"}]})
    for i, (title, name, currency) in enumerate((
        ("Account wallet (raw USDT)", "wallet_balance", "USDT"),
        ("Account equity (USDT coin)", "equity", "USDT"),
        ("Account unrealized P&L (Mark, USDT)", "unrealized_pnl", "USDT"),
        ("Account total equity (USD)", "account_total_equity_usd", "USD")), 5):
        panels.append(panel(i, title, query(name, 'scope="account",currency="' + currency + '"'), unit="currencyUSD",
                            x=(i - 5) * 6, y=5, description="Bybit snapshot, not bot-allocated capital; coin USDT and account USD are separate. No backfilled historical equity."))
    stats_labels = 'scope="${scope}",window="${window}",currency="USDT"'
    for i, (title, name, unit) in enumerate((
        ("Net closed P&L: verified cycles / selected scope", "net_closed_pnl", "currencyUSD"),
        ("Win rate: verified cycles", "win_rate", "percentunit"),
        ("Profit factor (No data if no losses)", "profit_factor", "short"),
        ("Fully reconciled cycles (N)", "cycles", "short")), 9):
        panels.append(panel(i, title, query(name, stats_labels), unit=unit, x=(i - 9) * 6, y=10,
                            description="Fixed KPI window selected above, not arbitrary time picker. Unknown cycles excluded; missing history is not zero. Net already includes fees/funding."))
    for i, (title, name) in enumerate((("Equity history since observations began", "equity"),
                                      ("Cumulative closed P&L of verified cycles", "net_closed_pnl")), 13):
        labels = 'scope="account",currency="USDT"' if name == "equity" else 'scope="${scope}",window="all",currency="USDT"'
        panels.append(panel(i, title, query(name, labels), kind="timeseries", unit="currencyUSD", x=(i - 13) * 12, y=15, w=12, h=8,
                            description="OTLP 60s samples. No invented equity before collection; corrections can revise closed P&L. Time picker controls displayed observations, not trade closure filtering."))
    financial = (("Mean win", "average_win", stats_labels, "currencyUSD"),
                 ("Mean loss", "average_loss", stats_labels, "currencyUSD"),
                 ("Maximum observed equity drawdown (includes cash flows)", "max_drawdown_ratio", 'scope="account",currency="USDT"', "percentunit"),
                 ("Account realized cash change (covered linear USDT)", "realized_cash_change", 'scope="account",currency="USDT"', "currencyUSD"),
                 ("Account trading fees (expense positive)", "fees", 'scope="account",currency="USDT"', "currencyUSD"),
                 ("Account funding (received positive)", "funding", 'scope="account",currency="USDT"', "currencyUSD"),
                 ("Cash-flow-adjusted USDT equity change (only if reconciled)", "cash_adjusted_profit", 'scope="account",currency="USDT"', "currencyUSD"),
                 ("No losses in verified sample (1 = PF undefined)", "profit_factor_no_losses", stats_labels, "short"))
    for i, (title, name, labels, unit) in enumerate(financial, 15):
        panels.append(panel(i, title, query(name, labels), unit=unit, x=((i - 15) % 4) * 6, y=23 + ((i - 15) // 4) * 5,
                            description="Separate components, never subtract again from net closed P&L. DD is a lower bound from observed snapshots, not strategy TWR."))
    for i, (kind, title) in enumerate((("position", "Actual open positions (5m view, Mark, exchange TP/SL; check as-of)"),
                                      ("trade", "Latest 1000 cycle / closing-order records; check unit/status"),
                                      ("strategy", "Actual strategy observations; latest decision reason")), 23):
        p = panel(i, title, "", kind="table", x=0, y=33 + (i - 23) * 9, w=18, h=9,
                  description="Only acknowledged generation ${projection}. Empty table is NOT proof of no positions unless row-count panel is complete and fresh. Account scope table; attribution column identifies bot. Ingestion is publication time; business times are columns. Fixed latest-view, not historical SQL.")
        p["datasource"] = LOGS
        p["targets"] = [{"refId": "A", "expr": log_query(kind), "datasource": LOGS, "queryType": "range", "direction": "backward", "maxLines": 5000}]
        p["timeFrom"] = "15m"
        p["transformations"] = [
            {"id": "extractFields", "options": {"source": "Line", "format": "json", "replace": True}},
            {"id": "groupBy", "options": {"fields": {"entity_key": {"operation": "groupby"},
                **{name: {"operation": "aggregate", "aggregations": ["lastNotNull"]} for name in
                   ("symbol", "positionIdx", "side", "size", "avgPrice", "markPrice", "unrealisedPnl", "takeProfit", "stopLoss",
                    "opened_at_ms", "closed_at_ms", "entry", "exit", "qty", "net_pnl", "fees", "funding", "unit",
                    "money_status", "attribution_status", "reason", "risk_reason", "pump_pct", "stall", "local_high", "ready", "blocked",
                    "watching", "decision_ms", "candle_ms", "scheduled_bar", "as_of_ms", "published_at_ms")}}}},
        ]
        panels.append(p)
        check = panel(i + 3, kind + " projection integrity (1 complete / 0 incomplete)", "", x=18, y=p["gridPos"]["y"], w=6, h=9,
                      description="Must be 1 before trusting an empty table. Exact unique row count, non-truncated projection, ack <15m. Query errors/NoData are incomplete. Loki default maxLines 5000; rare repeated retries can truncate table: inspect manifest/limit and verify in acceptance PoC.")
        expected = query("projection_expected_rows", 'kind="' + kind + '"')
        trunc = query("projection_truncated", 'kind="' + kind + '"')
        source = {"position": ("positions", 180), "strategy": ("strategy", 1200), "trade": ("coverage_end", 900)}[kind]
        fresh = '(time() - ' + query(source[0] + "_timestamp_seconds") + ') < bool ' + str(source[1])
        check["datasource"] = {"type": "datasource", "uid": "-- Mixed --"}
        check["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": [{"value": None, "color": "red"}, {"value": 1, "color": "green"}]}
        check["targets"] = [
            {"refId": "A", "expr": log_query(kind, range_query=True), "datasource": LOGS, "queryType": "instant"},
            {"refId": "B", "expr": expected, "datasource": DS, "instant": True},
            {"refId": "C", "expr": '(' + trunc + ' == bool 0) * on() ((time() - ' + query("projection_timestamp_seconds") + ') < bool 900) * on() (' + fresh + ') * on() (' + query("storage_degraded") + ' == bool 0)', "datasource": DS, "instant": True},
            {"refId": "D", "datasource": {"type": "__expr__", "uid": "__expr__"}, "type": "math", "expression": "($A == $B) && ($C == 1)", "hide": False},
        ]
        for target in check["targets"][:3]:
            target["hide"] = True
        panels.append(check)
    panels.append(panel(29, "Filled entry orders / exits / TP / SL / exchange rejections", query("order_counts", 'scope="${scope}"', "sum by(outcome)"),
                        x=0, y=60, w=12, h=7, description="Unique order IDs, includes open cycles. Bot entry IDs require local proof; exits require attributable cycles. Partials are not extra cycles. Rejection history has a short Bybit retention window, not lifetime complete. SDK errors below are ambiguous."))
    panels.append(panel(30, "Actual decision / skip counts (persisted since journal start)", query("decision_count", "", "sum by(reason)"),
                        x=12, y=60, w=12, h=7, description="Includes order_error from existing place_order calls only. A timeout can mean accepted; this observation NEVER retries an order."))
    for i, (title, name) in enumerate((("Performance storage degraded", "storage_degraded"),
                                      ("Performance errors", "errors_total"), ("Lost strategy observations", "dropped_events_total"),
                                      ("Journal bytes including WAL", "storage_bytes")), 31):
        panels.append(panel(i, title, query(name), x=(i - 31) * 6, y=67))
    panels.append(panel(35, "Selected KPI window covered (1 yes; subset still excludes unknown cycles)", query("kpi_window_complete", stats_labels), x=0, y=72, w=12))
    panels.append(panel(36, "Cash-adjusted result as-of (lagged to reconciled transaction coverage)", '(' + query("cash_adjusted_as_of_timestamp_seconds") + ') * 1000', unit="dateTimeAsIso", x=12, y=72, w=12))
    # scalar result avoids Grafana vector formatter's trailing sample timestamp.
    generation_query = 'query_result(scalar(' + query("projection_generation") + '))'
    return {"uid": "bybit-trading-performance", "title": "Bybit ShortBot — Trading Performance", "schemaVersion": 39,
            "version": 1, "editable": False, "refresh": "1m", "timezone": "browser", "tags": ["bybit", "performance", "as-code"],
            "time": {"from": "now-24h", "to": "now"}, "panels": panels,
            "templating": {"list": [
                {"name": "account", "type": "constant", "query": "__ACCOUNT_ALIAS__", "hide": 2},
                {"name": "scope", "type": "custom", "query": "bot,account", "current": {"text": "bot", "value": "bot"}},
                {"name": "window", "type": "custom", "query": "all,utc_day,7d,30d", "current": {"text": "all", "value": "all"}},
                {"name": "projection", "type": "query", "datasource": DS, "query": {"query": generation_query, "refId": "generation"},
                 "regex": r"/^([0-9]+)(?:\.0+)?$/", "refresh": 2, "hide": 2, "multi": False, "includeAll": False},
            ]}}
