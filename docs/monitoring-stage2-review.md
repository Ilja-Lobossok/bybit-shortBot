# Финальная проверка этапа 2 — 30.10.2026

Объект: релиз этапов 1 и 2 в `codex/monitoring-stage2-live-metrics`, база `11df778`.
После принятия этапов разрешены один release commit, push отдельной ветки и PR.
Merge, deploy и торговые операции в scope не входят и не выполнялись.
Render проверен только чтением dashboard, без Environment values, Shell и disk files.

## Результат

| Проверка | Статус | Основание |
|---|---|---|
| 1. Ветка включает оба этапа и зависимости | **PASS по составу релиза** | Все файлы и pins включаются в один release commit по manifest ниже; фактический SHA и PR сообщаются отдельно |
| 2. `MONITORING_ENABLED=false` сохраняет торговое поведение | **PASS** | Нет SDK, HTTP или фонового monitoring thread; исходные args, response identity, exception identity и порядок вызовов сохранены |
| 3. Сбои OTLP, отсутствие credentials, переполнение | **PASS** | Fail-open, отдельные daemon threads, timeout 5s, mailbox 1024, nonblocking lock, конечные labels, тесты отказов и заблокированного exporter |
| 4. Нет новых Bybit calls / изменений ордеров, риска и state | **PASS** | Офлайн-регрессии; AST-сравнение 27 существующих методов после нормализации monitoring hooks; профильные файлы без Git diff |
| 5. Текущий Python / Render Worker | **PASS в проверенном объёме** | Live native worker: Python 3.11.10; 153 локальных теста на той же версии, свежая установка requirements, pip check, Linux dependency resolution. Запуска нового build в Linux/Render не было |
| 6. Disk и данные | **PASS для изменений кода** | Сборщик читает только disk usage; нет migration, outbox, SQLite, изменения mount/DATA_DIR или формата state. Production-файлы не открывались |
| 7. Включение/выключение через Environment | **PASS с перезапуском** | Флаг читается при старте; Save and deploy применяет его. Горячего переключения нет; прежний риск перезапуска описан ниже |

Итог: локальный код прошёл проверку. Публикация PR не означает разрешение
production-деплоя. Ограничения перезапуска требуют отдельного решения до выкладки.

## Исправления во время review

- `.python-version`: неверное `python-3.11.0` заменено на `3.11.10`, совпадающее
  с версией в текущих Render build logs. `PYTHON_VERSION` в Render имеет приоритет.
- `monitoring/metrics.py`: предупреждения об отказе Cloud ограничены первым
  сообщением и одним сообщением за 15 минут непрерывного отказа; счётчик учитывает
  каждый неудачный batch. После восстановления следующий отказ снова виден сразу.
- После stop новые export не выполняются, включая финальный export SDK при
  shutdown. Уже выполняющийся HTTP может закончиться в фоне; торговый поток
  не делает join/flush. Внутренняя ошибка monitoring также выставляет stop.
- Добавлены проверки отсутствующих credentials, rate limit логов, shutdown export
  и переполненной очереди при исходном однократном исполнении order fake.
- Уточнены Render Environment, runtime и ограничения перезапуска в инструкции.

## Проверки

```text
Python 3.11.10: 156 tests — OK
Python 3.12.10: 156 tests — OK
Fresh full requirements install on Python 3.11.10 — OK
pip check — No broken requirements found
Linux x86_64 / CPython 3.11 dependency resolution — OK (dry-run)
Trading AST comparison — 27 existing methods preserved
Protected files — no Git changes
git diff --check — OK
```

Linux dry-run использовал локально собранный pure-Python wheel `pybit==5.14.0`,
поскольку эта версия опубликована как sdist; свежая установка исходного sdist
отдельно прошла. Dry-run не является запуском Linux-приложения.

Команда офлайн-тестов из корня репозитория:

```powershell
python -m unittest tests.test_monitoring_otel tests.test_monitoring_runtime tests.test_monitoring_bot tests.test_accounting -q
```

Не запускать общий discovery или существующий `tests.test_api` для этого review.
Все новые exchange calls — mocks/fakes; реальные ключи тестам не нужны.

## Риски, которые остаются

1. **Перезапуск:** существующий `ShortBot.__init__()` задаёт `last_bar_close=0`.
   Новое включение процесса может повторить обработку текущей 15-минутной свечи
   и stall watchlist. Monitoring этого не исправляет: изменение затронуло бы
   торговую логику. Не выполнять серию toggles/restarts в одной свече; выбор окна
   сам по себе не гарантирует отсутствие повторной обработки. Оба плановых
   перезапуска (выкладка и последующее включение) нужно учитывать отдельно.
   Проверка после принятия этапов подтвердила fake order при раннем stall=4.
   Обычный дубль блокируется local/exchange position guards, но есть окно между
   принятием order и сохранением pending. [Разбор риска](restart-safety-review.md).
2. **Downtime:** persistent disk запрещает zero-downtime deploy; старый процесс
   останавливается перед новым. Нельзя запускать второй worker для сравнения.
3. **Ресурсы:** SDK использует общие CPU/RAM. Очередь и cardinality ограничены,
   но нагрузка на реальном Starter ещё не измерена. Ошибки Cloud не блокируют
   торговый поток, однако абсолютная изоляция ресурсов не обещается.
4. **Логи:** monitoring не пишет собственные данные на диск, но безопасные
   предупреждения попадают в существующий `bot.log`. Ротация этого legacy-лога
   здесь не добавляется; полное отсутствие новых байтов на disk не обещается.
5. **Доставка:** при переполнении теряются наблюдения; при outage нет гарантии
   доставки каждой точки. После полного stop нужна внешняя проверка отсутствия
   samples. Это технические метрики, не финансовый журнал.
6. **Другой runtime:** старый Dockerfile использует Python 3.9 и не проверен для
   SDK >=3.10. Этот релиз рассчитан на существующий native Render worker.

## Полный состав release commit

```text
.python-version
bot.py
live/bybit_client.py
requirements.txt
monitoring/__init__.py
monitoring/otel_test.py
monitoring/otlp.py
monitoring/runtime.py
monitoring/metrics.py
tests/test_monitoring_otel.py
tests/test_monitoring_runtime.py
tests/test_monitoring_bot.py
docs/monitoring-stage1.md
docs/monitoring-stage2.md
docs/monitoring-stage2-review.md
docs/restart-safety-review.md
```

Не добавлять `.env`, tokens, данные, logs, виртуальные окружения или credentials.
Стратегия, TP/SL, risk, retries, confirmation, Telegram, state schema и
`render.yaml` не изменены.

## Порядок будущего деплоя (сейчас не выполнялся)

1. Запланировать окно перезапуска с учётом риска свечи. Зафиксировать текущий
   rollback SHA `11df778`, команды и disk mount. На момент review Auto-Deploy
   установлен **On Commit**: отключить его **до** будущего push/merge в `main`.
2. После отдельного разрешения включить весь manifest в commit и опубликовать
   согласованный release SHA в привязанной `main`. Проверить diff/состав release;
   один только текущий HEAD не содержит monitoring. Сохранить резервную копию
   актуального `state.json` вне Git и без вывода содержимого; при rollback не
   восстанавливать устаревший state поверх новых сделок.
3. У существующего worker задать `MONITORING_ENABLED=false` и **Save only**.
   Не менять торговые env, build/start command, DATA_DIR и disk. Сохранить mount
   `/opt/render/project/src/data`, 1 GB. Credentials можно подготовить здесь же
   только в Environment; их наличие не включает мониторинг при false.
4. **Manual Deploy → Deploy a specific commit**: указать согласованный release
   SHA, содержащий оба этапа. Не выбирать Restart service для установки нового
   кода. Дождаться остановки старого и запуска единственного нового worker.
5. Проверить build logs: Python 3.11.10, успешная установка pinned SDK, запуск
   `python bot.py`. Проверить восстановление state и обычное продвижение цикла
   по логам; не выполнять тестовые ордера или дополнительный запуск `bot.py`.
   Если сборка использует другой Python, остановить включение и выяснить
   источник версии; `.python-version` уступает `PYTHON_VERSION`.
6. Для отдельно запланированного включения выставить `MONITORING_ENABLED=true`,
   проверить наличие трёх OTLP env из stage2.md и нажать **Save and deploy**.
   Это второй перезапуск; не делать его автоматически сразу после первого.
   `Save only → Restart service` не применяет отложенные env.
7. Через 1–2 минуты проверить в Grafana Explore main progress, process start,
   CPU/RSS, disk и API series. После полной чистой свечи проверить cycle success
   (обычно нужно до 15–20 минут). Ожидание каждые 5 секунд не должно обновлять
   cycle success. Проверить dropped/export errors и логи на безопасные warnings.
8. При проблемах только monitoring: `MONITORING_ENABLED=false` → **Save and
   deploy**, с тем же учётом перезапуска. Новые samples прекратятся, история в
   Cloud останется. При проблемах самого release — deploy проверенного прежнего
   SHA; не менять disk, не восстанавливать старый state и не дублировать worker.

Официальная семантика: [Environment](https://render.com/docs/configure-environment-variables),
[Deploy / Restart service](https://render.com/docs/deploys),
[Python version](https://render.com/docs/python-version),
[Persistent disk](https://render.com/docs/disks).
