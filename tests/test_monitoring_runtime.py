"""Offline production telemetry tests; all outbound sockets are forbidden."""

import io
import logging
import math
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

from opentelemetry.sdk.metrics.export import InMemoryMetricReader, MetricExportResult

from monitoring.metrics import MetricPipeline, ResourceSampler, SafeExporter
from monitoring.runtime import (
    DISABLED, EVENT_CAPACITY, STAGES, RuntimeMonitoring, capture_environment,
    notify, observe_api_call, start_monitoring,
)
from tests.test_monitoring_otel import OfflineTestCase, RecordingExporter, TEST_ENV, TEST_TOKEN, metrics


def monitor():
    return RuntimeMonitoring(dict(TEST_ENV, MONITORING_ENABLED="true"), "unused-test-disk")


def complete_cycle(observation):
    for stage in STAGES:
        observation.stage_completed(stage)
    observation.finish_cycle()


class RuntimeTests(OfflineTestCase):
    def test_disabled_is_default_and_starts_no_thread(self):
        with patch("monitoring.runtime.RuntimeMonitoring") as factory:
            self.assertIs(start_monitoring({}, "unused"), DISABLED)
            self.assertIs(start_monitoring({"MONITORING_ENABLED": "false"}, "unused"), DISABLED)
        factory.assert_not_called()

    def test_environment_is_captured_before_dotenv_and_whitelisted(self):
        env = dict(TEST_ENV, MONITORING_ENABLED="false", BYBIT_API_KEY=TEST_TOKEN)
        snapshot = capture_environment(env)
        env.update(MONITORING_ENABLED="true")  # A later dotenv load cannot enable it.
        self.assertIs(start_monitoring(snapshot, "unused"), DISABLED)
        enabled = capture_environment(env)
        self.assertNotIn("BYBIT_API_KEY", enabled)
        with self.assertRaises(TypeError):
            enabled["MONITORING_ENABLED"] = "false"

    def test_main_progress_cannot_be_advanced_by_an_exporter_thread(self):
        observation = monitor()
        with patch("monitoring.runtime.time.time", return_value=100):
            observation.progress()
        thread = threading.Thread(target=observation.progress)
        thread.start()
        thread.join(timeout=1)
        self.assertEqual(observation.main_progress, 100)

    def test_waiting_or_partial_stages_do_not_complete_a_candle(self):
        observation = monitor()
        observation.progress()
        self.assertIsNone(observation.cycle_success)
        observation.begin_cycle()
        observation.stage_completed("positions")
        observation.finish_cycle()
        self.assertIsNone(observation.cycle_success)

    def test_only_a_complete_clean_cycle_updates_success(self):
        observation = monitor()
        observation.begin_cycle()
        complete_cycle(observation)
        successful = observation.cycle_success
        self.assertIsNotNone(successful)
        observation.begin_cycle()
        observation.api_result("get_tickers", "error", "exchange", 0.1)
        observation.api_result("get_tickers", "success", "exchange", 0.1)
        complete_cycle(observation)
        self.assertEqual(observation.cycle_success, successful)

    def test_failed_balance_stage_is_not_a_clean_cycle(self):
        observation = monitor()
        observation.begin_cycle()
        observation.stage_completed("balance_before", False)
        complete_cycle(observation)
        self.assertIsNone(observation.cycle_success)

    def test_api_arguments_result_identity_and_duration_are_preserved(self):
        observation = monitor()
        response = {"retCode": 0, "result": {"orderId": "synthetic-order"}}
        call = Mock(return_value=response)
        with patch("monitoring.runtime.time.perf_counter", side_effect=[5, 5.25]):
            actual = observe_api_call(observation, "place_order", call, "positional", qty="3", symbol="TESTUSDT")
        self.assertIs(actual, response)
        call.assert_called_once_with("positional", qty="3", symbol="TESTUSDT")
        self.assertEqual(observation.take_events(), [("place_order", "success", "exchange", 0.25)])

    def test_api_exception_identity_and_category_are_preserved(self):
        for error, category in [(TimeoutError(TEST_TOKEN), "timeout"),
                                (ConnectionError(TEST_TOKEN), "network"),
                                (ValueError(TEST_TOKEN), "exception")]:
            observation = monitor()
            call = Mock(side_effect=error)
            with self.subTest(category=category), self.assertRaises(type(error)) as actual:
                observe_api_call(observation, "place_order", call, qty="3")
            self.assertIs(actual.exception, error)
            call.assert_called_once_with(qty="3")
            self.assertEqual(observation.take_events()[0][2], category)

    def test_broken_observer_and_clock_do_not_retry_or_change_a_native_result(self):
        broken = SimpleNamespace(enabled=True, api_result=Mock(side_effect=RuntimeError(TEST_TOKEN)))
        response = {"retCode": 0}
        call = Mock(return_value=response)
        with patch("monitoring.runtime.time.perf_counter", side_effect=RuntimeError(TEST_TOKEN)):
            self.assertIs(observe_api_call(broken, "place_order", call, qty="1"), response)
        call.assert_called_once_with(qty="1")
        error = RuntimeError("original operation error")
        call = Mock(side_effect=error)
        with self.assertRaises(RuntimeError) as actual:
            observe_api_call(broken, "place_order", call)
        self.assertIs(actual.exception, error)
        call.assert_called_once_with()

    def test_disabled_calls_do_not_even_sample_a_clock(self):
        call = Mock(return_value=object())
        with patch("monitoring.runtime.time.perf_counter", side_effect=AssertionError("must not run")):
            self.assertIs(observe_api_call(DISABLED, "get_positions", call), call.return_value)
        call.assert_called_once_with()

    def test_mailbox_is_bounded_and_labels_are_normalized(self):
        observation = monitor()
        for _ in range(EVENT_CAPACITY + 7):
            observation.api_result(TEST_TOKEN, "error", TEST_TOKEN, 0.5)
        events = observation.take_events()
        self.assertEqual(len(events), EVENT_CAPACITY)
        self.assertEqual(observation.dropped_events, 7)
        self.assertEqual(events[0][:3], ("other", "error", "exception"))
        self.assertNotIn(TEST_TOKEN, str(events))

    def test_busy_mailbox_never_waits_or_turns_a_failed_cycle_green(self):
        observation = monitor()
        observation.begin_cycle()
        observation._event_lock.acquire()
        try:
            observation.api_result("get_positions", "error", "timeout", 0.5)
        finally:
            observation._event_lock.release()
        complete_cycle(observation)
        self.assertIsNone(observation.cycle_success)
        self.assertEqual(observation.dropped_events, 1)

    def test_worker_start_failure_is_fail_open_and_sanitized(self):
        with patch("monitoring.runtime.RuntimeMonitoring.start", side_effect=RuntimeError(TEST_TOKEN)), \
                self.assertLogs("monitoring.runtime") as logs:
            actual = start_monitoring(dict(TEST_ENV, MONITORING_ENABLED="true"), "unused")
        self.assertIs(actual, DISABLED)
        self.assertNotIn(TEST_TOKEN, " ".join(logs.output))

    def test_stop_never_joins_or_flushes_from_the_trading_thread(self):
        observation = monitor()
        observation._thread = Mock()
        observation.stop()
        self.assertTrue(observation._stop.is_set())
        observation._thread.join.assert_not_called()

    def test_sdk_initialization_failure_disables_only_monitoring(self):
        observation = monitor()
        with patch.dict(sys.modules, {"monitoring.metrics": None}), \
                self.assertLogs("monitoring.runtime"):
            observation._run()
        self.assertFalse(observation.enabled)
        native_call = Mock(return_value={"retCode": 0})
        self.assertEqual(observe_api_call(observation, "place_order", native_call), {"retCode": 0})
        native_call.assert_called_once_with()

    def test_missing_cloud_credentials_never_create_a_transport_or_affect_a_native_call(self):
        observation = RuntimeMonitoring({"MONITORING_ENABLED": "true"}, "unused-test-disk")
        with patch("monitoring.metrics.create_otlp_exporter") as factory, \
                self.assertLogs("monitoring.runtime"):
            observation._run()
        factory.assert_not_called()
        self.assertFalse(observation.enabled)
        native_call = Mock(return_value={"retCode": 0})
        self.assertEqual(observe_api_call(observation, "place_order", native_call, qty="3"), {"retCode": 0})
        native_call.assert_called_once_with(qty="3")


class PipelineTests(OfflineTestCase):
    def pipeline(self, observation=None, exporter=None):
        observation = observation or monitor()
        reader = InMemoryMetricReader()
        sampler = Mock()
        sampler.sample.return_value = dict(cpu=3.0, memory=1024, disk=2048, start=1000)
        pipeline = MetricPipeline(observation, exporter=exporter or RecordingExporter(),
                                  sampler=sampler, extra_readers=[reader], export_interval_millis=math.inf)
        self.addCleanup(pipeline.close)
        self.addCleanup(observation.stop)
        return observation, pipeline, reader

    def test_partial_sdk_setup_failure_closes_the_started_exporter(self):
        exporter = RecordingExporter()
        with patch("monitoring.metrics.MeterProvider", side_effect=RuntimeError("synthetic SDK failure")), \
                self.assertRaises(RuntimeError):
            MetricPipeline(monitor(), exporter=exporter, sampler=Mock(), export_interval_millis=math.inf)
        self.assertTrue(exporter.closed)

    def test_all_requested_metrics_and_api_histogram_are_created(self):
        observation, pipeline, reader = self.pipeline()
        observation.begin_cycle()
        complete_cycle(observation)
        observation.balance_validated()
        observation.api_result("get_positions", "success", "exchange", 0.25)
        observation.api_result("place_order", "error", "exchange", 0.5)
        pipeline.consume_events()
        pipeline.sample_resources()
        values = metrics(reader.get_metrics_data())
        required = {"bot_main_progress_timestamp_seconds", "bot_cycle_success_timestamp_seconds",
                    "bot_balance_success_timestamp_seconds", "bot_api_requests_total",
                    "bot_api_errors_total", "bot_api_duration_seconds", "bot_process_cpu_seconds_total",
                    "bot_process_resident_memory_bytes", "bot_disk_available_bytes",
                    "bot_process_start_timestamp_seconds"}
        self.assertTrue(required <= values.keys())
        self.assertEqual(sum(point.value for point in values["bot_api_requests_total"].data.data_points), 2)
        self.assertEqual(sum(point.value for point in values["bot_api_errors_total"].data.data_points), 1)
        histogram = values["bot_api_duration_seconds"].data.data_points
        self.assertEqual(sum(point.count for point in histogram), 2)
        self.assertAlmostEqual(sum(point.sum for point in histogram), 0.75)
        self.assertTrue(values["bot_process_cpu_seconds_total"].data.is_monotonic)
        self.assertEqual(values["bot_process_cpu_seconds_total"].data.data_points[0].value, 3)

    def test_collecting_resources_does_not_advance_main_thread_heartbeat(self):
        observation, pipeline, reader = self.pipeline()
        observation.progress()
        before = observation.main_progress
        pipeline.sample_resources()
        reader.get_metrics_data()
        self.assertEqual(observation.main_progress, before)
        self.assertIsNone(observation.cycle_success)

    def test_metrics_never_include_credentials_ids_or_arbitrary_resource_attributes(self):
        observation, pipeline, reader = self.pipeline()
        observation.api_result("get_positions", "success", "exchange", 0.25)
        pipeline.consume_events()
        with patch.dict("os.environ", {"OTEL_RESOURCE_ATTRIBUTES": "secret=" + TEST_TOKEN}):
            payload = reader.get_metrics_data().to_json()
        self.assertNotIn(TEST_TOKEN, payload)
        self.assertNotIn("orderId", payload)
        self.assertNotIn("https://", payload)

    def test_resource_sampler_uses_existing_process_and_disk_only(self):
        process = Mock()
        process.cpu_times.return_value = (1, 2, 50, 100)
        process.memory_info.return_value = SimpleNamespace(rss=100)
        process.create_time.return_value = 200
        with patch("monitoring.metrics.psutil.Process", return_value=process), \
                patch("monitoring.metrics.shutil.disk_usage", return_value=SimpleNamespace(free=300)) as usage:
            sampler = ResourceSampler("synthetic-mount")
            self.assertEqual(sampler.sample(), dict(cpu=3, memory=100, disk=300, start=200))
            usage.assert_called_once_with("synthetic-mount")
            process.memory_info.side_effect = OSError()
            self.assertNotIn("memory", sampler.sample())

    def test_export_failures_are_counted_without_leaking_sdk_logs(self):
        class BrokenExporter(RecordingExporter):
            def export(self, *args, **kwargs):
                logging.getLogger("opentelemetry.synthetic").error(TEST_TOKEN, exc_info=True)
                raise RuntimeError(TEST_TOKEN)
        observation, pipeline, reader = self.pipeline(exporter=BrokenExporter())
        pipeline.sample_resources()
        with self.assertLogs(level="WARNING") as logs:
            result = pipeline.exporter.export(reader.get_metrics_data())
        self.assertEqual(result, MetricExportResult.FAILURE)
        self.assertEqual(observation.export_errors, 1)
        self.assertNotIn(TEST_TOKEN, " ".join(logs.output))

    def test_export_warnings_are_rate_limited_but_failures_are_all_counted(self):
        class FailedExporter(RecordingExporter):
            def export(self, *args, **kwargs):
                return MetricExportResult.FAILURE
        observation, pipeline, reader = self.pipeline(exporter=FailedExporter())
        batch = reader.get_metrics_data()
        with patch("monitoring.metrics.time.monotonic", side_effect=[1000, 1060, 1900]), \
                self.assertLogs("monitoring.metrics", level="WARNING") as logs:
            for _ in range(3):
                pipeline.exporter.export(batch)
        self.assertEqual(observation.export_errors, 3)
        self.assertEqual(len(logs.output), 2)

    def test_stop_prevents_a_shutdown_export_or_false_failure_count(self):
        exporter = RecordingExporter()
        observation, pipeline, reader = self.pipeline(exporter=exporter)
        observation.stop()
        result = pipeline.exporter.export(reader.get_metrics_data())
        self.assertEqual(result, MetricExportResult.FAILURE)
        self.assertEqual(observation.export_errors, 0)
        self.assertEqual(exporter.batches, [])

    def test_blocked_cloud_export_cannot_block_native_api_calls(self):
        entered, release, api_finished = threading.Event(), threading.Event(), threading.Event()

        class BlockedExporter(RecordingExporter):
            def export(self, *args, **kwargs):
                entered.set()
                release.wait(3)
                return MetricExportResult.SUCCESS
        observation, pipeline, reader = self.pipeline(exporter=BlockedExporter())
        pipeline.sample_resources()
        batch = reader.get_metrics_data()
        export_thread = threading.Thread(target=lambda: pipeline.exporter.export(batch))
        export_thread.start()
        self.assertTrue(entered.wait(1))
        native_call = Mock(return_value={"retCode": 0})

        def api_call():
            observe_api_call(observation, "place_order", native_call, qty="3")
            api_finished.set()
        trade_thread = threading.Thread(target=api_call)
        try:
            trade_thread.start()
            self.assertTrue(api_finished.wait(1), "API observer waited for Cloud export")
            native_call.assert_called_once_with(qty="3")
        finally:
            release.set()
            trade_thread.join(timeout=1)
            export_thread.join(timeout=1)

    def test_thread_log_filter_preserves_logging_from_other_threads(self):
        entered, release = threading.Event(), threading.Event()

        class BlockedExporter(RecordingExporter):
            def export(self, *args, **kwargs):
                entered.set()
                release.wait(3)
                return MetricExportResult.SUCCESS
        observation = monitor()
        exporter = SafeExporter(BlockedExporter(), observation)
        thread = threading.Thread(target=lambda: exporter.export(None))
        with self.assertLogs(level="WARNING") as logs:
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                logging.getLogger("opentelemetry.trade-thread").warning("existing log remains visible")
            finally:
                release.set()
                thread.join(timeout=1)
        self.assertIn("existing log remains visible", " ".join(logs.output))

    def test_real_background_scheduler_exports_without_advancing_main_progress(self):
        delivered = threading.Event()
        export_threads = []

        class SignalExporter(RecordingExporter):
            def export(self, batch, **kwargs):
                export_threads.append(threading.get_ident())
                values = metrics(batch)
                if sum(point.value for point in values["bot_api_requests_total"].data.data_points) > 0:
                    delivered.set()
                return MetricExportResult.SUCCESS

        observation = monitor()
        observation.api_result("get_positions", "success", "exchange", 0.1)
        progress = observation.main_progress
        sampler = Mock()
        sampler.sample.return_value = dict(cpu=1, memory=1024, disk=2048, start=1000)

        def pipeline_factory(item):
            return MetricPipeline(item, exporter=SignalExporter(), sampler=sampler,
                                  export_interval_millis=25)

        with patch("monitoring.metrics.MetricPipeline", side_effect=pipeline_factory):
            observation.start()
            try:
                self.assertTrue(delivered.wait(2))
                self.assertEqual(observation.main_progress, progress)
                self.assertTrue(export_threads)
                self.assertTrue(all(identifier != observation.owner_thread for identifier in export_threads))
            finally:
                observation.stop()
                observation._thread.join(timeout=2)
        self.assertFalse(observation._thread.is_alive())
