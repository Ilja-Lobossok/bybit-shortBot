"""Finite OpenTelemetry smoke test. No bot imports, exchange calls, or local state.

Run ``python -m monitoring.otel_test`` for console output. Network export requires
an explicit ``--mode otlp`` and credentials supplied through the environment.
This is a standalone diagnostic, not instrumentation of the trading process.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Mapping
from uuid import uuid4

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    InMemoryMetricReader,
    MetricExporter,
    MetricExportResult,
)
from opentelemetry.sdk.resources import Resource
from monitoring.otlp import CloudCredentials, ConfigurationError, create_otlp_exporter


@dataclass(frozen=True)
class Settings:
    mode: str = "console"
    iterations: int = 3
    interval_seconds: float = 60.0
    timeout_seconds: float = 5.0
    endpoint: str = field(default="", repr=False)
    username: str = field(default="", repr=False)
    token: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, args: argparse.Namespace, env: Mapping[str, str]) -> Settings:
        if not 1 <= args.iterations <= 20:
            raise ConfigurationError("iterations must be between 1 and 20")
        if not math.isfinite(args.interval_seconds) or not 5 <= args.interval_seconds <= 300:
            raise ConfigurationError("interval-seconds must be between 5 and 300")
        if not math.isfinite(args.timeout_seconds) or not 0.1 <= args.timeout_seconds <= 30:
            raise ConfigurationError("timeout-seconds must be between 0.1 and 30")

        options = dict(mode=args.mode, iterations=args.iterations,
                       interval_seconds=args.interval_seconds,
                       timeout_seconds=args.timeout_seconds)
        # Console mode deliberately ignores all cloud credentials, including .env.
        if args.mode == "console":
            return cls(**options)

        credentials = CloudCredentials.from_env(env)
        return cls(**options, endpoint=credentials.endpoint,
                   username=credentials.username, token=credentials.token)


@contextmanager
def quiet_transport_logs():
    """Do not print SDK response bodies, URLs or exception text with credentials.

    Scoped to this standalone run, restored afterwards, and never set at import.
    The caller reports only batch numbers and success/failure, not raw errors.
    """
    saved = []
    for name in ("opentelemetry", "urllib3", "requests"):
        logger = logging.getLogger(name)
        saved.append((logger, logger.handlers[:], logger.propagate))
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
    try:
        yield
    finally:
        for logger, handlers, propagate in saved:
            logger.handlers = handlers
            logger.propagate = propagate


def make_exporter(settings: Settings) -> MetricExporter:
    if settings.mode == "console":
        return ConsoleMetricExporter(out=sys.stdout)
    return create_otlp_exporter(settings, timeout_seconds=settings.timeout_seconds,
                                exporter_type=OTLPMetricExporter)


def run_test(
    settings: Settings,
    *,
    exporter: MetricExporter | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    output: Callable[[str], None] = print,
) -> int:
    """Collect and export one cumulative batch per iteration; return 0/1/130.

    An SDK InMemoryMetricReader supplies snapshots for this finite diagnostic.
    There is no periodic background exporter, unbounded queue or shutdown export.
    A failed batch is not re-enqueued here; only SDK timeout-bounded retries apply.
    """
    reader = InMemoryMetricReader()
    # Resource.create() discovers environment attributes. Use explicit metadata
    # instead: neither machine details nor arbitrary env values are exported.
    provider = MeterProvider(
        metric_readers=[reader],
        resource=Resource({"service.name": "bybit-shortbot-monitoring-test",
                           "service.instance.id": uuid4().hex}),
        shutdown_on_exit=False,
    )
    meter = provider.get_meter("bybit_shortbot.monitoring_test", "1")
    counter = meter.create_counter(
        "bot_monitoring_test_total", description="Ticks of the standalone monitoring test"
    )
    attributes = {"environment": "monitoring-test", "component": "otel-test"}
    heartbeat = [0.0]
    meter.create_observable_gauge(
        "bot_monitoring_heartbeat_timestamp_seconds",
        callbacks=[lambda _options: [Observation(heartbeat[0], attributes)]],
        unit="s", description="Last tick of this test process, not of the trading bot",
    )

    failed = False
    active_exporter = exporter
    with quiet_transport_logs():
        try:
            if active_exporter is None:
                active_exporter = make_exporter(settings)
            for index in range(settings.iterations):
                heartbeat[0] = clock()
                counter.add(1, attributes)
                try:
                    batch = reader.get_metrics_data()
                    result = active_exporter.export(
                        batch, timeout_millis=settings.timeout_seconds * 1000
                    ) if batch is not None else MetricExportResult.FAILURE
                except Exception:
                    result = MetricExportResult.FAILURE
                success = result == MetricExportResult.SUCCESS
                failed |= not success
                output(f"Batch {index + 1}/{settings.iterations}: "
                       + ("export succeeded" if success else "export failed (details suppressed)"))
                if index + 1 < settings.iterations:
                    sleep(settings.interval_seconds)
        except KeyboardInterrupt:
            output("Monitoring test interrupted.")
            return 130
        except Exception:
            output("Monitoring test failed; configuration/SDK details suppressed.")
            failed = True
        finally:
            for component in (active_exporter, provider):
                if component is not None:
                    try:
                        component.shutdown(timeout_millis=settings.timeout_seconds * 1000)
                    except Exception:
                        output("Monitoring cleanup failed (details suppressed).")
                        failed = True
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("console", "otlp"), default="console")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--interval-seconds", type=float, default=60)
    parser.add_argument("--timeout-seconds", type=float, default=5)
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env(args, os.environ)
    except ConfigurationError as error:
        print(f"Configuration error: {error}")
        return 2
    return run_test(settings)


if __name__ == "__main__":
    raise SystemExit(main())
