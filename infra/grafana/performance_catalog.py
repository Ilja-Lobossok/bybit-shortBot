"""Metrics-only Trading Dashboard. Uses the existing Cloud Prometheus datasource."""
DS = {"type": "prometheus", "uid": "__METRICS_DATASOURCE_UID__"}
BASE = 'bot="bybit-shortbot",component="performance",environment="__ENVIRONMENT__",account_alias="${account}"'


def metric(name, labels=""):
    return 'bot_performance_' + name + '{' + BASE + (',' + labels if labels else '') + '}'


def current(name, labels=""):
    start = metric("process_start_timestamp_seconds")
    return '(' + metric(name, labels) + ' and on(job,instance) (' + start + ' == on() group_left() max(' + start + ')))'


def query(name, labels="", aggregate="max"):
    return aggregate + '(' + current(name, labels) + ')'


def panel(ident, title, expr, *, kind="stat", unit="short", description="", x=0, y=0, w=6, h=5):
    return {"id": ident, "title": title, "type": kind, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "description": description,
            "targets": [{"refId": "A", "expr": expr, "datasource": DS, "instant": kind != "timeseries", "range": kind == "timeseries", "legendFormat": "__auto"}],
            "fieldConfig": {"defaults": {"unit": unit, "noValue": "unknown / incomplete"}, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "legend": {"displayMode": "list", "placement": "bottom"}}}


def dashboard():
    panels = []
    labels = 'scope="${scope}",window="${window}",currency="USDT"'
    account = 'scope="account",currency="USDT"'

    def add(title, name, *, selector="", unit="short", y, x, w=6, h=5, kind="stat", description=""):
        item = panel(len(panels) + 1, title, query(name, selector), kind=kind, unit=unit,
                     description=description, x=x, y=y, w=w, h=h)
        panels.append(item)
        return item

    add("History endpoint coverage (1 covered / 0 incomplete)", "history_complete", y=0, x=0,
        description="Coverage is not proof of lifetime completeness. All financial KPI describe reconciled cycles only.")
    add("Pending / incomplete records", "pending_records", y=0, x=6)
    add("Unattributed records (excluded from bot KPI)", "unattributed_records", y=0, x=12)
    age = add("Balance snapshot age", "balance_timestamp_seconds", unit="s", y=0, x=18)
    age["targets"][0]["expr"] = "time() - " + query("balance_timestamp_seconds")
    panels[0]["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": [{"value": None, "color": "red"}, {"value": 1, "color": "green"}]}
    for index in (1, 2):
        panels[index]["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": [{"value": None, "color": "green"}, {"value": 1, "color": "orange"}]}
    for index, (title, name) in enumerate((
        ("Account wallet balance (raw USDT)", "wallet_balance"),
        ("Account equity (USDT coin)", "equity"),
        ("Unrealized P&L (Bybit Mark, USDT)", "unrealized_pnl"),
        ("Bybit cumulative realized P&L (USDT coin)", "exchange_cumulative_realized_pnl"))):
        add(title, name, selector=account, unit="currencyUSD", y=5, x=index * 6,
            description="Actual Bybit coin snapshot. Wallet/equity belong to the whole account, not allocated bot capital. Historical equity begins with real collection.")
    for index, (title, name, unit) in enumerate((
        ("Net closed P&L (verified cycles / selected scope)", "net_closed_pnl", "currencyUSD"),
        ("Profitable cycles", "wins", "short"), ("Losing cycles", "losses", "short"),
        ("Reconciled cycles (N, includes breakeven)", "cycles", "short"))):
        add(title, name, selector=labels, unit=unit, y=10, x=index * 6,
            description="Fixed KPI window; only fully reconciled flat-to-flat cycles. Bot scope requires proven ownership. Transfers are not profit; net already includes fees/funding.")
    add("Win rate (verified cycles)", "win_rate", selector=labels, unit="percentunit", y=15, x=0)
    add("Profit factor (undefined if no losses)", "profit_factor", selector=labels, y=15, x=6,
        description="No invented finite PF. Check no-loss status below; N=0 gives unknown.")
    add("Open positions / selected scope", "positions", selector='scope="${scope}"', y=15, x=12,
        description="Complete Bybit position snapshot; bot is proven subset. A failed snapshot retains previous as-of and never means zero positions.")
    add("Selected KPI window covered (1 yes / 0 partial)", "kpi_window_complete", selector=labels, y=15, x=18,
        description="Time picker controls observed charts; KPI windows are all/UTC day/7d/30d. Unknown cycles stay excluded even if endpoints are covered.")
    curve = add("Account equity and wallet history (USDT)", "equity", selector=account,
                kind="timeseries", unit="currencyUSD", y=20, x=0, w=12, h=8,
                description="Actual ~60s observations, no reconstructed history. Deposits change balances and are not trading profit.")
    curve["targets"][0]["legendFormat"] = "Equity"
    curve["targets"].append({"refId": "B", "expr": query("wallet_balance", account), "datasource": DS,
                             "instant": False, "range": True, "legendFormat": "Wallet"})
    add("Cumulative net profit of verified closed cycles", "net_closed_pnl",
        selector='scope="${scope}",window="all",currency="USDT"', kind="timeseries",
        unit="currencyUSD", y=20, x=12, w=12, h=8,
        description="Covered journal, not lifetime account profit. Corrections can revise the gauge; fees are not deducted twice.")
    for index, (title, state) in enumerate((("Watchlist", "watching"), ("Pump candidates", "candidate"),
                                            ("Ready entry signals", "ready"), ("Blocked candidates", "blocked"))):
        add(title, "strategy_size", selector='state="' + state + '"', y=28, x=index * 6,
            description="Counts from the actual strategy pass, never a simulated second evaluation. Check strategy age; a waiting 5s loop is not a new candle.")
    add("Actual signal / skip decision counts", "decision_count", kind="timeseries", y=33, x=0, w=12, h=8,
        description="Persisted observed decisions by finite reason. Dropped observations are visible separately; SDK order_error is not proof of exchange rejection.")["targets"][0]["expr"] = query("decision_count", aggregate="sum by(reason)")
    add("Unrealized P&L history (account USDT)", "unrealized_pnl", selector=account,
        kind="timeseries", unit="currencyUSD", y=33, x=12, w=12, h=8)
    for index, (title, name, selector, unit) in enumerate((
        ("Average winning cycle", "average_win", labels, "currencyUSD"),
        ("Average losing cycle", "average_loss", labels, "currencyUSD"),
        ("Maximum observed equity drawdown (includes flows)", "max_drawdown_ratio", account, "percentunit"),
        ("No losses in verified sample (1 = PF undefined)", "profit_factor_no_losses", labels, "short"))):
        add(title, name, selector=selector, unit=unit, y=41, x=index * 6)
    for index, (title, name) in enumerate((("Account realized cash change (covered linear USDT)", "realized_cash_change"),
                                          ("Account trading fees (expense positive)", "fees"),
                                          ("Account funding (received positive)", "funding"),
                                          ("Account total equity (USD, separate currency)", "account_total_equity_usd"))):
        add(title, name, selector=account if index < 3 else 'scope="account",currency="USD"',
            unit="currencyUSD", y=46, x=index * 6, description="Separate account components, not amounts to subtract again from net closed P&L.")
    age = add("Strategy observation age", "strategy_timestamp_seconds", unit="s", y=51, x=0)
    age["targets"][0]["expr"] = "time() - " + query("strategy_timestamp_seconds")
    age = add("Position snapshot age", "positions_timestamp_seconds", unit="s", y=51, x=6)
    age["targets"][0]["expr"] = "time() - " + query("positions_timestamp_seconds")
    add("Stalling watchlist symbols", "strategy_size", selector='state="stalling"', y=51, x=12)
    add("Journal bytes including WAL", "storage_bytes", unit="bytes", y=51, x=18)
    for index, (title, name) in enumerate((("Performance storage degraded", "storage_degraded"),
                                          ("Performance errors", "errors_total"),
                                          ("Lost strategy observations", "dropped_events_total"))):
        add(title, name, y=56, x=index * 8, w=8)
    return {"uid": "bybit-trading-performance", "title": "Bybit ShortBot — Trading Dashboard", "schemaVersion": 39,
            "version": 1, "editable": False, "refresh": "1m", "timezone": "browser", "tags": ["bybit", "trading", "metrics-only", "as-code"],
            "time": {"from": "now-24h", "to": "now"}, "panels": panels,
            "templating": {"list": [
                {"name": "account", "type": "constant", "query": "__ACCOUNT_ALIAS__", "hide": 2},
                {"name": "scope", "type": "custom", "query": "bot,account", "current": {"text": "bot", "value": "bot"}},
                {"name": "window", "type": "custom", "query": "all,utc_day,7d,30d", "current": {"text": "all", "value": "all"}},
            ]}}
