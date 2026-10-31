# Trading Performance: контракт данных v1

Реализация этапа 4. Область торгового журнала — linear USDT; wallet/equity USDT
и суммарная account equity USD показываются отдельно. Все Bybit timestamps UTC ms.
`state.json` и `risk_state.trades_history` не являются финансовым первоисточником.
Legacy P&L из risk history не импортируется в финансовые суммы.

## Источники и точность

`performance/bybit_reader.py` содержит отдельную signed HTTPS session с allowlist
шести GET endpoints: wallet-balance, position/list, execution/list, order/history,
position/closed-pnl, account/transaction-log. Place/cancel/amend/transfer отсутствуют.
Используются существующие `BYBIT_API_KEY`, `BYBIT_API_SECRET`, `BYBIT_TESTNET`.
HTTP session отдельная от торговой; allowlist по-прежнему разрешает только GET.
Новых credentials нет. Ключи захватываются из process env до dotenv вместе с
существующим OTLP credential; не передаются в metric labels/SQLite/Loki.
Сохраняются только поля из `performance/contracts.py`, без полного request/response,
headers, произвольных текстов ошибок, orderLinkId и адресов кошельков.

`performance.sqlite3` содержит canonical facts и revisions, account snapshots,
positions_current, coverage windows/cursors, ownership, strategy events/current,
outbox и meta. Money/qty/prices хранятся исходными Decimal strings в JSON TEXT;
SQLite REAL и float из торгового расчёта не используются для финансовых сумм.
Float используется только для OTLP gauge представления. Строковые точные значения
можно получить read-only экспортом `scripts/performance_export.py`.

Миграция v1 атомарна, WAL + synchronous FULL + busy_timeout 100ms, один writer.
Дедуп: execution — execId, transaction — id, order/closedPnL — symbol+orderId.
Перечитанная изменённая запись получает revision, не второй результат. Более
старый updatedTime не перезаписывает новый. Account separation: fingerprint
существующего API key привязан к DB. При ротации ключа автоматическое продолжение
запрещено: сначала оператор подтверждает тот же аккаунт и согласует перенос binding
или отдельный журнал. Fingerprint не является доказательством exchange account UID.

## Coverage и восстановление

Snapshots раз в ~60s; history раз в ~300s. Это интервалы запуска, не SLA: GET и
пагинация занимают время в фоне. Request rate ≤1/s, connect/read 2/5s, stream cap
2MiB и проверка общего elapsed time между chunks. При ошибке 60s backoff,
без немедленного retry; HTTP403 — 10m backoff коллектора. Низкий
X-Bapi-Limit-Status увеличивает паузу до 60s. Торговая retry policy не меняется.
Общие IP/UID quota и CPU/disk с торговлей остаются общими.

История по умолчанию последние 7d, `PERFORMANCE_HISTORY_DAYS=1..730` только при
создании журнала. Это запрошенный интервал, не гарантия retention биржи. Изменение
значения не расширяет уже сохранённый origin автоматически. Для backfill за origin
нужно отдельное согласованное расширение журнала, не удаление state/DB.
Окна ≤7d; не более 4 страниц/source за tick, 4 sources. Page+cursor/checkpoint
фиксируются одной транзакцией; window complete только после последней страницы.
После restart незавершённое окно продолжается; при сбое cursor удаляется и окно
проходит заново по IDs. Существующий watermark не продвигается на partial response.
Постоянное rolling overlap 24h и lag 2m ловят поздние события/изменения; исправления
старше overlap автоматически не гарантируются. Для них нужен согласованный replay
нужного интервала. Старые отмены/отклонения Bybit может уже не хранить.

Портфель заменяется только после всех страниц. API error не удаляет позиции.
Хранятся acquisition start/end и Bybit updatedTime/seq; последовательные страницы
не считаются атомарным снимком биржи. Дубликат position key отменяет замену.
После restart positions_current остаётся последним наблюдением с прежним as-of.

## Финансовая сверка и принадлежность

`reconciliation.py` строит циклы flat → position → flat по исполнениям и positionIdx
из order history. Partial fills одного order не считаются новыми сделками.
Для `money_status=reconciled` обязательны: полное endpoint coverage периода,
terminal orders, совпадение summed fills с cumExecQty, closedSize с исполнениями
выхода, avg entry с фактической VWAP, подтверждение начала с flat, полная связка
tradeId/execId с транзакциями, fees в USDT и совпадение суммы transaction change
с Bybit closedPnl в пределах **0.00000001 USDT**. Допуск — критерий сверки;
сумма closedPnl не округляется и balancing entries не создаются.

Net — сумма Bybit closedPnl; fees/funding выводятся как детализация и повторно
не вычитаются. Fee >0 = расход, <0 = rebate; funding >0 = получение.
`change = cashFlow + funding - fee` проверяется на каждом transaction.
Fee currency отличная от USDT сохраняется отдельно; без conversion evidence
цикл остаётся incomplete. Hedge funding не распределяется догадкой: idx≠0
остаётся incomplete. Reversal, orphan close, special executions, неоднозначный
порядок разнонаправленных fills с одинаковым timestamp не становятся точным KPI.
Closed order всё равно доступен с `unit=closing_order`, exchange_reported и
unknown attribution, если полного цикла нет. updatedTime — source_updated_ms,
не выдуманное время фактического выхода.

Bot attribution требует entry order ID из фактически сохранённого ботом order
или bootstrap tracker и подтверждения flat/полноты последовательности. Новая
entry observation сохраняется после штатного `tracker.add_position`, после уже
существующего guard exchange positions; state не меняется. Историческому ID
нужны transaction size evidence. Все opening orders цикла должны принадлежать
боту. Ручное увеличение объёма делает bot attribution unknown. Открытая позиция
дополнительно сверяется по side и оставшемуся qty. Один symbol не доказывает
принадлежность. Legacy без ID и чужие сделки исключаются из bot KPI.

Есть три независимых признака: money_status, attribution_status, reason.
Unknown reason не меняет подтверждённые деньги. TP/SL/Liq/ADL берутся только
из order metadata; совпадение цены с planned TP ничего не доказывает.
Account KPI включает только сверенные циклы; bot KPI — их подтверждённое подмножество.
Это **covered/reconciled sample**, не обещание всей lifetime истории счёта/бота.
Account wallet/equity нельзя честно разделить на bot/manual части без модели
аллокации капитала: отдельный «баланс бота» не выдумывается.

## Показатели

- Wallet raw, USDT coin equity/UPL/cumulative realized и total equity USD — Bybit
  snapshots. Mark берётся из позиции; historical equity до сбора отсутствует.
- Net closed P&L, wins/losses, win rate, averages, PF, N — по сверенным полным циклам, окна
  all-covered / текущий UTC day / rolling 7d / 30d. Time picker рисует observations,
  а не пересчитывает произвольное SQL-окно. Coverage окна показан отдельно.
- N=0: P&L/win rate/PF unknown. Без losses PF не конечное число; флаг no_losses.
- Account realized cash change, fees, funding — USDT linear transaction components
  за покрытый интервал; transfer/bonus/borrow не прибыль. Неизвестный type или
  trade другой категории блокирует aggregate complete, исходный факт сохраняется.
- Cash-adjusted USDT equity change — только по двум реально полученным snapshots
  с exchange times внутри полного transaction coverage, классифицированными flows
  и совпадением wallet delta с суммой change. Вычитаются nontrading flows.
  Есть отдельный as-of; из-за lag показатель отстаёт от текущей equity. Это не
  portfolio USD ROI/TWR; без согласования boundaries unknown.
- DD — maximum среди наблюдений USDT equity с начала сбора, **включая cash flows**,
  нижняя оценка непрерывной просадки. Withdraw может увеличить DD. Это не TWR
  drawdown и не peak wallet из RiskManager.
- Counts — уникальные entry/exit orders с fills, partial exit orders, TP/SL/unknown
  metadata и реально увиденные rejected orders (короткий retention). Decision
  order_error = SDK exception/error существующего place_order, может быть ambiguous
  accepted order; никогда не триггер retry. Gauge counts могут пересчитываться.

## Strategy observations

`bot.py` добавляет только `_performance(...)`, bootstrap и snapshot копии.
Функции стратегии не вызываются повторно. Записываются реальные ветки отсеивания,
watchlist/stall/local_high/blocked/ready, pump по действующей формуле low24,
candle_ms реально использованной свечи и scheduled_bar. Trade args/results,
исключения и call order сохраняются. `_request` обёрнут только для учёта исхода
существующего place_order, с вызовом ровно один раз.

Mailbox 1024; копии без DB/network и без ожидающих locks. Переполнение — dropped
indicator; финансовые факты восстанавливаются через GET, потерянные решения нет.
Crash до приёма observer не даёт exactly-once decision history. Persisted decisions
дедуп по run:sequence; повторная обработка свечи после restart видна как новое
наблюдение. Предыдущий риск повторной обработки свечи не исправлялся.

## Metrics-only dashboard / необязательный Loki

Основной Trading Dashboard читает **только существующий Cloud Metrics datasource**.
Таблицы позиций/сделок и LogQL/generation variables удалены из dashboard; для
запуска не нужен logs token, Logs datasource UID или проверка Loki transforms.
Watchlist/candidates/ready/blocked/stalling — bounded `strategy_size{state=...}`
gauges, счётчики реальных решений — `decision_count{reason=...}`. Counts вычисляются
по полному уже наблюдённому списку до ограничения detailed rows, без повторного
вызова стратегии; сохраняются в SQLite. До первого snapshot positions/strategy
не публикуются как фиктивный ноль. Новые financial instruments используют прежний
OTLP endpoint/username/token; прежние health metrics не содержали этих сумм.

Loki оставлен только как отключённый optional export: default false, отдельный
optional код не является зависимостью обновления финансового cache. При отсутствии
logs-токена или ошибке отправки metrics/SQLite/торговля продолжаются. Дальнейшее
описание projection относится только к этому необязательному пути, не dashboard.

Индексируемый resource: service.name=bybit-performance; environment/account_alias
конечные metadata/labels. Никаких symbol/order/exec/generation index labels.
Body allowlisted, exact Decimal strings; никаких raw API payloads/secrets.
Metric labels только scope/window/currency/kind/state/reason/outcome + постоянный alias.

Каждая generation публикует canonical latest view: ≤200 positions, ≤1000 cycle /
unmatched closing-order records, ≤200 strategy rows + manifest expected/truncated.
Business timestamps отличаются от publication time. Generation — числовое metric
value, не time-series label. Empty новая generation заменяет старую (tombstone).
Old business rows перепубликуются, но не становятся новыми сделками.

Полная таблица публикуется раз в ~5m; metrics/account snapshots ~60s. As-of каждой
позиции остаётся исходным, 5m latest-view не выдаётся за live tick. Перед production
проверить стоимость по фактическим bytes/generation × ~8640 публикаций/30d плюс
retries и общий quota stack. Лимит 2MiB не является обещанием бесплатного тарифа.

Outbox содержит одну generation ≤2MiB, максимум 3 HTTP попытки за 15m, затем
supersede с error indicator. Ack только HTTP200 без OTLP partial_success; raw
retries не считаются сделками. Attempts сохраняются до send, crash не обнуляет
лимит. Основной metrics-only dashboard не использует generation/manifest и не
требует этого канала. Если в будущем отдельно разрешат log tables, их presentation
и Cloud limits надо проверять отдельно; это не gate текущего deploy.

Offline projection oracle проверяет missing rows, retry, latest correction,
empty tombstone, truncation и expiry. Это не live тест версии Grafana/Loki.
Cloud apply/реальные API/реальные деньги в локальных тестах не используются.

## Disk / память / отказ

Soft cap DB+WAL+SHM 200MiB, reserve free 200MiB; WAL checkpoint target 32MiB,
не жёсткий max WAL size. 20k canonical rows/source — лимит аналитической загрузки
в память; превышение останавливает refresh с degraded/stale, а не обрезает KPI.
Strategy detail/observation IDs 7d; financial facts/revisions/equity не удаляются
молча. При cap сбор приостанавливается до согласованного архива/расширения. Это
конечный архив, а не бесконечная история на 1GB. Дополнительный журнал может
упереться в cap до свободного disk — намеренно. `bot.log`/state/backups не удаляются.
SQLite backup API создаёт новый private файл и проверяет integrity; backup на
том же диске не DR. SQLite connection закрывается явно (важно на Windows).

`PERFORMANCE_ENABLED` наследует существующий `MONITORING_ENABLED`; explicit false
отключает performance. GET collector включён вместе с performance, explicit
`PERFORMANCE_READONLY_API_ENABLED=false` отключает только GET. Новые обязательные
Render vars отсутствуют. Logs default false. Метрики раз в 60s через существующий OTLP HTTPS factory и
safe exporter, logs single attempt с bounded time/body, только background thread.
Ошибка telemetry/SQL/collector не входит в торговый retry. Если worker analytics
полностью погиб, freshness пропадёт; health heartbeat основного бота остаётся
независимым. Нет zero-overhead гарантии: CPU/IO/quota общие, нужен rollout check.

Первичные документы: [Bybit executions](https://bybit-exchange.github.io/docs/v5/order/execution),
[orders](https://bybit-exchange.github.io/docs/v5/order/order-list),
[closed PnL](https://bybit-exchange.github.io/docs/v5/position/close-pnl),
[transaction log](https://bybit-exchange.github.io/docs/v5/account/transaction-log),
[USDT P&L](https://www.bybit.com/en/help-center/article/Profit-Loss-calculations-USDT-Contract),
[Loki LogQL](https://grafana.com/docs/loki/latest/query/log_queries/),
[Grafana variable scalar/vector formatting](https://github.com/grafana/grafana/blob/v12.2.0/packages/grafana-prometheus/src/metric_find_query.ts#L80),
[SQLite online backup](https://www.sqlite.org/backup.html).
