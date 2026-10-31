"""Default-off background writer/reader/export. Failures never escape into trading."""
import json
import logging
from pathlib import Path
import re
import threading
import time

from performance.observations import DISABLED, Mailbox, REASONS

logger = logging.getLogger(__name__)


class Runtime(Mailbox):
    def __init__(self, env, disk_path):
        super().__init__()
        self.env, self.disk_path = env, Path(disk_path)
        self.started = time.time()
        self.errors = self.degraded = self.export_errors = 0
        self.reasons = sorted(REASONS)
        self.cache = ()
        self._stop = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run, name="bybit-performance", daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()  # No flush/join on trading path.

    def tick(self, store, collector, logs, now, *, history=False):
        from performance.storage import BudgetExceeded, encoded
        from performance.reconciliation import reconcile, summaries
        from performance.metrics import points
        from performance.projections import generation
        self.degraded = 0
        for event in self.take():
            try:
                store.observe(event)
            except Exception:
                self.dropped += 1
                self.errors += 1
        if collector:
            for operation in (collector.snapshots, collector.history if history else None):
                if operation:
                    try:
                        store.check_budget()
                        operation(now)
                    except Exception:
                        self.errors += 1
                        self.degraded = 1
        view = reconcile(store, now)
        summary = summaries(store, view, now)
        has_pending = store.db.execute("SELECT 1 FROM outbox LIMIT 1").fetchone() is not None
        if logs and (has_pending or now >= int(store.meta("next_projection_ms", 0))):
            # One durable pending generation. Retry at most once per tick, and
            # supersede after 15m (explicit gap), never accumulate an infinite queue.
            pending = store.db.execute("SELECT * FROM outbox LIMIT 1").fetchone()
            if pending and now - pending["created_ms"] > 900_000:
                with store.db:
                    store.db.execute("DELETE FROM outbox")
                self.errors += 1
                pending = None
            if pending:
                batch = json.loads(pending["payload"])
            else:
                batch = generation(store, view, now)
                store.check_budget()
                with store.db:
                    store.set_meta("last_generation", batch["generation"])
                    store.db.execute("INSERT INTO outbox(generation,created_ms,payload) VALUES(?,?,?)", (batch["generation"], now, encoded(batch)))
            attempts = pending["attempts"] if pending else 0
            # <=3 attempts per generation => <=3000 trade lines even if a server
            # repeats rather than deduplicates failed/partial batches (<5000 limit).
            delivered = False
            if attempts < 3:
                with store.db:
                    store.db.execute("UPDATE outbox SET attempts=attempts+1 WHERE generation=?", (batch["generation"],))
                delivered = logs.send(batch)
            if delivered:
                # Ack only a full non-partial OTLP response; leave pre-crash replay idempotent.
                ack = {key: batch[key] for key in ("generation", "created_ms", "expected", "truncated")}
                with store.db:
                    store.set_meta("projection_ack", encoded(ack))
                    store.set_meta("next_projection_ms", now + 300_000)
                    store.db.execute("DELETE FROM outbox WHERE generation=?", (batch["generation"],))
            else:
                self.errors += 1
        self.cache = points(store, view, summary, self, now)

    def run(self):
        from performance.storage import Store
        store = reader = logs = metrics = None
        try:
            store = Store(self.disk_path / "performance.sqlite3")
            collector = None
            if self.env.get("PERFORMANCE_READONLY_API_ENABLED", "false").lower() == "true":
                from performance.bybit_reader import Reader, Collector
                reader = Reader(self.env)
                store.bind_account(reader.fingerprint)
                days = int(self.env.get("PERFORMANCE_HISTORY_DAYS", 7))
                if not 1 <= days <= 730:
                    raise ValueError("Invalid history days")
                collector = Collector(reader, store, lookback_days=days)
            try:
                from performance.metrics import MetricPipeline
                metrics = MetricPipeline(self, self.env)
            except Exception:
                self.errors += 1
                logger.warning("Performance metrics unavailable; details suppressed.")
            if self.env.get("PERFORMANCE_LOG_EXPORT_ENABLED", "false").lower() == "true":
                try:
                    from performance.exporters import LogExporter
                    logs = LogExporter(self.env)
                except Exception:
                    self.errors += 1
                    logger.warning("Performance logs unavailable; details suppressed.")
            next_collect = next_history = next_maintenance = 0
            while not self._stop.wait(0.25):
                now = int(time.time() * 1000)
                try:
                    # Drain actual decision copies promptly, independent of collector cadence.
                    for event in self.take():
                        try:
                            store.observe(event)
                        except Exception:
                            self.dropped += 1
                            self.errors += 1
                    if now >= next_collect:
                        self.tick(store, collector, logs, now, history=now >= next_history)
                        next_collect = now + 60_000
                        if now >= next_history:
                            next_history = now + 300_000
                    if now >= next_maintenance:
                        store.maintenance(now)
                        next_maintenance = now + 3_600_000
                except Exception:
                    self.errors += 1
                    self.degraded = 1
                    # Keep the last good financial cache with stale timestamps.
                    # Update operational state even if SQLite is full or corrupt.
                    old = [p for p in self.cache if p[0] not in ("storage_degraded", "errors_total", "dropped_events_total")]
                    self.cache = tuple(old + [("storage_degraded", 1, {}), ("errors_total", self.errors + self.export_errors, {}),
                                              ("dropped_events_total", self.dropped, {})])
                    next_collect = now + 60_000
                    self._stop.wait(1)
        except Exception:
            logger.warning("Performance worker disabled; trading continues. Details suppressed.")
        finally:
            self.enabled = False
            for resource in (metrics, logs, reader, store):
                if resource:
                    try:
                        resource.close()
                    except Exception:
                        pass


def start_performance(env, disk_path):
    if env.get("PERFORMANCE_ENABLED", "false").lower() != "true":
        return DISABLED
    try:
        for key, default in (("MONITORING_ENVIRONMENT", "production"), ("PERFORMANCE_ACCOUNT_ALIAS", "primary")):
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", env.get(key, default)):
                raise ValueError("Invalid performance label")
        runtime = Runtime(env, disk_path)
        runtime.start()
        return runtime
    except Exception:
        logger.warning("Performance could not start; trading continues. Details suppressed.")
        return DISABLED
