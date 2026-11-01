"""Real PromQL checks for closed positions, fresh samples and re-entry gaps."""
import performance_catalog


def scenarios(series, scenario):
    from build import resolve
    graph = next(p for p in performance_catalog.dashboard()['panels'] if p['id'] == 111)
    expr = resolve(graph['targets'][0]['expr'])
    common = dict(component='performance', account_alias='primary', job='bybit-performance')
    labels = dict(common, scope='account', currency='USDT', symbol='TESTUSDT', direction='short')
    inputs = [
        series('bot_performance_process_start_timestamp_seconds', [0] * 9, **common),
        series('bot_performance_position_combined_pnl', [5, 5, 5, 7, 7, 7, 7, 7, 9], **labels),
        series('bot_performance_position_active', [1, 1, 0, 1, 1, 1, 1, 1, 1], **labels),
        series('bot_performance_position_observation_timestamp_seconds', [0, 60, 120, 180, 240, 240, 240, 240, 480], **labels),
        series('bot_performance_position_run_start_timestamp_seconds', [0, 0, 0, 180, 180, 180, 180, 180, 180], **labels),
    ]
    tests = [{'expr': 'sum(' + expr + ') or vector(0)', 'eval_time': str(minute) + 'm',
              'exp_samples': [{'labels': '{}', 'value': value}]}
             for minute, value in ((0, 0), (1, 5), (2, 0), (3, 0), (4, 7), (7, 0), (8, 9))]
    return [scenario('position graph: first observation, close, re-entry, stale cache and recovery', inputs, expressions=tests)]
