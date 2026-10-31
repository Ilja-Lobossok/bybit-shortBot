# Этап 3 — Bot Health Dashboard as Code

Статус: **код подготовлен и проверен офлайн; в Grafana ничего не создано**.
Terraform apply, live plan, Render deploy/restart, commit и push на этом этапе
не выполнялись. Торговый код, зависимости бота, `state.json` и Telegram не меняются.
Новые серверы, Prometheus, Alloy и база данных не требуются.

## Состав

```text
infra/grafana/
  .terraform-version          # проверенный CLI 1.17.1
  .terraform.lock.hcl         # Grafana Provider 4.45.2, Windows/Linux checksums
  versions.tf
  provider.tf                 # auth только из GRAFANA_AUTH
  variables.tf
  main.tf                     # folder + dashboard; существующий datasource read-only
  alerts.tf                   # 8 Grafana-managed rules, по умолчанию paused
  outputs.tf
  terraform.tfvars.example    # только несекретные настройки
  catalog.py                  # исходные панели, PromQL, thresholds и rules
  build.py                    # воспроизводимые JSON и временные promtool fixtures
  scenarios.py                # синтетические временные ряды
  dashboards/bot-health.json  # сгенерированная конфигурация дашборда
  alerts/bot-health.rules.json # тот же источник для Terraform и тестов
  tests/safety.tftest.hcl     # mock_provider, только command=plan
scripts/check_grafana_iac.py
tests/test_dashboard_as_code.py
docs/monitoring-stage3-runbook.md
```

JSON входит в Git благодаря узким исключениям `.gitignore`. Файлы `.tfvars`,
plans, Terraform state, `.terraform/` и `.test-output/` исключены. Lock-файл входит
в состав кода. CLI можно запускать из отдельного каталога: установка в PATH или
в Render не нужна.

## Что будет на дашборде

23 панели, включая поясняющую: состояние основного потока, возраст heartbeat,
возраст и время успешной свечи, подтверждение баланса, время старта и uptime,
наблюдаемые рестарты; Bybit requests/errors по operation/result/category,
p95 и средняя latency; CPU, RSS, свободный DATA_DIR disk; ошибки доставки и drops.

Используются все **14 существующих Prometheus-имён**: histogram даёт отдельные
`_bucket`, `_count`, `_sum`. Финансовых результатов эти метрики не содержат.

- UID dashboard: `bybit-bot-health`, folder UID: `bybit-monitoring`.
- Default stack: `https://tanwolf1218.grafana.net`.
- Существующий datasource UID: `grafanacloud-prom`, тип `prometheus`.
- Фильтры: `bot="bybit-shortbot"`, `component="trading-bot"`, environment из Terraform.
- API operation — ограниченный multi-select; применяется к API-графикам.
  Карточки целого процесса намеренно показывают все операции.
- Обновление раз в минуту; начальный диапазон — 6 часов.
- Main progress и успешный цикл разделены: обычное ожидание 5 секунд не означает
  успешную обработку 15-минутной свечи.
- Отсутствующие измерения показываются как **No data**. Состояние бота при
  отсутствии heartbeat — **NO DATA**, при устаревании — **STALE**.
- CPU: 100% соответствует одному полностью занятому ядру, а не лимиту контейнера.
  Disk — свободные bytes; процент занятого диска без total metric не вычисляется.
- Latency без наблюдений не подменяется нулём. Rate/quantile используют окно 15m,
  соответствующее редким торговым циклам; между ними возможны нули/No data.
- Gauges/суммы текущего процесса фильтруются по новейшему process start через
  `job, instance`. Grafana Cloud сопоставляет эти labels с
  `service.name, service.instance.id`; перед live apply проверить mapping своего
  datasource. Старый более высокий RSS не должен перекрывать новый процесс.
- Временные markers показывают последнее наблюдаемое событие; некоторое время
  после restart старые series ещё доступны в Prometheus lookback.

### Ограничение рестартов

Отдельного счётчика Render restarts нет. Используется
`changes((max(bot_process_start_timestamp_seconds{...}))[$__range:1m])`.
Это нижняя оценка **наблюдаемых** смен времени старта единственного worker:
короткий процесс, не успевший экспортировать данные, и несколько стартов между
шагами могут быть пропущены. При нескольких одновременно работающих worker эта
оценка не является счётчиком рестартов каждого из них. Отсутствие данных не
превращается в нулевую оценку. Тестировать рестарты production для панели не нужно.

## Alert rules

Все правила оцениваются с интервалом 60s **только после включения**.
По умолчанию `alerts_enabled=false` → все восемь `is_paused=true`.

| ID | Условие | Pending | Severity |
|---|---|---|---|
| heartbeat | Main progress старше 180s или нет актуального ряда | 2m | critical |
| cycle | Успешный цикл старше 20m; если текущий процесс ни разу не завершил цикл — grace 25m после старта | 5m | warning |
| bybit-errors | Новые API errors за 15m > 0 | 2m | warning |
| bybit-critical | За 15m минимум 5 errors и доля ошибок >=25% | 2m | critical |
| disk | Свободно строго меньше 100 MiB | 5m | critical |
| export | За 15m минимум 3 export failures | 2m | warning |
| dropped | За 15m потеряны API-наблюдения | 2m | warning |
| restarts | Более 2 наблюдаемых смен старта за 30m | 0s | warning |

У heartbeat нет здорового fallback при потере данных. При живом datasource
PromQL возвращает 1 для отсутствующего heartbeat; pending продолжает работать.
У остальных rules fallback/NoData зависят от смысла: тишина API не является
ошибкой, disk NoData выделяется отдельно. Ошибка исполнения запроса — `Error`,
не `OK`. Поведение Grafana `DatasourceError`/`DatasourceNoData` отдельно от
PromQL; оно требует будущей проверки в Grafana и зависит от версии сервиса.

Counter increase считается до агрегации: reset процесса не превращается в
отрицательное значение или новую ошибку. Самая первая накопленная пачка без
предыдущего sample не входит в `increase`; её видно в cumulative карточках.
При полном outage счётчик export errors может стать видимым только после
восстановления. Потеря доставки контролируется heartbeat. Полная недоступность
самой Grafana Cloud не может надёжно контролироваться только этой же Cloud.

Rules не выполняют команд и не имеют trade/Render webhook. Модуль **не управляет**
contact points, Telegram, mute timings или глобальным notification policy.
Для будущего включения обязателен `alert_contact_point_name` существующего
notification-only получателя; используется прямая маршрутизация rules.
Имя недостаточно для проверки назначения: оператор должен убедиться, что этот
contact point только уведомляет и не вызывает Render deploy hook или торговый API.

## Как подключить существующий Grafana Cloud

1. Внутри существующего stack создать отдельный **Grafana Service Account**
   для Terraform. Базовая роль Editor с правами на нужную папку/alert rules и
   чтение datasource; если используются специальные permissions — проверить их
   по plan/API. Не повышать до Admin автоматически при отказе доступа.
2. Создать ограниченный по сроку token этого service account вручную.
   Это токен **Grafana HTTP API**, а не Cloud Access Policy Token `metrics:write`
   из Render. Существующий `GRAFANA_CLOUD_API_TOKEN` менять не требуется.
3. Передать token только в окружение локального Terraform-процесса:

```powershell
$grafanaSecret = Read-Host 'Grafana Service Account token' -AsSecureString
$env:GRAFANA_AUTH = [System.Net.NetworkCredential]::new('', $grafanaSecret).Password
Remove-Variable grafanaSecret
```

Значение не выводить, не записывать в `.tfvars`, shell history, Git или Render.
Не включать `TF_LOG` с настоящими credentials. После работы удалить env:
`Remove-Item Env:\GRAFANA_AUTH`.

4. Из корня checkout подготовить несекретную конфигурацию:

```powershell
Copy-Item infra/grafana/terraform.tfvars.example infra/grafana/terraform.tfvars
terraform -chdir=infra/grafana init -input=false -lockfile=readonly
terraform -chdir=infra/grafana plan -out=bot-health.tfplan
```

Проверить URL, datasource UID и environment. Первый plan должен создавать только
folder, dashboard и rule group с **восемью paused rules**, без изменений других
ресурсов. При совпадении существующих UID остановиться и согласовать import;
`overwrite=false` защищает первоначальное создание от конфликта dashboard UID,
`prevent_destroy` — от удаления. Последующие согласованные обновления уже
управляемого dashboard выполняются Provider как обновление того же UID.
Код рассчитан на обычный JSON model Grafana и schema закреплённого Provider;
API-совместимость конкретной версии Cloud и permissions пока не проверялась
реальным plan/apply. Direct notification settings требуют поддержки routing
в stack (документированы с Grafana 10.4); при несовместимости не подменять общий
notification policy ради обхода.

**Здесь остановиться до подтверждения пользователя.** Реальный plan — чтение
Grafana API; в выполненных офлайн-тестах вместо него использовался mock provider.
Первый будущий apply только после разрешения:

```powershell
terraform -chdir=infra/grafana apply bot-health.tfplan
```

State и plan хранить вне Git с ограниченным доступом и резервной копией.
Совместная работа требует согласованного backend до первого apply; этот модуль
не создаёт хранилище, сервер или backend. В Git хранится код и lock-файл.

5. После отдельного разрешённого apply проверить dashboard и **Paused** у всех
   rules. Проверить реальные labels `job/instance`, 14 metric names и No data.
   Можно использовать Grafana Preview без остановки worker и без торговых тестов.
6. Активация — отдельный следующий шаг с подтверждением: выбрать существующий
   notification-only contact point, задать `alerts_enabled=true` и имя получателя,
   снова проверить plan, затем согласовать apply. Runbook должен быть опубликован
   в `main`, куда указывают ссылки alerts; публикация требует отдельного согласования.
   Без имени получателя plan
   завершится ошибкой precondition. Доставку уведомлений проверять только с
   согласованным получателем; в рамках подготовки сообщения не отправлялись.

## Изменение конфигурации и офлайн-тесты

Исходник — `infra/grafana/catalog.py`. После изменения выполнить:

```powershell
python infra/grafana/build.py
python infra/grafana/build.py --check
```

`--check` проверяет воспроизводимость JSON. JSON можно review как обычный Grafana
model; ручные изменения будут обнаружены как drift. Terraform читает именно
проверенные JSON, а не отдельную скрытую копию PromQL.

Проверенные инструменты: Python 3.11.10, Terraform 1.17.1, Grafana Provider 4.45.2,
promtool 3.15.1. Официальные CLI скачаны отдельно от repo с проверкой SHA-256;
в production ничего не устанавливалось. Для локального запуска нужны Terraform
и promtool; сам Prometheus server запускать не нужно.

Первичная подготовка provider без backend и credentials:

```powershell
terraform -chdir=infra/grafana init -backend=false -input=false -lockfile=readonly
python scripts/check_grafana_iac.py --terraform terraform --promtool promtool
```

Вместо имён можно указать абсолютные пути к portable CLI. Runner удаляет из
дочернего окружения Grafana credentials/TF_VAR overrides, использует dummy auth,
выполняет только fmt, validate, mock plan-тесты и promtool. Он проверяет, что
все четыре Terraform run-блока действительно исполнены, а не молча пропущены.
Общий Python test discovery не используется; `tests.test_api` не запускается.

Результат подготовки:

- 18 unittest checks: JSON drift, все 14 имён, layout, datasource/scope, No data,
  counter/histogram expressions, safe resource allowlist и mock-only tests.
- 4 Terraform mock plan-теста: paused defaults, отказ включения без receiver,
  explicit routing, отказ label injection. `terraform validate`/fmt прошли.
- 16 сценариев promtool, 31 rule/query expressions проходят syntax check:
  heartbeat freeze/loss/staleness, другая environment, отказ resource sampler,
  нормальное ожидание и отсутствие первого success, API errors/pending, counter
  reset, граница disk, export/drop failures, instance churn и старый RSS.

Promtool использует те же expressions и pending из JSON и применяет тот же
порог **A > 0.5**, что Grafana expression B. Без этого Prometheus alert считал бы
даже sample со значением 0 активным. Отдельные expr-tests проверяют значения 0/1.
Grafana API, NoData/Error transitions внутри Grafana, actual UI rendering и
доставка в contact point **не проверены live**: apply ожидает подтверждения.

После будущего включения появятся 8 evaluation rules с минутным интервалом;
paused rules не оцениваются. Usage/лимиты проверить в существующем Cloud stack,
не меняя тариф. Dashboard querying тоже потребляет quota; новых серверов нет.

## Источники

- [Grafana Provider 4.45.2](https://registry.terraform.io/providers/grafana/grafana/4.45.2/docs)
- [Provider auth / GRAFANA_AUTH](https://github.com/grafana/terraform-provider-grafana/blob/v4.45.2/docs/index.md)
- [Grafana service accounts vs Cloud Access Policies](https://grafana.com/docs/grafana/latest/administration/service-accounts/)
- [Terraform provisioning и риск перезаписи notification policy](https://grafana.com/docs/grafana/latest/alerting/set-up/provision-alerting-resources/terraform-provisioning/)
- [Rule group schema / direct notification settings](https://github.com/grafana/terraform-provider-grafana/blob/v4.45.2/docs/resources/rule_group.md)
- [Grafana No Data / Error](https://grafana.com/docs/grafana/latest/alerting/fundamentals/alert-rule-evaluation/nodata-and-error-states/)
- [Terraform mock providers](https://developer.hashicorp.com/terraform/language/tests/mocking)
- [promtool rule tests](https://prometheus.io/docs/prometheus/latest/configuration/unit_testing_rules/)
- [OTLP resource mapping job/instance](https://grafana.com/docs/grafana-cloud/send-data/otlp/otlp-format-considerations/)
- [Terraform CLI 1.17.1 и SHA256SUMS](https://releases.hashicorp.com/terraform/1.17.1/)
- [promtool 3.15.1 и SHA256SUMS](https://github.com/prometheus/prometheus/releases/tag/v3.15.1)
