"""Bounded read-only data access and honest PAPER account snapshots.

No order endpoints, credentials, portfolio thresholds, or strategy signals here.
Missing prices block valuation, not protective exits with a valid local quote.
"""
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from execution_quality import assess_okx_orderbook, okx

_blocked_until={}


def bounded_http_get(url, headers=None, timeout=25, retries=4, budget_seconds=12):
    """Honour Retry-After inside an explicit per-request time budget.

    A denied/rate-limited host is suspended only in this process, not forever.
    Source errors stay errors; no cached price or successful-looking empty data.
    Slow historical sources may use 90s AFTER the early risk checkpoint. Public
    execution quotes keep their own short timeout and consume-time freshness.
    """
    host=urllib.parse.urlsplit(url).netloc
    if not math.isfinite(budget_seconds) or not 0<budget_seconds<=90:
        raise ValueError('invalid request budget')
    deadline=time.monotonic()+budget_seconds
    cooldown=_blocked_until.get(host,0)-time.monotonic()
    if cooldown>0:
        if cooldown>=deadline-time.monotonic():
            raise RuntimeError('data source in cooldown: '+host)
        time.sleep(cooldown)
    for attempt in range(min(int(retries),2)+1):
        remaining=deadline-time.monotonic()
        if remaining<=0:raise TimeoutError('data request budget exhausted: '+host)
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'PAPER-runtime/1.0','Accept':'application/json,text/csv,*/*',**(headers or {})})
            with urllib.request.urlopen(req,timeout=min(timeout,8,remaining)) as response:
                return response.read().decode('utf-8',errors='replace')
        except HTTPError as exc:
            if exc.code in (401,403,451):
                _blocked_until[host]=time.monotonic()+300
                raise
            if exc.code!=429 and not 500<=exc.code<600:raise
            value=exc.headers.get('Retry-After') if exc.headers else None
            try:wait=max(0,float(value)) if value else 2**attempt
            except (TypeError,ValueError):
                try:wait=max(0,parsedate_to_datetime(value).timestamp()-time.time())
                except (TypeError,ValueError,OverflowError):wait=2**attempt
            if exc.code==429:_blocked_until[host]=time.monotonic()+wait
            if attempt>=min(int(retries),2) or wait>=deadline-time.monotonic():raise
        except (OSError,TimeoutError):
            wait=2**attempt
            if attempt>=min(int(retries),2) or wait>=deadline-time.monotonic():raise
        time.sleep(wait)
    raise RuntimeError('unreachable request state')


def valid_price(value):
    try:return not isinstance(value,bool) and math.isfinite(float(value)) and float(value)>0
    except (TypeError,ValueError):return False


def fresh(observation,price_key='reference_mid',now_ms=None):
    try:
        now_ms=time.time()*1000 if now_ms is None else now_ms
        return (observation.get('available') is True and valid_price(observation.get(price_key))
                and -2000<=now_ms-float(observation['price_epoch_ms'])<=30000)
    except (KeyError,TypeError,ValueError):return False


def execution_quote(symbol,side,market):
    observation=assess_okx_orderbook(symbol,side,market)
    if not fresh(observation):raise ValueError('no fresh execution quote: '+symbol)
    return observation


def mark_quote(symbol):
    row=okx('/api/v5/public/mark-price',instType='SWAP',instId=symbol+'-USDT-SWAP')[0]
    result={'available':True,'reference_mid':float(row['markPx']),
            'price_epoch_ms':int(row['ts']),'source':'OKX:mark-price'}
    if not fresh(result):raise ValueError('no fresh mark price: '+symbol)
    return result


def account_equity(account,prices,futures=False):
    if any(not valid_price(prices.get(s)) for s in account['positions']):return None
    eq=float(account['cash'])
    for symbol,p in account['positions'].items():
        price=float(prices[symbol]);qty=float(p['quantity'])
        if futures:
            direction=1 if p['side']=='LONG' else -1
            eq+=float(p['margin'])+direction*qty*(price-float(p['entry_price']))
        else:eq+=qty*price
    return eq


def snapshot(account,observations,futures=False,now_ms=None):
    now_ms=int(time.time()*1000) if now_ms is None else int(now_ms)
    marks={s:float(q['reference_mid']) for s,q in observations.items() if fresh(q,now_ms=now_ms)}
    eq=account_equity(account,marks,futures)
    stamp=datetime.fromtimestamp(now_ms/1000,timezone.utc).isoformat()
    missing=[s for s in account['positions'] if s not in marks]
    meta={'schema_version':1,'valuation_at':stamp,'valuation_epoch_ms':now_ms,
          'valuation_status':'OK' if eq is not None else 'UNAVAILABLE',
          'missing_symbols':missing,'price_observations':observations,
          'equity':eq,'not_realtime':True}
    if eq is not None:
        account['peak_equity']=max(float(account.get('peak_equity',10000)),eq)
        dd=max(0,1-eq/account['peak_equity']) if account['peak_equity'] else 0
        account.setdefault('drawdown_observed_since',stamp)
        account['max_observed_drawdown']=max(float(account.get('max_observed_drawdown',0)),dd)
        meta.update(current_drawdown=dd,max_observed_drawdown=account['max_observed_drawdown'],
                    drawdown_basis='valid discrete snapshots; not full historical/tick maximum')
    account['last_equity']=eq
    account['runtime']=meta
    return eq,marks


def format_amount(value):return 'UNKNOWN' if value is None else f'{value:,.2f}'


def finish_labels(account,save):
    """Optional bounded research after the financial checkpoint, never before."""
    from execution_quality import settle_execution_ab
    deadline=time.monotonic()+8
    errors=[]
    for symbol in sorted({s['symbol'] for s in account.get('execution_ab_signals',[])}):
        if time.monotonic()>=deadline:break
        try:settle_execution_ab(account,symbol,max_requests=4,deadline=deadline)
        except Exception as exc:errors.append({'symbol':symbol,'reason':str(exc)})
    account.setdefault('runtime',{})['research_errors']=errors
    save(account)
