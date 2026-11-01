"""Exercise the real SDK encoder/size guard through a fake HTTP boundary."""
from unittest.mock import Mock, patch
import requests
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from performance.metrics import MetricPipeline, MAX_PERFORMANCE_REQUEST_BYTES
from performance.runtime import Runtime
from monitoring.otlp import CloudCredentials, ConfigurationError, create_otlp_exporter
from tests.performance_fixtures import Offline, position, seed, wallet
from tests.test_monitoring_otel import TEST_ENV, TEST_TOKEN


class RealPayloadTests(Offline):
    def portfolio(self, count, *, tombstones=False):
        seed(self.store)
        # Real upper bounds on existing labels, including non-BMP alphanumeric
        # symbols (4-byte UTF-8) and maximum 32-char env/account alias labels.
        symbol = '\U00010400' * 32 if tombstones else 'PAIR'
        if tombstones:
            old = [dict(position(), symbol=f'{symbol}{i:03}USDT', curRealisedPnl='-2') for i in range(200)]
            self.store.replace_positions(old, 10_000)
            self.store.replace_positions([], 70_000)
        current = [dict(position(), symbol=f'{symbol}{i + 200:03}USDT', curRealisedPnl='-2') for i in range(count)]
        self.store.replace_positions(current, 130_000)
        self.store.snapshot(wallet(), 130_000)
        env = dict(TEST_ENV, MONITORING_ENVIRONMENT='p' * 32, PERFORMANCE_ACCOUNT_ALIAS='a' * 32) if tombstones else dict(TEST_ENV)
        runtime = Runtime(env, self.path.parent)
        runtime.tick(self.store, None, None, 130_000)
        return runtime, env

    def check_real_http_export(self, count, *, tombstones=False):
        runtime, env = self.portfolio(count, tombstones=tombstones)
        session = requests.Session()
        response = requests.Response()
        response.status_code = 200
        response._content = b''
        session.request = Mock(return_value=response)
        # Block only the actual HTTP boundary. The real exporter, encoder,
        # max_request_size check and SafeExporter all run unchanged.
        with patch('requests.Session', return_value=session):
            pipeline = MetricPipeline(runtime, env)
        try:
            self.assertTrue(pipeline.provider.force_flush(timeout_millis=5000))
            self.assertGreater(session.request.call_count, 0, 'Real exporter dropped the body before HTTP')
            body = session.request.call_args.kwargs['data']
            self.assertGreater(len(body), 64 * 1024)
            self.assertLessEqual(len(body), MAX_PERFORMANCE_REQUEST_BYTES)
            message = ExportMetricsServiceRequest.FromString(body)
            names = {m.name for r in message.resource_metrics for s in r.scope_metrics for m in s.metrics}
            self.assertIn('bot_performance_open_combined_pnl', names)
            self.assertIn('bot_performance_position_combined_pnl', names)
            self.assertEqual(runtime.export_errors, 0)
            self.assertGreater(session.request.call_args.kwargs['timeout'], 0)
            self.assertLessEqual(session.request.call_args.kwargs['timeout'], 5)
            self.assertNotIn(TEST_TOKEN.encode(), body)
        finally:
            pipeline.close()

    def test_actual_82_position_body_passes_real_size_guard(self):
        self.check_real_http_export(82)

    def test_full_200_curves_and_200_tombstones_fit_bounded_request(self):
        self.check_real_http_export(200, tombstones=True)

    def test_previous_64k_cap_reproduces_pre_http_drop_for_82_positions(self):
        runtime, env = self.portfolio(82)
        session = requests.Session()
        session.request = Mock(side_effect=AssertionError('Old size guard must drop before HTTP'))
        with patch('requests.Session', return_value=session), \
                patch('performance.metrics.MAX_PERFORMANCE_REQUEST_BYTES', 64 * 1024):
            pipeline = MetricPipeline(runtime, env)
        try:
            with self.assertLogs('monitoring.metrics', level='WARNING'):
                pipeline.provider.force_flush(timeout_millis=5000)
            session.request.assert_not_called()
            self.assertEqual(runtime.export_errors, 1)
        finally:
            pipeline.close()

    def test_smoke_health_default_stays_64k_and_timeout_unchanged(self):
        factory = Mock()
        exporter = create_otlp_exporter(CloudCredentials.from_env(TEST_ENV), exporter_type=factory)
        kwargs = factory.call_args.kwargs
        self.assertEqual(kwargs['max_request_size'], 64 * 1024)
        self.assertEqual(kwargs['timeout'], 5)
        kwargs['session'].close()
        for invalid in (0, -1, 1024 * 1024 + 1, True, float('inf')):
            with self.assertRaises(ConfigurationError):
                create_otlp_exporter(CloudCredentials.from_env(TEST_ENV), max_request_bytes=invalid)
