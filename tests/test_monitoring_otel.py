"""Offline smoke-test regressions: no Bybit dependency and no network allowed."""

import argparse
import base64
import contextlib
import io
import logging
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    MetricExporter,
    MetricExportResult,
)

from monitoring.otel_test import ConfigurationError, Settings, main, run_test


TEST_ENDPOINT = "https://otlp-gateway-test.grafana.net/otlp"
TEST_TOKEN = "synthetic-secret-not-a-real-token"
TEST_ENV = {
    "OTEL_EXPORTER_OTLP_ENDPOINT": TEST_ENDPOINT,
    "GRAFANA_CLOUD_OTLP_USERNAME": "12345",
    "GRAFANA_CLOUD_API_TOKEN": TEST_TOKEN,
}


def settings_args(**overrides):
    options = dict(mode="otlp", iterations=3, interval_seconds=60, timeout_seconds=0.1)
    options.update(overrides)
    return argparse.Namespace(**options)


def metrics(batch):
    return {metric.name: metric for resource in batch.resource_metrics
            for scope in resource.scope_metrics for metric in scope.metrics}


class RecordingExporter(MetricExporter):
    def __init__(self, outcomes=None):
        super().__init__()
        self.batches = []
        self.timeouts = []
        self.closed = False
        self.outcomes = iter(outcomes) if outcomes is not None else None

    def export(self, metrics_data, timeout_millis=10000, **kwargs):
        self.batches.append(metrics_data)
        self.timeouts.append(timeout_millis)
        outcome = next(self.outcomes) if self.outcomes is not None else MetricExportResult.SUCCESS
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def force_flush(self, timeout_millis=10000):
        return True

    def shutdown(self, timeout_millis=30000, **kwargs):
        self.closed = True


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        patchers = [patch.dict(os.environ, {}, clear=True),
                    patch.object(socket, "getaddrinfo", side_effect=AssertionError("Network forbidden")),
                    patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden"))]
        # Catch accidental network use even if a transport mock is removed.
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)


class ConfigurationTests(OfflineTestCase):
    def test_base_endpoint_and_credentials(self):
        settings = Settings.from_env(settings_args(), TEST_ENV)
        self.assertEqual(settings.endpoint, TEST_ENDPOINT + "/v1/metrics")
        self.assertEqual(settings.username, "12345")

    def test_trailing_slash_does_not_duplicate_metrics_path(self):
        settings = Settings.from_env(settings_args(), dict(TEST_ENV, OTEL_EXPORTER_OTLP_ENDPOINT=TEST_ENDPOINT + "/"))
        self.assertEqual(settings.endpoint, TEST_ENDPOINT + "/v1/metrics")

    def test_console_needs_no_credentials(self):
        settings = Settings.from_env(settings_args(mode="console"), {})
        self.assertEqual((settings.endpoint, settings.token, settings.username), ("", "", ""))

    def test_each_missing_credential_is_rejected_without_echoing_values(self):
        for missing in TEST_ENV:
            env = dict(TEST_ENV)
            del env[missing]
            with self.subTest(missing=missing), self.assertRaises(ConfigurationError) as error:
                Settings.from_env(settings_args(), env)
            self.assertIn(missing, str(error.exception))
            self.assertNotIn(TEST_TOKEN, str(error.exception))

    def test_unsafe_or_wrong_endpoints_are_rejected(self):
        endpoints = [
            "http://otlp-gateway-test.grafana.net/otlp",
            "https://example.org/otlp",
            "https://grafana.net.attacker.example/otlp",
            "https://user:synthetic-secret@otlp-gateway-test.grafana.net/otlp",
            TEST_ENDPOINT + "?token=" + TEST_TOKEN,
            TEST_ENDPOINT + "#" + TEST_TOKEN,
            TEST_ENDPOINT + "/v1/metrics",
            "https://otlp-gateway-test.grafana.net:bad/otlp",
            "https://otlp-gateway-test.grafana.net:444/otlp",
        ]
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint), self.assertRaises(ConfigurationError) as error:
                Settings.from_env(settings_args(), dict(TEST_ENV, OTEL_EXPORTER_OTLP_ENDPOINT=endpoint))
            self.assertNotIn(TEST_TOKEN, str(error.exception))
            self.assertNotIn("synthetic-secret@", str(error.exception))

    def test_invalid_auth_values_are_rejected(self):
        for name, value in [("GRAFANA_CLOUD_OTLP_USERNAME", "user:pass"),
                            ("GRAFANA_CLOUD_OTLP_USERNAME", "１２３"),
                            ("GRAFANA_CLOUD_API_TOKEN", "token\nInjected: header"),
                            ("GRAFANA_CLOUD_API_TOKEN", " token"),
                            ("GRAFANA_CLOUD_API_TOKEN", "токен")]:
            with self.subTest(name=name, value=value), self.assertRaises(ConfigurationError):
                Settings.from_env(settings_args(), dict(TEST_ENV, **{name: value}))

    def test_numeric_limits_reject_non_finite_or_unbounded_runs(self):
        for name, value in [("iterations", 0), ("iterations", 21),
                            ("interval_seconds", 0), ("interval_seconds", 301),
                            ("interval_seconds", float("nan")),
                            ("timeout_seconds", 0), ("timeout_seconds", 31),
                            ("timeout_seconds", float("inf"))]:
            with self.subTest(name=name, value=value), self.assertRaises(ConfigurationError):
                Settings.from_env(settings_args(**{name: value}), TEST_ENV)

    def test_settings_repr_does_not_contain_credentials(self):
        representation = repr(Settings.from_env(settings_args(), TEST_ENV))
        self.assertNotIn(TEST_TOKEN, representation)
        self.assertNotIn("12345", representation)
        self.assertNotIn(TEST_ENDPOINT, representation)


class MetricTests(OfflineTestCase):
    def test_cumulative_counter_and_heartbeat_follow_test_ticks(self):
        exporter = RecordingExporter()
        sleep = Mock()
        ticks = iter([1000.0, 1060.0, 1120.0])
        result = run_test(Settings(), exporter=exporter, sleep=sleep,
                          clock=lambda: next(ticks), output=lambda _: None)
        self.assertEqual(result, 0)
        self.assertEqual(len(exporter.batches), 3)
        self.assertEqual([call.args for call in sleep.call_args_list], [(60.0,), (60.0,)])
        for index, batch in enumerate(exporter.batches):
            values = metrics(batch)
            self.assertEqual(set(values), {"bot_monitoring_test_total", "bot_monitoring_heartbeat_timestamp_seconds"})
            counter = values["bot_monitoring_test_total"].data
            self.assertTrue(counter.is_monotonic)
            self.assertEqual(counter.aggregation_temporality, AggregationTemporality.CUMULATIVE)
            self.assertEqual(counter.data_points[0].value, index + 1)
            self.assertEqual(values["bot_monitoring_heartbeat_timestamp_seconds"].data.data_points[0].value,
                             1000 + 60 * index)
            self.assertEqual(dict(counter.data_points[0].attributes),
                             {"environment": "monitoring-test", "component": "otel-test"})
        self.assertEqual(exporter.timeouts, [5000.0] * 3)
        self.assertTrue(exporter.closed)

    def test_failed_batches_do_not_create_an_unbounded_retry_queue(self):
        exporter = RecordingExporter([MetricExportResult.FAILURE] * 3)
        output = []
        result = run_test(Settings(), exporter=exporter, sleep=lambda _: None, output=output.append)
        self.assertEqual(result, 1)
        self.assertEqual(len(exporter.batches), 3)
        self.assertEqual(len(output), 3)
        self.assertTrue(exporter.closed)

    def test_export_exception_is_sanitized_and_later_batches_continue(self):
        exporter = RecordingExporter([RuntimeError(TEST_TOKEN), MetricExportResult.SUCCESS])
        output = []
        result = run_test(Settings(iterations=2), exporter=exporter,
                          sleep=lambda _: None, output=output.append)
        self.assertEqual(result, 1)
        self.assertEqual(len(exporter.batches), 2)
        self.assertNotIn(TEST_TOKEN, " ".join(output))

    def test_interruption_closes_exporter_without_a_final_network_export(self):
        exporter = RecordingExporter()
        result = run_test(Settings(), exporter=exporter,
                          sleep=Mock(side_effect=KeyboardInterrupt), output=lambda _: None)
        self.assertEqual(result, 130)
        self.assertEqual(len(exporter.batches), 1)
        self.assertTrue(exporter.closed)

    def test_arbitrary_resource_environment_values_are_not_exported(self):
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "secret=" + TEST_TOKEN
        os.environ["OTEL_SERVICE_NAME"] = TEST_TOKEN
        exporter = RecordingExporter()
        run_test(Settings(iterations=1), exporter=exporter, output=lambda _: None)
        self.assertNotIn(TEST_TOKEN, exporter.batches[0].to_json())

    def test_run_does_not_create_local_state_or_load_dotenv(self):
        exporter = RecordingExporter()
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                Path(".env").write_text("GRAFANA_CLOUD_API_TOKEN=" + TEST_TOKEN, encoding="utf-8")
                run_test(Settings(iterations=1), exporter=exporter, output=lambda _: None)
                self.assertEqual({path.name for path in Path(directory).iterdir()}, {".env"})
                self.assertNotIn(TEST_TOKEN, exporter.batches[0].to_json())
            finally:
                os.chdir(previous)

    def test_console_cli_ignores_cloud_secrets_and_never_constructs_http_exporter(self):
        os.environ.update(TEST_ENV)
        stdout = io.StringIO()
        with patch("monitoring.otel_test.OTLPMetricExporter") as factory, contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["--iterations", "1"]), 0)
        factory.assert_not_called()
        self.assertIn("bot_monitoring_test_total", stdout.getvalue())
        self.assertNotIn(TEST_TOKEN, stdout.getvalue())

    def test_exporter_setup_failure_is_sanitized(self):
        output = []
        with patch("monitoring.otel_test.make_exporter", side_effect=RuntimeError(TEST_TOKEN)):
            result = run_test(Settings(iterations=1), output=output.append)
        self.assertEqual(result, 1)
        self.assertNotIn(TEST_TOKEN, " ".join(output))

    def test_cleanup_failure_is_sanitized_and_marks_run_failed(self):
        exporter = RecordingExporter()
        output = []
        with patch.object(exporter, "shutdown", side_effect=RuntimeError(TEST_TOKEN)):
            result = run_test(Settings(iterations=1), exporter=exporter, output=output.append)
        self.assertEqual(result, 1)
        self.assertNotIn(TEST_TOKEN, " ".join(output))


class TransportTests(OfflineTestCase):
    def test_otlp_request_is_protobuf_authenticated_and_timeout_bounded(self):
        captured = []

        def send(_session, request, **kwargs):
            captured.append((request, kwargs))
            response = requests.Response()
            response.status_code = 200
            response._content = b""
            return response

        os.environ.update(TEST_ENV)
        stdout = io.StringIO()
        with patch.object(requests.Session, "send", autospec=True, side_effect=send), contextlib.redirect_stdout(stdout):
            result = main(["--mode", "otlp", "--iterations", "1", "--timeout-seconds", "0.1"])
        self.assertEqual(result, 0)
        self.assertEqual(len(captured), 1)
        request, options = captured[0]
        self.assertEqual(request.url, TEST_ENDPOINT + "/v1/metrics")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.headers["Content-Type"], "application/x-protobuf")
        expected_auth = base64.b64encode(("12345:" + TEST_TOKEN).encode()).decode()
        self.assertEqual(request.headers["Authorization"], "Basic " + expected_auth)
        self.assertTrue(options["verify"])
        self.assertGreater(options["timeout"], 0)
        self.assertLessEqual(options["timeout"], 0.1)
        payload = ExportMetricsServiceRequest.FromString(request.body)
        metric_names = {metric.name for resource in payload.resource_metrics
                        for scope in resource.scope_metrics for metric in scope.metrics}
        self.assertIn("bot_monitoring_test_total", metric_names)
        self.assertNotIn(TEST_TOKEN, str(payload))
        self.assertNotIn(TEST_TOKEN, stdout.getvalue())

    def test_http_and_transport_errors_do_not_leak_response_bodies_or_exceptions(self):
        for failure in [401, 503, requests.Timeout(TEST_TOKEN), requests.ConnectionError(TEST_TOKEN)]:
            def send(_session, _request, **kwargs):
                if isinstance(failure, Exception):
                    raise failure
                response = requests.Response()
                response.status_code = failure
                response.reason = TEST_TOKEN
                response._content = TEST_TOKEN.encode()
                return response

            os.environ.update(TEST_ENV)
            stdout, stderr = io.StringIO(), io.StringIO()
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(requests.Session, "send", autospec=True, side_effect=send) as transport, \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                    self.assertLogs(level="ERROR") as logs:
                # Own marker keeps assertLogs valid while SDK logs are suppressed.
                logging.getLogger("test.marker").error("test marker")
                result = main(["--mode", "otlp", "--iterations", "1", "--timeout-seconds", "0.1"])
            self.assertEqual(result, 1)
            # The pinned SDK also has one immediate connection-error retry.
            # It must not turn a single failed batch into an unbounded loop.
            self.assertGreaterEqual(transport.call_count, 1)
            self.assertLessEqual(transport.call_count, 2)
            self.assertNotIn(TEST_TOKEN, stdout.getvalue() + stderr.getvalue() + " ".join(logs.output))

    def test_configuration_error_has_no_network_request_or_secret_echo(self):
        os.environ.update(TEST_ENV)
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] += "?token=" + TEST_TOKEN
        stdout = io.StringIO()
        with patch.object(requests.Session, "send") as send, contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["--mode", "otlp", "--iterations", "1"]), 2)
        send.assert_not_called()
        self.assertNotIn(TEST_TOKEN, stdout.getvalue())

    def test_log_configuration_is_restored_after_run(self):
        logger = logging.getLogger("opentelemetry")
        before = (logger.handlers[:], logger.propagate)
        run_test(Settings(iterations=1), exporter=RecordingExporter(), output=lambda _: None)
        self.assertEqual((logger.handlers, logger.propagate), before)


if __name__ == "__main__":
    unittest.main()
