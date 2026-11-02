# Исправление учёта закрытий

Два разных результата отображаются отдельно:

- `closed_order_*` и `closed_orders` — уникальные **закрытые ордера Bybit**.
  Сумма — исходный `closedPnl`, `money_status=exchange_reported`. Разные
  частичные выходы считаются отдельно. Отсутствие старого входа не скрывает
  подтверждённый биржей выход. Это не полный flat-to-flat цикл.
- Старые `net_closed_pnl`, `cycles`, `wins`, `losses` — только полностью
  сверенные циклы. Их строгие финансовые проверки сохранены.

Префикс всех метрик `bot_performance_`. Для закрытых ордеров доступны
`closed_orders`, `closed_order_net_pnl`, `closed_order_wins`,
`closed_order_losses`, `closed_order_breakeven`, `closed_order_win_rate`,
`closed_order_profit_factor`, `closed_order_average_win`,
`closed_order_average_loss`, `closed_order_history_complete` с ограниченными
labels `scope=account|bot`, `window=all|utc_day|7d|30d`, `currency=USDT`.
`closed_order_history_start_timestamp_seconds` обозначает начало запроса
истории. ID ордеров в labels отсутствуют. PF без убытков и среднее отсутствующей
группы остаются неопределёнными; не подменяются нулём.

Для периода закрытия используется `updatedTime`. У защитного SL `createdTime`
может относиться к открытию позиции за много дней до исполнения. В
`live/accounting.py::ClosedTrade.from_records` исправлен только временной
фильтр; сохранены проверки направления, полной суммы количества, средней цены
входа, дедупликация и атомарный учёт. Новых торговых запросов/повторов нет.
Формат `state.json`, настройки риска, размеры, TP/SL, входы и основной цикл
не изменены. При корректно восстановленном закрытии штатный risk manager
получит его фактический результат, включая убыток, как и предусмотрено ранее.

Документация Bybit: [closed PnL endpoint](https://bybit-exchange.github.io/docs/v5/position/close-pnl),
[состав closed P&L](https://www.bybit.com/en/help-center/article/Profit-Loss-calculations-USDT-Contract).
Комиссии и funding уже входят в `closedPnl`; повторных вычитаний нет.
Переводы не являются закрытыми ордерами и сюда не попадают.

Фоновый Collector дополнительно догружает только closed PnL за последние
30 дней (или больший существующий history lookback). Не более 4 GET-страниц
за 5-минутный history tick, каждое окно до 7 дней. Отдельный restartable cursor
не мешает обычной текущей сверке. Все запросы — GET через прежний ключ и
ограничитель 1/s. Догрузка занимает несколько проходов; до покрытия периода
`closed_order_history_complete=0`. Новых env, секретов, схем SQLite и серверов
нет. Старые финансовые записи не удаляются и не заменяются догадками.

`Store.refresh_tracker_evidence` фоново читает атомарный state (до 4 MiB) и
переносит только `entry_order_id` и подтверждённые `order_ids`/symbol закрытий
из risk history как доказательство владения. Деньги из risk history никогда
не импортируются. Суммы всегда берутся из Bybit. Без ID принадлежность
остаётся unknown и запись видна только в account. Это восстанавливает
атрибуцию после рестартов/потерь необязательных наблюдений, не меняя state.

На дашборде новые карточки закрытых ордеров находятся на основном экране.
Полные циклы оставлены в отдельном свёрнутом разделе. Scope по умолчанию
account, период all; период карточек выбирается отдельно от временной шкалы
графиков. Старый URL с var-scope=bot или var-window=utc_day переопределяет
эти значения. Используйте account/all для всей доступной истории.
График накопленной суммы содержит только реальные OTLP-точки с запуска
новой версии. Догрузка истории меняет текущую сумму, но не рисует прошлые
наблюдения, которых в Grafana не было.

Проверка: `python scripts/check_performance.py` (офлайн, без Bybit/Telegram).
`tests/test_closed_orders.py` проверяет старые SL, неполные входы, дубли,
ревизии, рестарт, курсоры, отрицательные суммы и разделение account/bot.
`tests/test_accounting.py` проверяет единственное уведомление подтверждённого
убытка и отсутствие place/cancel вызовов при таком закрытии.

Перед выпуском: backup state и онлайн SQLite backup, проверка свободного
места и Auto-Deploy Off; Terraform plan допускает только update Trading
Dashboard без удаления. После CI/merge — один ручной deploy. Проверить
целостность SQLite, завершение backfill, число и точные суммы closed PnL
независимым GET, свежесть основного потока и OTLP. Секреты не выводить.
