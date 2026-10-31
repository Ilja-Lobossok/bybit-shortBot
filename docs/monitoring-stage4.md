# Этап 4: Trading Dashboard — только метрики

Текущий упрощённый scope PR #3: **существующие Bybit credentials + SQLite на
существующем диске + существующий Grafana Cloud OTLP metrics pipeline**.
Новые API keys/tokens, Loki, сервисы, серверы, Alloy и Prometheus не нужны.
Production deploy / Terraform apply не выполнялись.

## Что видно

- Баланс wallet и equity USDT с графиком, account unrealized P&L по Mark.
- Bybit cumulative realized P&L отдельно от net закрытых сверенных циклов.
- Profitable / losing cycles, N/breakeven, win rate, profit factor.
- Число открытых позиций: весь account и доказанное bot подмножество.
- Watchlist, pump candidates, ready signals, blocked/stalling counts, реальные
  signal/skip reasons и возраст последней обработки стратегии.
- Coverage, unknown/incomplete/unattributed, свежесть снимков и ошибки журнала.

Scope selector относится к сверенным циклам/позициям: `bot` или `account`.
Wallet/equity — значения всего счёта, не выдуманный отдельный капитал бота.
Legacy/чужие/неполные циклы не приписываются боту. Transfers не прибыль,
closedPnl уже net: fees/funding повторно не вычитаются. До сбора нет исторической
equity. PF без losses undefined, N=0 даёт unknown для rate/PF/P&L. Dashboard
показывает фиксированные KPI windows all-covered / UTC day / 7d / 30d; time picker
меняет графики observations, не произвольную SQL-выборку. Подробности в
[контракте данных](performance-data-contract.md).

## Запуск без новых Environment settings

Нужны только уже работающие переменные Render:

| Существующие переменные | Использование |
|---|---|
| MONITORING_ENABLED=true | performance наследует действующий флаг |
| BYBIT_API_KEY / BYBIT_API_SECRET / BYBIT_TESTNET | те же credentials и mainnet/testnet, только GET в отдельной session |
| OTEL_EXPORTER_OTLP_ENDPOINT | прежний Grafana OTLP HTTPS endpoint |
| GRAFANA_CLOUD_OTLP_USERNAME / GRAFANA_CLOUD_API_TOKEN | прежние OTLP Instance ID / metrics token |
| MONITORING_ENVIRONMENT | прежнее production; default production |
| DATA_DIR | прежний persistent disk mount, не менять |

Создавать или переносить secrets не надо. Secrets читаются из реального process
env до dotenv; не логируются и не входят в SQLite/metric labels. HTTP reader не
содержит order/cancel/amend/transfer методов и не использует торговую HTTP session.
Новых библиотек нет: SQLite/Decimal stdlib, requests/OTLP уже в requirements.txt.

Optional overrides, **добавлять для обычного запуска не требуется**:

- `PERFORMANCE_ENABLED=false`: выключить performance, сохранив Bot Health.
- `PERFORMANCE_READONLY_API_ENABLED=false`: отключить только GET collection.
- `PERFORMANCE_HISTORY_DAYS=7`: default; initial requested lookback, не гарантия retention.
- `PERFORMANCE_ACCOUNT_ALIAS=primary`: default, постоянный безопасный alias.
- `PERFORMANCE_LOG_EXPORT_ENABLED=false`: default. Logs никак не нужны dashboard.

Optional Loki код сохранён выключенным. Отсутствующий optional logs token и
сломанный exporter не останавливают metrics refresh, SQLite или торговлю.
Никаких действий по настройке Loki для этой версии не требуется.

## SQLite и GET

`DATA_DIR/performance.sqlite3`: migration v1, WAL/FULL, один фоновый writer;
canonical facts/revisions, checkpoints/cursors, баланс/equity snapshots и реальные
strategy observations. State, RiskManager, TP/SL, sizing, retries, confirmation и
Telegram не меняются. Нет обратной связи из аналитики в торговые решения.

Snapshot ~60s; history ~300s, lag 2m, rolling overlap 24h. Только необходимые
GET, ≤1 request/s, finite pages/response body, bounded timeout/backoff. Отдельная
session сохраняет аргументы/поведение торговых вызовов. Quota по UID/IP общая.
Soft cap DB+WAL+SHM 200MiB, reserve free 200MiB, analytical cap 20k facts/source.
На cap — degraded/pause, не молчаливое удаление фактов. Финансовая история конечна
на 1GB; exact private export: `scripts/performance_export.py`. Backup — SQLite API,
не copy одного файла при активном WAL. Существующие state/backups/logs не удаляются.

## Тесты

```powershell
python scripts/check_performance.py
python scripts/check_grafana_iac.py --terraform terraform --promtool promtool
```

Явный offline allowlist, без legacy `tests/test_api.py`. Проверяются synthetic
partials, дубли, corrections/restarts, delayed/missing history, account/bot
attribution, cash flows, DB busy/full, очереди/таймауты, секреты, отсутствие Logs,
shared credentials, торговые args/results/exception/call count и AST baseline.
IaC: JSON reproducibility, PromQL parsing, Terraform validate и mock-provider
plan-only tests. Никаких реальных ордеров, Cloud apply или live API в этих тестах.
Результаты/отдельный read-only preflight: [review](monitoring-stage4-review.md).

## Terraform и порядок одного деплоя

Используется прежний `infra/grafana` и существующий Cloud Metrics datasource
`grafanacloud-prom`. Dashboard `bybit-trading-performance` включён по умолчанию,
alias primary. **Новые tfvars/Logs UID не нужны.** Сохранить существующий локальный
Terraform state и прежние настройки alerts/окружения; stage 3 JSON/rules неизменны.
При имеющемся explicit `performance_dashboard_enabled=false` его нужно убрать
или переключить в true; это операторская настройка, не новый secret.

1. После разрешения merge PR #3, Auto-Deploy должен остаться Off.
2. Используя **прежнюю авторизацию Terraform этапа 3**, выполнить real plan:
   ожидается отдельный dashboard, без новых datasource/receiver/server и без
   изменения Bot Health/notification settings. Затем отдельно разрешённый apply.
3. Read-only проверить свободный disk и state; сохранить свежий backup перед
   согласованным restart. Новую DB заранее создавать не требуется.
4. Один согласованный manual Render deploy reviewed main SHA, без изменения
   Environment и без второго worker. Performance запустится вслед за действующим
   MONITORING_ENABLED=true; logs останутся off.
5. Проверить health/цикл, `bot_performance_*`, freshness, coverage и ошибки GET.
   Первые балансы появятся после snapshot, сверенные циклы — после истории;
   unknown при недостатке подтверждений ожидаем. При analytics error не повторять
   ордер и не рестартовать автоматически. Риск повторной обработки свечи не менялся.

`GRAFANA_CLOUD_API_TOKEN` используется для **отправки метрик**, а Terraform
управляет dashboard через Grafana API и требует прежний `GRAFANA_AUTH`. Это разные
permissions; OTLP token не подставляется как admin credential. Не создавать новый
token: использовать существующий способ авторизации этапа 3. В текущем agent
shell GRAFANA_AUTH не найден (включая user/machine environment), поэтому реальный
Terraform apply здесь нельзя обещать без доступа к этой прежней авторизации.
Исходный JSON можно импортировать через уже открытую Grafana session, если отдельно
разрешат этот способ; сейчас ни import, ни apply не выполнялись.

Explore после согласованного deploy:

```promql
count by (__name__) ({__name__=~"bot_performance_.*",component="performance",environment="production"})
```

Метрики `wins/losses/cycles/net_closed_pnl/win_rate/profit_factor` имеют bounded
labels scope/window/currency; `positions` — scope; `strategy_size` — state из
watching/candidate/ready/blocked/stalling; `decision_count` — конечный reason enum.
Никаких time series на сделку, order ID или symbol. Точные Decimal значения
остаются в SQLite; Cloud gauges — периодическое числовое представление.
