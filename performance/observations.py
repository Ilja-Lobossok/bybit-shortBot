"""Non-waiting copies of actual decisions. No DB, network or strategy calls here."""
from collections import deque
import os
import threading
import time
from types import MappingProxyType
from uuid import uuid4

REASONS = frozenset(("not_usdt", "invalid_market", "turnover", "pump", "near_high", "candidate",
                     "cooldown", "tracked", "watching", "missing_klines", "ttl", "new_high", "stall",
                     "below_tp", "ready", "position_limit", "balance", "risk", "exchange_position",
                     "funds", "missing_price", "min_order", "accepted", "pending", "order_error", "none"))
KEYS = ("PERFORMANCE_ENABLED", "PERFORMANCE_READONLY_API_ENABLED", "PERFORMANCE_LOG_EXPORT_ENABLED",
        "MONITORING_ENABLED", "BYBIT_API_KEY", "BYBIT_API_SECRET", "BYBIT_TESTNET",
        "PERFORMANCE_HISTORY_DAYS", "PERFORMANCE_ACCOUNT_ALIAS", "PERFORMANCE_GRAFANA_LOGS_TOKEN",
        "MONITORING_ENVIRONMENT", "OTEL_EXPORTER_OTLP_ENDPOINT", "GRAFANA_CLOUD_OTLP_USERNAME",
        "GRAFANA_CLOUD_API_TOKEN")


def capture_environment(env=None):
    env = os.environ if env is None else env
    if not is_enabled(env):
        return MappingProxyType({"PERFORMANCE_ENABLED": "false"})
    values = {key: env[key] for key in KEYS if key in env}
    values["PERFORMANCE_ENABLED"] = "true"
    return MappingProxyType(values)


def is_enabled(env):
    """Follow the already deployed monitoring flag; an explicit override wins."""
    return env.get("PERFORMANCE_ENABLED", env.get("MONITORING_ENABLED", "false")).lower() == "true"


class Disabled:
    enabled = False


DISABLED = Disabled()


def notify(observer, kind, **body):
    try:
        if observer is not None and observer.enabled:
            observer.emit(kind, body)
    except Exception:
        pass


def observe_order_call(observer, operation, observed_symbol, call, *args, **kwargs):
    """Exactly one original call, identical result/exception; no new order retry."""
    try:
        result = call(*args, **kwargs)
    except BaseException:
        if operation == "place_order":
            notify(observer, "decision", symbol=observed_symbol, reason="order_error")
        raise
    if operation == "place_order":
        try:
            if result.get("retCode") != 0:
                notify(observer, "decision", symbol=observed_symbol, reason="order_error")
        except Exception:
            pass
    return result


class Mailbox:
    enabled = True

    def __init__(self, capacity=1024):
        self.events = deque(maxlen=capacity)
        self.lock = threading.Lock()
        self.run_id = uuid4().hex
        self.seq = 0
        self.dropped = 0

    def emit(self, kind, body):
        # Every allowed field is copied here, before the mutable tracker changes.
        allowed = {
            "decision": ("symbol", "reason", "pump_pct", "stall", "local_high", "candle_ms", "scheduled_bar"),
            "entry": ("symbol", "order_id", "flat_before"),
            "bootstrap": ("entries",),
            "strategy_snapshot": ("rows", "scheduled_bar"),
        }
        if kind not in allowed:
            return
        from performance.contracts import decimal, safe_token, safe_symbol
        copied = {}
        if kind in ("decision", "entry"):
            copied["symbol"] = safe_symbol(body.get("symbol", ""))
            if kind == "entry":
                copied.update(order_id=safe_token(body["order_id"]), flat_before=body.get("flat_before") is True)
            else:
                reason = body.get("reason", "none")
                copied["reason"] = reason if reason in REASONS else "none"
                if reason == "risk":
                    # Classify the already-returned guard result; never re-evaluate
                    # risk or persist arbitrary text / changing numeric labels.
                    detail = body.get("risk_detail", "")
                    prefixes = (("Дневной лимит убытка:", "daily_loss"),
                                ("Нет подтвержденного положительного баланса", "balance"),
                                ("Просадка ", "drawdown"), ("3 убытка подряд", "consecutive_losses"))
                    copied["risk_reason"] = next((code for prefix, code in prefixes if isinstance(detail, str) and detail.startswith(prefix)), "unknown")
                for key in ("pump_pct", "stall", "local_high", "candle_ms", "scheduled_bar"):
                    if key in body:
                        copied[key] = str(decimal(body[key]))
        elif kind == "bootstrap":
            copied["entries"] = [{"symbol": safe_symbol(x["symbol"]),
                                  "order_id": safe_token(x["order_id"]), "flat_before": False}
                                 for x in body.get("entries", [])[:500]]
        else:
            copied["rows"] = []
            actual_rows = body.get("rows", [])
            copied["counts"] = {state: sum(row.get(state) is True for row in actual_rows)
                                for state in ("watching", "candidate", "ready", "blocked")}
            copied["counts"]["stalling"] = sum(row.get("watching") is True and decimal(row.get("stall", 0)) > 0 for row in actual_rows)
            copied["rows_truncated"] = len(actual_rows) > 500
            for row in body.get("rows", [])[:500]:
                value = {"symbol": safe_symbol(row["symbol"])}
                for key in ("pump_pct", "last", "near_high", "local_high", "stall", "updated_ts", "candle_ms"):
                    if row.get(key) is not None:
                        value[key] = str(decimal(row[key]))
                for key in ("blocked", "ready", "watching", "candidate"):
                    value[key] = row.get(key) is True
                copied["rows"].append(value)
            copied["scheduled_bar"] = int(body.get("scheduled_bar", 0))
        if not self.lock.acquire(blocking=False):
            self.dropped += 1
            return
        try:
            self.seq += 1
            if len(self.events) == self.events.maxlen:
                self.dropped += 1
                return  # drop new; no unbounded queue or blocked order
            self.events.append({"id": self.run_id + ":" + str(self.seq), "time_ms": int(time.time() * 1000),
                                "kind": kind, "body": copied})
        finally:
            self.lock.release()

    def take(self):
        with self.lock:
            events = list(self.events)
            self.events.clear()
        return events
