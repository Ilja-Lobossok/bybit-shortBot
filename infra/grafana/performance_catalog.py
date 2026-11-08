"""Metrics-only Trading Dashboard. Uses the existing Cloud Prometheus datasource."""
DS = {"type": "prometheus", "uid": "__METRICS_DATASOURCE_UID__"}
BASE = 'bot="bybit-shortbot",component="performance",environment="__ENVIRONMENT__",account_alias="${account}"'


def metric(name, labels=""):
    return 'bot_performance_' + name + '{' + BASE + (',' + labels if labels else '') + '}'


def current(name, labels=""):
    start = metric("process_start_timestamp_seconds")
    return '(' + metric(name, labels) + ' and on(job,instance) (' + start + ' == on() group_left() max(' + start + ')))'


def query(name, labels="", aggregate="max"):
    return aggregate + '(' + current(name, labels) + ')'


def panel(ident, title, expr, *, kind="stat", unit="short", description="", x=0, y=0, w=6, h=5):
    return {"id": ident, "title": title, "type": kind, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "description": description,
            "targets": [{"refId": "A", "expr": expr, "datasource": DS, "instant": kind != "timeseries", "range": kind == "timeseries", "legendFormat": "__auto"}],
            "fieldConfig": {"defaults": {"unit": unit, "noValue": "unknown / incomplete"}, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "legend": {"displayMode": "list", "placement": "bottom"}}}


def _legacy_dashboard():
    panels = []
    labels = 'scope="${scope}",window="${window}",currency="USDT"'
    account = 'scope="account",currency="USDT"'

    def add(title, name, *, selector="", unit="short", y, x, w=6, h=5, kind="stat", description=""):
        item = panel(len(panels) + 1, title, query(name, selector), kind=kind, unit=unit,
                     description=description, x=x, y=y, w=w, h=h)
        panels.append(item)
        return item

    add("History endpoint coverage (1 covered / 0 incomplete)", "history_complete", y=0, x=0,
        description="Coverage is not proof of lifetime completeness. All financial KPI describe reconciled cycles only.")
    add("Pending / incomplete records", "pending_records", y=0, x=6)
    add("Unattributed records (excluded from bot KPI)", "unattributed_records", y=0, x=12)
    age = add("Balance snapshot age", "balance_timestamp_seconds", unit="s", y=0, x=18)
    age["targets"][0]["expr"] = "time() - " + query("balance_timestamp_seconds")
    panels[0]["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": [{"value": None, "color": "red"}, {"value": 1, "color": "green"}]}
    for index in (1, 2):
        panels[index]["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": [{"value": None, "color": "green"}, {"value": 1, "color": "orange"}]}
    for index, (title, name) in enumerate((
        ("Account wallet balance (raw USDT)", "wallet_balance"),
        ("Account equity (USDT coin)", "equity"),
        ("Unrealized P&L (Bybit Mark, USDT)", "unrealized_pnl"),
        ("Bybit cumulative realized P&L (USDT coin)", "exchange_cumulative_realized_pnl"))):
        add(title, name, selector=account, unit="currencyUSD", y=5, x=index * 6,
            description="Actual Bybit coin snapshot. Wallet/equity belong to the whole account, not allocated bot capital. Historical equity begins with real collection.")
    for index, (title, name, unit) in enumerate((
        ("Net closed P&L (verified cycles / selected scope)", "net_closed_pnl", "currencyUSD"),
        ("Profitable cycles", "wins", "short"), ("Losing cycles", "losses", "short"),
        ("Reconciled cycles (N, includes breakeven)", "cycles", "short"))):
        add(title, name, selector=labels, unit=unit, y=10, x=index * 6,
            description="Fixed KPI window; only fully reconciled flat-to-flat cycles. Bot scope requires proven ownership. Transfers are not profit; net already includes fees/funding.")
    add("Win rate (verified cycles)", "win_rate", selector=labels, unit="percentunit", y=15, x=0)
    add("Profit factor (undefined if no losses)", "profit_factor", selector=labels, y=15, x=6,
        description="No invented finite PF. Check no-loss status below; N=0 gives unknown.")
    add("Open positions / selected scope", "positions", selector='scope="${scope}"', y=15, x=12,
        description="Complete Bybit position snapshot; bot is proven subset. A failed snapshot retains previous as-of and never means zero positions.")
    add("Selected KPI window covered (1 yes / 0 partial)", "kpi_window_complete", selector=labels, y=15, x=18,
        description="Time picker controls observed charts; KPI windows are all/UTC day/7d/30d. Unknown cycles stay excluded even if endpoints are covered.")
    curve = add("Account equity and wallet history (USDT)", "equity", selector=account,
                kind="timeseries", unit="currencyUSD", y=20, x=0, w=12, h=8,
                description="Actual ~60s observations, no reconstructed history. Deposits change balances and are not trading profit.")
    curve["fieldConfig"]["defaults"]["custom"] = {"axisSoftMin": 400, "axisSoftMax": 500}
    curve["targets"][0]["legendFormat"] = "Equity"
    curve["targets"].append({"refId": "B", "expr": query("wallet_balance", account), "datasource": DS,
                             "instant": False, "range": True, "legendFormat": "Wallet"})
    add("Cumulative net profit of verified closed cycles", "net_closed_pnl",
        selector='scope="${scope}",window="all",currency="USDT"', kind="timeseries",
        unit="currencyUSD", y=20, x=12, w=12, h=8,
        description="Covered journal, not lifetime account profit. Corrections can revise the gauge; fees are not deducted twice.")
    for index, (title, state) in enumerate((("Watchlist", "watching"), ("Pump candidates", "candidate"),
                                            ("Ready entry signals", "ready"), ("Blocked candidates", "blocked"))):
        add(title, "strategy_size", selector='state="' + state + '"', y=28, x=index * 6,
            description="Counts from the actual strategy pass, never a simulated second evaluation. Check strategy age; a waiting 5s loop is not a new candle.")
    add("Actual signal / skip decision counts", "decision_count", kind="timeseries", y=33, x=0, w=12, h=8,
        description="Persisted observed decisions by finite reason. Dropped observations are visible separately; SDK order_error is not proof of exchange rejection.")["targets"][0]["expr"] = query("decision_count", aggregate="sum by(reason)")
    add("Unrealized P&L history (account USDT)", "unrealized_pnl", selector=account,
        kind="timeseries", unit="currencyUSD", y=33, x=12, w=12, h=8)
    for index, (title, name, selector, unit) in enumerate((
        ("Average winning cycle", "average_win", labels, "currencyUSD"),
        ("Average losing cycle", "average_loss", labels, "currencyUSD"),
        ("Maximum observed equity drawdown (includes flows)", "max_drawdown_ratio", account, "percentunit"),
        ("No losses in verified sample (1 = PF undefined)", "profit_factor_no_losses", labels, "short"))):
        add(title, name, selector=selector, unit=unit, y=41, x=index * 6)
    for index, (title, name) in enumerate((("Account realized cash change (covered linear USDT)", "realized_cash_change"),
                                          ("Account trading fees (expense positive)", "fees"),
                                          ("Account funding (received positive)", "funding"),
                                          ("Account total equity (USD, separate currency)", "account_total_equity_usd"))):
        add(title, name, selector=account if index < 3 else 'scope="account",currency="USD"',
            unit="currencyUSD", y=46, x=index * 6, description="Separate account components, not amounts to subtract again from net closed P&L.")
    age = add("Strategy observation age", "strategy_timestamp_seconds", unit="s", y=51, x=0)
    age["targets"][0]["expr"] = "time() - " + query("strategy_timestamp_seconds")
    age = add("Position snapshot age", "positions_timestamp_seconds", unit="s", y=51, x=6)
    age["targets"][0]["expr"] = "time() - " + query("positions_timestamp_seconds")
    add("Stalling watchlist symbols", "strategy_size", selector='state="stalling"', y=51, x=12)
    add("Journal bytes including WAL", "storage_bytes", unit="bytes", y=51, x=18)
    for index, (title, name) in enumerate((("Performance storage degraded", "storage_degraded"),
                                          ("Performance errors", "errors_total"),
                                          ("Lost strategy observations", "dropped_events_total"))):
        add(title, name, y=56, x=index * 8, w=8)
    return {"uid": "bybit-trading-performance", "title": "Bybit ShortBot — Trading Dashboard", "schemaVersion": 39,
            "version": 1, "editable": True, "refresh": "1m", "timezone": "browser", "tags": ["bybit", "trading", "metrics-only", "as-code"],
            "time": {"from": "now-24h", "to": "now"}, "panels": panels,
            "templating": {"list": [
                {"name": "account", "type": "constant", "query": "__ACCOUNT_ALIAS__", "hide": 2},
                {"name": "scope", "type": "custom", "query": "bot,account", "current": {"text": "bot", "value": "bot"}},
                {"name": "window", "type": "custom", "query": "all,utc_day,7d,30d", "current": {"text": "all", "value": "all"}},
            ]}}


def all_panels(value):
    for item in value['panels']:
        yield item
        yield from all_panels(item) if item.get('panels') else ()


def dashboard():
    """User-facing portfolio first; closed KPIs/costs/diagnostics are collapsed."""
    result = _legacy_dashboard()
    old = {p['title']: p for p in result['panels']}
    visible = []
    account = 'scope="account",currency="USDT"'
    keys = 'on(job,instance,account_alias,symbol,direction)'

    def fresh(expr):
        # Never disguise a retained stale cache as current portfolio P&L.
        return '(' + expr + ') and on() (time() - ' + query('positions_timestamp_seconds') + ' < 150)'

    def add(ident, title, name, *, x=0, y=0, w=6, h=5, kind='stat', money=True, description=''):
        item = panel(ident, title, fresh(query(name, account)), x=x, y=y, w=w, h=h, kind=kind,
                     unit='suffix:USDT' if money else 'short', description=description)
        item['fieldConfig']['defaults'].update(decimals=2 if money else 0, noValue='Нет свежих данных')
        item['fieldConfig']['defaults']['color'] = {'mode': 'thresholds'} if money else {'mode': 'fixed', 'fixedColor': 'blue'}
        if money:
            item['fieldConfig']['defaults']['thresholds'] = {'mode': 'absolute', 'steps': [{'value': None, 'color': 'red'}, {'value': 0, 'color': 'green'}]}
        visible.append(item)
        return item

    def take(title, *, x, y, w=6, h=5, rename=None):
        item = old.pop(title)
        item['gridPos'] = dict(x=x, y=y, w=w, h=h)
        if rename:
            item['title'] = rename
        visible.append(item)
        return item

    for x, title, rename in ((0, 'Account wallet balance (raw USDT)', 'Баланс · USDT'),
                             (6, 'Account equity (USDT coin)', 'Equity · USDT')):
        item = take(title, x=x, y=0, rename=rename)
        item['fieldConfig']['defaults'].update(unit='suffix:USDT', decimals=2, noValue='Нет свежих данных')
        item['fieldConfig']['defaults']['color'] = {'mode': 'fixed', 'fixedColor': 'blue'}
        item['targets'][0]['expr'] = '(' + item['targets'][0]['expr'] + ') and on() (time() - ' + query('balance_timestamp_seconds') + ' < 150)'
    add(100, 'P&L открытых позиций · с реализованной частью', 'open_combined_pnl', x=12,
        description='Все открытые USDT-позиции субаккаунта: нереализованный P&L + уже отражённый реализованный P&L этих позиций. Комиссии повторно не вычитаются. Не история всей торговли и не отдельный накопитель Bybit TP/SL для всех фьючерсов; будущие расходы закрытия не включены.')
    item = add(101, 'Открытые позиции · весь субаккаунт', 'positions', x=18, money=False)
    item['targets'][0]['expr'] = fresh(query('positions', 'scope="account"'))
    add(102, 'Нереализованный P&L · Mark', 'open_unrealized_pnl', y=5)
    add(103, 'Реализованная часть открытых позиций', 'open_realized_pnl', x=6, y=5,
        description='Уже отражённые комиссии, funding и результат частичных закрытий текущих позиций. Это не только funding.')
    add(104, 'В плюсе', 'open_positive', x=12, y=5, w=3, money=False)
    add(105, 'В минусе', 'open_negative', x=15, y=5, w=3, money=False)
    add(106, 'Худший текущий убыток', 'open_worst_loss', x=18, y=5,
        description='Минимум текущего P&L с уже реализованной частью среди открытых позиций. Ноль, если убыточных нет.')
    add(107, 'Средний текущий убыток · только убыточные', 'open_average_loss', y=10,
        description='Среднее отрицательных результатов текущих позиций с уже реализованной частью. Не среднее всех позиций; ноль, если убыточных нет.')
    add(108, 'Худший наблюдавшийся убыток открытой позиции', 'position_worst_observed_loss', x=6, y=10,
        description='Минимум P&L с реализованной частью по минутным наблюдениям текущих циклов. История начинается с включения сбора; внутриминутные экстремумы и старая история неизвестны. Не просадка equity и не ROI на маржу.')
    age = take('Position snapshot age', x=12, y=10, rename='Возраст снимка позиций')
    age['fieldConfig']['defaults'].update(decimals=0, noValue='Нет данных')
    age['fieldConfig']['defaults']['thresholds'] = {'mode': 'absolute', 'steps': [{'value': None, 'color': 'green'}, {'value': 150, 'color': 'red'}]}
    started = add(110, 'Наблюдения текущих позиций с', 'position_risk_first_timestamp_seconds', x=18, y=10, money=False)
    started['targets'][0]['expr'] = '(' + started['targets'][0]['expr'] + ') * 1000'
    started['fieldConfig']['defaults']['unit'] = 'dateTimeAsIso'

    selection = account + ',symbol=~"${position_symbol:regex}"'
    expression = '(' + current('position_combined_pnl', selection) + ' and ' + keys + ' (' + current('position_active', selection) + ' == 1)' \
                 + ' and ' + keys + ' (time() - ' + current('position_observation_timestamp_seconds', selection) + ' < 150)' \
                 + ' and ' + keys + ' (time() - ' + current('position_run_start_timestamp_seconds', selection) + ' >= 60))'
    graph = add(111, 'Открытые позиции · реализованный + нереализованный P&L', 'open_combined_pnl', y=15, w=24, h=10, kind='timeseries',
                description='Отдельная линия на символ/направление, все открытые USDT-позиции субаккаунта. Легенда скрыта; наведите на линию. Фильтр Все / Худшие 10 и выбор символов сверху. Новые наблюдаемые циклы получают минутный разрыв, закрытые линии заканчиваются. При пропущенном между снимками закрытии/повторном входе идентичность может быть неполной; старые и внутриминутные данные не восстанавливаются. Не точная копия накопителя Bybit All Futures TP/SL.')
    graph['targets'][0].update(expr='bottomk(${position_limit}, ' + expression + ')', legendFormat='{{symbol}} · {{direction}}')
    graph['options'].update(legend={'showLegend': False, 'displayMode': 'hidden', 'placement': 'bottom'}, tooltip={'mode': 'single', 'sort': 'none'})
    graph['fieldConfig']['defaults']['custom'] = {'drawStyle': 'line', 'lineWidth': 1, 'fillOpacity': 0, 'spanNulls': False,
                                                 'showPoints': 'never', 'thresholdsStyle': {'mode': 'line'}}
    graph['fieldConfig']['defaults']['color'] = {'mode': 'palette-classic'}
    graph['fieldConfig']['defaults']['thresholds'] = {'mode': 'absolute', 'steps': [{'value': None, 'color': 'red'}, {'value': 0, 'color': 'green'}]}
    take('Account equity and wallet history (USDT)', x=0, y=25, w=12, h=8)
    add(112, 'P&L текущего набора открытых позиций · история наблюдений', 'open_combined_pnl', x=12, y=25, w=12, h=8, kind='timeseries',
        description='Сумма меняется при открытии и закрытии позиций. Это не накопленная прибыль всей торговли; закрытая позиция выходит из текущего набора.')
    for x, title, rename in ((0, 'Watchlist', 'Watchlist'), (6, 'Pump candidates', 'Pump-кандидаты'),
                             (12, 'Ready entry signals', 'Сигналы на вход'), (18, 'Blocked candidates', 'Пропущенные кандидаты')):
        take(title, x=x, y=33, rename=rename)

    close_labels = 'scope="${scope}",window="${window}",currency="USDT"'
    close_source = '(' + query('closed_history_timestamp_seconds') + ' or on() ' + query('coverage_end_timestamp_seconds') + ')'
    close_fresh = '(time() - ' + close_source + ' < 1800)'
    close_description = ('Источник — уникальные закрытые ордера Bybit closedPnl, включая убытки. '
                         'Частичные выходы разными ордерами считаются отдельно; это не число полных циклов. '
                         'Комиссии и funding уже включены. Scope account — весь субаккаунт; bot — только доказанные ID. '
                         'Период выбирается отдельно сверху: all / UTC day / 7d / 30d; даты графика не меняют этот период. '
                         'История до начала загрузки неизвестна. Проверяйте полноту загрузки.')
    for index, (title, name, unit) in enumerate((
        ('Закрытые ордера Bybit', 'closed_orders', 'short'),
        ('Закрытия в плюс', 'closed_order_wins', 'short'),
        ('Закрытия в минус', 'closed_order_losses', 'short'),
        ('Результат закрытий · уже после расходов', 'closed_order_net_pnl', 'suffix:USDT'),
        ('Win rate · закрытые ордера', 'closed_order_win_rate', 'percentunit'),
        ('Profit factor · закрытые ордера', 'closed_order_profit_factor', 'short'),
        ('Средний закрытый плюс', 'closed_order_average_win', 'suffix:USDT'),
        ('Средний закрытый минус', 'closed_order_average_loss', 'suffix:USDT'),
    )):
        item = panel(121 + index, title, '(' + query(name, close_labels) + ') and on() ' + close_fresh, unit=unit,
                     x=index % 4 * 6, y=38 + index // 4 * 5, description=close_description)
        item['fieldConfig']['defaults'].update(decimals=2, noValue='Нет выборки / показатель не определён')
        if unit == 'suffix:USDT':
            item['fieldConfig']['defaults']['color'] = {'mode': 'thresholds'}
            item['fieldConfig']['defaults']['thresholds'] = {'mode': 'absolute', 'steps': [{'value': None, 'color': 'red'}, {'value': 0, 'color': 'green'}]}
        visible.append(item)
    history = panel(129, 'Закрытия Bybit · полнота и свежесть', '(' + query('closed_order_history_complete', close_labels) + ') * on() (time() - ' + close_source + ' < bool 1800)',
                    x=0, y=48, description=close_description)
    history['fieldConfig']['defaults']['mappings'] = [{'type': 'value', 'options': {
        '0': {'text': 'Загрузка / неполный период', 'color': 'orange'},
        '1': {'text': 'Период загружен', 'color': 'green'}}}]
    visible.append(history)
    since = panel(130, 'История закрытий запрашивается с', '(' + query('closed_order_history_start_timestamp_seconds') + ') * 1000',
                  unit='dateTimeAsIso', x=6, y=48, description=close_description)
    visible.append(since)
    close_curve = panel(131, 'Накопленный результат закрытий Bybit · с начала экспорта',
                        '(' + query('closed_order_net_pnl', 'scope="${scope}",window="all",currency="USDT"') + ') and on() ' + close_fresh,
                        kind='timeseries', unit='suffix:USDT', x=12, y=48, w=12, h=8,
                        description=close_description + ' График содержит только реальные OTLP-наблюдения. '
                        'Загрузка старой истории корректирует сумму сейчас, но не создаёт вымышленные точки в прошлом.')
    close_curve['targets'][0]['legendFormat'] = 'Bybit closedPnl'
    visible.append(close_curve)

    def row(ident, title, children, y):
        for index, item in enumerate(children):
            item['gridPos'] = dict(x=index % 4 * 6, y=y + 1 + index // 4 * 5, w=6, h=5)
            item['fieldConfig']['defaults']['noValue'] = 'Недостаточно подтверждённых данных'
        visible.append(dict(id=ident, title=title, type='row', collapsed=True, gridPos=dict(x=0, y=y, w=24, h=1), panels=children))

    status = panel(113, 'Статистика закрытий · статус', query('closed_statistics_status', 'scope="${scope}",window="${window}",currency="USDT"'))
    status['fieldConfig']['defaults']['mappings'] = [{'type': 'value', 'options': {
        '0': {'text': 'История неполная', 'color': 'orange'}, '1': {'text': 'Нет подтверждённых закрытий', 'color': 'blue'},
        '2': {'text': 'Подтверждённая выборка', 'color': 'green'}}}]
    closed_titles = ('Net closed P&L (verified cycles / selected scope)', 'Profitable cycles', 'Losing cycles',
                     'Reconciled cycles (N, includes breakeven)', 'Win rate (verified cycles)',
                     'Profit factor (undefined if no losses)', 'Average winning cycle', 'Average losing cycle',
                     'No losses in verified sample (1 = PF undefined)')
    closed = [status] + [old.pop(title) for title in closed_titles]
    row(190, 'Полные циклы · строгая сверка (отдельно от закрытых ордеров)', closed, 56)

    costs = [old.pop(title) for title in ('Account trading fees (expense positive)', 'Account funding (received positive)',
                                        'Account realized cash change (covered linear USDT)')]
    cashflow = panel(114, 'Результат изменения цены · подтверждённый период', query('realized_price_change', account), unit='suffix:USDT')
    costs.append(cashflow)
    for ident, name, title in ((115, 'coverage_start_timestamp_seconds', 'Подтверждённый период с'),
                               (116, 'coverage_end_timestamp_seconds', 'Подтверждённый период по')):
        costs.append(panel(ident, title, '(' + query(name) + ') * 1000', unit='dateTimeAsIso'))
    completeness = panel(117, 'Разбивка расходов · полнота', query('cost_breakdown_complete', account))
    completeness['fieldConfig']['defaults']['mappings'] = [{'type': 'value', 'options': {
        '0': {'text': 'Неполная история / классификация', 'color': 'orange'}, '1': {'text': 'Период сверён', 'color': 'green'}}}]
    costs.append(completeness)
    for item in costs:
        item['description'] = 'Только подтверждённый период журнала, не вся жизнь текущих позиций. Funding: плюс — получение, минус — уплата. Комиссии: плюс — расход. Уже входят в реализованные результаты; повторно не вычитайте их из P&L.'
    row(191, 'Комиссии и funding · за подтверждённый период', costs, 57)
    diagnostics = list(old.values())
    for ident, title, name in ((118, 'Не попало на график · лимит 200', 'position_graph_truncated'),
                               (119, 'Разрывы наблюдения текущих позиций', 'position_observation_gaps'),
                               (120, 'Полнота текущего P&L', 'open_money_complete')):
        diagnostics.append(panel(ident, title, query(name, account)))
    diagnostics.extend([
        panel(134, 'SQLite · занятые страницы / бюджет', query('storage_used_bytes') + ' / ' + query('storage_budget_bytes'), unit='percentunit'),
        panel(135, 'SQLite · выделено с WAL', query('storage_bytes'), unit='bytes'),
        panel(136, 'Подробные наблюдения · лимит 20 000', query('storage_detail_rows')),
        panel(137, 'ID наблюдений · лимит 50 000', query('storage_dedup_rows')),
        panel(138, 'Отставание полной сверки', 'time() - ' + query('coverage_end_timestamp_seconds'), unit='s'),
        panel(139, 'Отставание истории закрытий', 'time() - ' + close_source, unit='s'),
    ])
    row(192, 'Диагностика данных · сверка, атрибуция и сбор', diagnostics, 58)
    result['panels'] = visible
    # A retained financial cache is not a current account sample. Preserve the
    # timestamp of the actual collection at every historical evaluation step.
    for item in all_panels(result):
        if item['title'] == 'Account equity and wallet history (USDT)':
            for target in item['targets']:
                target['expr'] = '(' + target['expr'] + ') and on() (time() - ' + query('balance_timestamp_seconds') + ' < 150)'
        if item['gridPos']['y'] >= 38:
            item['gridPos']['y'] += 3
    good_snapshots = ('(time() - ' + query('balance_timestamp_seconds') + ' < bool 150) * on() '
                      '(time() - ' + query('positions_timestamp_seconds') + ' < bool 150) * on() '
                      '(' + query('storage_degraded') + ' == bool 0)')
    status_expr = '((' + good_snapshots + ') * on() (1 + ((time() - ' + query('coverage_end_timestamp_seconds') + ' < bool 1800) or on() vector(0)))) or on() vector(0)'
    status_panel = panel(132, 'Мониторинг · свежесть и запись', status_expr, x=0, y=38, w=24, h=3,
                         description='0: снимки старше 150s, ошибка записи или нет данных. 1: свежие снимки, история догружается. 2: свежие снимки и сверка не старше 30m. Полнота старого периода не доказывает текущую свежесть.')
    status_panel['fieldConfig']['defaults']['mappings'] = [{'type':'value','options':{
        '0':{'text':'Снимки устарели / ошибка записи','color':'red'},
        '1':{'text':'Снимки свежие · история догружается','color':'orange'},
        '2':{'text':'Данные свежие','color':'green'}}}]
    result['panels'].append(status_panel)
    variables = result['templating']['list']
    variables[1].update(label='Закрытия: scope', current={'text': 'account', 'value': 'account'})
    variables[2].update(label='Закрытия: период', current={'text': 'all', 'value': 'all'})
    variables.extend([
        {'name': 'position_symbol', 'label': 'Позиции: символы', 'type': 'query', 'datasource': DS, 'refresh': 1,
         'query': 'label_values(' + metric('position_active', account) + ', symbol)', 'multi': True,
         'includeAll': True, 'allValue': '.*', 'current': {'text': 'All', 'value': '$__all'}, 'sort': 1},
        {'name': 'position_limit', 'label': 'График позиций', 'type': 'custom', 'query': 'Все : 200,Худшие 10 : 10',
         'current': {'text': 'Все', 'value': '200'}},
    ])
    return result
