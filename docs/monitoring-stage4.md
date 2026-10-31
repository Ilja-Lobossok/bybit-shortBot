# Этап 4: внедрение Trading Performance

Код подготовлен для review. Production Render, Cloud resources и данные Bybit
не изменялись. Все три runtime-флага false по умолчанию. Существующий smoke-test
`python -m monitoring.otel_test` сохранён. Новых зависимостей нет: sqlite3/Decimal
в stdlib, requests/OTLP protobuf+SDK уже закреплены в requirements stages 1–2.
Поддерживаемый production runtime Python 3.11.10, тот же Background Worker.

## Структура

```text
performance/
  observations.py        # bounded copies фактических decisions/order outcomes
  runtime.py             # один background writer/collector/projection scheduler
  contracts.py           # allowlist полей, Decimal, валидация
  storage.py             # migration, WAL, dedup/revisions/checkpoints/budget/backup
  migrations/001_initial.sql
  bybit_reader.py         # отдельный signed GET-only клиент + Collector
  reconciliation.py      # циклы, attribution, coverage, cash reconciliation
  calculations.py        # net/statistics/DD/flows, без влияния на risk manager
  metrics.py             # immutable snapshot → observable OTLP gauges
  projections.py         # bounded generation/manifest и offline oracle
  exporters.py           # OTLP HTTPS logs, bounded attempts, без SDK retry queue
scripts/check_performance.py
scripts/performance_export.py
tests/performance_fixtures.py
tests/test_performance_{storage,reconciliation,reader,exporters,observations,dashboard}.py
infra/grafana/performance_catalog.py
infra/grafana/performance.tf
infra/grafana/dashboards/trading-performance.json
docs/performance-data-contract.md
```

`bot.py`: только start/stop, copies tracker IDs и реально выполненных веток.
`live/bybit_client.py`: exactly-once observer вокруг прежнего `_request` для
place_order outcome. Параметры/результаты/исключения/порядок прежних запросов
сохранены. `PositionTracker`, RiskManager, accounting, Telegram, strategy config,
TP/SL/qty/order retry/confirmation и Render config не менялись.

В PR входят ранее локальные материалы этапа 3: их ещё нет в remote main, а новый
dashboard использует их Terraform provider/folder/build/test foundation. Bot Health
JSON/alert expressions не меняются. Ни dashboard, ни alerting не применялись.

## Render Environment — подготовить перед отдельным разрешённым deploy

| Переменная | Значение / назначение |
|---|---|
| PERFORMANCE_ENABLED | `true` включает background journal; default false |
| PERFORMANCE_READONLY_API_ENABLED | `true` включает только GET collector; default false |
| PERFORMANCE_LOG_EXPORT_ENABLED | `true` включает Cloud Logs projection; default false |
| PERFORMANCE_BYBIT_API_KEY | новый **read-only** key того же subaccount, секретное поле |
| PERFORMANCE_BYBIT_API_SECRET | secret этого read-only key, секретное поле |
| PERFORMANCE_BYBIT_TESTNET | `false` для текущего production; согласовать с аккаунтом бота |
| PERFORMANCE_ACCOUNT_ALIAS | `primary`, безопасный стабильный alias, не UID аккаунта |
| PERFORMANCE_HISTORY_DAYS | `7` default; 1..730 только при создании DB; не обещает retention |
| PERFORMANCE_GRAFANA_LOGS_TOKEN | Cloud Access Policy token с `logs:write` для текущего stack, секретное поле |
| MONITORING_ENVIRONMENT | существующее `production` |
| OTEL_EXPORTER_OTLP_ENDPOINT | существующий `https://otlp-gateway-prod-eu-north-0.grafana.net/otlp` |
| GRAFANA_CLOUD_OTLP_USERNAME | существующий OTLP Instance ID `1899238` |
| GRAFANA_CLOUD_API_TOKEN | существующий metrics:write token, не раскрывать/не менять ради Loki |
| DATA_DIR | существующий `/opt/render/project/src/data`; не менять mount/disk |

Ничего из secrets не вводить в Terraform tfvars/Git/чат/CLI arguments. Новый
logs token не равен Grafana Service Account `GRAFANA_AUTH`. Ключ Bybit не должен
разрешать торговлю/переводы; reader не умеет выполнять такие действия даже с
ошибочно расширенными permissions. Старые торговые `BYBIT_*` не используются им.
Флаги и credentials читаются из реального process env **до** dotenv.
Save only в Render сохраняет значения для следующего deploy, не включает модуль
в уже работающем процессе. Изменение флагов требует планового перезапуска/deploy;
не создавать второй экземпляр worker ради включения мониторинга.

## Проверки офлайн

Из repo, в окружении с requirements.txt:

```powershell
python scripts/check_performance.py
python scripts/check_grafana_iac.py --terraform terraform --promtool promtool
```

Второй скрипт требует ранее `terraform init` и локальные CLI; выполняет только
validate, **mock-provider plan tests**, promtool и unit tests. Не использует Cloud
credentials, не выполняет real plan/apply. Не запускать generic unittest discovery:
старый `tests/test_api.py` не входит в offline suite и не предназначен для этого.
В ограниченной Windows sandbox TMP/TEMP можно указать на writable временный
каталог; это настройка локального тестового процесса, не Render Environment.

Проверяются partial fills/closes, fee/funding signs, повторы и corrections,
delayed data, рестарт SQLite/outbox/cursors, неполная/чужая/legacy история, hedge
funding unknown, missing entry, unknown reason, mixed fee currency, cash transfers,
coverage gaps, DB busy/full/budget/backup, bounded queues/response/outbox, secrets,
аргументы/result/exception identity, AST production methods и прежние regression tests.
Синтетический Loki oracle проверяет duplicates/missing row/empty tombstone/expiry/
limit/old business time с новой публикацией. Он не заменяет Cloud UI acceptance.

## Grafana as Code

Не создавать новый datasource/server. В существующем stack открыть Connections →
Data sources → `grafanacloud-tanwolf1218-logs`; скопировать **UID из URL/настройки**,
не display name. Проверить retention/quota Logs и доступ к финансовым таблицам.
В существующем gitignored `infra/grafana/terraform.tfvars` добавить:

```hcl
performance_dashboard_enabled = true
logs_datasource_uid           = "REPLACE_WITH_EXISTING_LOKI_UID"
performance_account_alias     = "primary"
```

Сохранить прежние остальные tfvars/локальный state без изменений. Не терять и не
коммитить существующий Terraform state: уже управляемые stage 3 resources должны
остаться теми же. `GRAFANA_AUTH` задаётся оператором только в env. После отдельного
разрешения подготовить real `terraform plan` и проверить: добавить отдельный
`bybit-trading-performance`; не создавать datasources/Render/receivers, не менять
Bot Health/notification policy. **Apply сейчас не разрешён и не выполнялся.**
Dashboard URL после применения: `/d/bybit-trading-performance` текущего stack.

Metrics/snapshots обновляются ~60s, Loki latest-view ~5m для ограничения трафика.
В таблицах виден исходный as-of, задержка публикации не скрывается. Перед rollout
измерить bytes/generation и проверить месячную quota stack вместе с остальными
логами; сам выбор Cloud Free не гарантирует отсутствие превышения quota.

Обязательный Cloud acceptance до включения реальных финансовых логов: в отдельном
test environment/alias передать synthetic projections, проверить OTLP scope,
service_name mapping, environment/account metadata, scalar generation variable,
JSON/groupBy, last revision, полный count, missing row, genuine empty portfolio,
truncation, expired generation, body closed_at против ingestion timestamp.
Проверить limit не менее 5000 и что panel table показывает все expected строки.
Это Cloud write/UI test и требует отдельного разрешения; в этой реализации он
не проводился. Ошибку transforms/метаданных исправлять только в presentation,
не запускать торговлю для тестовых данных.

## Explore и границы результата

```promql
count by (__name__) ({__name__=~"bot_performance_.*",component="performance",environment="production",account_alias="primary"})
```

```promql
max(bot_performance_equity{component="performance",environment="production",account_alias="primary",scope="account",currency="USDT"})
```

```logql
{service_name="bybit-performance"} | environment="production" | account_alias="primary" | json | kind="manifest"
```

Readiness: свежие balance/positions; moving coverage_end, pending/degraded известны;
показатели не выдаются за полный результат при incomplete. Loki manifest/ack
generation совпадают; integrity=1 по каждой таблице. При переходном indexing lag
0/unknown корректны. Raw log line count не равно числу сделок.

`bot_performance_*` instruments (все gauges, включая исправляемые count/total):

| Группа / suffix | Значение |
|---|---|
| wallet_balance / equity / unrealized_pnl / exchange_cumulative_realized_pnl | USDT coin поля Bybit; scope account |
| account_total_equity_usd | account totalEquity, USD, отдельно от USDT |
| net_closed_pnl / cycles / win_rate / average_win / average_loss / breakeven / profit_factor / profit_factor_no_losses | verified full cycles; labels scope=account/bot, window=all/utc_day/7d/30d |
| kpi_window_complete | endpoint coverage выбранного окна, не отсутствие всех неизвестных сделок |
| fees / funding / realized_cash_change | покрытые account linear USDT transaction components |
| cash_adjusted_profit / cash_adjusted_as_of_timestamp_seconds | условно сверенная flow-adjusted equity change и её lagged as-of |
| drawdown_ratio / max_drawdown_ratio | observed equity DD с cash flows, с начала сбора snapshots |
| positions | actual current positions account / доказанное bot подмножество |
| order_counts | canonical IDs: entry_filled/exit_filled/partial_exit_orders/rejected/tp/sl/unknown/mixed/liquidation/adl |
| decision_count | persisted реальные reason enums, включая ambiguous SDK order_error |
| history_complete / pending_records / unattributed_records | полнота endpoint coverage и unresolved records |
| coverage_start_timestamp_seconds / coverage_end_timestamp_seconds | фактический покрытый интервал журнала |
| balance_timestamp_seconds / positions_timestamp_seconds / strategy_timestamp_seconds | последние успешные observations каждого источника |
| storage_bytes / storage_degraded / dropped_events_total / errors_total | бюджет и ошибки analytics; total gauges сбрасываются с процессом |
| projection_generation / projection_expected_rows / projection_truncated / projection_timestamp_seconds | ack и целостность latest Cloud view; kind — конечный label |
| process_start_timestamp_seconds | latest performance process selector |

Подробные определения/точность: [контракт](performance-data-contract.md).
Первые equity samples появятся после включения, прошлой кривой нет. Если за
собранный период нет доказанных bot cycles, bot KPI будут unknown/N=0. Это ожидаемо,
не основание присваивать боту все сделки субаккаунта.

## Точный порядок до production

1. Review PR и offline results; **merge только по отдельному разрешению**.
2. Проверить рабочий runtime, Auto-Deploy Off, mount/свободное место, размер
   bot.log/backups. Сохранить свежий state backup по прежней процедуре; если DB
   уже существует — SQLite online backup, а не copy одного файла с WAL.
3. Подтвердить тот же Bybit subaccount, новый read-only key, testnet=false, отдельный
   logs:write token и Logs quota/privacy. Не раскрывать значения.
4. Согласовать initial history horizon 7d или другой и budget: DB+WAL+SHM soft
   200MiB, free reserve 200MiB, 20k facts/source analytical load cap. Не удалять
   state/старые backups автоматически; внешний private архив отдельно.
5. Получить разрешение на synthetic Cloud acceptance и Terraform plan/apply;
   проверить generation/metadata/transforms/row limit до реальных Loki данных.
6. Только после отдельного разрешения выставить три PERFORMANCE-флага и секреты
   через Render Save only; проверить наличие ключей без раскрытия. Торговые
   Bybit/Telegram/disk настройки оставить прежними.
7. Согласовать **один** manual deploy конкретного reviewed SHA и время: старый
   риск повторной обработки свечи после restart сохраняется. Не второй worker,
   не пробный запуск bot.py, не retry деплоя ради проверки метрики.
8. После deploy read-only проверить health/main progress, SQLite migration и
   coverage, API collector errors/quota, free disk/CPU/RAM, Grafana freshness и
   небольшую вручную выбранную выборку реальных Bybit records без торговых операций.
9. При проблеме analytics не повторять/закрывать/создавать order. Сохранить evidence,
   оставить панели incomplete; отключение через флаги требует отдельно согласованного
   restart/deploy. Восстановление старого state/DB поверх новых сделок запрещено.

## Что намеренно остаётся unknown

Неполная legacy история без ID/первых fills, старые rejected orders, hedge funding
allocation, reversal/special executions без полной модели, late correction старше
rolling overlap, любое cash reconciliation расхождение. Данные не теряются из
финансовых facts ради красивой статистики; в KPI они не попадают как точные.
Cloud хранит bounded latest-view в рамках retention, полный private архив — SQLite.
Без новой согласованной схемы хранения нет произвольного SQL по всей истории
SQLite в Grafana Cloud. Никаких дополнительных серверов в этой реализации нет.
