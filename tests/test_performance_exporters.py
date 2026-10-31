import json
from types import SimpleNamespace
from unittest.mock import Mock

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest, ExportLogsServiceResponse
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from performance.exporters import LogExporter
from performance.metrics import MetricPipeline, NAMES, points
from performance.projections import generation, validate_projection
from performance.reconciliation import reconcile, summaries
from performance.runtime import Runtime
from tests.performance_fixtures import Offline, seed, position, wallet
from tests.test_performance_reader import Response
from tests.test_monitoring_otel import TEST_ENV, TEST_TOKEN, RecordingExporter, metrics


class ExportTests(Offline):
    def batch(self):
        seed(self.store)
        self.store.replace_positions([position()], 5000)
        return generation(self.store, reconcile(self.store, 5000), 5000)

    def test_otlp_body_labels_time_and_no_credentials(self):
        batch = self.batch()
        session = Mock()
        session.post.return_value = Response(b"")
        exporter = LogExporter(dict(TEST_ENV, PERFORMANCE_GRAFANA_LOGS_TOKEN=TEST_TOKEN), session=session)
        self.assertTrue(exporter.send(batch))
        options = session.post.call_args.kwargs
        body = ExportLogsServiceRequest.FromString(options["data"])
        labels = {x.key for x in body.resource_logs[0].resource.attributes}
        self.assertEqual(labels, {"service.name", "environment", "account_alias"})
        self.assertTrue(session.post.call_args.args[0].endswith("/otlp/v1/logs"))
        self.assertNotIn(TEST_TOKEN.encode(), options["data"])
        self.assertNotIn(b"Authorization", options["data"])
        for row in body.resource_logs[0].scope_logs[0].log_records:
            self.assertEqual(row.time_unix_nano, 5_000_000_000)
            json.loads(row.body.string_value)
        self.assertEqual(options["timeout"], (2, 5))
        self.assertFalse(options["allow_redirects"])

    def test_partial_rejection_http_failure_and_credentials_unavailable(self):
        batch = self.batch()
        partial = ExportLogsServiceResponse()
        partial.partial_success.rejected_log_records = 1
        session = Mock()
        exporter = LogExporter(dict(TEST_ENV, PERFORMANCE_GRAFANA_LOGS_TOKEN=TEST_TOKEN), session=session)
        for response in (Response(partial.SerializeToString()), Response(b"", status=429), Response(b"bad")):
            session.post.return_value = response
            self.assertFalse(exporter.send(batch))
        session.post.side_effect = RuntimeError(TEST_TOKEN)
        self.assertFalse(exporter.send(batch))
        with self.assertRaises(ValueError) as error:
            LogExporter(TEST_ENV, session=session)  # metrics token is never silently used for logs
        self.assertNotIn(TEST_TOKEN, str(error.exception))

    def test_projection_retries_dedup_tombstones_and_business_time(self):
        batch = self.batch()
        manifest = batch["rows"][-1]
        status, rows = validate_projection(batch["rows"] * 3, manifest, kind="trade", generation_id=batch["generation"], now=6000)
        self.assertEqual((status, len(rows)), ("complete", 1))
        self.assertEqual(rows[0]["closed_at_ms"], 4000)
        self.assertEqual(rows[0]["published_at_ms"], 5000)
        # Complete empty generation is the tombstone for the former portfolio.
        self.store.replace_positions([], 6000)
        newer = generation(self.store, reconcile(self.store, 6000), 6000)
        status, rows = validate_projection(batch["rows"] + newer["rows"], newer["rows"][-1], kind="position",
                                            generation_id=newer["generation"], now=6000)
        self.assertEqual((status, rows), ("complete", []))

    def test_projection_missing_row_expiry_and_wrong_generation_are_incomplete(self):
        batch = self.batch()
        manifest = batch["rows"][-1]
        for logs, generation_id, now in (([], batch["generation"], 6000),
                                          (batch["rows"], 99999, 6000),
                                          (batch["rows"], batch["generation"], 1_000_000)):
            self.assertEqual(validate_projection(logs, manifest, kind="position", generation_id=generation_id, now=now)[0], "incomplete")

    def test_row_limit_not_silently_complete(self):
        seed(self.store)
        view = reconcile(self.store, 5000)
        view["positions"] = [dict(position(), entity_key=str(i)) for i in range(201)]
        batch = generation(self.store, view, 5000)
        self.assertEqual(batch["expected"]["position"], 200)
        self.assertEqual(validate_projection(batch["rows"], batch["rows"][-1], kind="position", generation_id=batch["generation"], now=5000)[0], "incomplete")

    def test_durable_outbox_retry_budget_and_restart(self):
        seed(self.store)
        runtime = Runtime({}, self.path.parent)
        exporter = Mock()
        exporter.send.return_value = False
        for time in (5000, 65000, 125000, 185000):
            runtime.tick(self.store, None, exporter, time)
        self.assertEqual(exporter.send.call_count, 3)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 1)
        self.assertIsNone(self.store.meta("projection_ack"))
        # Reboot cannot reset an exhausted generation's durable attempt budget.
        other = Runtime({}, self.path.parent)
        other.tick(self.store, None, exporter, 245000)
        self.assertEqual(exporter.send.call_count, 3)
        exporter.send.return_value = True
        other.tick(self.store, None, exporter, 1_000_000)  # expire old projection; publish a fresh complete one
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)
        self.assertIsNotNone(self.store.meta("projection_ack"))

    def test_creates_all_metrics_without_high_cardinality_labels(self):
        batch = self.batch()
        self.store.snapshot(wallet(), 5000)
        runtime = Runtime(TEST_ENV, self.path.parent)
        view = reconcile(self.store, 5000)
        runtime.cache = points(self.store, view, summaries(self.store, view, 5000), runtime, 5000)
        # Include each optional observable name with synthetic values for instrument validation.
        runtime.cache += tuple((name, 1, {}) for name in NAMES if name not in {p[0] for p in runtime.cache})
        reader, exporter = InMemoryMetricReader(), RecordingExporter()
        pipeline = MetricPipeline(runtime, TEST_ENV, exporter=exporter, extra_readers=[reader])
        try:
            names = metrics(reader.get_metrics_data())
            self.assertEqual(set(names), {"bot_performance_" + name for name in NAMES})
            for metric in names.values():
                for point in metric.data.data_points:
                    self.assertFalse({"symbol", "order_id", "exec_id", "generation", "entity_key"} & set(point.attributes))
                    self.assertNotIn(TEST_TOKEN, str(point.attributes))
        finally:
            pipeline.close()

    def test_observation_and_cloud_failure_cannot_issue_an_order(self):
        seed(self.store)
        runtime = Runtime({}, self.path.parent)
        logs = Mock()
        logs.send.return_value = False
        reader = Mock()
        reader.snapshots.side_effect = RuntimeError("synthetic failure")
        runtime.tick(self.store, reader, logs, 5000, history=True)
        self.assertEqual(runtime.degraded, 1)
        reader.place_order.assert_not_called()
        reader.cancel_order.assert_not_called()

    def test_successful_projection_cadence_is_five_minutes(self):
        seed(self.store)
        runtime, logs = Runtime({}, self.path.parent), Mock()
        logs.send.return_value = True
        for stamp in (5000, 65000, 125000, 185000, 245000):
            runtime.tick(self.store, None, logs, stamp)
        self.assertEqual(logs.send.call_count, 1)
        runtime.tick(self.store, None, logs, 305000)
        self.assertEqual(logs.send.call_count, 2)
