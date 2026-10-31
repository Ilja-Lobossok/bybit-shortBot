"""Allowlisted financial fields. Decimal strings survive storage without float casts."""
from decimal import Decimal, InvalidOperation
import re

FIELDS = {
    "executions": "execId orderId symbol side execType execTime execQty execPrice execFee feeCurrency closedSize seq",
    "orders": "orderId symbol side positionIdx orderStatus qty cumExecQty avgPrice createdTime updatedTime stopOrderType createType",
    "closed_pnl": "orderId symbol side qty closedSize avgEntryPrice avgExitPrice closedPnl openFee closeFee createdTime updatedTime execType",
    "transactions": "id symbol currency category type transactionTime orderId tradeId cashFlow funding fee change cashBalance qty size",
    "positions": "symbol side positionIdx size avgPrice markPrice positionValue unrealisedPnl curRealisedPnl takeProfit stopLoss updatedTime seq",
}
DECIMALS = set("execQty execPrice execFee closedSize qty cumExecQty avgPrice avgEntryPrice avgExitPrice "
               "closedPnl openFee closeFee cashFlow funding fee change cashBalance size markPrice positionValue "
               "unrealisedPnl curRealisedPnl takeProfit stopLoss".split())
INTEGERS = set("positionIdx execTime createdTime updatedTime transactionTime seq".split())
REQUIRED = {
    "executions": "execId orderId symbol side execType execTime execQty execPrice closedSize",
    "orders": "orderId symbol side positionIdx orderStatus qty cumExecQty createdTime updatedTime",
    "closed_pnl": "orderId symbol side closedSize avgEntryPrice avgExitPrice closedPnl createdTime updatedTime execType",
    "transactions": "id currency type transactionTime cashFlow funding fee change",
    "positions": "symbol side positionIdx size avgPrice markPrice unrealisedPnl",
}


def decimal(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or len(str(value)) > 100:
            raise ValueError()
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Invalid financial decimal") from None


def safe_token(value, *, empty=False, maximum=100):
    if not isinstance(value, str) or len(value) > maximum or not re.fullmatch(r"[A-Za-z0-9_.:-]*", value):
        raise ValueError("Invalid financial identifier")
    if not value and not empty:
        raise ValueError("Missing financial identifier")
    return value


def safe_symbol(value):
    # Bybit also lists Unicode symbols. Allow letters/digits, never arbitrary text.
    if not isinstance(value, str) or not 1 <= len(value) <= 40 or not all(c.isalnum() or c in "_.-" for c in value):
        raise ValueError("Invalid financial symbol")
    return value


def sanitize_fact(kind, raw):
    result = {}
    for key in FIELDS[kind].split():
        if key not in raw or raw[key] in (None, ""):
            continue
        value = raw[key]
        if key in DECIMALS:
            decimal(value)
            result[key] = str(value)
        elif key in INTEGERS:
            result[key] = int(value)
        elif key == "symbol":
            result[key] = safe_symbol(value)
        else:
            result[key] = safe_token(value)
    # Zero positions sometimes have empty side/prices. They are valid tombstones.
    required = REQUIRED[kind].split()
    if kind == "positions" and decimal(result.get("size", "-1")) == 0:
        required = ["symbol", "positionIdx", "size"]
    if any(key not in result for key in required):
        raise ValueError("Incomplete financial fact")
    if kind in ("executions", "orders", "closed_pnl", "positions"):
        if result.get("side") not in ("Buy", "Sell", None):
            raise ValueError("Unknown financial side")
    if kind == "transactions":
        if decimal(result["cashFlow"]) + decimal(result["funding"]) - decimal(result["fee"]) != decimal(result["change"]):
            raise ValueError("Transaction arithmetic mismatch")
    return result


def identity(kind, row):
    if kind == "executions":
        return row["execId"]
    if kind == "transactions":
        return row["id"]
    return row["symbol"] + ":" + row["orderId"]


def timestamp(kind, row):
    key = {"executions": "execTime", "transactions": "transactionTime", "orders": "createdTime",
           "closed_pnl": "createdTime"}[kind]
    return int(row[key])


def wallet_snapshot(response):
    accounts = [x for x in response["result"]["list"] if x.get("accountType") == "UNIFIED"]
    if len(accounts) != 1:
        raise ValueError("Missing UNIFIED account")
    account = accounts[0]
    coins = [x for x in account["coin"] if x.get("coin") == "USDT"]
    if len(coins) != 1:
        raise ValueError("Missing USDT balance")
    coin = coins[0]
    result = {}
    for key in ("walletBalance", "equity", "unrealisedPnl", "cumRealisedPnl", "spotBorrow"):
        if key in coin and coin[key] != "":
            decimal(coin[key])
            result[key] = str(coin[key])
    if not all(key in result for key in ("walletBalance", "equity", "unrealisedPnl")):
        raise ValueError("Incomplete USDT balance")
    # USD totals are explicitly separate from the coin denominated values.
    for key in ("totalEquity", "totalWalletBalance", "totalPerpUPL"):
        if account.get(key) not in (None, ""):
            decimal(account[key])
            result[key + "USD"] = str(account[key])
    return result
