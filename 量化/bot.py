#!/usr/bin/env python3
"""Regime-adaptive, long-only, paper trading bot. Python 3.11+ stdlib only."""
from __future__ import annotations
import argparse, json, math, os, statistics, sys, time, urllib.parse, urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT.parent) not in sys.path: sys.path.insert(0, str(ROOT.parent))
from execution_quality import assess_okx_orderbook, record_execution_ab, settle_execution_ab

SOURCE_EVENTS=[]
SOURCE_BLOCKS={}
SOURCE_SKIP_REPORTED=set()

def validated_candles(rows):
    if not rows:raise ValueError("empty candle response")
    seen=set()
    for t,o,h,l,c,v,end in rows:
        if (t in seen or end<t or any(not math.isfinite(float(x)) for x in (o,h,l,c,v))
                or min(o,h,l,c)<=0 or v<0 or h<max(o,c,l) or l>min(o,c,h)):
            raise ValueError("invalid or duplicate candle")
        seen.add(t)
    return sorted(rows,key=lambda x:x[0])

def source_event(symbol,source,error=None):
    SOURCE_EVENTS.append({"symbol":symbol,"source":source,"status":"ERROR" if error else "OK",
        "observed_at":datetime.now(timezone.utc).isoformat(),"reason":str(error) if error else None})

def source_access_blocked(error):
    return getattr(error,"code",None) in (403,451)

def block_source_for_run(source,error):
    if source_access_blocked(error):
        SOURCE_BLOCKS[source]=str(error)

def source_is_blocked(symbol,source):
    reason=SOURCE_BLOCKS.get(source)
    if reason is None:return False
    key=(symbol,source)
    if key not in SOURCE_SKIP_REPORTED:
        SOURCE_EVENTS.append({"symbol":symbol,"source":source,"status":"SKIPPED",
            "observed_at":datetime.now(timezone.utc).isoformat(),
            "reason":"process cooldown after permanent access error: "+reason})
        SOURCE_SKIP_REPORTED.add(key)
    return True

def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default

def save_json(path, data):
    p=Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    tmp=p.with_suffix(p.suffix+".tmp")
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
    os.replace(tmp,p)

def sma(x,n): return sum(x[-n:])/n if len(x)>=n else None
def ema(x,n):
    if len(x)<n:return None
    v=sum(x[:n])/n; a=2/(n+1)
    for z in x[n:]:v=a*z+(1-a)*v
    return v
def returns(x): return [x[i]/x[i-1]-1 for i in range(1,len(x)) if x[i-1]]
def stdev(x,n): return statistics.pstdev(x[-n:]) if len(x)>=n else None
def rsi(x,n=14):
    if len(x)<=n:return None
    d=[x[i]-x[i-1] for i in range(1,len(x))][-n:]
    g=sum(max(z,0) for z in d)/n; l=sum(max(-z,0) for z in d)/n
    return 100 if l==0 else 100-100/(1+g/l)
def atr(b,n=14):
    if len(b)<=n:return None
    tr=[max(b[i][2]-b[i][3],abs(b[i][2]-b[i-1][4]),abs(b[i][3]-b[i-1][4])) for i in range(1,len(b))]
    return sum(tr[-n:])/n
def clamp(x,a,b): return max(a,min(b,x))

def fetch_klines(symbol, interval="4h", limit=500, end_time=None):
    q={"symbol":symbol,"interval":interval,"limit":min(limit,1000)}
    if end_time:q["endTime"]=int(end_time)
    url="https://api.binance.com/api/v3/klines?"+urllib.parse.urlencode(q)
    req=urllib.request.Request(url,headers={"User-Agent":"RobustQuantBot/1.0"})
    if not source_is_blocked(symbol,"Binance"):
        try:
            with urllib.request.urlopen(req,timeout=25) as r:data=json.load(r)
            rows=validated_candles([[int(z[0]),float(z[1]),float(z[2]),float(z[3]),float(z[4]),float(z[5]),int(z[6])] for z in data])
            source_event(symbol,"Binance"); return rows
        except Exception as e:
            source_event(symbol,"Binance",e)
            block_source_for_run("Binance",e)
    # GitHub Actions 的部分出口会被 Binance HTTP 451 限制，自动切换 Bybit 现货K线。
    bybit_interval={"1h":"60","4h":"240","1d":"D"}.get(interval,interval)
    q={"category":"spot","symbol":symbol,"interval":bybit_interval,"limit":min(limit,1000)}
    if end_time:q["end"]=int(end_time)
    url="https://api.bybit.com/v5/market/kline?"+urllib.parse.urlencode(q)
    req=urllib.request.Request(url,headers={"User-Agent":"RobustQuantBot/1.0"})
    if not source_is_blocked(symbol,"Bybit"):
        try:
            with urllib.request.urlopen(req,timeout=25) as r:payload=json.load(r)
            if payload.get("retCode")!=0:raise ValueError("Bybit API error: "+str(payload.get("retCode")))
            rows=payload.get("result",{}).get("list",[])
            interval_ms={"1h":3600000,"4h":14400000,"1d":86400000}.get(interval,14400000)
            out=[[int(z[0]),float(z[1]),float(z[2]),float(z[3]),float(z[4]),float(z[5]),int(z[0])+interval_ms-1] for z in rows]
            out=validated_candles(out); source_event(symbol,"Bybit"); return out
        except Exception as e:
            source_event(symbol,"Bybit",e)
            block_source_for_run("Bybit",e)
    # Bybit 某些云出口也可能返回 403；OKX 作为第二备用源。
    inst=symbol[:-4]+"-USDT" if symbol.endswith("USDT") else symbol
    okx_bar={"1h":"1H","4h":"4H","1d":"1Dutc"}.get(interval,"4H")
    q={"instId":inst,"bar":okx_bar,"limit":min(limit,100)}
    if end_time:q["after"]=int(end_time)
    url="https://www.okx.com/api/v5/market/history-candles?"+urllib.parse.urlencode(q)
    req=urllib.request.Request(url,headers={"User-Agent":"RobustQuantBot/1.0"})
    with urllib.request.urlopen(req,timeout=25) as r:payload=json.load(r)
    if str(payload.get("code"))!="0":raise ValueError("OKX API error: "+str(payload.get("code")))
    rows=payload.get("data",[])
    interval_ms={"1h":3600000,"4h":14400000,"1d":86400000}.get(interval,14400000)
    out=[[int(z[0]),float(z[1]),float(z[2]),float(z[3]),float(z[4]),float(z[5]),int(z[0])+interval_ms-1] for z in rows]
    out=validated_candles(out); source_event(symbol,"OKX"); return out

def fetch_history(symbol, interval, bars):
    out=[]; end=None
    while len(out)<bars:
        part=fetch_klines(symbol,interval,min(1000,bars-len(out)),end)
        if not part:break
        if end is not None and part[0][0]>end:raise ValueError("non-advancing candle history")
        out=part+out; end=part[0][0]-1
        if len(part)<2:break
        time.sleep(.08)
    uniq={z[0]:z for z in out}
    return [uniq[k] for k in sorted(uniq)][-bars:]

def features(b):
    c=[z[4] for z in b]; v=[z[5] for z in b]; a=atr(b); e20=ema(c,20); e50=ema(c,50); e200=ema(c,200)
    rv=stdev(returns(c),20) or 0
    trend=0 if not all((e20,e50,e200,a)) else abs(e20-e50)/a
    if rv>.055: regime="HIGH_VOL"
    elif e20>e50>e200 and trend>.7: regime="BULL_TREND"
    elif e20<e50<e200 and trend>.7: regime="BEAR_TREND"
    else: regime="RANGE"
    hi=max(z[2] for z in b[-21:-1]); lo=min(z[3] for z in b[-21:-1]); rr=rsi(c)
    vol_ratio=v[-1]/(sma(v[:-1],20) or v[-1] or 1)
    mom20=c[-1]/c[-21]-1 if len(c)>21 else 0
    mom90=c[-1]/c[-91]-1 if len(c)>91 else 0
    z=(c[-1]-(sma(c,20) or c[-1]))/((stdev(c,20) or c[-1]*.01))
    return {"regime":regime,"atr":a or c[-1]*.03,"rsi":rr or 50,"breakout":c[-1]>hi,
            "breakdown":c[-1]<lo,"vol_ratio":vol_ratio,"mom20":mom20,"mom90":mom90,"z":z,
            "ema20":e20 or c[-1],"ema50":e50 or c[-1],"close":c[-1],"rv":rv}

def signal(f, relative_momentum=0.0):
    # Weighted ensemble changes by regime; score in [-1,1].
    trend=0.0
    if f["breakout"] and f["vol_ratio"]>1.05: trend=.85
    elif f["close"]>f["ema20"]>f["ema50"]: trend=.50
    elif f["close"]<f["ema20"]<f["ema50"]: trend=-.70
    mean=clamp(-f["z"]/2.5,-1,1) if f["rsi"]<42 or f["rsi"]>65 else 0
    momentum=clamp(.65*f["mom20"]/.10+.35*f["mom90"]/.25,-1,1)
    momentum=clamp(momentum+.2*relative_momentum,-1,1)
    weights={"BULL_TREND":(.55,.05,.40),"BEAR_TREND":(.65,.05,.30),"RANGE":(.15,.65,.20),"HIGH_VOL":(.20,.10,.20)}[f["regime"]]
    s=weights[0]*trend+weights[1]*mean+weights[2]*momentum
    if f["regime"] in ("BEAR_TREND","HIGH_VOL"):s-=.22
    return clamp(s,-1,1),{"trend":trend,"mean_reversion":mean,"momentum":momentum}

def new_account(c):
    now=datetime.now(timezone.utc).isoformat()
    return {"version":"1.0","created_at":now,"cash":c["initial_cash"],"positions":{},"trades":[],
            "equity_curve":[],"peak_equity":c["initial_cash"],"day_start_equity":c["initial_cash"],"day":now[:10],"last_bar":{}}
def exposure(a,prices):
    """Missing marks are unknown, never a zero-return position at its entry."""
    try:
        values=[float(prices[s]) for s in a["positions"]]
        if any(not math.isfinite(px) or px<=0 for px in values):return None
        return sum(p["qty"]*float(prices[s]) for s,p in a["positions"].items())
    except (KeyError,TypeError,ValueError):return None
def equity(a, prices):
    value=exposure(a,prices)
    return None if value is None else a["cash"]+value
def costs(value,c):return value*c["fee_rate"]

def record_equity(a,prices,initial,timestamp_ms=None):
    """Persist the mark-to-market snapshot and its high-water mark together."""
    eq=equity(a,prices)
    if eq is None:return None
    a["peak_equity"]=max(float(a.get("peak_equity",initial)),eq)
    a["equity_curve"].append([int(time.time()*1000) if timestamp_ms is None else int(timestamp_ms),eq])
    return eq

def portfolio_risk_state(a,c,prices,now):
    """Update daily/high-water state and evaluate portfolio circuit breakers."""
    eq=equity(a,prices)
    # Unknown portfolio valuation blocks entries, but not a known-price stop.
    if eq is None:return False,None
    a["peak_equity"]=max(float(a.get("peak_equity",c["initial_cash"])),eq)
    day=datetime.fromtimestamp(now/1000,timezone.utc).date().isoformat()
    if a["day"]!=day:a["day"],a["day_start_equity"]=day,eq
    dd=1-eq/a["peak_equity"] if a["peak_equity"] else 0
    locked=dd>=c["max_portfolio_drawdown"] or eq/a["day_start_equity"]-1<=-c["daily_loss_limit"]
    return locked,eq

def close_position(a,c,symbol,price,now,bar_key,reason,source):
    """Book one conservative PAPER market exit at an actually observed price."""
    p=a["positions"][symbol]; sell_px=float(price)*(1-c["slippage_bps"]/10000)
    gross=p["qty"]*sell_px; fee=costs(gross,c); pnl=gross-fee-p["cost"]
    a["cash"]+=gross-fee; del a["positions"][symbol]
    a["trades"].append({"time":int(now),"decision_bar_epoch_ms":int(bar_key),"symbol":symbol,"side":"SELL",
        "price":sell_px,"qty":p["qty"],"fee":fee,"pnl":pnl,"reason":reason,
        "execution_price_source":source})
    return {"symbol":symbol,"action":"SELL:"+reason,"execution_epoch_ms":int(now),
            "decision_bar_epoch_ms":int(bar_key),"execution_price_source":source}

def process_live_position(a,c,symbol,bars,prices,execution_price,execution_time,decision_bar):
    """Check existing PAPER positions on every fresh order-book observation.

    Entry signals remain tied to confirmed bars.  This path uses no historical
    OHLC range for exits, so a pre-entry high/low cannot trigger a paper fill.
    """
    p=a["positions"].get(symbol)
    if not p:return None
    px=float(execution_price); now=int(execution_time); bar_key=int(decision_bar)
    locked,_=portfolio_risk_state(a,c,{**prices,symbol:px},now)
    if px<=float(p["stop"]):reason="ATR_STOP"
    elif px>=float(p["target"]):reason="TAKE_PROFIT"
    elif locked:reason="CIRCUIT_BREAKER"
    else:reason=None
    if reason:
        return close_position(a,c,symbol,px,now,bar_key,reason,"live_orderbook_mid")
    # A new high raises the trail for the next observation; it can never lower it.
    if not bars or len(bars)<220:
        return {"symbol":symbol,"action":"HOLD:RISK_CHECKED_NO_ATR","execution_epoch_ms":now,
                "decision_bar_epoch_ms":bar_key,"observed_price":px,
                "reason":"History unavailable: fixed stop/target checked; trailing update deferred"}
    f=features(bars); old_stop=float(p["stop"])
    p["highest"]=max(float(p["highest"]),px)
    p["stop"]=max(old_stop,p["highest"]-c["atr_trail_multiple"]*f["atr"])
    return {"symbol":symbol,"action":"HOLD:RISK_CHECKED","execution_epoch_ms":now,
            "decision_bar_epoch_ms":bar_key,"execution_price_source":"live_orderbook_mid",
            "observed_price":px,"stop":p["stop"],"target":p["target"]}

def process_bar(a,c,symbol,bars,prices,relative=0,execution_price=None,execution_time=None,decision_bar=None,live=False):
    # Backtests use the next bar open. Live paper runs act only after the latest bar
    # closes and therefore must fill at a contemporaneous observable price.
    signal_bars=bars if live else bars[:-1]
    f=features(signal_bars); sig,parts=signal(f,relative)
    observed_bar=bars[-1]
    px=float(execution_price) if live else observed_bar[1]
    now=int(execution_time) if live else observed_bar[0]
    bar_key=int(decision_bar) if decision_bar is not None else observed_bar[0]
    out={"symbol":symbol,"signal":sig,"regime":f["regime"],"action":"HOLD","parts":parts,
         "decision_bar_epoch_ms":bar_key,"execution_epoch_ms":now,
         "execution_price_source":"live_orderbook_mid" if live else "next_bar_open"}
    locked,eq=portfolio_risk_state(a,c,{**prices,symbol:px},now)
    p=a["positions"].get(symbol)
    if p:
        # The stop carried into this bar was executable for the entire bar. A
        # higher trailing stop derived from this bar's high only becomes active
        # for the next bar; otherwise OHLC ordering creates look-ahead bias.
        old_stop=p["stop"]
        reason=None
        if not live and observed_bar[3]<=old_stop: reason="ATR_STOP"
        elif not live and observed_bar[2]>=p["target"]: reason="TAKE_PROFIT"
        elif sig<-.35: reason="SIGNAL_EXIT"
        elif locked: reason="CIRCUIT_BREAKER"
        if reason:
            if not live and reason=="ATR_STOP":
                sell_px=min(observed_bar[1],old_stop)*(1-c["slippage_bps"]/10000)
            elif not live and reason=="TAKE_PROFIT":
                sell_px=p["target"]*(1-c["slippage_bps"]/10000)
            else:
                sell_px=px*(1-c["slippage_bps"]/10000)
            gross=p["qty"]*sell_px; fee=costs(gross,c); pnl=gross-fee-p["cost"]
            a["cash"]+=gross-fee; del a["positions"][symbol]
            a["trades"].append({"time":now,"decision_bar_epoch_ms":bar_key,"symbol":symbol,"side":"SELL",
                "price":sell_px,"qty":p["qty"],"fee":fee,"pnl":pnl,"reason":reason,
                "execution_price_source":out["execution_price_source"]})
            out["action"]="SELL:"+reason
        elif not live:
            p["highest"]=max(p["highest"],observed_bar[2])
            p["stop"]=max(old_stop,p["highest"]-c["atr_trail_multiple"]*f["atr"])
    elif eq is None:
        out["action"]="SKIP_PORTFOLIO_VALUATION"
    elif not locked and sig>=c["min_score"] and len(a["positions"])<c["max_positions"]:
        risk=eq*c["risk_per_trade"]; stop_dist=max(c["atr_stop_multiple"]*f["atr"],px*.015)
        value=min(risk/stop_dist*px,eq*c["max_position_pct"],eq*c["max_total_exposure"]-exposure(a,prices),a["cash"]/(1+c["fee_rate"]))
        # High correlation proxy: reduce new crypto risk as portfolio fills.
        value*=max(.55,1-.12*len(a["positions"]))
        if value>=c["min_order_usdt"]:
            buy_px=px*(1+c["slippage_bps"]/10000); qty=value/buy_px; fee=costs(value,c)
            a["cash"]-=value+fee; risk_unit=stop_dist
            a["positions"][symbol]={"qty":qty,"entry":buy_px,"cost":value+fee,"stop":buy_px-stop_dist,
                "target":buy_px+c["take_profit_r"]*risk_unit,"highest":buy_px,"entry_time":now,
                "decision_bar_epoch_ms":bar_key,"signal":sig}
            a["trades"].append({"time":now,"decision_bar_epoch_ms":bar_key,"symbol":symbol,"side":"BUY",
                "price":buy_px,"qty":qty,"fee":fee,"signal":sig,"regime":f["regime"],
                "execution_price_source":out["execution_price_source"]})
            out["action"]="BUY"
    a["last_bar"][symbol]=bar_key
    return out

def metrics(a,prices,initial):
    eq=equity(a,prices); sells=[t for t in a["trades"] if t["side"]=="SELL"]; pn=[t["pnl"] for t in sells]
    gp=sum(x for x in pn if x>0); gl=-sum(x for x in pn if x<0); curve=[x[1] for x in a["equity_curve"]]
    peak=initial; mdd=0
    for x in curve:peak=max(peak,x);mdd=max(mdd,1-x/peak)
    return {"equity":round(eq,2) if eq is not None else None,
      "net_return_pct":round((eq/initial-1)*100,2) if eq is not None else None,"closed_trades":len(sells),
      "win_rate_pct":round(100*sum(x>0 for x in pn)/len(pn),2) if pn else 0,"profit_factor":round(gp/gl,3) if gl else (999 if gp else 0),
      "max_drawdown_pct":round(mdd*100,2),"drawdown_basis":"sampled_valid_marks_only",
      "valuation_status":"OK" if eq is not None else "UNAVAILABLE",
      "fees_paid":round(sum(t.get("fee",0) for t in a["trades"]),2),"open_positions":len(a["positions"])}

def execution_quote_usable(gate):
    """Recheck the existing book's 30-second validity at consumption time."""
    try:
        px=float(gate["reference_mid"]); ts=float(gate["price_epoch_ms"])
        age=time.time()*1000-ts
        return (bool(gate.get("available")) and math.isfinite(px) and px>0
                and math.isfinite(ts) and -2000<=age<=30000)
    except (KeyError,TypeError,ValueError):
        return False

def paper(c):
    SOURCE_EVENTS.clear()
    path=ROOT/c["state_file"]
    missing=object(); a=load_json(path,missing)
    if a is missing:
        a=new_account(c)
    else:
        # Never reinterpret a corrupt/empty existing account as first startup.
        # Fail before fetching prices or writing any account/report files.
        required={"created_at","cash","positions","trades","equity_curve",
                  "peak_equity","day_start_equity","day","last_bar"}
        if (not isinstance(a,dict) or not required.issubset(a)
                or not isinstance(a["positions"],dict) or not isinstance(a["trades"],list)
                or not isinstance(a["equity_curve"],list) or not isinstance(a["last_bar"],dict)):
            raise ValueError("Invalid existing paper account; refusing to reset history")
    data={}; prices={}; gates={}; data_errors=[]
    symbols=list(dict.fromkeys([*a["positions"],*c["symbols"]]))
    for s in symbols:
        try:
            b=fetch_history(s,c["timeframe"],c["history_bars"]); now=int(time.time()*1000)
            b=[x for x in b if x[6]<now]
            if len(b)<220:raise ValueError("insufficient closed history")
            data[s]=b
        except Exception as e:
            data_errors.append({"symbol":s,"reason":str(e)})
    moms={s:features(b)["mom20"] for s,b in data.items()}; med=statistics.median(moms.values()) if moms else 0; results=[]
    # Capture all observable execution prices before making any portfolio decision.
    for s in symbols:
        symbol=s.removesuffix("USDT")
        gate=assess_okx_orderbook(symbol,"BUY","SPOT")
        gates[s]=gate
        if execution_quote_usable(gate):
            prices[s]=gate["reference_mid"]
    exited=set()
    for s in symbols:
        b=data.get(s,[]); bar_key=b[-1][0] if b else a["last_bar"].get(s,0)
        gate=gates[s]
        # Earlier symbols' books may age while another API call is slow. Never
        # replay their prices as current fills; attempt one fresh public quote.
        if not execution_quote_usable(gate):
            gate=assess_okx_orderbook(s.removesuffix("USDT"),"BUY","SPOT")
            gates[s]=gate
        if not execution_quote_usable(gate):
            prices.pop(s,None)
            results.append({"symbol":s,"action":"SKIP_RISK_DATA" if s in a["positions"] else "SKIP_EXECUTION_DATA",
                            "decision_bar_epoch_ms":bar_key,
                            "reason":"No fresh executable book: "+str(gate.get("reason","stale or invalid quote"))})
            continue
        prices[s]=gate["reference_mid"]
        # An earlier quote can expire while a later symbol is fetched. Do not
        # use it to size entries or to infer a portfolio circuit breaker.
        for held in list(a["positions"]):
            if not execution_quote_usable(gates.get(held,{})):prices.pop(held,None)
        if s in a["positions"]:
            if not gate.get("available") or gate.get("reference_mid",0)<=0:
                results.append({"symbol":s,"action":"SKIP_RISK_DATA","decision_bar_epoch_ms":b[-1][0],
                                "reason":gate.get("reason","live orderbook unavailable")})
            else:
                risk=process_live_position(a,c,s,b,prices,gate["reference_mid"],
                                           gate["price_epoch_ms"],bar_key)
                results.append(risk)
                if risk["action"].startswith("SELL:"):exited.add(s)
        if len(b)<220 or a["last_bar"].get(s)==bar_key or s in exited:continue
        if not gate.get("available") or gate.get("reference_mid",0)<=0:
            results.append({"symbol":s,"action":"SKIP_EXECUTION_DATA","decision_bar_epoch_ms":b[-1][0],
                            "reason":gate.get("reason","live orderbook unavailable")})
            continue
        observed_ms=int(datetime.now(timezone.utc).timestamp()*1000)
        decision=process_bar(a,c,s,b,prices,clamp((moms[s]-med)/.15,-1,1),
                             execution_price=gate["reference_mid"],execution_time=observed_ms,
                             decision_bar=b[-1][0],live=True)
        results.append(decision)
        if decision["action"]=="BUY":
            symbol=s.removesuffix("USDT")
            record_execution_ab(a,symbol,"BUY",gate["reference_mid"],decision["signal"],gate)
            a["execution_ab_signals"][-1].update({"decision_bar_epoch_ms":b[-1][0],
                "observation_note":"Paper fill uses the contemporaneous OKX orderbook mid after the decision bar closed; not a historical bar-open fill."})
    snapshot_ms=int(time.time()*1000)
    for held in list(a["positions"]):
        if not execution_quote_usable(gates.get(held,{})):prices.pop(held,None)
    record_equity(a,prices,c["initial_cash"],timestamp_ms=snapshot_ms)
    report={"time":datetime.fromtimestamp(snapshot_ms/1000,timezone.utc).isoformat(),
            "metrics":metrics(a,prices,c["initial_cash"]),"decisions":results,"research_errors":[],
            "data_errors":data_errors,"data_source_events":list(SOURCE_EVENTS),
            "valuation_missing_symbols":[s for s in a["positions"] if s not in prices],
            "price_observations":{s:{"price":prices.get(s),"price_epoch_ms":g.get("price_epoch_ms"),
                "source":"OKX:book_mid","valid_at_snapshot":execution_quote_usable(g)} for s,g in gates.items()}}
    # Financial checkpoint precedes optional research. If a process is killed
    # during label retrieval, the completed risk actions are not lost locally.
    save_json(path,a)
    save_json(ROOT/c["report_file"],report)
    # Shadow outcome collection cannot delay a later symbol's stop/target check
    # or make the financial snapshot appear newer than its market observation.
    for s in data:
        try: settle_execution_ab(a,s.removesuffix("USDT"))
        except Exception as e:
            report["research_errors"].append({"symbol":s,"reason":str(e)})
    save_json(path,a)
    save_json(ROOT/c["report_file"],report); print(json.dumps(report,ensure_ascii=False,indent=2))

def backtest(c,days):
    bars=max(300,min(5000,int(days*6)+220)); data={s:fetch_history(s,c["timeframe"],bars) for s in c["symbols"]}; n=min(map(len,data.values()))
    data={s:v[-n:] for s,v in data.items()}; a=new_account(c); prices={}
    for i in range(220,n):
        moms={s:features(v[:i])["mom20"] for s,v in data.items()}; med=statistics.median(moms.values())
        for s,v in data.items():
            prices[s]=v[i][1]; process_bar(a,c,s,v[:i+1],prices,clamp((moms[s]-med)/.15,-1,1))
        record_equity(a,prices,c["initial_cash"],data[c["symbols"][0]][i][0])
    report={"bars":n-220,"metrics":metrics(a,prices,c["initial_cash"]),"note":"单次历史回测仅用于排错，不能证明未来收益"}
    save_json(ROOT/"state/backtest_report.json",report); print(json.dumps(report,ensure_ascii=False,indent=2))

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("command",choices=["paper","backtest","status"]); ap.add_argument("--days",type=int,default=730); z=ap.parse_args()
    c=load_json(ROOT/"config.json");
    if c.get("mode")!="paper":raise SystemExit("安全锁：V1 只允许 paper 模式")
    if z.command=="paper":paper(c)
    elif z.command=="backtest":backtest(c,z.days)
    else:print(json.dumps(load_json(ROOT/c["report_file"],{"status":"尚未运行"}),ensure_ascii=False,indent=2))
if __name__=="__main__":main()
