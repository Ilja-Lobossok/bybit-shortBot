# Open positions and the Trading Dashboard

This release changes analytical monitoring only. The trading modules, order
execution/retries, TP/SL, position sizes, risk limits, Telegram and `state.json`
are unchanged. No new credentials, dependencies, servers or Loki are required.
The existing position GET supplies all new open-position statistics.

## What the headline means

For the current set of linear USDT positions:

`open_combined_pnl = sum(unrealisedPnl + curRealisedPnl)`

The two components are also shown separately. `curRealisedPnl` is the realized
part reported for the current holding position, which can include fees, funding
and partial-close results. It is **not** funding alone. Never subtract the fee or
funding panels again from this sum or from reconciled closed-cycle P&L.

This is an explicitly USDT-denominated current-position measure, not the whole
account's lifetime profit, cash deposits, future closing proceeds, or the exact
USD-denominated All Futures TP/SL tracker. That tracker can retain positions
closed since its activation and can cover currencies outside this collector.
The current-set total changes composition when a position opens/closes.

The main screen always covers the whole subaccount. Bot-attributed closed
results remain a separately labeled subset: missing ownership evidence never
makes an old position a foreign trade. Missing `curRealisedPnl` makes the combined
measure unavailable, never a fabricated zero cost. A successful empty snapshot
does produce real zeros. A failed GET keeps previous timestamps; the headline
is hidden after 150 seconds rather than pretending a stale portfolio is current.

## Metrics (prefix `bot_performance_`)

| Names | Meaning / scope |
| --- | --- |
| `open_unrealized_pnl`, `open_realized_pnl`, `open_combined_pnl` | Current-position components and sum; scope account / proven bot subset, USDT |
| `open_money_complete` | 1 only when all selected current rows contain the required money fields |
| `open_positive`, `open_negative`, `open_breakeven` | Counts using combined current-position P&L |
| `open_average_loss`, `open_worst_loss`, `open_best_pnl` | Average among losing positions, worst loss (0 if none), best result |
| `position_worst_observed_loss` | Worst observed combined P&L among currently open observed runs, clamped at 0 |
| `position_average_observed_loss` | Average observed worst loss across current runs, including 0 for never-negative runs |
| `position_risk_first_timestamp_seconds` | Earliest first observation of the currently open runs |
| `position_graph_truncated` | Active positions beyond the 200-curve presentation cap; aggregate cards still cover all positions |
| `position_observation_gaps` | Gaps over 150s or incomplete money observations in current runs |
| `position_combined_pnl` | Per-symbol/direction combined current P&L |
| `position_active` | 1 while open; temporary 0 tombstone after a close/reversal |
| `position_observation_timestamp_seconds` | Actual last successful position snapshot, not exporter heartbeat |
| `position_run_start_timestamp_seconds` | First observation of the current locally tracked run |
| `cost_breakdown_complete` | Covered journal period and complete transaction classification |
| `realized_price_change` | Covered linear-USDT cash-flow component; `change - funding + fee` |
| `closed_statistics_status` | 0 incomplete coverage; 1 no verified closures; 2 verified sample. Not proof of complete attribution |

New numeric instruments are gauges. Only the four per-position instruments use
`symbol` and `direction` (long/short). All other labels remain bounded aggregates.
No order ID, execution ID, local run ID, timestamp, price or error text is a label.
At 81 positions, approximately 324 per-position active series are added; the
200-curve cap bounds these to 800 plus short-lived tombstones (at most 200).
Symbol churn affects Cloud active-series usage and must be checked during release;
Cloud retention/quota depends on the existing account's actual plan.

The performance exporter uses a bounded 1 MiB request ceiling. The original
64 KiB smoke/health ceiling remains unchanged: a full per-position payload can
exceed it, and the SDK drops such a batch before HTTP rather than splitting it
automatically. Real SDK/HTTP-boundary regression tests cover 82 positions and
the 200-curve cap plus 200 tombstones with maximum-size UTF-8 labels. The 5s
timeout and periodic export cadence are unchanged; no additional queue/retries
or additional per-batch requests are introduced.

## Observations and SQLite v2

Migration `002_position_risk.sql` is transactional and additive. It does not
rewrite existing financial facts, ownership, equity, checkpoints or trading state.
It creates locally identified observed runs and five-minute position samples.
No past observations are synthesized during migration. Every complete existing
GET snapshot updates extrema using Decimal arithmetic, even though stored chart
detail is downsampled to the last observation in each five-minute bucket.

Run minima/maxima and sample counts survive restart. An observed flat interval,
direction change or changed exchange `createdTime` starts a new run. Delayed or
duplicate snapshot timestamps cannot rewind current state. A failed/partial GET
cannot close a run. A close/reopen entirely between snapshots with no observable
identity change is unknowable; this is **observed position history**, not a
tick-complete or guaranteed trade-lifecycle reconstruction. Intraminute extrema
and pre-activation worst losses must not be claimed.

Five-minute samples retain at most 7 days **and** 100,000 rows (whichever limit
is reached first, pruned hourly). At 81 persistent positions the row cap is about
4.3 days. Extrema summaries and exchange facts are not expired by that pruning.
The existing 200 MiB SQLite/WAL/SHM budget and 200 MiB free-space reserve remain.
The minute-resolution graph history lives in Grafana Cloud; local samples are
not automatically replayed into old OTLP timestamps after an export outage.

The graph hides its legend and exposes symbol/direction in a single-line tooltip.
Choose symbols or All / Worst 10 (lowest P&L per graph timestamp). Closed curves
stop after their inactive export. New observed runs are excluded for the first
60 seconds to provide a visible lifecycle gap; summary cards update immediately.
Freshness also gates individual curves. A monitoring outage is a gap, not a zero.
The historical graph can show formerly open positions up to their close time.

Fees/funding and closed KPIs are in collapsed sections with their own coverage
and scope/window selectors. Coverage start/end is explicit; incomplete history
is not shown as zero. The top-level financial cards do not follow the closed-KPI
scope/window selectors. The equity chart keeps Soft min=400 / Soft max=500;
the dashboard remains editable.

## Offline verification and release

Run `python scripts/check_performance.py`, then the existing
`scripts/check_grafana_iac.py` with the installed Terraform/promtool paths.
Tests forbid network, cover costs/partial closes/missing values, empty accounts,
restarts/replays, lifecycle gaps, migration from v1, limits and observation pruning.
The explicit suite does not discover the live `tests/test_api.py`.

Before one approved manual Render deploy: verify Auto-Deploy Off and main SHA;
back up `state.json` and use SQLite's online backup API for the live journal;
verify both backups and free space. Terraform must update only the Trading
Dashboard, with no creates/deletes and no Bot Health/folder/alert changes. Load
the existing encrypted local Grafana credential through Import-Clixml into the
process environment only, and remove GRAFANA_AUTH in finally.

After deploy verify v2 integrity, progressing snapshots, unchanged state
validity, aggregate money against the same-position GET snapshot, per-position
series count, chart freshness/gaps and zero telemetry/storage errors. Never use
a real order or a second restart as a monitoring test.

An old v1-only monitoring binary cannot open v2; the schema is deliberately not
silently downgraded. A rollback of monitoring needs a compatible binary or a
separately reviewed restore of its pre-release SQLite backup. Trading continues
if the analytics worker is disabled. Do not roll back or restore trading state
automatically, since the exchange may already have progressed.
