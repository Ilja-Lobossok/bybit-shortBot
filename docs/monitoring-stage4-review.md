# Stage 4 — упрощённый metrics-only review, 31.10.2026

PR #3 обновлён по новому scope: тот же Bybit key, тот же OTLP token, SQLite и
Trading Dashboard на PromQL. Новые secrets/servers/services/Loki не требуются.
Merge/apply/deploy не выполнялись. Торговые условия/вызовы не менялись; копирование
strategy snapshot перенесено после entry attempts, чтобы watchlist отражал уже
сделанные штатные удаления. Ready означает сигналы, реально полученные в этом
проходе, не обещание будущего ордера. AST/regression tests остаются обязательными.

## Изменения

- `observations.py`: inherit MONITORING_ENABLED с explicit override; захват прежних
  BYBIT_* из process env; точные aggregate counts strategy до detailed row cap.
- `bybit_reader.py`: те же BYBIT_* и testnet; separate GET-only session; bounded
  budget/backoff, 403 collector backoff 10m; без изменения торговых retries.
- `runtime.py`: GET default on при enabled performance; Logs default off;
  optional log exception не отменяет свежий financial cache.
- `metrics.py`/storage: wins/losses и strategy_size, persisted aggregate counts;
  отсутствие position snapshot не публикуется как zero positions.
- Terraform/JSON: только существующий metrics datasource, dashboard default on,
  удалены Loki datasource lookup, обязательный UID и LogQL/generation panels.
- SQLite ledger/dedup/reconciliation, cash-flow/fee rules и attribution сохранены.

Offline validation: **299 tests PASS** на Python 3.11.10 и 3.12.10; Terraform
1.17.1 / Grafana provider 4.45.2: fmt/validate и **6 mock plan tests PASS**;
promtool 3.15.1: **71 rules/recording queries parse** и прежние synthetic alert
scenarios PASS. JSON build reproducibility PASS. Основные coverage cases: existing env без новых flags, Logs
отсутствует/ломается, metrics продолжаются; shared credentials не выходят в payload;
profit/win/loss/signal gauges; partial/delayed/repeated/restarted/incomplete history;
старая торговля/health/smoke tests; Terraform metrics-only mock plan и PromQL.

## Read-only production preflight (отдельно от offline tests)

Через существующий Render Shell без записи файлов/Environment/DB/деплоя:
Python 3.11.10, SQLite 3.40.1; runtime commit соответствует принятому f928be3;
mount прежний, state валиден, свободно ~953MiB, performance DB ещё нет.
Существующие BYBIT и OTLP variables присутствуют; значения не выводились.
MONITORING_ENABLED=true; PERFORMANCE_* overrides отсутствуют — наследование
позволяет один deploy без новых Environment settings. Проверка GET permissions
прежним ключом успешна для всех 6 endpoints: wallet, positions, executions,
orders, closed PnL, transaction log. Запросы GET-only, limit=1 для списков,
не более 1/s; в выводе только boolean success, не balances/IDs/secrets.
Это проверка доступа, не сверка реальных финансовых сумм. Auto-Deploy **Off**,
service остаётся Live на прежнем commit; настройки не менялись.

Terraform auth: в текущем agent shell, user и machine env нет GRAFANA_AUTH.
Metrics token не заменяет Grafana dashboard API credential. Для apply нужен
доступ к **прежней** авторизации этапа 3, новый token не создаётся. Поэтому все
три действия «merge + apply + deploy» без какого-либо участия пользователя сейчас
подтвердить нельзя; code/runtime не требуют новых secrets. Самостоятельный deploy
запрещён и не выполнялся.

Материальные ограничения сохранены: verified/covered sample вместо выдуманной
lifetime истории, legacy attribution unknown, hedge funding/reversal incomplete,
24h overlap не гарантирует старые corrections, конечный disk budget, shared API/CPU
quota и прежний same-candle restart risk. Дашборд не является бухгалтерским SQL UI.
