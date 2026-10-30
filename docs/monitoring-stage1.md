# Этап 1: самостоятельный тест Grafana Cloud OTLP

Этот этап проверяет только доставку двух синтетических метрик. Модуль не импортирует
`bot.py`, не подключается к Bybit/Telegram, не читает `.env`, не создаёт SQLite и не
меняет `state.json`. Запуск `python bot.py` и деплой Render для проверки не нужны.

## Что отправляется

| Метрика | Тип | Значение |
|---|---|---|
| `bot_monitoring_test_total` | Cumulative counter | 1, 2, 3… по числу итераций этого запуска |
| `bot_monitoring_heartbeat_timestamp_seconds` | Gauge, Unix seconds | Время последней итерации тестового процесса |

У обеих метрик фиксированные attributes `environment="monitoring-test"`,
`component="otel-test"`. Resource содержит только имя тестового сервиса и случайный
instance ID на запуск. Произвольные `OTEL_RESOURCE_ATTRIBUTES` не подхватываются.
Heartbeat относится к тесту, а не к работающему торговому боту.

## 1. Локальная установка и проверка без сети

Команды выполняются из корня репозитория, в отдельном виртуальном окружении.
Тестовый модуль требует Python 3.10+; Python 3.12 тоже подходит. Для этого этапа
достаточно трёх OpenTelemetry-пакетов, без установки Bybit и пакетов backtest.
Существующий `requests==2.31.0` также совместим с этим отправителем.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install opentelemetry-api==1.45.1 opentelemetry-sdk==1.45.1 opentelemetry-exporter-otlp-proto-http==1.45.1
.\.venv\Scripts\python.exe -m unittest tests.test_monitoring_otel -v
.\.venv\Scripts\python.exe -m monitoring.otel_test --iterations 1
```

Новые зависимости также добавлены в `requirements.txt`. Для установки всего
существующего файла нужен совместимый с проектом Python 3.11: старые версии
NumPy/pandas/pyarrow в нём не предназначены для Python 3.12+. Установка всех
зависимостей бота и изменение его Python/Docker runtime в этот этап не входят.

Режим по умолчанию — `console`: печатается JSON двух метрик, HTTP-экспортёр не
создаётся даже при наличии cloud credentials. Не запускай общий discovery всех
старых тестов проекта для этой проверки: отдельные существующие API-тесты могут
быть рассчитаны на биржевое подключение.

## 2. Создать Grafana Cloud Free

1. Открой [Grafana Cloud](https://grafana.com/products/cloud/), создай аккаунт и stack.
2. Выбери регион и проверь, что используешь Free, а не полагаешься на возможности
   временного Pro trial. Отдельный Render-сервис создавать не нужно.
3. В Cloud Portal открой свой stack и карточку **OpenTelemetry → Configure**.
   Альтернативный вход — **Connections → Add new connection → OpenTelemetry**.
4. Скопируй **OTLP endpoint** и числовой **Instance ID именно с этой карточки**.
   Не подставляй автоматически ID от другой службы или URL Prometheus remote write.
5. Создай Cloud Access Policy Token с `metrics:write`, ограниченный нужным stack.
   Если мастер предлагает также logs/traces, для этого этапа они не требуются.
   Токен увидишь при создании: сохрани его в менеджере секретов.

Это токен отправки телеметрии, а не Grafana service-account token для дашбордов.
Ни Alloy, ни отдельный Prometheus не нужны.

Проверенные ограничения Free: 10 000 активных series, retention метрик 14 дней,
3 активных пользователя. Пара метрик с минутной отправкой намного меньше этого
лимита; фактическое использование проверяется в Cloud Usage. Pro сейчас начинается
с $19/месяц плюс usage, но переход на него здесь не требуется.

## 3. Передать credentials через окружение

| Переменная | Что указать |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Базовый HTTPS endpoint из OpenTelemetry tile, заканчивающийся `/otlp` |
| `GRAFANA_CLOUD_OTLP_USERNAME` | Числовой OTLP Instance ID |
| `GRAFANA_CLOUD_API_TOKEN` | Cloud Access Policy Token с `metrics:write` |

Модуль сам добавляет `/v1/metrics` и формирует Basic Authorization.
Токен вводится отдельно, без ручного Base64 и без `OTEL_EXPORTER_OTLP_HEADERS`.
Эта диагностическая команда использует именно три переменные выше; другие
endpoint/header-переменные SDK не являются её интерфейсом.

Поддерживаются только HTTPS endpoints Grafana Cloud на `*.grafana.net`, порт 443,
без query, fragment и встроенных credentials. Используй адрес своего stack;
конкретное имя gateway/региона не нужно угадывать.

Безопасный ввод в PowerShell без токена в истории команд:

```powershell
$env:OTEL_EXPORTER_OTLP_ENDPOINT = Read-Host 'OTLP base endpoint из Cloud Portal'
$env:GRAFANA_CLOUD_OTLP_USERNAME = Read-Host 'OTLP Instance ID'
$monitoringToken = Read-Host 'Cloud Access Policy Token' -AsSecureString
try {
    $env:GRAFANA_CLOUD_API_TOKEN = [System.Net.NetworkCredential]::new('', $monitoringToken).Password
    .\.venv\Scripts\python.exe -m monitoring.otel_test --mode otlp --iterations 3 --interval-seconds 60 --timeout-seconds 5
    $monitoringExitCode = $LASTEXITCODE
} finally {
    Remove-Item Env:GRAFANA_CLOUD_API_TOKEN -ErrorAction SilentlyContinue
    $monitoringToken.Dispose()
}
$monitoringExitCode
```

Значение токена неизбежно находится в памяти процесса во время HTTPS-запроса.
Его нельзя выводить через `Get-ChildItem Env:`, включать в аргументы команды,
записывать в Git, пример конфигурации или скриншот. `.env` для этого теста не нужен.
Не меняй окружение существующего Render Worker на этом этапе.

## 4. Поведение и ограничения отправителя

- По умолчанию 3 batches: сразу, затем две паузы по 60 секунд. После этого выход.
- Допустимы 1–20 итераций, интервал 5–300 секунд, timeout 0.1–30 секунд.
- Один batch содержит текущий cumulative counter и heartbeat. Нет дисковой
  очереди, фонового периодического экспортёра и дополнительного экспорта при выходе.
- Встроенный SDK может повторять временные HTTP/connection ошибки в рамках своего
  retry/timeout-механизма. Поверх него нет собственного бесконечного retry.
  Timeout не является гарантией точного wall-clock лимита при DNS/OS-задержках.
- Неуспешный batch отмечается безопасным сообщением; следующие итерации продолжаются.
  Ошибки SDK/HTTP, response bodies, headers и объекты credentials не печатаются.
- Exit code: `0` — все batches успешны, `1` — был сбой экспорта/cleanup,
  `2` — неправильная конфигурация, `130` — Ctrl+C.
- TLS-проверка включена. Неявные proxy/`.netrc` настройки окружения отключены.
- `export succeeded` означает успех по ответу экспортёра. Только запрос в Grafana
  подтверждает появление нужной метрики в нужном stack.

Для нескольких отправок модуль использует SDK `InMemoryMetricReader` и явный
ограниченный цикл экспорта. Это самостоятельный smoke test, не готовый механизм
встраивания мониторинга в торговый поток.

## 5. Проверить в Grafana

Открой **Explore**, выбери преднастроенный Cloud Metrics / Prometheus datasource
этого stack, установи **Last 15 minutes**, затем выполни instant query:

```promql
bot_monitoring_test_total{environment="monitoring-test", component="otel-test"}
```

Во время трёх итераций ожидаются значения 1, 2, 3 для одной series/instance. Новый
запуск создаёт новый instance ID; не суммируй разные запуски как один тест.

Heartbeat:

```promql
bot_monitoring_heartbeat_timestamp_seconds{environment="monitoring-test", component="otel-test"}
```

Возраст последнего тестового tick:

```promql
time() - bot_monitoring_heartbeat_timestamp_seconds{environment="monitoring-test", component="otel-test"}
```

После окончания теста возраст сначала растёт, затем instant query может вернуть
No Data из-за staleness. Исторический range query продолжит показывать samples
в пределах retention. Отдельные alert rules сейчас не создаются.

Имена приведены для стандартного преобразования OTLP → Prometheus. Если ничего не
найдено, открой Metrics browser или выполни запрос обнаружения:

```promql
{__name__=~"bot_monitoring_.*", environment="monitoring-test"}
```

Проверь выбранный stack/datasource, часы компьютера, период запроса, исходящий HTTPS,
base endpoint, OTLP Instance ID и scope токена. Подожди 1–2 минуты и обнови Explore.
Сбой credentials исправляй через Cloud Portal; не включай сырые SDK debug-логи.

## Официальные источники

- [Прямая отправка OTLP, endpoint и credentials](https://grafana.com/docs/grafana-cloud/send-data/otlp/send-data-otlp/)
- [Cloud credentials и write tokens](https://grafana.com/docs/grafana-cloud/platform/security-and-account-management/security-and-access/authentication-and-permissions/access-policies/manage-cloud-data-source-credentials/)
- [OpenTelemetry Python exporters](https://opentelemetry.io/docs/languages/python/exporters/)
- [Тарифы и retention Grafana Cloud](https://grafana.com/pricing/)
- [Series и частота отправки DPM](https://grafana.com/docs/grafana-cloud/platform/pricing-and-usage/metrics/)

Этап заканчивается подтверждённым появлением двух тестовых метрик. Подключение к
боту, SQLite, дашборды, Telegram-изменения и деплой Render остаются за его пределами.
