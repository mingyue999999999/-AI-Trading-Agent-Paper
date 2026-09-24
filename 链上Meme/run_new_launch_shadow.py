"""Auditable discovery and paired paper-only threshold research."""
import copy
import json
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote
import new_launch_shadow as launch
import 外部信号复核 as external
import 链上Meme机器人 as base
import gate_diagnostics as diagnostics

REPORT_FILE = Path('链上Meme/候选复核报告.json')
REGISTRY_FILE = Path('链上Meme/候选跟踪.json')
EXPERIMENT_FILES = {
    'control_8pct': Path('链上Meme/流动性8对照账户.json'),
    'challenger_5pct': Path('链上Meme/流动性5对照账户.json'),
}
MAX_SCAN = 100
TTL = 72 * 3600
SCAN_SECONDS = 360

def save_report(report, path=REPORT_FILE):
    launch.save_account(report, path)

def load_registry():
    if not REGISTRY_FILE.exists():
        return {}
    value = json.loads(REGISTRY_FILE.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('invalid candidate registry; refusing silent reset')
    return value

def select_seeds(seeds, vendor_rows, registry, accounts, epoch):
    """Positions first; interleave persistent watches, new profiles and external discoveries."""
    registry = {k: dict(v) for k, v in registry.items() if epoch-v['first_seen'] <= TTL}
    fresh, vendor, held = [], [], []
    def add(chain, address, origin, target):
        if chain not in base.CHAINS or not isinstance(address, str) or not address:
            return
        key = f'{chain}:{address}'
        registry.setdefault(key, dict(chain=chain, address=address, first_seen=epoch, last_checked=0, sources=[]))
        registry[key]['sources'] = sorted(set(registry[key]['sources'] + [origin]))
        if key not in target:
            target.append(key)
    for account in accounts:
        for key, p in account['positions'].items():
            add(p['chain'], p['address'], 'held', held)
        for key, w in account['watchlist'].items():
            if epoch-w.get('last_seen', 0) <= TTL:
                chain, address = key.split(':', 1)
                add(chain, address, 'watchlist', [])
    for row in seeds:
        if isinstance(row,dict):
            add(row.get('chainId'), row.get('tokenAddress'), 'dex_profiles', fresh)
    for row in vendor_rows:
        add(row['chain'], row['address'], row['source'], vendor)
    persistent = sorted(registry, key=lambda k: (registry[k]['last_checked'], registry[k]['first_seen'], k))
    selected = list(held)
    for i in range(max(map(len, (persistent, fresh, vendor)), default=0)):
        for queue in (persistent, fresh, vendor):
            if i < len(queue) and queue[i] not in selected and len(selected) < MAX_SCAN:
                selected.append(queue[i])
    return selected, registry

def profile_coverage(seeds,selected):
    """Explain every raw profile before market gates, without changing selection.

    Counts are exclusive raw-record counts, NOT independent token or gate counts.
    A profile skipped here has unknown market/sellability risk, not a safety pass.
    """
    seen=set();selected=set(selected);rows=[]
    for index,row in enumerate(seeds):
        detail={'profile_index':index}
        if not isinstance(row,dict):
            detail['decision']='INVALID_PROFILE'
        else:
            chain,address=row.get('chainId'),row.get('tokenAddress')
            detail.update(chain=chain,address=address)
            if chain not in base.CHAINS:detail['decision']='UNSUPPORTED_CHAIN'
            elif not isinstance(address,str) or not address:detail['decision']='INVALID_ADDRESS'
            else:
                key=f'{chain}:{address}';detail['key']=key
                if key in seen:detail['decision']='DUPLICATE_PROFILE'
                else:
                    seen.add(key)
                    detail['decision']='SELECTED' if key in selected else 'DEFER_SELECTION_BUDGET'
        rows.append(detail)
    return {'raw_records':len(seeds),'supported_unique_tokens':len(seen),
            'decision_counts':dict(Counter(r['decision'] for r in rows)),
            'records':rows,'scope':'raw latest profiles only; excludes watchlist/held/external additions',
            'risk_note':'Unreviewed profiles have UNKNOWN risk; no historical claim.'}

def discover(accounts=None, registry=None):
    epoch = int(time.time())
    vendor_rows, source_status = external.load_optional_feeds(base.get_json)
    evidence_by_key = external.merge(vendor_rows)
    seeds = base.get_json('https://api.dexscreener.com/token-profiles/latest/v1')
    if not isinstance(seeds, list):
        raise ValueError('invalid latest profiles response')
    selected, registry = select_seeds(seeds, vendor_rows, registry or {}, accounts or [], epoch)
    out, challenge, reviews, prices, all_metrics = [], [], [], {}, {}
    for key in selected:
        meta = registry[key]
        row = dict(key=key, sources=meta['sources'], observed_at=base.now())
        if time.time()-epoch > SCAN_SECONDS:
            row['decision'] = 'DEFER_SCAN_TIME_BUDGET'
            reviews.append(row)
            continue
        try:
            rows = base.get_json('https://api.dexscreener.com/token-pairs/v1/' +
                                 quote(meta['chain'], safe='') + '/' + quote(meta['address'], safe=''))
            if not isinstance(rows, list):
                raise ValueError('invalid pair response')
            # The seed token must be the quoted base token, never an unrelated returned pair.
            matches = [p for p in rows if p.get('chainId') == meta['chain'] and
                       (p.get('baseToken') or {}).get('address') == meta['address']]
            pair = base.best_pair(matches)
            registry[key]['last_checked'] = int(time.time())
            if pair is None:
                row['decision'] = 'NO_VALID_PAIR'
                reviews.append(row)
                continue
            metrics = diagnostics.snapshot(pair, time.time())
            evidence = external.evaluate(evidence_by_key.get(key))
            gate = diagnostics.evaluate(metrics)
            gate5 = diagnostics.evaluate(metrics, min_liq_fdv=.05)
            row.update(symbol=(pair.get('baseToken') or {}).get('symbol'), pair=pair,
                       metrics=metrics, gate=gate, challenger_gate=gate5,
                       independent_gate=gate['passed'], external_evidence=evidence,
                       contract_risk='UNVERIFIED_SHADOW_ONLY')
            prices[key] = metrics['price']
            if all(diagnostics.finite(metrics[k]) for k in ('liquidity', 'buys', 'sells')):
                all_metrics[key] = metrics
            for threshold, target, g in ((.08, out, gate), (.05, challenge, gate5)):
                if g['passed'] and not evidence['blockers']:
                    score = launch.score_launch(metrics, threshold)
                    if score is None:
                        raise AssertionError('diagnostic and execution gates disagree')
                    target.append(dict(chain=meta['chain'], address=meta['address'], symbol=row['symbol'],
                                       metrics=metrics, independent_score=score,
                                       score=round(score+evidence['bonus'], 2), external_evidence=evidence))
            row['independent_score'] = launch.score_launch(metrics) if gate['passed'] else None
            row['decision'] = ('REJECT_INDEPENDENT_MARKET_GATE' if not gate['passed'] else
                               'REJECT_EXTERNAL_RISK_BLOCKER' if evidence['blockers'] else 'WATCH')
        except Exception as exc:
            row['decision'] = 'DATA_ERROR'
            row['error_type'] = type(exc).__name__
        reviews.append(row)
        time.sleep(.22)
    report = dict(time=base.now(), mode='PAPER_ONLY_RESEARCH', schema_version=3,
                  source_status=source_status, external_records=len(vendor_rows),
                  seed_count=len(seeds), registry_count=len(registry), selected=len(selected),
                  profile_coverage=profile_coverage(seeds,selected),
                  deferred_count=max(0,len(registry)-len(selected)), reviewed=len(reviews),
                  accepted_for_watch=len(out), accepted_5pct=len(challenge), reviews=reviews,
                  summary=diagnostics.summarize(reviews), challenger_summary=diagnostics.summarize(reviews,'challenger_gate'),
                  decision_counts=dict(Counter(r['decision'] for r in reviews)),
                  ratio_only_added=[r['key'] for r in reviews if r.get('challenger_gate',{}).get('passed')
                                    and not r.get('gate',{}).get('passed') and not r['external_evidence']['blockers']],
                  risk_note='Absent external evidence is UNKNOWN, not verified safe. Paper only.',
                  experiment_note='Independent matched 8% and 5% accounts use the same snapshots, score formula and costs; only liquidity/FDV entry gate differs.')
    return (sorted(out,key=lambda c:c['score'],reverse=True),
            sorted(challenge,key=lambda c:c['score'],reverse=True), report, registry, prices, all_metrics)

def account_summary(a, prices, previous_trades, epoch):
    missing = [k for k in a['positions'] if not diagnostics.finite(prices.get(k)) or prices[k] <= 0]
    eq = None if missing else launch.equity(a, prices)
    recent = a['trades'][previous_trades:]
    last = a['trades'][-1]['time'] if a['trades'] else None
    from datetime import datetime
    origin = last or a.get('created_at')
    idle = (epoch-datetime.fromisoformat(origin).timestamp())/3600 if origin else None
    invested = sum(p['invested_usdt'] for p in a['positions'].values())
    return dict(last_run_at=a.get('last_run_at'), cash=a['cash'], equity=eq,
                net_pnl=eq-a['initial_balance'] if eq is not None else None,
                return_pct=(eq/a['initial_balance']-1)*100 if eq is not None else None,
                realized_pnl=a['realized_pnl'], unrealized_pnl=eq-a['cash']-invested if eq is not None else None,
                fees_slippage=a['fees_slippage'], max_drawdown_pct=a['max_drawdown_pct'],
                positions=len(a['positions']), trades_total=len(a['trades']),
                new_buys=sum(t['side']=='BUY' for t in recent), new_sells=sum(t['side']=='SELL' for t in recent),
                last_trade_at=last, hours_without_trade=idle,
                inactivity_status='REVIEW_72H' if idle is not None and idle>=72 else
                                  'REVIEW_24H' if idle is not None and idle>=24 else 'OBSERVE',
                missing_prices=missing, entry_pause=a.get('entry_pause'),
                observation_decisions=dict(Counter(w.get('decision','UNKNOWN') for w in a['watchlist'].values())))

def main():
    if launch.LIVE_TRADING or base.LIVE_TRADING:
        raise SystemExit('PAPER ONLY safety stop')
    paths = {'original_shadow': launch.STATE_FILE, **EXPERIMENT_FILES}
    # Both experiment accounts must start together; never silently reconstruct a lost half.
    if len({p.exists() for p in EXPERIMENT_FILES.values()}) > 1:
        raise RuntimeError('paired experiment state incomplete; refusing reset')
    accounts = {name: launch.load_account(path) for name,path in paths.items()}
    epoch = int(time.time())
    for name,a in accounts.items():
        if name != 'original_shadow':
            a.setdefault('experiment', dict(id='liquidity_ratio_v1', started_at=base.now(),
                         threshold=.08 if name=='control_8pct' else .05, paper_only=True))
    c8,c5,report,registry,prices,all_metrics = discover(list(accounts.values()),load_registry())
    report['accounts'] = {}
    for name,a in accounts.items():
        before = len(a['trades'])
        candidates = c5 if name=='challenger_5pct' else c8
        missing = [k for k in a['positions'] if not diagnostics.finite(prices.get(k)) or prices[k]<=0]
        # Do not manufacture valuations or opens when an existing position has no fresh quote.
        if missing:
            a['last_attempt_at'] = base.now()
            a['entry_pause'] = 'MISSING_HELD_PRICES'
        else:
            if name != 'original_shadow':
                eq = launch.equity(a,prices)
                peak = max(a['peak_equity'],eq)
                if (peak-eq)/peak >= .05 or a.get('max_drawdown_pct',0)>=5:
                    a['experiment']['paused'] = True
                if a['experiment'].get('paused'):
                    candidates=[]
                    a['entry_pause']='EXPERIMENT_DRAWDOWN_5PCT_REVIEW_REQUIRED'
                else:
                    a.pop('entry_pause',None)
            else:
                a.pop('entry_pause',None)
            launch.run(a,copy.deepcopy(candidates),dict(prices),epoch,all_metrics)
        launch.save_account(a,paths[name])
        report['accounts'][name]=account_summary(a,prices,before,epoch)
    # Never leak a previous good mark through the summary when held prices fail.
    report['account_equity']=report['accounts']['original_shadow']['equity']
    report['positions']=len(accounts['original_shadow']['positions'])
    save_report(registry,REGISTRY_FILE)
    save_report(report)
    # Every observation survives the next latest-report overwrite.
    history=Path('链上Meme/诊断历史')/(report['time'][:10]+'.jsonl')
    history.parent.mkdir(parents=True,exist_ok=True)
    with history.open('a',encoding='utf-8') as f:
        baseline_path=Path('链上Meme/基准候选诊断.json')
        baseline=json.loads(baseline_path.read_text()) if baseline_path.exists() else None
        f.write(json.dumps(dict(shadow=report,baseline=baseline),ensure_ascii=False,allow_nan=False)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='reviews'},ensure_ascii=False,indent=2))

if __name__=='__main__':
    main()
