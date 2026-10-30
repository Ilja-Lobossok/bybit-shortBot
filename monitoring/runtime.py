"""Fail-open trading observations. This module imports no SDK and makes no HTTP calls."""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections import deque
from types import MappingProxyType

logger = logging.getLogger(__name__)

ENV_KEYS = ("MONITORING_ENABLED", "MONITORING_ENVIRONMENT",
            "OTEL_EXPORTER_OTLP_ENDPOINT", "GRAFANA_CLOUD_OTLP_USERNAME",
            "GRAFANA_CLOUD_API_TOKEN")
OPERATIONS = frozenset(("get_instruments_info", "get_tickers", "get_kline",
                        "get_wallet_balance", "place_order", "get_positions",
                        "get_order_history", "get_closed_pnl", "get_open_orders",
                        "cancel_order", "other"))
STAGES = frozenset(("balance_before", "positions", "tickers", "watchlist",
                    "stall", "entries", "balance_after"))
ERROR_CATEGORIES = frozenset(("exchange", "timeout", "network", "exception", "invalid_response"))
EVENT_CAPACITY = 1024


def capture_environment(env=None):
    """Call before the existing load_dotenv(): monitoring never loads file secrets."""
    env = os.environ if env is None else env
    enabled = env.get("MONITORING_ENABLED", "false")
    if enabled.lower() != "true":
        return MappingProxyType({"MONITORING_ENABLED": "false"})
    return MappingProxyType({key: env[key] for key in ENV_KEYS if key in env})


class NullMonitoring:
    enabled = False


DISABLED = NullMonitoring()


def notify(monitor, method, *args, **kwargs):
    """A telemetry exception must never escape into a trading handler/retry."""
    try:
        if monitor is not None and monitor.enabled:
            getattr(monitor, method)(*args, **kwargs)
    except Exception:
        pass


def _duration(started):
    try:
        value = time.perf_counter() - started
        return value if math.isfinite(value) and value >= 0 else None
    except Exception:
        return None


def observe_api_call(monitor, operation, call, *args, **kwargs):
    """Invoke exactly once, return the same object, and re-raise the same exception."""
    try:
        enabled = monitor is not None and monitor.enabled
    except Exception:
        enabled = False
    if not enabled:
        return call(*args, **kwargs)
    try:
        started = time.perf_counter()
    except Exception:
        started = None
    try:
        response = call(*args, **kwargs)
    except BaseException as error:
        name = type(error).__name__
        category = ("timeout" if name in ("Timeout", "ReadTimeout", "ConnectTimeout", "TimeoutError")
                    else "network" if name in ("ConnectionError", "ConnectionResetError", "SSLError", "ProxyError")
                    else "exception")
        notify(monitor, "api_result", operation, "error", category, _duration(started))
        raise
    try:
        code = response.get("retCode") if isinstance(response, dict) else None
        result = "success" if code == 0 else "error"
        category = "exchange" if code is not None else "invalid_response"
    except Exception:
        result, category = "error", "invalid_response"
    notify(monitor, "api_result", operation, result, category, _duration(started))
    return response


class RuntimeMonitoring:
    """Main-thread timestamps plus a bounded non-waiting API-event mailbox."""

    enabled = True

    def __init__(self, env, disk_path):
        self.env = env
        self.disk_path = disk_path
        self.owner_thread = threading.get_ident()
        self.main_progress = None
        self.cycle_success = None
        self.balance_success = None
        self._cycle_active = False
        self._cycle_ok = False
        self._completed_stages = set()
        self._events = deque(maxlen=EVENT_CAPACITY)
        self._event_lock = threading.Lock()
        self.dropped_events = 0
        self.export_errors = 0
        self._stop = threading.Event()
        self._thread = None

    def progress(self):
        if threading.get_ident() == self.owner_thread:
            self.main_progress = time.time()

    def begin_cycle(self):
        self.progress()
        self._cycle_active = True
        self._cycle_ok = True
        self._completed_stages.clear()

    def fail_cycle(self):
        if self._cycle_active:
            self._cycle_ok = False

    def stage_completed(self, stage, success=True):
        self.progress()
        if not success:
            self.fail_cycle()
        if stage in STAGES:
            self._completed_stages.add(stage)

    def finish_cycle(self):
        self.progress()
        if self._cycle_active and self._cycle_ok and self._completed_stages == STAGES:
            self.cycle_success = time.time()
        self._cycle_active = False

    def balance_validated(self):
        self.progress()
        self.balance_success = time.time()

    def api_result(self, operation, result, category, duration):
        self.progress()
        if result != "success":
            # Synchronous status: queue pressure cannot turn a failed cycle green.
            self.fail_cycle()
        operation = operation if operation in OPERATIONS else "other"
        result = "success" if result == "success" else "error"
        category = category if category in ERROR_CATEGORIES else "exception"
        if not self._event_lock.acquire(blocking=False):
            self.dropped_events += 1
            return
        try:
            if len(self._events) == EVENT_CAPACITY:
                self.dropped_events += 1
            self._events.append((operation, result, category, duration))
        finally:
            self._event_lock.release()

    def take_events(self):
        if not self._event_lock.acquire(blocking=False):
            return []
        try:
            events = list(self._events)
            self._events.clear()
            return events
        finally:
            self._event_lock.release()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="bybit-monitoring", daemon=True)
        self._thread.start()

    def stop(self):
        # No join/flush on the trading thread, including during network failure.
        self._stop.set()

    def _run(self):
        pipeline = None
        try:
            from monitoring.metrics import MetricPipeline
            pipeline = MetricPipeline(self)
            next_resources = 0.0
            while not self._stop.wait(0.1):
                pipeline.consume_events()
                now = time.monotonic()
                if now >= next_resources:
                    pipeline.sample_resources()
                    next_resources = now + 10
        except Exception:
            self.enabled = False
            self._stop.set()
            logger.warning("Monitoring worker disabled after an internal error; details suppressed.")
        finally:
            if pipeline is not None:
                try:
                    pipeline.close()
                except Exception:
                    logger.warning("Monitoring cleanup failed; details suppressed.")


def start_monitoring(env, disk_path):
    if env.get("MONITORING_ENABLED", "false").lower() != "true":
        return DISABLED
    try:
        monitor = RuntimeMonitoring(env, disk_path)
        monitor.start()
        return monitor
    except Exception:
        logger.warning("Monitoring could not start; trading continues without it.")
        return DISABLED
