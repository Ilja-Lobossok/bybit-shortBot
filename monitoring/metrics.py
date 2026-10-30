"""SDK pipeline, used only in monitoring threads (never on the order path)."""

from __future__ import annotations

import logging
import math
import re
import shutil
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

import psutil
from opentelemetry.metrics import Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource

from monitoring.otlp import CloudCredentials, ConfigurationError, create_otlp_exporter
from monitoring.runtime import OPERATIONS, ERROR_CATEGORIES

logger = logging.getLogger(__name__)
EXPORT_TIMEOUT_SECONDS = 5
EXPORT_WARNING_INTERVAL_SECONDS = 900
DURATION_BOUNDS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30)


@contextmanager
def quiet_current_transport_thread():
    """Suppress raw SDK errors only in this exporter thread; preserve bot logging."""
    owner = threading.get_ident()

    class ThreadFilter(logging.Filter):
        def filter(self, record):
            return not (record.thread == owner and
                        record.name.startswith(("opentelemetry", "requests", "urllib3")))

    log_filter = ThreadFilter()
    targets = set(logging.getLogger().handlers)
    for name, value in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(value, logging.Logger) and name.startswith(("opentelemetry", "requests", "urllib3")):
            targets.add(value)
            targets.update(value.handlers)
    for target in targets:
        target.addFilter(log_filter)
    try:
        yield
    finally:
        for target in targets:
            target.removeFilter(log_filter)


class SafeExporter(MetricExporter):
    def __init__(self, delegate, monitor):
        super().__init__()
        self.delegate = delegate
        self.monitor = monitor
        self._last_warning = None
        self._failure_active = False

    def export(self, metrics_data, timeout_millis=5000, **kwargs):
        if self.monitor._stop.is_set():
            return MetricExportResult.FAILURE
        with quiet_current_transport_thread():
            try:
                result = self.delegate.export(metrics_data, timeout_millis=timeout_millis, **kwargs)
            except Exception:
                result = MetricExportResult.FAILURE
        if result != MetricExportResult.SUCCESS:
            self.monitor.export_errors += 1
            if not self.monitor._stop.is_set():
                now = time.monotonic()
                if (not self._failure_active or self._last_warning is None
                        or now - self._last_warning >= EXPORT_WARNING_INTERVAL_SECONDS):
                    logger.warning("Monitoring export failed; details suppressed.")
                    self._last_warning = now
                self._failure_active = True
        else:
            self._failure_active = False
        return result

    def force_flush(self, timeout_millis=5000):
        return True  # No exporter-side application queue to flush.

    def shutdown(self, timeout_millis=5000, **kwargs):
        with quiet_current_transport_thread():
            try:
                self.delegate.shutdown(timeout_millis=timeout_millis, **kwargs)
            except Exception:
                if not self.monitor._stop.is_set():
                    logger.warning("Monitoring exporter cleanup failed; details suppressed.")


class ResourceSampler:
    def __init__(self, disk_path):
        self.process = psutil.Process()
        self.disk_path = disk_path

    def sample(self):
        values = {}
        for name, read in (
            ("cpu", lambda: sum(self.process.cpu_times()[:2])),
            ("memory", lambda: self.process.memory_info().rss),
            ("disk", lambda: shutil.disk_usage(self.disk_path).free),
            ("start", self.process.create_time),
        ):
            try:
                values[name] = read()
            except Exception:
                pass  # Omit a failed measurement instead of inventing zero.
        return values


class MetricPipeline:
    def __init__(self, monitor, *, exporter=None, sampler=None,
                 extra_readers=(), export_interval_millis=60000):
        environment = monitor.env.get("MONITORING_ENVIRONMENT", "production")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", environment):
            raise ConfigurationError("Invalid MONITORING_ENVIRONMENT")
        credentials = CloudCredentials.from_env(monitor.env)
        self.monitor = monitor
        self.labels = {"bot": "bybit-shortbot", "environment": environment, "component": "trading-bot"}
        self.resource_values = {}
        self.sampler = sampler if sampler is not None else ResourceSampler(monitor.disk_path)
        with quiet_current_transport_thread():
            self.exporter = SafeExporter(
                exporter if exporter is not None else create_otlp_exporter(credentials), monitor
            )
        self.reader = None
        self.provider = None
        try:
            self._configure(extra_readers, export_interval_millis)
        except Exception:
            try:
                self.close()
            except Exception:
                pass
            raise

    def _configure(self, extra_readers, export_interval_millis):
        monitor = self.monitor
        self.reader = PeriodicExportingMetricReader(
            self.exporter, export_interval_millis=export_interval_millis,
            export_timeout_millis=EXPORT_TIMEOUT_SECONDS * 1000,
        )
        self.provider = MeterProvider(
            metric_readers=[self.reader, *extra_readers],
            resource=Resource({"service.name": "bybit-shortbot", "service.instance.id": uuid4().hex}),
            shutdown_on_exit=False,
            views=[View(instrument_name="bot_api_duration_seconds",
                        aggregation=ExplicitBucketHistogramAggregation(DURATION_BOUNDS))],
        )
        meter = self.provider.get_meter("bybit_shortbot.runtime", "2")
        self.requests = meter.create_counter("bot_api_requests_total")
        self.errors = meter.create_counter("bot_api_errors_total")
        self.duration = meter.create_histogram("bot_api_duration_seconds", unit="s")
        # Zero counters are observable even before the first failure. Never seed
        # a histogram with a fictitious zero-duration request.
        for operation in OPERATIONS:
            for result in ("success", "error"):
                self.requests.add(0, dict(self.labels, operation=operation, result=result))
            for category in ERROR_CATEGORIES:
                self.errors.add(0, dict(self.labels, operation=operation, category=category))

        def observe(read):
            def callback(_options):
                value = read()
                return [] if value is None else [Observation(value, self.labels)]
            return callback

        for name, field in (
            ("bot_main_progress_timestamp_seconds", "main_progress"),
            ("bot_cycle_success_timestamp_seconds", "cycle_success"),
            ("bot_balance_success_timestamp_seconds", "balance_success"),
        ):
            meter.create_observable_gauge(name, [observe(lambda field=field: getattr(monitor, field))], unit="s")
        for name, field, unit in (
            ("bot_process_resident_memory_bytes", "memory", "By"),
            ("bot_disk_available_bytes", "disk", "By"),
            ("bot_process_start_timestamp_seconds", "start", "s"),
        ):
            meter.create_observable_gauge(name, [observe(lambda field=field: self.resource_values.get(field))], unit=unit)
        meter.create_observable_counter("bot_process_cpu_seconds_total",
                                        [observe(lambda: self.resource_values.get("cpu"))], unit="s")
        meter.create_observable_counter("bot_monitoring_dropped_events_total",
                                        [observe(lambda: monitor.dropped_events)])
        meter.create_observable_counter("bot_monitoring_export_errors_total",
                                        [observe(lambda: monitor.export_errors)])

    def consume_events(self):
        for operation, result, category, duration in self.monitor.take_events():
            self.requests.add(1, dict(self.labels, operation=operation, result=result))
            if result == "error":
                self.errors.add(1, dict(self.labels, operation=operation, category=category))
            if duration is not None and math.isfinite(duration) and duration >= 0:
                self.duration.record(duration, dict(self.labels, operation=operation))

    def sample_resources(self):
        self.resource_values = self.sampler.sample()

    def close(self):
        with quiet_current_transport_thread():
            component = self.provider or self.reader or self.exporter
            component.shutdown(timeout_millis=EXPORT_TIMEOUT_SECONDS * 1000)
