"""Synthetic values only. No keys, account identifiers or production payloads."""
import socket
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from performance.storage import Store


class Offline(unittest.TestCase):
    def setUp(self):
        super().setUp()
        for guard in (patch.object(socket, "getaddrinfo", side_effect=AssertionError("Network forbidden")),
                      patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden"))):
            guard.start()
            self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "performance.sqlite3"
        self.store = Store(self.path, reserve_bytes=0)
        self.addCleanup(lambda: self.store.close())


def order(ident, side, qty, time, **extra):
    return dict(orderId=ident, symbol="TESTUSDT", side=side, positionIdx=0, orderStatus="Filled",
                qty=str(qty), cumExecQty=str(qty), createdTime=time, updatedTime=time + 1, **extra)


def fill(ident, order_id, side, qty, price, closed, time):
    return dict(execId=ident, orderId=order_id, symbol="TESTUSDT", side=side, execType="Trade", execTime=time,
                execQty=str(qty), execPrice=str(price), closedSize=str(closed), execFee=str(qty / 10), feeCurrency="USDT")


def transaction(ident, *, time=1000, **extra):
    value = dict(id=ident, currency="USDT", category="linear", symbol="TESTUSDT", type="TRADE",
                 transactionTime=time, cashFlow="0", funding="0", fee="0", change="0")
    value.update(extra)
    return value


def wallet(wallet="100", equity="100", time=5000):
    return {"retCode": 0, "time": time, "result": {"list": [{"accountType": "UNIFIED", "totalEquity": equity,
            "coin": [{"coin": "USDT", "walletBalance": wallet, "equity": equity,
                      "unrealisedPnl": str(int(equity) - int(wallet)), "cumRealisedPnl": "14.5"}]}]}}


def position(**extra):
    return dict(symbol="TESTUSDT", side="Sell", positionIdx=0, size="10", avgPrice="10", markPrice="8",
                unrealisedPnl="20", takeProfit="6", stopLoss="40", **extra)


def seed(store, *, owner=True, final=True):
    rows = {
        "orders": [order("entry", "Sell", 10, 1000), order("exit-a", "Buy", 6, 3000, stopOrderType="TakeProfit"),
                   order("exit-b", "Buy", 4, 4000, stopOrderType="TakeProfit")],
        "executions": [fill("e1", "entry", "Sell", 4, 10, 0, 1000), fill("e2", "entry", "Sell", 6, 10, 0, 2000),
                       fill("e3", "exit-a", "Buy", 6, 8, 6, 3000), fill("e4", "exit-b", "Buy", 4, 9, 4, 4000)],
        "closed_pnl": [dict(orderId="exit-a", symbol="TESTUSDT", side="Buy", closedSize="6", avgEntryPrice="10",
                            avgExitPrice="8", closedPnl="11.1", openFee="0.6", closeFee="0.6", createdTime=3000, updatedTime=3100, execType="Trade"),
                       dict(orderId="exit-b", symbol="TESTUSDT", side="Buy", closedSize="4", avgEntryPrice="10",
                            avgExitPrice="9", closedPnl="3.4", openFee="0.4", closeFee="0.4", createdTime=4000, updatedTime=4100, execType="Trade")],
        "transactions": [transaction("t1", time=1000, orderId="entry", tradeId="e1", size="-4", fee="0.4", change="-0.4"),
                         transaction("t2", time=2000, orderId="entry", tradeId="e2", size="-10", fee="0.6", change="-0.6"),
                         transaction("f1", time=2500, type="SETTLEMENT", funding="0.5", change="0.5"),
                         transaction("t3", time=3000, orderId="exit-a", tradeId="e3", size="-4", cashFlow="12", fee="0.6", change="11.4"),
                         transaction("t4", time=4000, orderId="exit-b", tradeId="e4", size="0", cashFlow="4", fee="0.4", change="3.6")],
    }
    for kind, values in rows.items():
        store.page(kind, 0, 5000, values, 5000, final=final)
    with store.db:
        store.set_meta("history_origin_ms", 0)
    if owner:
        store.observe({"id": "run:1", "kind": "entry", "time_ms": 1000,
                       "body": {"symbol": "TESTUSDT", "order_id": "entry", "flat_before": True}})
    return rows
