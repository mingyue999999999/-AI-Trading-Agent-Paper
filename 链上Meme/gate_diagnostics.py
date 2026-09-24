"""Explain every market gate using the exact snapshot, never infer missing data."""
import math
from collections import Counter

BASE = dict(price=(0, None), age_h=(2, 720), liquidity=(250000, None),
            volume24=(500000, None), fdv=(500000, 100000000), liq_fdv=(.05, None),
            transactions=(500, None), buy_sell_ratio=(.8, 2.5), h1=(-5, 25), h24=(-15, 100))
LAUNCH = dict(price=(0, None), age_h=(.25, 72), liquidity=(75000, None),
              volume24=(100000, None), fdv=(200000, 30000000), liq_fdv=(.08, None),
              transactions=(200, None), buy_sell_ratio=(.7, 3.5), h1=(-10, 35), h24=(-30, 200))

def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

def evaluate(metrics, profile='launch', min_liq_fdv=None):
    rules = dict(BASE if profile == 'base' else LAUNCH)
    if min_liq_fdv is not None:
        rules['liq_fdv'] = (min_liq_fdv, None)
    values = dict(metrics)
    buys, sells = values.get('buys'), values.get('sells')
    valid_flow = all(finite(v) and v >= 0 for v in (buys, sells))
    values['transactions'] = buys + sells if valid_flow else None
    values['buy_sell_ratio'] = buys / max(1, sells) if valid_flow else None
    checks = {}
    for key, (low, high) in rules.items():
        v = values.get(key)
        valid = finite(v)
        passed = valid and v >= low and (high is None or v <= high)
        if key == 'price':
            passed = valid and v > 0
        checks[key] = dict(value=v if valid else None, minimum=low, maximum=high,
                           passed=passed, missing_or_invalid=not valid,
                           below_by=max(0, low-v) if valid else None,
                           above_by=max(0, v-high) if valid and high is not None else None)
    failures = [k for k, c in checks.items() if not c['passed']]
    return dict(passed=not failures, failures=failures, checks=checks)

def snapshot(pair, epoch):
    def number(value):
        try:
            v = float(value)
            return v if math.isfinite(v) else None
        except (TypeError, ValueError):
            return None
    tx = (pair.get('txns') or {}).get('h24') or {}
    pc = pair.get('priceChange') or {}
    liq = number((pair.get('liquidity') or {}).get('usd'))
    fdv = number(pair.get('fdv') or pair.get('marketCap'))
    created = number(pair.get('pairCreatedAt'))
    return dict(price=number(pair.get('priceUsd')), liquidity=liq, fdv=fdv,
                volume24=number((pair.get('volume') or {}).get('h24')),
                buys=number(tx.get('buys')), sells=number(tx.get('sells')),
                h1=number(pc.get('h1')), h24=number(pc.get('h24')),
                age_h=(epoch-created/1000)/3600 if created and created > 0 else None,
                liq_fdv=liq/fdv if liq is not None and fdv and fdv > 0 else None)

def summarize(reviews, field='gate'):
    counts, sole = Counter(), Counter()
    evaluated = 0
    for row in reviews:
        gate = row.get(field)
        if gate is None:
            continue
        evaluated += 1
        counts.update(gate['failures'])
        if len(gate['failures']) == 1:
            sole.update(gate['failures'])
    return dict(evaluated=evaluated, failure_counts=dict(counts),
                sole_failure_counts=dict(sole),
                note='Failures overlap. Sole failures are NOT safe trade permissions; all risk gates still apply.')
