"""Rebuild analytical views from canonical exchange facts; no feedback to trading."""
from collections import defaultdict
import json
from performance.calculations import ZERO, TOLERANCE, close_reason, statistics, transaction_totals, drawdown
from performance.contracts import decimal as D
from performance.storage import DAY

TERMINAL = frozenset(("Filled", "Cancelled", "PartiallyFilledCanceled", "Rejected", "Deactivated"))


def reconcile(store, now):
    executions = store.facts("executions")
    orders = {x["orderId"]: x for x in store.facts("orders")}
    closes = {x["orderId"]: x for x in store.facts("closed_pnl") if x["symbol"].endswith("USDT")}
    transactions = store.facts("transactions")
    owners = {x["order_id"]: dict(x) for x in store.db.execute("SELECT * FROM ownership")}
    fills = defaultdict(list)
    groups = defaultdict(list)
    unresolved = 0
    for fill in executions:
        fills[fill["orderId"]].append(fill)
        order = orders.get(fill["orderId"])
        if not order or not fill["symbol"].endswith("USDT") or fill["execType"] != "Trade":
            unresolved += 1
            continue
        groups[(fill["symbol"], order["positionIdx"])].append(fill)
    trades, active = [], {}
    for (symbol, idx), events in groups.items():
        events.sort(key=lambda x: (int(x["execTime"]), int(x.get("seq", 0)), x["execId"]))
        cycle, quantity = [], ZERO
        poisoned = False
        for fill in events:
            qty, closed = D(fill["execQty"]), D(fill["closedSize"])
            if qty <= 0 or closed < 0 or closed > qty:
                poisoned = True
            if closed == 0:
                if cycle and fill["side"] != cycle[0]["side"]:
                    poisoned = True
                quantity += qty
            elif closed == qty and cycle and fill["side"] != cycle[0]["side"]:
                quantity -= qty
            else:
                # Leading partial history, reversal, correction/special execution:
                # do not guess a position cycle or split a single execution.
                poisoned = True
                quantity -= closed
            cycle.append(fill)
            if quantity <= 0:
                if quantity == 0 and not poisoned:
                    trades.append(_cycle(store, cycle, orders, closes, transactions, owners, idx))
                else:
                    unresolved += 1
                cycle, quantity, poisoned = [], ZERO, False
        if cycle:
            row = _cycle(store, cycle, orders, closes, transactions, owners, idx, open_cycle=True)
            row["remaining_qty"] = str(quantity)
            row["attribution_status"] = "unknown" if poisoned else row["attribution_status"]
            active[(symbol, idx)] = row

    # Expose each exchange close record even if a full entry/cycle is unavailable.
    used = {ident for trade in trades for ident in trade["close_order_ids"]}
    unmatched = [dict(entity_key="close:" + x["orderId"], symbol=x["symbol"], net_pnl=x["closedPnl"],
                      money_status="exchange_reported", attribution_status="unknown", reason="unknown",
                      closed_at_ms=None, source_updated_ms=x["updatedTime"],
                      entry=x["avgEntryPrice"], exit=x["avgExitPrice"], qty=x["closedSize"],
                      unit="closing_order", fee_detail_status="incomplete")
                 for x in closes.values() if x["orderId"] not in used]
    positions = []
    for stored in store.db.execute("SELECT * FROM positions_current ORDER BY entity_key"):
        value = json.loads(stored["payload"])
        cycle = active.get((value["symbol"], value["positionIdx"]))
        owned = bool(cycle and cycle["attribution_status"] == "bot" and
                     D(cycle["remaining_qty"]) == D(value["size"]) and cycle["side"] == value["side"])
        positions.append(dict(value, entity_key=stored["entity_key"], as_of_ms=stored["received_ms"],
                              attribution_status="bot" if owned else "unknown"))
    end = min((int(store.meta("watermark:" + kind, 0)) for kind in
               ("executions", "orders", "closed_pnl", "transactions")), default=0)
    origin = int(store.meta("history_origin_ms", end))
    complete = end >= origin and end > 0 and all(store.covered(kind, origin, end) for kind in
                                                ("executions", "orders", "closed_pnl", "transactions"))
    # Endpoint coverage is not proof that Bybit retained all historical order states.
    pending = unresolved + len(unmatched) + sum(x["money_status"] != "reconciled" for x in trades)
    entry_ids = {x["orderId"] for x in executions if x["execType"] == "Trade" and D(x["closedSize"]) == 0}
    exit_ids = {x["orderId"] for x in executions if x["execType"] == "Trade" and D(x["closedSize"]) > 0}
    bot_entry_ids = {i for i in entry_ids if i in owners and i in orders and owners[i]["symbol"] == orders[i]["symbol"]}
    bot_exit_ids = {i for cycle in trades + list(active.values()) if cycle["attribution_status"] == "bot" for i in cycle["close_order_ids"]}
    counts = []
    for scope, entries, exits in (("account", entry_ids, exit_ids), ("bot", bot_entry_ids, bot_exit_ids)):
        for outcome, value in (("entry_filled", len(entries)), ("exit_filled", len(exits)),
                               ("rejected", sum(x["orderStatus"] == "Rejected" and
                                                (scope == "account" or x["orderId"] in owners) for x in orders.values())),
                               ("partial_exit_orders", sum(max(0, len(x["close_order_ids"]) - 1) for x in trades if
                                                            scope == "account" or x["attribution_status"] == "bot") +
                                sum(len(x["close_order_ids"]) for x in active.values() if scope == "account" or x["attribution_status"] == "bot"))):
            counts.append((scope, outcome, value))
        for reason in ("tp", "sl", "unknown", "mixed", "liquidation", "adl"):
            counts.append((scope, reason, sum(close_reason([orders[i]]) == reason for i in exits if i in orders)))
    return {"trades": trades, "unmatched_closes": unmatched, "positions": positions,
            "pending": pending, "coverage_start_ms": origin, "coverage_end_ms": end,
            "history_status": "covered" if complete else "incomplete", "transactions": transactions,
            "unattributed": len(unmatched) + sum(x["attribution_status"] != "bot" for x in trades),
            "orders": list(orders.values()), "order_counts": counts}


def _cycle(store, events, orders, closes, transactions, owners, idx, open_cycle=False):
    opening = [x for x in events if D(x["closedSize"]) == 0]
    closing = [x for x in events if D(x["closedSize"]) > 0]
    entry_ids = set(x["orderId"] for x in opening)
    close_ids = set(x["orderId"] for x in closing)
    symbol = events[0]["symbol"]
    start = min(int(x["execTime"]) for x in events)
    end = max(int(x["execTime"]) for x in events)
    all_ids = entry_ids | close_ids
    coverage = all(store.covered(kind, start, end) for kind in ("executions", "orders", "closed_pnl", "transactions"))
    terminal = all(orders[i]["orderStatus"] in TERMINAL for i in all_ids)
    complete_fills = all(sum((D(x["execQty"]) for x in events if x["orderId"] == i), ZERO) ==
                         D(orders[i]["cumExecQty"]) for i in all_ids)
    # Transaction size is a second check that the first fill started from flat.
    first = events[0]
    first_tx = [x for x in transactions if x.get("tradeId") == first["execId"] and x["currency"] == "USDT"]
    tx_flat = len(first_tx) == 1 and "size" in first_tx[0] and abs(D(first_tx[0]["size"])) == D(first["execQty"])
    proof = all(i in owners and owners[i]["symbol"] == symbol for i in entry_ids)
    local_flat = proof and any(owners[i]["flat_before"] for i in entry_ids)
    # No ambiguous mixed direction timestamps are considered ordered evidence.
    by_time = defaultdict(set)
    for event in events:
        by_time[event["execTime"]].add(event["side"])
    ordered = all(len(sides) == 1 for sides in by_time.values())
    attribution = "bot" if proof and (local_flat or tx_flat) and coverage and complete_fills and ordered else "unknown"
    quantity = sum((D(x["execQty"]) for x in opening), ZERO)
    entry = sum((D(x["execQty"]) * D(x["execPrice"]) for x in opening), ZERO) / quantity
    exit_qty = sum((D(x["execQty"]) for x in closing), ZERO)
    exit_price = sum((D(x["execQty"]) * D(x["execPrice"]) for x in closing), ZERO) / exit_qty if exit_qty else None
    pnl_rows = [closes[i] for i in close_ids if i in closes]
    net = sum((D(x["closedPnl"]) for x in pnl_rows), ZERO)
    pnl_match = len(pnl_rows) == len(close_ids) and all(
        x["execType"] == "Trade" and x["symbol"] == symbol and
        x["side"] == orders[x["orderId"]]["side"] and
        D(x["closedSize"]) == sum((D(e["execQty"]) for e in closing if e["orderId"] == x["orderId"]), ZERO)
        and abs(D(x["avgEntryPrice"]) - entry) <= TOLERANCE
        and abs(D(x["avgExitPrice"]) - sum((D(e["execQty"]) * D(e["execPrice"]) for e in closing if e["orderId"] == x["orderId"]), ZERO) / D(x["closedSize"])) <= TOLERANCE
        for x in pnl_rows)
    fees = defaultdict(lambda: ZERO)
    fee_complete = True
    for fill in events:
        if "execFee" not in fill or "feeCurrency" not in fill:
            fee_complete = False
        else:
            fees[fill["feeCurrency"]] += D(fill["execFee"])
    # Funding attribution is only unambiguous for one-way USDT cycles.
    tx = [x for x in transactions if x["currency"] == "USDT" and x.get("symbol") == symbol and
          start <= int(x["transactionTime"]) <= end and
          (x.get("tradeId") in {e["execId"] for e in events} or x["type"] == "SETTLEMENT")]
    tx_trades = {x.get("tradeId") for x in tx if x["type"] == "TRADE"}
    tx_complete = all(e["execId"] in tx_trades for e in events) and idx == 0
    tx_net = sum((D(x["change"]) for x in tx), ZERO)
    funding = sum((D(x["funding"]) for x in tx), ZERO)
    fee_match = set(fees) <= {"USDT"} and abs(fees.get("USDT", ZERO) - sum((D(x["fee"]) for x in tx), ZERO)) <= TOLERANCE
    matched = (not open_cycle and coverage and terminal and complete_fills and pnl_match and ordered and
               (local_flat or tx_flat) and tx_complete and abs(tx_net - net) <= TOLERANCE and fee_complete and fee_match)
    return {"entity_key": "cycle:" + events[0]["execId"], "unit": "position_cycle", "symbol": symbol,
            "positionIdx": idx, "side": first["side"], "opened_at_ms": start,
            "closed_at_ms": None if open_cycle else end, "entry": str(entry),
            "exit": str(exit_price) if exit_price is not None else None, "qty": str(quantity),
            "net_pnl": str(net) if pnl_match and not open_cycle else None,
            "money_status": "reconciled" if matched else "incomplete",
            "attribution_status": attribution, "reason": close_reason([orders[i] for i in close_ids]),
            "entry_order_ids": sorted(entry_ids), "close_order_ids": sorted(close_ids),
            "fees": {currency: str(amount) for currency, amount in fees.items()},
            "funding": str(funding) if coverage and tx_complete else None,
            "fee_detail_status": "complete" if fee_complete and tx_complete else "incomplete"}


def summaries(store, view, now):
    result = []
    windows = {"all": view["coverage_start_ms"], "utc_day": now // DAY * DAY,
               "7d": now - 7 * DAY, "30d": now - 30 * DAY}
    for window, start in windows.items():
        for scope in ("account", "bot"):
            trades = [x for x in view["trades"] if x["closed_at_ms"] >= start and
                      (scope == "account" or x["attribution_status"] == "bot")]
            stats = statistics(trades)
            stats.update(scope=scope, window=window, coverage_complete=int(start >= view["coverage_start_ms"] and view["history_status"] == "covered"),
                         complete_cycles_only=1)
            result.append(stats)
    dd = drawdown(json.loads(row[0])["equity"] for row in store.db.execute("SELECT payload FROM account_snapshots ORDER BY received_ms"))
    latest_time, snapshot = store.latest_snapshot()
    tx = transaction_totals(view["transactions"], start=view["coverage_start_ms"], end=view["coverage_end_ms"])
    cash_adjusted = None
    # No attempt to align a snapshot to a different timestamp by interpolation.
    # Exact adjusted curve is exposed only if transaction cash balances anchor both snapshots.
    first = store.db.execute("SELECT exchange_ms,payload FROM account_snapshots ORDER BY exchange_ms LIMIT 1").fetchone()
    anchor = store.db.execute("SELECT exchange_ms,payload FROM account_snapshots WHERE exchange_ms<=? ORDER BY exchange_ms DESC LIMIT 1",
                              (int(store.meta("watermark:transactions", 0)),)).fetchone()
    adjusted_at = None
    if first and anchor and anchor[0] > first[0]:
        first_time, first_value = first[0], json.loads(first[1])
        anchor_time, anchor_value = anchor[0], json.loads(anchor[1])
        eligible = [x for x in view["transactions"] if first_time < int(x["transactionTime"]) <= anchor_time and x["currency"] == "USDT"]
        flows = transaction_totals(eligible, start=first_time + 1, end=anchor_time)
        balance_delta = D(anchor_value["walletBalance"]) - D(first_value["walletBalance"])
        cash_delta = sum((D(x["change"]) for x in eligible), ZERO)
        if (store.covered("transactions", first_time + 1, anchor_time) and flows["classification_complete"] and
                abs(balance_delta - cash_delta) <= TOLERANCE):
            cash_adjusted = str(D(anchor_value["equity"]) - D(first_value["equity"]) - D(flows["nontrading"]))
            adjusted_at = anchor_time
    return {"statistics": result, "snapshot": snapshot, "snapshot_ms": latest_time,
            "drawdown": dd, "transaction_totals": tx, "cash_adjusted_profit": cash_adjusted,
            "cash_adjusted_as_of_ms": adjusted_at}
