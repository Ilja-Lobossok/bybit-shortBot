"""One-writer SQLite journal. Never opened on the trading thread; no state writes."""
from __future__ import annotations

import json
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import time

DAY = 86_400_000


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class BudgetExceeded(RuntimeError):
    pass


class Store:
    def __init__(self, path, *, max_bytes=200 * 1024**2, reserve_bytes=200 * 1024**2):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.reserve_bytes = reserve_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("Journal symlinks are not supported")
        # Restrict a newly created file before opening SQLite (never chmod state).
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        self.db = sqlite3.connect(self.path, timeout=0.1)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=100")
        self.db.execute("PRAGMA wal_autocheckpoint=256")
        self.db.execute("PRAGMA journal_size_limit=33554432")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            sql = (Path(__file__).parent / "migrations/001_initial.sql").read_text()
            self.db.executescript("BEGIN IMMEDIATE;\n" + sql +
                                  "\nINSERT INTO schema_migrations VALUES(1," + str(int(time.time() * 1000)) + ");"
                                  "\nPRAGMA user_version=1;\nCOMMIT;")
        elif version != 1:
            self.close()
            raise ValueError("Unsupported performance schema")

    def close(self):
        self.db.close()

    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        self.db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, str(value)))

    def size(self):
        return sum(p.stat().st_size for p in (self.path, Path(str(self.path) + "-wal"),
                                             Path(str(self.path) + "-shm")) if p.exists())

    def check_budget(self):
        if self.size() >= self.max_bytes or shutil.disk_usage(self.path.parent).free < self.reserve_bytes:
            raise BudgetExceeded("Performance storage budget reached")

    def bind_account(self, fingerprint):
        existing = self.meta("account_fingerprint")
        if existing and existing != fingerprint:
            raise ValueError("Read-only key changed; use a separately reviewed new journal")
        with self.db:
            self.set_meta("account_fingerprint", fingerprint)

    def ingest(self, kind, rows, now):
        """Caller owns transaction. Revision history retained; identical replays are no-ops."""
        from performance.contracts import sanitize_fact, identity, timestamp
        for raw in rows:
            value = sanitize_fact(kind, raw)
            ident = identity(kind, value)
            payload = encoded(value)
            previous = self.db.execute("SELECT revision,payload FROM facts WHERE kind=? AND id=?",
                                       (kind, ident)).fetchone()
            if previous and previous["payload"] == payload:
                continue
            # A stale delayed page must never overwrite a newer exchange revision.
            if previous:
                old = json.loads(previous["payload"])
                if int(value.get("updatedTime", 0) or 0) < int(old.get("updatedTime", 0) or 0):
                    continue
            revision = (self.db.execute("SELECT coalesce(max(revision),0) FROM revisions WHERE kind=? AND id=?", (kind, ident)).fetchone()[0] + 1)
            self.db.execute("INSERT OR REPLACE INTO facts VALUES(?,?,?,?,?,?,?)",
                            (kind, ident, value.get("symbol", ""), timestamp(kind, value), now, revision, payload))
            self.db.execute("INSERT INTO revisions VALUES(?,?,?,?,?)", (kind, ident, revision, now, payload))

    def page(self, kind, start, end, rows, now, *, final, cursor=""):
        self.check_budget()
        with self.db:
            self.ingest(kind, rows, now)
            self.db.execute("INSERT INTO sync_windows VALUES(?,?,?,?,?) ON CONFLICT(kind,start_ms,end_ms) "
                            "DO UPDATE SET status=excluded.status,completed_ms=excluded.completed_ms",
                            (kind, start, end, "complete" if final else "incomplete", now if final else None))
            if final:
                self.set_meta("watermark:" + kind, max(end, int(self.meta("watermark:" + kind, 0))))
                self.db.execute("DELETE FROM sync_cursors WHERE kind=?", (kind,))
            elif cursor:
                self.db.execute("INSERT OR REPLACE INTO sync_cursors VALUES(?,?,?,?)", (kind, start, end, cursor))

    def facts(self, kind):
        rows = self.db.execute("SELECT payload FROM facts WHERE kind=? ORDER BY event_ms,id LIMIT 20001", (kind,)).fetchall()
        if len(rows) > 20000:
            raise BudgetExceeded("Analytical row budget reached; archive/review required")
        return [json.loads(r[0]) for r in rows]

    def covered(self, kind, start, end):
        cursor = start
        for row in self.db.execute("SELECT start_ms,end_ms FROM sync_windows WHERE kind=? "
                                   "AND status='complete' AND end_ms>=? AND start_ms<=? ORDER BY start_ms",
                                   (kind, start, end)):
            if row[0] > cursor:
                return False
            cursor = max(cursor, row[1] + 1)
        return cursor > end

    def replace_positions(self, rows, now):
        from performance.contracts import sanitize_fact, decimal
        self.check_budget()
        values = [sanitize_fact("positions", row) for row in rows]
        keys = [(v["symbol"], int(v["positionIdx"])) for v in values]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate position identity")
        with self.db:
            self.db.execute("DELETE FROM positions_current")
            for value in values:
                if decimal(value["size"]) > 0:
                    key = value["symbol"] + ":" + str(value["positionIdx"])
                    self.db.execute("INSERT INTO positions_current VALUES(?,?,?)", (key, now, encoded(value)))
            self.set_meta("positions_success_ms", now)

    def snapshot(self, response, now):
        from performance.contracts import wallet_snapshot
        value = wallet_snapshot(response)
        self.check_budget()
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO account_snapshots VALUES(?,?,?)",
                            (now, int(response["time"]), encoded(value)))
            self.set_meta("balance_success_ms", now)

    def latest_snapshot(self):
        row = self.db.execute("SELECT received_ms,payload FROM account_snapshots ORDER BY received_ms DESC LIMIT 1").fetchone()
        return (row[0], json.loads(row[1])) if row else (None, {})

    def observe(self, event):
        """Only already-filtered observations, never HTTP dumps or risk history as money."""
        self.check_budget()
        with self.db:
            if not self.db.execute("INSERT OR IGNORE INTO observations VALUES(?,?)",
                                   (event["id"], event["time_ms"])).rowcount:
                return
            body = event["body"]
            if event["kind"] in ("entry", "bootstrap"):
                entries = [body] if event["kind"] == "entry" else body.get("entries", [])
                for entry in entries:
                    if entry.get("order_id"):
                        self.db.execute("INSERT INTO ownership VALUES(?,?,?,?,?) ON CONFLICT(order_id) "
                                        "DO UPDATE SET flat_before=max(flat_before,excluded.flat_before)",
                                        (entry["order_id"], entry["symbol"], int(entry.get("flat_before", False)),
                                         event["time_ms"], event["kind"]))
            if event["kind"] == "strategy_snapshot":
                previous = {r["entity_key"]: json.loads(r["payload"]) for r in self.db.execute("SELECT * FROM strategy_current")}
                self.db.execute("DELETE FROM strategy_current")
                for item in body.get("rows", []):
                    item = dict(previous.get(item["symbol"], {}), **item)
                    item["scheduled_bar"] = body.get("scheduled_bar", 0)
                    self.db.execute("INSERT OR REPLACE INTO strategy_current VALUES(?,?)",
                                    (item["symbol"], encoded(item)))
                self.set_meta("strategy_success_ms", event["time_ms"])
                self.set_meta("strategy_counts", encoded(body.get("counts", {})))
            self.db.execute("INSERT INTO strategy_events VALUES(?,?,?,?,?,?)",
                            (event["id"], event["time_ms"], event["kind"], body.get("symbol", ""),
                             body.get("reason", "none"), encoded(body)))
            if event["kind"] == "decision":
                key = "decision:" + body["reason"]
                self.set_meta(key, int(self.meta(key, 0)) + 1)
                if body["reason"] not in ("not_usdt", "invalid_market", "turnover", "pump", "near_high"):
                    row = self.db.execute("SELECT payload FROM strategy_current WHERE entity_key=?", (body["symbol"],)).fetchone()
                    value = json.loads(row[0]) if row else {}
                    value.update(body, decision_ms=event["time_ms"])
                    self.db.execute("INSERT OR REPLACE INTO strategy_current VALUES(?,?)", (body["symbol"], encoded(value)))

    def maintenance(self, now):
        # Only observation detail and export scratch expire. Financial facts and
        # equity samples are never silently deleted; cap pauses collection instead.
        with self.db:
            self.db.execute("DELETE FROM strategy_events WHERE event_ms<?", (now - 7 * DAY,))
            self.db.execute("DELETE FROM observations WHERE event_ms<?", (now - 7 * DAY,))
            self.db.execute("DELETE FROM sync_windows WHERE status='incomplete' AND end_ms<?", (now - 14 * DAY,))
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def backup(self, target):
        target = Path(target)
        if target.exists() or target.resolve() == self.path.resolve():
            raise ValueError("Backup target must be a new file")
        if shutil.disk_usage(target.parent).free < self.size() + self.reserve_bytes:
            raise BudgetExceeded("Insufficient backup space")
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(target)) as destination:
            self.db.backup(destination, pages=128, sleep=0.01)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup verification failed")
