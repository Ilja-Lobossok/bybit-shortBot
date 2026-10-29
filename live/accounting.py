"""Validation of exchange balances and completed short positions (no API calls)."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


def number(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite exchange amount")
    return result


@dataclass(frozen=True)
class AccountBalance:
    wallet: float
    equity: float
    unrealised_pnl: float
    available: float

    @classmethod
    def from_response(cls, response):
        if response.get("retCode") != 0:
            raise ValueError("Wallet query was unsuccessful")
        account = response["result"]["list"][0]
        if account["accountType"] != "UNIFIED":
            raise ValueError("Expected a Unified account")
        coins = [coin for coin in account["coin"] if coin["coin"] == "USDT"]
        if not coins:
            return cls(0.0, 0.0, 0.0, 0.0)
        coin = coins[0]
        # Subtract spot borrowing: it is not the bot's own trading capital.
        wallet = number(coin["walletBalance"]) - number(coin.get("spotBorrow", "0"))
        equity = number(coin["equity"])
        unrealised = number(coin["unrealisedPnl"])
        if account.get("totalAvailableBalance") not in (None, ""):
            # The account field is USD, whereas all sizing below is in USDT.
            # usdValue/equity is the exchange's USD valuation of one USDT.
            if equity <= 0:
                available = Decimal(0)
            else:
                usd_rate = number(coin["usdValue"]) / equity
                if usd_rate <= 0:
                    raise ValueError("Invalid USDT valuation")
                available = number(account["totalAvailableBalance"]) / usd_rate
        else:
            # Isolated margin has no account-wide available-balance field.
            available = wallet - sum(number(coin[key]) for key in (
                "totalPositionIM", "totalOrderIM", "locked", "bonus"
            ))
        available = max(Decimal(0), min(wallet, available))
        return cls(float(wallet), float(equity), float(unrealised), float(available))


@dataclass(frozen=True)
class ClosedTrade:
    entry: float
    exit: float
    qty: float
    pnl: float
    pnl_percent: float
    closed_at_ms: int
    order_ids: list

    @classmethod
    def from_records(cls, symbol, position, records, start_ms, end_ms) -> Optional["ClosedTrade"]:
        """Wait for all partial closes; never invent an exit from a ticker/zero.

        Bybit closedPnl already includes trading and funding fees. Do not deduct
        openFee/closeFee or funding a second time.
        """
        expected_qty = number(position["qty"])
        if expected_qty <= 0:
            return None
        unique = {}
        for row in records:
            if row["symbol"] != symbol or row["side"] != "Buy":
                continue  # Buy closes this bot's Sell/short positions.
            if row.get("execType") not in ("Trade", "BustTrade"):
                continue
            if not start_ms <= int(row["createdTime"]) <= end_ms:
                continue
            if int(row["updatedTime"]) > end_ms:
                continue
            order_id = row["orderId"]
            if order_id in unique and unique[order_id] != row:
                raise ValueError("Closed PnL changed during pagination; retry later")
            unique[order_id] = row
        if not unique:
            return None
        qty = sum(number(row["closedSize"]) for row in unique.values())
        if qty <= 0 or abs(qty - expected_qty) > max(Decimal("0.00000001"), expected_qty * Decimal("0.00000001")):
            return None  # History delayed, partial close, or ambiguous manual trading.
        entry_value = Decimal(0)
        exit_value = Decimal(0)
        pnl = Decimal(0)
        for row in unique.values():
            size = number(row["closedSize"])
            entry = number(row["avgEntryPrice"])
            exit_price = number(row["avgExitPrice"])
            if size <= 0 or entry <= 0 or exit_price <= 0:
                raise ValueError("Invalid closed-position price or size")
            entry_value += size * entry
            exit_value += size * exit_price
            pnl += number(row["closedPnl"])
        # Avoid attributing another position's history to a stale tracker record.
        tracked_entry = number(position["entry_price"])
        if tracked_entry <= 0 or abs(entry_value / qty - tracked_entry) > tracked_entry * Decimal("0.001"):
            return None
        return cls(
            entry=float(entry_value / qty), exit=float(exit_value / qty),
            qty=float(qty), pnl=float(pnl), pnl_percent=float(pnl / entry_value * 100),
            closed_at_ms=max(int(row["updatedTime"]) for row in unique.values()),
            order_ids=sorted(unique),
        )
