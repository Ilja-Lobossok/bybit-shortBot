CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_ms INTEGER NOT NULL);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE facts(
 kind TEXT NOT NULL, id TEXT NOT NULL, symbol TEXT NOT NULL,
 event_ms INTEGER NOT NULL, received_ms INTEGER NOT NULL,
 revision INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(kind,id));
CREATE INDEX facts_time ON facts(kind,event_ms);
CREATE TABLE revisions(
 kind TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
 received_ms INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(kind,id,revision));
CREATE TABLE sync_windows(
 kind TEXT NOT NULL, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
 status TEXT NOT NULL, completed_ms INTEGER, PRIMARY KEY(kind,start_ms,end_ms));
CREATE TABLE sync_cursors(kind TEXT PRIMARY KEY, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, cursor TEXT NOT NULL);
CREATE TABLE account_snapshots(
 received_ms INTEGER PRIMARY KEY, exchange_ms INTEGER NOT NULL,
 payload TEXT NOT NULL);
CREATE TABLE positions_current(
 entity_key TEXT PRIMARY KEY, received_ms INTEGER NOT NULL, payload TEXT NOT NULL);
CREATE TABLE ownership(
 order_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, flat_before INTEGER NOT NULL,
 observed_ms INTEGER NOT NULL, source TEXT NOT NULL);
CREATE TABLE strategy_events(
 id TEXT PRIMARY KEY, event_ms INTEGER NOT NULL, kind TEXT NOT NULL,
 symbol TEXT NOT NULL, reason TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX strategy_time ON strategy_events(event_ms);
CREATE TABLE strategy_current(entity_key TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE observations(id TEXT PRIMARY KEY, event_ms INTEGER NOT NULL);
CREATE TABLE outbox(
 generation INTEGER PRIMARY KEY, created_ms INTEGER NOT NULL, payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0);
