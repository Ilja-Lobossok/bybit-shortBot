# Stage 4 — implementation review, 31.10.2026

Base `f928be3eb3c68de93e696eda1aaad3389a6e00a8`; отдельная ветка
`codex/trading-performance-stage4`. Изменения подготовлены для PR в main.
Render/Cloud resources не менялись, реальные API Bybit и ордера не использовались.

| Проверка | Результат |
|---|---|
| SQLite schema/migration, WAL/FULL, pages+checkpoints, revisions/dedup | PASS, synthetic |
| Restarts, delayed facts, partial history, overlapping windows, cursor continuation | PASS, synthetic |
| Decimal net, fees/funding, no double subtraction, transfer not profit | PASS, synthetic |
| Full cycles vs fills/orders; foreign/legacy excluded from bot KPI | PASS, synthetic |
| Current snapshots, observed DD, fixed-window KPI with coverage | PASS, synthetic |
| Unknown/incomplete for hedge funding/reversal/special/missing evidence | PASS, conservative exclusion |
| Bounded queues/outbox/responses/DB, SQL busy/full, WAL-aware backup | PASS, offline failure tests |
| Arguments/results/exception identity and original order call count | PASS, mocks/regressions |
| Trading method AST after stripping observation-only calls | PASS against accepted base |
| Risk/position sizing/TP/SL/Telegram/state persistence/Render config unchanged | PASS, source diff + regressions |
| Metrics SDK creation/low-cardinality and protobuf Logs privacy | PASS, offline decoding |
| Projection duplicates/missing row/tombstone/expiry/truncation/business time | PASS, synthetic oracle |
| Actual Cloud LogQL/transforms/metadata/quotas and exchange financial reconciliation | NOT RUN, require separate approval |

Validation:

- Python **3.11.10** (production version): `scripts/check_performance.py`, **290 tests PASS**.
- Python **3.12.10**: same suite, **290 tests PASS**.
- Local SQLite **3.46.0**: migration/WAL/FULL/backup/integrity tested. Actual Render
  linked SQLite version is not reverified here; check before rollout.
- Grafana JSON reproducibility and layout/scope/secret guards: included in suite.
- Terraform **1.17.1**, Grafana provider **4.45.2**: fmt/validate PASS,
  **6 mock-provider plan tests PASS**, no real plan/apply.
- Promtool **3.15.1**: **67 rules/recording queries parse**, existing synthetic
  health alert scenarios PASS. These are PromQL checks, not live LogQL evaluation.
- Stage 1 standalone smoke path remains covered by its original offline tests.
- New runtime dependencies: **none**. Existing pinned OTLP/requests used; SQLite stdlib.

The suite uses an explicit module allowlist. It does not discover legacy
`tests/test_api.py`. New HTTP clients are exercised only with fakes and blocked
outbound sockets; no trading process is started by these checks.

## Material limits before production

1. Cloud presentation acceptance remains a gate: synthetic alias only, full/missing
   generations, empty portfolio, latest correction, scalar variable, JSON/groupBy,
   Loki query/table limits and metadata mapping. Requires separate Cloud write/apply
   approval; offline oracle does not prove a particular Cloud Grafana version.
2. Figures are covered/reconciled samples. No reconstructed pre-collection equity,
   no assumed bot ownership of all subaccount positions, no guessed hedge funding.
   Legacy unknown can reduce bot N, intentionally. Wallet/equity are account values.
3. Polling snapshots ~60s, history ~300s with 2m lag / 24h overlap, tables ~5m.
   Older corrections require separately approved replay. Initial 7d horizon is a
   request, not guaranteed retention. Actual read-only account check remains to do.
4. DB soft cap 200MiB + free reserve 200MiB; finite 20k rows/source analytical load.
   At cap, collection degrades instead of silently deleting facts. Agreed archive
   policy needed for long running use. Current bot.log/backups share the 1GB disk.
5. Logs batch budget is bounded, but actual monthly bytes and total Free quota must
   be checked. No additional server; no guarantee that unrelated stack usage is free.
6. Shared CPU/IO/IP/API quotas still require rollout measurement. Existing restart
   same-candle risk is unchanged, so no trial worker runs or automatic restarts.

Full deployment sequence and environment variable names are in
[monitoring-stage4.md](monitoring-stage4.md). Nothing authorizes a merge,
Terraform apply, secret changes or production deploy automatically.
