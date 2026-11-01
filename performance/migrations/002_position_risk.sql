CREATE TABLE position_runs(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 entity_key TEXT NOT NULL, symbol TEXT NOT NULL, direction TEXT NOT NULL,
 source_created_ms INTEGER, first_ms INTEGER NOT NULL, last_ms INTEGER NOT NULL,
 closed_ms INTEGER, min_pnl TEXT, max_pnl TEXT, last_pnl TEXT,
 samples INTEGER NOT NULL DEFAULT 0, gaps INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX position_open_run ON position_runs(entity_key) WHERE closed_ms IS NULL;
CREATE INDEX position_recent_runs ON position_runs(closed_ms);
CREATE TABLE position_samples(
 run_id INTEGER NOT NULL, bucket_ms INTEGER NOT NULL, observed_ms INTEGER NOT NULL,
 unrealized TEXT NOT NULL, realized TEXT, combined TEXT,
 PRIMARY KEY(run_id,bucket_ms));
CREATE INDEX position_sample_time ON position_samples(bucket_ms);
