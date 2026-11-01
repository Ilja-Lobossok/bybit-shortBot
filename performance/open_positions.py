"""Observed open-position analytics only. No SDK, network, trading or state writes."""
from performance.contracts import decimal as D

ZERO = D(0)
GRAPH_LIMIT = 200
TOMBSTONE_MS = 180_000
FRESH_MS = 150_000
SAMPLE_BUCKET_MS = 300_000
SAMPLE_MAX_ROWS = 100_000


def money(row):
    unrealized = D(row['unrealisedPnl'])
    realized = D(row['curRealisedPnl']) if 'curRealisedPnl' in row else None
    return unrealized, realized, unrealized + realized if realized is not None else None


def summarize(rows):
    values = [money(row) for row in rows]
    complete = all(value[2] is not None for value in values)
    net = [value[2] for value in values] if complete else []
    losses = [value for value in net if value < 0]
    return {
        'count': len(rows), 'complete': int(complete),
        'unrealized': sum((v[0] for v in values), ZERO),
        'realized': sum((v[1] for v in values), ZERO) if complete else None,
        'combined': sum(net, ZERO) if complete else None,
        'positive': sum(v > 0 for v in net) if complete else None,
        'negative': len(losses) if complete else None,
        'breakeven': sum(v == 0 for v in net) if complete else None,
        'average_loss': sum(losses, ZERO) / len(losses) if losses else ZERO if complete else None,
        'worst_loss': min(net + [ZERO]) if complete else None,
        'best': max(net) if net else ZERO if complete else None,
    }


def record(store, rows, now):
    """Caller owns transaction. Complete snapshots only; never close on API failure."""
    previous_time = int(store.meta('position_risk_success_ms', 0))
    if now <= previous_time:
        return  # Replay/delayed observation cannot rewind a live run.
    active = {r['entity_key']: dict(r) for r in store.db.execute('SELECT * FROM position_runs WHERE closed_ms IS NULL')}
    seen = set()
    for value in rows:
        key = value['symbol'] + ':' + str(value['positionIdx'])
        direction = 'long' if value['side'] == 'Buy' else 'short'
        created = value.get('createdTime')
        old = active.get(key)
        # API-created time is a reported identity hint, never an order/trade label.
        replaced = old and (old['direction'] != direction or
                           (created is not None and old['source_created_ms'] is not None and created != old['source_created_ms']))
        if replaced:
            store.db.execute('UPDATE position_runs SET closed_ms=? WHERE id=?', (now, old['id']))
            old = None
        if old is None:
            cur = store.db.execute('INSERT INTO position_runs(entity_key,symbol,direction,source_created_ms,first_ms,last_ms) VALUES(?,?,?,?,?,?)',
                                   (key, value['symbol'], direction, created, now, now))
            old = dict(id=cur.lastrowid, min_pnl=None, max_pnl=None, last_ms=now, gaps=0)
        unrealized, realized, net = money(value)
        gap = now - old['last_ms'] > FRESH_MS or net is None
        minimum = min(D(old['min_pnl']), net) if old['min_pnl'] is not None and net is not None else net
        maximum = max(D(old['max_pnl']), net) if old['max_pnl'] is not None and net is not None else net
        # Preserve observed extrema if a later API row lacks realized evidence.
        minimum = old['min_pnl'] if net is None else str(minimum)
        maximum = old['max_pnl'] if net is None else str(maximum)
        store.db.execute('UPDATE position_runs SET last_ms=?,min_pnl=?,max_pnl=?,last_pnl=?,samples=samples+1,gaps=gaps+? WHERE id=?',
                         (now, minimum, maximum, str(net) if net is not None else None, int(gap), old['id']))
        store.db.execute('INSERT INTO position_samples VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,bucket_ms) DO UPDATE SET '
                         'observed_ms=excluded.observed_ms,unrealized=excluded.unrealized,realized=excluded.realized,combined=excluded.combined',
                         (old['id'], now // SAMPLE_BUCKET_MS * SAMPLE_BUCKET_MS, now, str(unrealized),
                          str(realized) if realized is not None else None, str(net) if net is not None else None))
        seen.add(key)
    for key, old in active.items():
        if key not in seen:
            store.db.execute('UPDATE position_runs SET closed_ms=? WHERE id=?', (now, old['id']))
    store.set_meta('position_risk_success_ms', now)


def prune(store, now):
    # Risk samples are explicitly bounded observation detail, not exchange facts.
    store.db.execute('DELETE FROM position_samples WHERE bucket_ms<?', (now - 7 * 86_400_000,))
    store.db.execute('DELETE FROM position_samples WHERE rowid IN (SELECT rowid FROM position_samples '
                     'ORDER BY bucket_ms DESC,run_id DESC LIMIT -1 OFFSET ?)', (SAMPLE_MAX_ROWS,))


def risk_points(store, rows, now):
    """Four bounded series per symbol/direction. Never label by a lifecycle/ID."""
    runs = [dict(r) for r in store.db.execute('SELECT * FROM position_runs WHERE closed_ms IS NULL ORDER BY symbol,direction')]
    admitted = runs[:GRAPH_LIMIT]
    by_key = {r['symbol'] + ':' + str(r['positionIdx']): r for r in rows}
    output = []
    for run in admitted:
        labels = dict(scope='account', currency='USDT', symbol=run['symbol'], direction=run['direction'])
        output.extend((name, value, labels) for name, value in (
            ('position_active', 1), ('position_observation_timestamp_seconds', run['last_ms'] / 1000),
            ('position_run_start_timestamp_seconds', run['first_ms'] / 1000)))
        row = by_key.get(run['entity_key'])
        if row and money(row)[2] is not None:
            output.append(('position_combined_pnl', float(money(row)[2]), labels))
    # A closed/reversed direction must stop drawing immediately after export.
    # Do not emit an old tombstone over a newer run with the same symbol/side.
    live_labels = {(r['symbol'], r['direction']) for r in runs}
    closed = store.db.execute('SELECT symbol,direction FROM position_runs WHERE closed_ms>=? GROUP BY symbol,direction LIMIT ?',
                              (now - TOMBSTONE_MS, GRAPH_LIMIT))
    for run in closed:
        if (run['symbol'], run['direction']) not in live_labels:
            output.append(('position_active', 0, dict(scope='account', currency='USDT', symbol=run['symbol'], direction=run['direction'])))
    minima = [D(r['min_pnl']) for r in runs if r['min_pnl'] is not None]
    losses = [min(value, ZERO) for value in minima]
    complete = len(minima) == len(runs)
    output.extend((name, float(value), dict(scope='account', currency='USDT')) for name, value in (
        ('position_worst_observed_loss', min(losses + [ZERO]) if complete else None),
        ('position_average_observed_loss', (sum(losses, ZERO) / len(losses) if losses else ZERO) if complete else None),
        ('position_graph_truncated', max(0, len(runs) - GRAPH_LIMIT)),
        ('position_observation_gaps', sum(r['gaps'] for r in runs)),
        ('position_risk_first_timestamp_seconds', min((r['first_ms'] for r in runs), default=now) / 1000)) if value is not None)
    return output
