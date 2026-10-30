# Этап 2: технические метрики работающего бота

Мониторинг выключен по умолчанию: `MONITORING_ENABLED=false`. Он не выполняет
дополнительных запросов Bybit, не меняет стратегию, ордера, risk limits, сохранение
`state.json` или Telegram. На этом этапе нет SQLite, дашбордов и alert rules.

## Устройство

- `monitoring/runtime.py` не импортирует OpenTelemetry. Торговый поток обновляет
  простые временные маркеры и передаёт API-события в ограниченный mailbox.
- Mailbox содержит не более 1024 событий. Его lock берётся только с
  `blocking=False`: занятый lock/переполнение приводят к потере наблюдения,
  а не к ожиданию ордера. Такая потеря отражается отдельной метрикой.
- Фоновый daemon `bybit-monitoring` собирает события примерно каждые 100 мс и
  измеряет ресурсы раз в 10 секунд. Только этот поток работает с SDK instruments.
- `PeriodicExportingMetricReader` отправляет OTLP/HTTPS в своём фоновом потоке
  приблизительно раз в 60 секунд, с timeout 5 секунд. SDK имеет ограниченные
  внутренние повторы; собственной бесконечной очереди доставки нет.
- SDK/HTTP не вызываются из торгового потока. Остановка мониторинга только
  устанавливает Event, без join/flush на пути остановки бота. После этого новые
  export, включая финальную попытку periodic reader, не отправляются. Уже
  выполняющийся HTTP-запрос может завершиться в фоне.
- Параметры подключения и создание HTTPS exporter общие со smoke-test этапа 1
  (`monitoring/otlp.py`). Его глобальное подавление логов в production не используется.

Общие labels: `bot="bybit-shortbot"`, `component="trading-bot"`,
`environment="production"` по умолчанию. Resource: фиксированное имя сервиса и
случайный instance ID на запуск. Произвольные resource-значения из окружения не
подхватываются. Bybit account/order IDs, URL, тексты ошибок и секреты не экспортируются.

## Метрики

Указаны ожидаемые имена Prometheus после стандартного преобразования OTLP.

| Метрика | Инструмент | Значение и место обновления |
|---|---|---|
| `bot_main_progress_timestamp_seconds` | ObservableGauge | Unix seconds реального продвижения основного потока: очередной проход `ShortBot.run()`, завершение API-вызова и этапов свечи |
| `bot_cycle_success_timestamp_seconds` | ObservableGauge | Только конец полностью успешной обработки свечи в `run()` |
| `bot_balance_success_timestamp_seconds` | ObservableGauge | В `refresh_balance()` после валидации, сохранения состояния и присвоения `account_balance` |
| `bot_api_requests_total` | Counter | Число существующих вызовов методов pybit, `operation`, `result=success/error` |
| `bot_api_errors_total` | Counter | Ошибки этих вызовов, `operation`, ограниченная `category` |
| `bot_api_duration_seconds` | Histogram | Длительность вызова SDK по `perf_counter()`, только label `operation` |
| `bot_process_cpu_seconds_total` | ObservableCounter | Накопленное user+system CPU time процесса, `psutil`, секунды |
| `bot_process_resident_memory_bytes` | ObservableGauge | RSS процесса, `psutil`, байты |
| `bot_disk_available_bytes` | ObservableGauge | Свободное место файловой системы `DATA_DIR`, байты |
| `bot_process_start_timestamp_seconds` | ObservableGauge | Фактическое время старта процесса из `psutil.Process.create_time()` |
| `bot_monitoring_dropped_events_total` | ObservableCounter | Потерянные API-наблюдения при переполнении/конкуренции mailbox |
| `bot_monitoring_export_errors_total` | ObservableCounter | Неуспешные batches/исключения OTLP exporter |

ObservableCounter корректен для CPU: ОС уже предоставляет накопленную монотонную
сумму, повторно прибавлять её в обычный Counter нельзя. Gauges временных маркеров
выбраны observable, чтобы экспорт читать только из памяти. Counter API и Histogram
обновляются фоновым потребителем; их инструменты остаются стандартными SDK.

Для Histogram видны `bot_api_duration_seconds_bucket`,
`bot_api_duration_seconds_sum`, `bot_api_duration_seconds_count`. Границы:
0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30 секунд и +Inf.

Нет фиктивных нулевых измерений latency. Histogram появляется после первого
наблюдаемого вызова. Ошибочные counters инициализируются нулями для конечного
набора labels. Временной marker отсутствует до первого соответствующего события;
неизмеренный ресурс не подменяется нулём.

CPU/RSS относятся к процессу, а не ко всему контейнеру Render. Проценты от лимита
контейнера без подтверждённого лимита не вычисляются.

## Что считается успешным циклом

Обычное ожидание каждые 5 секунд обновляет только main progress.
`last_bar_close` по-прежнему управляет существующей обработкой 15-минутной свечи;
он сам не является метрикой успеха.

Для нового успешного timestamp должны завершиться все этапы:

1. Подтверждение баланса до обработки.
2. Сверка отслеживаемых позиций.
3. Получение непустых тикеров и проверка кандидатов.
4. Обработка watchlist, включая нормальное отсутствие кандидатов.
5. Проверка stall, включая отсутствие готовых сигналов.
6. Штатная обработка готовых входов, если они есть.
7. Подтверждение баланса после обработки.

Пустой список сигналов или штатный отказ по риск-правилам не являются ошибками.
Подавленная ошибка API/обработки, невалидные необходимые данные, неудачное
сохранение, ожидающее подтверждение входа/закрытия делают этот цикл неполным.
Даже восстановившаяся после retry API-ошибка консервативно исключает такой проход
из полностью успешных. Следующая чистая свеча снова может обновить marker.

Это только наблюдение: ошибки по-прежнему подавляются/обрабатываются как раньше,
оставшиеся этапы не пропускаются и торговые решения от telemetry-флага не зависят.
Heartbeat не меняется из экспортёра или потока измерения ресурсов.

## Семантика API и ограничения

Точка наблюдения — `BybitClient._request()`, вызывающий исходный метод SDK ровно
один раз с прежними args/kwargs. Его ответ возвращается без копирования, исключение
пробрасывается исходным объектом. Обработчики и tenacity-decorators сохранены.
Прямые `get_open_orders`/`cancel_order` из `ShortBot` проходят через ту же точку.

`operation`: get_instruments_info, get_tickers, get_kline, get_wallet_balance,
place_order, get_positions, get_order_history, get_closed_pnl, get_open_orders,
cancel_order, other. `category`: exchange, timeout, network, exception,
invalid_response. Неизвестное значение нормализуется, а не превращается в label.

Успешность API означает возвращённый словарь с `retCode=0`. Дальнейшая ошибка
валидации бизнес-данных отдельно делает торговый цикл неполным. Счётчик не обещает
учесть каждый физический HTTP retry внутри самого pybit: видна граница метода SDK.
Внешние повторные вызовы существующих tenacity/бота и страницы истории наблюдаются
отдельно. Retry-политики не изменены.

Если mailbox переполнен, API counters/latency могут недосчитаться: смотри dropped
events. Признак неуспешного цикла обновляется синхронно до очереди и не теряется
вместе с API-событием. Это техническая телеметрия, не финансовый журнал.

## Переменные окружения Render

| Имя | Назначение |
|---|---|
| `MONITORING_ENABLED` | Только `true` включает; по умолчанию `false` |
| `MONITORING_ENVIRONMENT` | Необязательная метка окружения, по умолчанию `production`, 1–32 символа: буквы, цифры, `_`, `-` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Базовый HTTPS endpoint из OpenTelemetry tile, заканчивающийся `/otlp` |
| `GRAFANA_CLOUD_OTLP_USERNAME` | Числовой OTLP Instance ID |
| `GRAFANA_CLOUD_API_TOKEN` | Cloud Access Policy Token с `metrics:write` |
| `DATA_DIR` | Уже существующая настройка: файловая система для измерения свободного места |

Monitoring-настройки захватываются **до существующего `load_dotenv()`** в `bot.py`.
Файл `.env` не может включить live monitoring или подставить его credentials.
Менять существующую загрузку торговых/Telegram настроек не требуется.

Новые pip-зависимости не нужны: SDK закреплён этапом 1, `psutil` уже был в проекте.
Не нужно создавать дополнительный сервис, PostgreSQL, Prometheus или Alloy.

## Поведение при сбоях

- Некорректная локальная конфигурация/недоступный SDK при инициализации отключают только
  monitoring worker. В логе — постоянное безопасное сообщение без значения ошибки.
- Сбой доставки или отказ авторизации Grafana не завершают сборщик API-событий. SDK агрегаты остаются
  в памяти, но гарантии доставки каждой временной точки нет.
- Каждая неудачная отправка учитывается в export errors. Безопасное предупреждение
  пишется при первом сбое непрерывного отказа, затем не чаще раза в 15 минут;
  успешная отправка сбрасывает этот интервал для следующего отказа. Ответ Cloud,
  исключение и credentials в сообщение не включаются.
- Нет бесконечного накопления, файлового outbox и сохранения token на диск.
- Фильтр SDK/HTTP логов действует только для экспортирующего потока. Он не меняет
  уровни, handlers, propagation или сообщения торгового потока.
- SDK и retry ограничены timeout; DNS/OS задержки не дают строгой гарантии
  wall-clock длительности. Всё сетевое ожидание остаётся за пределами торговли.
- Память очереди и cardinality ограничены. CPU/RAM общие с ботом: нулевая стоимость
  наблюдения не обещается; реальные показатели Render следует сравнить после
  отдельно согласованного деплоя. Деплой в рамках реализации не выполняется.

## Офлайн-тестирование

Из корня репозитория, в изолированном окружении с зависимостями:

```powershell
python -m unittest tests.test_monitoring_otel tests.test_monitoring_runtime tests.test_monitoring_bot tests.test_accounting -q
```

Важен явный список: не включай существующий `tests.test_api`, предназначенный для
другого способа проверки. Новый integration test изолирует dotenv, file logging и
сокеты до импорта legacy bot. Все биржевые методы — mocks/fakes. Старые проверки
ордеров, pending-состояния и `state.json` повторяются с включённым и сломанным
наблюдателем. Проверяются также фоновые потоки, blocked Cloud export, нулевой
дополнительный запрос, очередь и отсутствие секретов в новых логах/метриках.

Smoke-test этапа 1 сохраняет прежний интерфейс:

```powershell
python -m monitoring.otel_test --mode console --iterations 1
```

Он по-прежнему не запускает торговлю. Успех smoke-test не означает успешность
настоящего торгового цикла.

## Проверка в Grafana Explore после отдельного включения и деплоя

Флаг читается при старте процесса. Это не горячее переключение. В Render
**Environment → Save only** сохраняет настройки для следующего deploy,
а **Save and deploy** применяет их через перезапуск существующего build.
**Manual Deploy → Restart service** использует прежние env текущего запуска и
не применяет отложенные изменения Save only. Поэтому для последующего включения
или выключения меняй `MONITORING_ENABLED` и используй **Save and deploy**.

Перед будущим push/merge проверь Auto-Deploy: у текущего worker при review он
включён **On Commit**. Для контролируемой выкладки сначала отключи его. Первый
деплой изменений выполняй с `MONITORING_ENABLED=false`, затем после проверки
бота отдельно запланируй включение. Сохрани start command `python bot.py`,
build command `pip install -r requirements.txt`, существующий disk mount и
`DATA_DIR`. Не запускай вторую копию бота ради проверки метрик.

Существующая торговая логика устанавливает `last_bar_close=0` при каждом старте.
Перезапуск может повторно обработать текущую 15-минутную свечу, включая stall
watchlist. Это прежнее поведение, не исправляемое мониторингом. Не переключай
флаг несколько раз в одной свече. Выкладка с persistent disk также предполагает
остановку старого процесса перед запуском нового. Отсутствие влияния телеметрии
на ордера не означает отсутствие риска самого перезапуска.

Текущий native Python worker использует **3.11.10** (проверено в build logs
30.10.2026). `.python-version` содержит plain `3.11.10`; переменная Render
`PYTHON_VERSION`, если задана, имеет приоритет. Старый Dockerfile с Python 3.9
не является проверенным способом запуска этого этапа; текущие SDK требуют
Python >=3.10. Не переноси эту выкладку на Docker без отдельной проверки.

В Grafana выбери Cloud Metrics/Prometheus datasource нужного stack,
**Last 30 minutes**. Первые samples ожидаются примерно через минуту после SDK
старта плюс задержка ingest. Общий поиск:

```promql
{__name__=~"bot_.*", component="trading-bot", environment="production"}
```

Возраст продвижения основного потока, секунды:

```promql
time() - bot_main_progress_timestamp_seconds{environment="production", component="trading-bot"}
```

Возраст последней полностью успешной свечи:

```promql
time() - bot_cycle_success_timestamp_seconds{environment="production", component="trading-bot"}
```

Порог около 20 минут — только начальное предложение для 15-минутного цикла,
с поправкой на нормальную длительность обработки и первый запуск.

Свежесть подтверждённого баланса:

```promql
time() - bot_balance_success_timestamp_seconds{environment="production", component="trading-bot"}
```

Дополнительного минутного запроса баланса нет: свежесть должна соответствовать
существующему расписанию. Здесь трёхминутный порог обычно был бы неверным.

API-вызовы в секунду и ошибки за 15 минут:

```promql
sum by (operation, result) (rate(bot_api_requests_total{component="trading-bot", environment="production"}[5m]))
```

```promql
sum by (operation, category) (increase(bot_api_errors_total{component="trading-bot", environment="production"}[15m]))
```

p95 API latency в секундах (при очень малом числе запросов результат нестабилен):

```promql
histogram_quantile(0.95, sum by (le, operation) (rate(bot_api_duration_seconds_bucket{component="trading-bot", environment="production"}[15m])))
```

CPU в используемых ядрах, RAM в MiB, свободное место в GiB:

```promql
rate(bot_process_cpu_seconds_total{component="trading-bot", environment="production"}[5m])
```

```promql
bot_process_resident_memory_bytes{component="trading-bot", environment="production"} / 1024 / 1024
```

```promql
bot_disk_available_bytes{component="trading-bot", environment="production"} / 1024 / 1024 / 1024
```

Uptime процесса в секундах:

```promql
time() - bot_process_start_timestamp_seconds{component="trading-bot", environment="production"}
```

Потери событий и ошибки отправки:

```promql
increase(bot_monitoring_dropped_events_total{component="trading-bot", environment="production"}[15m])
```

```promql
increase(bot_monitoring_export_errors_total{component="trading-bot", environment="production"}[15m])
```

При outage последнее значение export errors придёт только после восстановления
доставки. Для полной остановки/обрыва доставки нужна проверка отсутствия samples:

```promql
absent_over_time(bot_main_progress_timestamp_seconds{bot="bybit-shortbot", component="trading-bot", environment="production"}[5m])
```

Отсутствие ряда cycle success до первого чистого прохода — отдельное состояние,
а не timestamp 0 и не успех. Отсутствие новых сделок не является неисправностью.
Alert rules при последующей настройке должны учитывать возраст marker, отсутствие
samples и ошибки datasource. Сам этот этап alert rules не создаёт.

## Официальные источники

- [OpenTelemetry Python instruments](https://opentelemetry.io/docs/languages/python/instrumentation/)
- [SDK exporters и periodic reader](https://opentelemetry.io/docs/languages/python/exporters/)
- [Grafana Cloud OTLP](https://grafana.com/docs/grafana-cloud/send-data/otlp/send-data-otlp/)
- [Обработка No Data в Grafana](https://grafana.com/docs/grafana/latest/alerting/fundamentals/alert-rule-evaluation/nodata-and-error-states/)
- [Версия Python в Render](https://render.com/docs/python-version)
- [Применение Environment в Render](https://render.com/docs/configure-environment-variables)
- [Deploy и Restart service](https://render.com/docs/deploys)
- [Ограничения persistent disk](https://render.com/docs/disks)
