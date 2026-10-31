"""Decimal-only analytics. Missing evidence produces None/unknown, never invented zero."""
from decimal import Decimal
from performance.contracts import decimal as D

ZERO = Decimal(0)
TOLERANCE = Decimal("0.00000001")  # USDT reconciliation tolerance, not an adjusted ledger entry
TRADING_TYPES = frozenset(("TRADE", "SETTLEMENT", "DELIVERY", "LIQUIDATION", "ADL", "FEE_REFUND"))
NONTRADING_TYPES = frozenset(("TRANSFER_IN", "TRANSFER_OUT", "BONUS", "BONUS_RECOLLECT", "AIRDROP",
                              "BORROW", "REPAY", "BONUS_TRANSFER_IN", "BONUS_TRANSFER_OUT"))


def statistics(trades):
    values = [D(t["net_pnl"]) for t in trades if t["money_status"] == "reconciled"]
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    return {"n": len(values), "wins": len(wins), "losses": len(losses),
            "breakeven": len(values) - len(wins) - len(losses),
            "net_pnl": str(sum(values, ZERO)) if values else None,
            "win_rate": str(D(len(wins)) / len(values)) if values else None,
            "average_win": str(sum(wins, ZERO) / len(wins)) if wins else None,
            "average_loss": str(sum(losses, ZERO) / len(losses)) if losses else None,
            "profit_factor": str(sum(wins, ZERO) / abs(sum(losses, ZERO))) if losses else None,
            "profit_factor_status": "finite" if losses else "no_losses" if values else "no_trades"}


def drawdown(samples):
    """Observed equity DD, including cash flows; never market-continuous or return DD."""
    peak = None
    current = maximum = ZERO
    for value in samples:
        equity = D(value)
        peak = equity if peak is None else max(peak, equity)
        current = (peak - equity) / peak if peak > 0 else ZERO
        maximum = max(maximum, current)
    return {"current": str(current), "maximum": str(maximum)} if peak is not None else {}


def transaction_totals(rows, *, start, end, currency="USDT"):
    selected = [x for x in rows if x["currency"] == currency and start <= int(x["transactionTime"]) <= end]
    unknown = any(x["type"] not in TRADING_TYPES | NONTRADING_TYPES or
                  (x["type"] in TRADING_TYPES and x.get("category") != "linear") for x in selected)
    trading = [x for x in selected if x["type"] in TRADING_TYPES and x.get("category") == "linear"]
    return {"fees": str(sum((D(x["fee"]) for x in trading), ZERO)),
            "funding": str(sum((D(x["funding"]) for x in trading), ZERO)),
            "realized": str(sum((D(x["change"]) for x in trading), ZERO)),
            "nontrading": str(sum((D(x["change"]) for x in selected if x["type"] in NONTRADING_TYPES), ZERO)),
            "classification_complete": not unknown}


def close_reason(orders):
    reasons = set()
    for row in orders:
        stop, create = row.get("stopOrderType", ""), row.get("createType", "")
        if "TakeProfit" in stop or "TakeProfit" in create:
            reasons.add("tp")
        elif "StopLoss" in stop or "StopLoss" in create:
            reasons.add("sl")
        elif "Liq" in create:
            reasons.add("liquidation")
        elif "Adl" in create:
            reasons.add("adl")
        else:
            reasons.add("unknown")  # price proximity never proves manual/TP/SL
    return next(iter(reasons)) if len(reasons) == 1 else "mixed" if reasons else "unknown"
