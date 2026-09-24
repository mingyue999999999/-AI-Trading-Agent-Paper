import csv
import io
import json
import math
import os
import sys
import random
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError
from datetime import datetime, timezone

ROOT_DIR=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path: sys.path.insert(0,ROOT_DIR)
from execution_quality import assess_okx_orderbook, record_execution_ab, settle_execution_ab
import paper_runtime as runtime

# ============================================================
# AI TRADING AGENT V3.3 FULL - PAPER ONLY
# 修复版：降低 CoinGecko 429；Bybit 失败后整轮切换 OKX；
# OI 快照保留；免费 ETF 源失败即 N/A；缺失维度自动重归一化。
# ============================================================

VERSION = "现货机器人正式版 + 订单流影子A/B"
LIVE_TRADING = False
ORDERFLOW_ENFORCEMENT = False
INITIAL_BALANCE = 10000.0
STATE_FILE = "现货/模拟账户.json"
OI_STATE_FILE = "现货/持仓量记录.json"

FEE_RATE = 0.001
MAX_POSITION_PCT = 0.20
MAX_TOTAL_EXPOSURE = 0.50
RISK_PER_TRADE = 0.01
STOP_LOSS_PCT = 0.06
TAKE_PROFIT_PCT = 0.15
TRAILING_STOP_PCT = 0.07
MIN_TRADE_USDT = 50.0
MAX_DRAWDOWN_PCT = 0.15
BUY_CONFIDENCE = 35.0
SELL_CONFIDENCE = -30.0

COINS = {
    "bitcoin": "BTC", "ethereum": "ETH", "ripple": "XRP",
    "solana": "SOL", "binancecoin": "BNB",
}
SYMBOLS = list(COINS.values())

WEIGHTS = {
    "technical": .25, "derivatives": .15, "macro": .15,
    "etf": .15, "flow": .12, "stablecoin": .08, "sentiment": .10,
}

COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY", "").strip()
FREE_ETF_BASE = "https://xoomar.com/api/markets/etf-flows"
BYBIT_DISABLED = False
CG_LAST_CALL = 0.0

def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def http_get(url, headers=None, timeout=25, retries=4):
    budget=90 if urllib.parse.urlsplit(url).hostname=='api.coingecko.com' else 12
    return runtime.bounded_http_get(url,headers,timeout,retries,budget_seconds=budget)


def get_json(url, headers=None, retries=4):
    return json.loads(http_get(url, headers=headers, retries=retries))

def safe_json(url, headers=None, retries=1, quiet=False):
    try:
        return get_json(url, headers=headers, retries=retries)
    except Exception as e:
        if not quiet: print("接口降级：", url.split("?")[0], "|", e)
        return None

# ---------------- CoinGecko ----------------
def cg_json(url, retries=5):
    global CG_LAST_CALL
    gap = time.time() - CG_LAST_CALL
    if gap < 6.0: time.sleep(6.0 - gap)
    try:
        return get_json(url, retries=retries)
    finally:
        CG_LAST_CALL = time.time()

def get_market_data():
    ids = ",".join(COINS.keys())
    return cg_json("https://api.coingecko.com/api/v3/coins/markets?"
                   + urllib.parse.urlencode({"vs_currency":"usd","ids":ids,
                                             "price_change_percentage":"24h"}))

def get_history(coin_id):
    d = cg_json(f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart?"
                + urllib.parse.urlencode({"vs_currency":"usd","days":365,"interval":"daily"}))
    return [float(x[1]) for x in d["prices"]], [float(x[1]) for x in d["total_volumes"]]

# ---------------- Technical ----------------
def ema_series(v, p):
    if len(v) < p: return []
    m=2/(p+1); cur=sum(v[:p])/p; out=[cur]
    for x in v[p:]:
        cur=(x-cur)*m+cur; out.append(cur)
    return out

def ema(v,p):
    x=ema_series(v,p); return x[-1] if x else None

def rsi(v,p=14):
    if len(v)<=p:return None
    g=[]; l=[]
    for i in range(1,len(v)):
        c=v[i]-v[i-1]; g.append(max(c,0)); l.append(max(-c,0))
    ag=sum(g[-p:])/p; al=sum(l[-p:])/p
    return 100.0 if al==0 else 100-100/(1+ag/al)

def macd(v):
    if len(v)<35:return None,None,None
    f=ema_series(v,12); s=ema_series(v,26); f=f[-len(s):]
    line=[a-b for a,b in zip(f,s)]; sig=ema_series(line,9)
    return (line[-1],sig[-1],line[-1]-sig[-1]) if sig else (None,None,None)

def bollinger(v,p=20):
    if len(v)<p:return None,None,None
    x=v[-p:]; mid=sum(x)/p; sd=math.sqrt(sum((z-mid)**2 for z in x)/p)
    return mid-2*sd,mid,mid+2*sd

def volatility(v,p=14):
    if len(v)<=p:return None
    x=[abs(v[i]/v[i-1]-1) for i in range(1,len(v)) if v[i-1]]
    return sum(x[-p:])/p*100 if x else None

def momentum(v,d):
    return (v[-1]/v[-d-1]-1)*100 if len(v)>d and v[-d-1] else None

def volume_change(v):
    if len(v)<8:return None
    a=sum(v[-8:-1])/7
    return (v[-1]/a-1)*100 if a else 0

def support_resistance(v,d=30):
    x=v[-d:]; return min(x),max(x)

def market_regime(price,prices):
    a,b,c=ema(prices,20),ema(prices,50),ema(prices,200)
    vol=volatility(prices)
    if None in (a,b,c):return "UNKNOWN"
    if price>a>b>c:return "BULL_TREND"
    if price<a<b<c:return "BEAR_TREND"
    if vol is not None and vol>=4:return "HIGH_VOLATILITY"
    return "RANGE"

def technical_dimension(price,ch,prices,volumes):
    pts=0.; mx=0.; reasons=[]
    e20,e50,e200=ema(prices,20),ema(prices,50),ema(prices,200)
    rv=rsi(prices); mv,sv,hist=macd(prices); lo,mid,hi=bollinger(prices)
    m7,m30=momentum(prices,7),momentum(prices,30); vc=volume_change(volumes)
    vol=volatility(prices); regime=market_regime(price,prices)
    if e20 and e50:
        mx+=2
        if price>e20>e50:pts+=2; reasons.append("价格 > EMA20 > EMA50")
        elif price<e20<e50:pts-=2; reasons.append("价格 < EMA20 < EMA50")
    if e200:
        mx+=2; pts += 2 if price>e200 else -2
        reasons.append("价格在EMA200"+("上方" if price>e200 else "下方"))
    if rv is not None:
        mx+=2
        if 50<=rv<68:pts+=1
        elif rv>=75:pts-=2
        elif rv<=30:pts+=1
        elif rv<45:pts-=1
        reasons.append(f"RSI={rv:.1f}")
    if mv is not None and sv is not None:
        mx+=2; pts += 1 if mv>sv else -1; pts += 1 if hist>0 else -1
        reasons.append("MACD "+("偏强" if mv>sv else "偏弱"))
    if m7 is not None:
        mx+=1; pts += 1 if m7>3 else (-1 if m7<-3 else 0)
    if m30 is not None:
        mx+=1; pts += 1 if m30>5 else (-1 if m30<-5 else 0)
    if mid is not None: mx+=1; pts += 1 if price>mid else -1
    if vc is not None:
        mx+=1
        if vc>20: pts += 1 if ch>0 else (-1 if ch<0 else 0)
    mx+=2
    if regime=="BULL_TREND":pts+=2
    elif regime=="BEAR_TREND":pts-=2
    reasons.append("Regime="+regime)
    detail={"ema20":e20,"ema50":e50,"ema200":e200,"rsi":rv,
            "macd":mv,"macd_signal":sv,"macd_hist":hist,
            "bollinger_lower":lo,"bollinger_middle":mid,"bollinger_upper":hi,
            "momentum7":m7,"momentum30":m30,"volume_change":vc,
            "volatility":vol,"regime":regime}
    return (100*pts/mx if mx else None),reasons,detail

# ---------------- Derivatives ----------------
def load_oi_snapshots(account):
    # GitHub runner is ephemeral. Keep OI baselines inside paper_account.json,
    # which the production workflow already commits after every run.
    try:
        x = account.get("_oi_snapshots", {})
        return x if isinstance(x, dict) else {}
    except Exception:
        return {}

def save_oi_snapshots(account,x):
    try:
        account["_oi_snapshots"] = x
    except Exception as e:
        print("OI快照保存失败：", e)

def get_derivatives_bybit(symbol):
    global BYBIT_DISABLED
    if BYBIT_DISABLED:return None
    pair=f"{symbol}USDT"
    url="https://api.bybit.com/v5/market/tickers?"+urllib.parse.urlencode({"category":"linear","symbol":pair})
    try:
        d=get_json(url,retries=0); row=d["result"]["list"][0]
        return {"funding_rate":float(row["fundingRate"])*100,
                "open_interest":float(row["openInterest"]),
                "oi_change_pct":None,"source":"Bybit","status":"OK"}
    except HTTPError as e:
        if e.code in (403,451):
            BYBIT_DISABLED=True
            print(f"Bybit HTTP {e.code}：本轮后续币种直接使用 OKX")
        return None
    except Exception:
        return None

def get_derivatives_okx(symbol,account):
    inst=f"{symbol}-USDT-SWAP"
    out={"funding_rate":None,"open_interest":None,"oi_change_pct":None,
         "source":"OKX","status":"N/A","funding_kind":None}

    # Prefer the current announced funding rate. Historical realizedRate is only
    # a fallback; this avoids presenting the last settled rate as the live rate.
    d=safe_json("https://www.okx.com/api/v5/public/funding-rate?"
                +urllib.parse.urlencode({"instId":inst}),retries=1)
    try:
        row=d["data"][0]
        rate=row.get("fundingRate")
        if rate not in (None, ""):
            out["funding_rate"]=float(rate)*100
            out["funding_kind"]="current"
    except Exception:
        pass

    if out["funding_rate"] is None:
        d=safe_json("https://www.okx.com/api/v5/public/funding-rate-history?"
                    +urllib.parse.urlencode({"instId":inst,"limit":1}),retries=1)
        try:
            row=d["data"][0]; rate=row.get("realizedRate") or row.get("fundingRate")
            if rate not in (None, ""):
                out["funding_rate"]=float(rate)*100
                out["funding_kind"]="last_realized"
        except Exception:
            pass

    # Reject impossible/corrupt values rather than allowing them into scoring.
    if out["funding_rate"] is not None and not (-5.0 <= out["funding_rate"] <= 5.0):
        print(f"{symbol} Funding异常值已忽略：{out['funding_rate']}")
        out["funding_rate"]=None

    d=safe_json("https://www.okx.com/api/v5/public/open-interest?"
                +urllib.parse.urlencode({"instType":"SWAP","instId":inst}),retries=1)
    try:
        row=d["data"][0]; cur=float(row.get("oiUsd") or row["oi"])
        out["open_interest"]=cur; snaps=load_oi_snapshots(account); prev=snaps.get(symbol)
        ts=int(time.time())
        if prev and float(prev.get("oi",0))>0:
            out["oi_change_pct"]=(cur/float(prev["oi"])-1)*100
            out["oi_change_window_h"]=max(0,(ts-int(prev.get("ts",ts)))/3600)
        snaps[symbol]={"oi":cur,"ts":ts}; save_oi_snapshots(account,snaps)
    except Exception:
        pass
    if out["funding_rate"] is not None or out["open_interest"] is not None:
        out["status"]="FALLBACK"
    return out

def get_derivatives(symbol,account):
    d=get_derivatives_bybit(symbol)
    return d if d and d.get("status")=="OK" else get_derivatives_okx(symbol,account)

def derivatives_dimension(ch,d):
    vals=[]; reasons=[]; f=d.get("funding_rate"); o=d.get("oi_change_pct")
    if f is not None:
        x=-80 if f>=.10 else -40 if f>=.05 else 35 if f<=-.05 else 0
        vals.append(x); reasons.append(f"{d['source']} Funding={f:+.4f}%" + (" (当前)" if d.get("funding_kind")=="current" else " (最近结算)" if d.get("funding_kind")=="last_realized" else ""))
    if o is not None:
        x=55 if o>=2 and ch>0 else -55 if o>=2 and ch<0 else 15 if o<=-2 and ch<0 else -10 if o<=-2 and ch>0 else 0
        vals.append(x); w=d.get("oi_change_window_h")
        reasons.append(f"{d['source']} OI较上次运行"+(f"({w:.1f}h)" if w is not None else "")+f"={o:+.2f}%")
    if not vals and d.get("open_interest") is not None:
        vals.append(0); reasons.append(f"{d['source']} OI当前值可用，但无变化基线")
    return (sum(vals)/len(vals) if vals else None),reasons

# ---------------- Macro ----------------
def fred_recent(s,limit=12):
    try:
        text=http_get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={s}",retries=2)
        rows=list(csv.reader(io.StringIO(text))); vals=[]
        for r in rows[1:]:
            if len(r)>=2 and r[1] not in ("","."):
                try:vals.append((r[0],float(r[1])))
                except:pass
        return vals[-limit:]
    except Exception as e:
        print("FRED降级：",s,e); return []

def get_macro():
    return {"dgs2":fred_recent("DGS2",12),"dgs10":fred_recent("DGS10",12),
            "fedfunds":fred_recent("FEDFUNDS",6),"cpi":fred_recent("CPIAUCSL",14)}

def pct_change(a,b):return (b/a-1)*100 if a else None

def macro_dimension(m):
    vals=[]; reasons=[]
    for key,label,mult in (("dgs2","美债2Y",120),("dgs10","美债10Y",100)):
        d=m[key]
        if len(d)>=2:
            delta=d[-1][1]-d[0][1]; vals.append(max(-100,min(100,-delta*mult)))
            reasons.append(f"{label} {d[-1][1]:.2f}% / 近期变化 {delta:+.2f}pct")
    ff=m["fedfunds"]
    if len(ff)>=2:
        delta=ff[-1][1]-ff[-2][1]; vals.append(max(-100,min(100,-delta*80)))
        reasons.append(f"Fed Funds {ff[-1][1]:.2f}% / 月变 {delta:+.2f}pct")
    c=m["cpi"]
    if len(c)>=14:
        yn=pct_change(c[-13][1],c[-1][1]); yp=pct_change(c[-14][1],c[-2][1])
        if yn is not None and yp is not None:
            delta=yn-yp; vals.append(max(-100,min(100,-delta*35)))
            reasons.append(f"CPI同比约 {yn:.2f}% / 变化 {delta:+.2f}pct")
    return (sum(vals)/len(vals) if vals else None),reasons

# ---------------- ETF / Flow ----------------
def coinglass_json(path,params=None):
    if not COINGLASS_API_KEY:return None
    qs="?"+urllib.parse.urlencode(params) if params else ""
    return safe_json("https://open-api-v4.coinglass.com"+path+qs,
                     headers={"CG-API-KEY":COINGLASS_API_KEY},retries=1)

ETF_PATH={"BTC":"bitcoin","ETH":"ethereum","SOL":"solana","XRP":"xrp"}

def get_etf(symbol):
    name=ETF_PATH.get(symbol)
    if not name:return {"available":False,"reason":"无适用ETF维度"}
    d=coinglass_json(f"/api/etf/{name}/flow-history")
    try:
        rows=sorted(d["data"],key=lambda x:x.get("timestamp",0))
        latest=float(rows[-1].get("flow_usd") or 0); s=sum(float(x.get("flow_usd") or 0) for x in rows[-3:])
        return {"available":True,"latest":latest,"sum3":s}
    except:return {"available":False,"reason":"CoinGlass ETF不可用"}

def etf_dimension(e):
    if not e.get("available"):return None,[e.get("reason","ETF N/A")]
    l,s=e["latest"],e["sum3"]; x=(35 if l>0 else -35 if l<0 else 0)+(45 if s>0 else -45 if s<0 else 0)
    return max(-100,min(100,x)),[f"ETF最新净流={l/1e6:+.1f}M USD",f"ETF近3期合计={s/1e6:+.1f}M USD"]

def free_etf_dimension(symbol):
    if symbol not in ("BTC","ETH"):return None,["无适用免费ETF维度"]
    d=safe_json(FREE_ETF_BASE+"?"+urllib.parse.urlencode({"asset":symbol.lower(),"days":10}),retries=0,quiet=True)
    if not isinstance(d,dict):return None,["免费ETF源不可用"]
    try:
        rows=d.get("data",[]); dates=[str(r.get("date","")) for r in rows if isinstance(r,dict) and r.get("date")]
        if not dates:return None,["免费ETF源暂无有效数据"]
        newest=max(dates); vals=[]
        for r in rows:
            if str(r.get("date",""))!=newest:continue
            for k in ("netFlowUsd","net_flow_usd","flowUsd","flow_usd"):
                if r.get(k) not in (None,""):
                    vals.append(float(r[k]));break
        if not vals:return None,[f"ETF {newest} 无净流字段"]
        net=sum(vals); x=70 if net>=5e8 else 40 if net>=1.5e8 else 15 if net>0 else -70 if net<=-5e8 else -40 if net<=-1.5e8 else -15 if net<0 else 0
        return x,[f"免费ETF {newest} 净流≈${net/1e6:+.1f}M"]
    except Exception as e:return None,[f"免费ETF解析失败：{e}"]

def get_exchange_flow(symbol):
    if not COINGLASS_API_KEY:return {"available":False}
    d=coinglass_json("/api/spot/coin/netflow",{"symbol":symbol})
    try:
        x=d["data"];return {"available":True,"net_1h":float(x.get("net_flow_usd_1h") or 0),
                             "ratio_1h":float(x.get("net_flow_usd_1h_market_cap_ratio") or 0)}
    except:return {"available":False}

def flow_dimension(f):
    if not f.get("available"):return None,["专业交易所资金流 N/A"]
    n,r=f["net_1h"],f["ratio_1h"]
    x=70 if r>=.003 else 35 if r>=.001 else -70 if r<=-.003 else -35 if r<=-.001 else 0
    return x,[f"现货1h净流={n/1e6:+.2f}M USD / 市值比={r:+.5f}"]

# ---------------- Stablecoin / sentiment ----------------
def stablecoin_history(cid):
    d=cg_json(f"https://api.coingecko.com/api/v3/coins/{cid}/market_chart?"
              +urllib.parse.urlencode({"vs_currency":"usd","days":8,"interval":"daily"}),retries=4)
    try:return [float(x[1]) for x in d["market_caps"]]
    except:return []

def get_stablecoin_liquidity():
    try:
        a=stablecoin_history("tether"); b=stablecoin_history("usd-coin")
        if len(a)<2 or len(b)<2:return {"available":False}
        old,new=a[0]+b[0],a[-1]+b[-1]
        return {"available":True,"change7":(new/old-1)*100 if old else None}
    except Exception as e:
        print("稳定币数据降级：",e);return {"available":False}

def stablecoin_dimension(s):
    c=s.get("change7")
    if not s.get("available") or c is None:return None,["稳定币流动性 N/A"]
    x=65 if c>=1 else 30 if c>=.25 else -65 if c<=-1 else -30 if c<=-.25 else 0
    return x,[f"USDT+USDC市值7日变化={c:+.2f}%"]

def get_fear_greed():
    d=safe_json("https://api.alternative.me/fng/?limit=1&format=json",retries=1)
    try:return int(d["data"][0]["value"]),d["data"][0].get("value_classification","")
    except:return None,"N/A"

def sentiment_dimension(v):
    if v is None:return None,["Fear & Greed N/A"]
    x=-65 if v>=85 else -30 if v>=75 else 35 if v<=20 else 15 if v<=30 else 0
    return x,[f"Fear & Greed={v}"]

# ---------------- Score/account ----------------
def combine_dimensions(d):
    n=z=0.;used={}
    for k,v in d.items():
        if v is None:continue
        n+=v*WEIGHTS[k];z+=WEIGHTS[k];used[k]=v
    return (n/z if z else 0),used

def data_coverage(d):
    a=[k for k,v in d.items() if v is not None]
    return sum(WEIGHTS[k] for k in a)*100/sum(WEIGHTS.values()),a

def confidence_label(x):
    if x>=60:return "强势偏多"
    if x>=BUY_CONFIDENCE:return "偏多 / 达到入场线"
    if x>=15:return "偏多观察"
    if x<=-60:return "强势偏空"
    if x<=SELL_CONFIDENCE:return "偏空"
    return "中性 / 等待"

def new_account():
    return {"version":VERSION,"created_at":now_utc(),"cash":INITIAL_BALANCE,"positions":{},
            "trades":[],"realized_pnl":0.,"fees_paid":0.,"peak_equity":INITIAL_BALANCE,
            "last_equity":INITIAL_BALANCE}

def load_account():
    if not os.path.exists(STATE_FILE):return new_account()
    try:
        with open(STATE_FILE,"r",encoding="utf-8") as f:a=json.load(f)
        required={"cash","positions","trades","realized_pnl","fees_paid","peak_equity"}
        if (not isinstance(a,dict) or not required.issubset(a)
                or not isinstance(a["positions"],dict) or not isinstance(a["trades"],list)
                or any(not math.isfinite(float(a[k])) for k in ("cash","realized_pnl","fees_paid","peak_equity"))):
            raise ValueError("invalid existing financial state; refusing defaults")
        defaults=new_account()
        for k,v in defaults.items():a.setdefault(k,v)
        if not a.get("created_at"):
            trade_times=[
                t.get("time") for t in a.get("trades", [])
                if isinstance(t, dict) and isinstance(t.get("time"), str) and t.get("time")
            ]
            a["created_at"]=min(trade_times) if trade_times else now_utc()
        a["version"]=VERSION;return a
    except Exception as e:
        raise RuntimeError(f"账户读取失败，拒绝重置模拟账户：{e}") from e

def save_account(a):
    tmp=STATE_FILE+".tmp"
    with open(tmp,"w",encoding="utf-8") as f:
        json.dump(a,f,ensure_ascii=False,indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp,STATE_FILE)

def total_equity(a,prices):
    return runtime.account_equity(a,prices)

def total_exposure(a,prices):
    eq=total_equity(a,prices)
    return None if eq is None else eq-a["cash"]

def regime_entry_threshold(regime):
    # 趋势行情允许正常参与；震荡/高波动提高入场质量要求，减少来回磨损。
    return {"BULL_TREND": 32.0, "RANGE": 40.0, "HIGH_VOLATILITY": 48.0, "BEAR_TREND": 55.0}.get(regime, BUY_CONFIDENCE)

def regime_size_multiplier(regime):
    return {"BULL_TREND": 1.00, "RANGE": 0.70, "HIGH_VOLATILITY": 0.50, "BEAR_TREND": 0.40}.get(regime, 0.70)

def paper_buy(a,symbol,price,conf,prices,regime="UNKNOWN"):
    if symbol in a["positions"]:return False,"已有持仓"
    eq=total_equity(a,prices)
    if eq is None:return False,"持仓缺价，无法可靠估值，禁止新开仓"
    room=eq*MAX_TOTAL_EXPOSURE-total_exposure(a,prices)
    if room<=MIN_TRADE_USDT:return False,"总仓位达到限制"
    val=min(eq*RISK_PER_TRADE/STOP_LOSS_PCT*min(1,max(.5,abs(conf)/70)),
            eq*MAX_POSITION_PCT*regime_size_multiplier(regime),room,a["cash"]/(1+FEE_RATE))
    if val<MIN_TRADE_USDT:return False,"可用资金不足"
    fee=val*FEE_RATE;qty=val/price;a["cash"]-=val+fee;a["fees_paid"]+=fee
    a["positions"][symbol]={"quantity":qty,"entry_price":price,"entry_time":now_utc(),
        "entry_confidence":conf,"highest_price":price,"stop_loss":price*(1-STOP_LOSS_PCT),
        "take_profit":price*(1+TAKE_PROFIT_PCT),"entry_fee":fee}
    a["trades"].append({"time":now_utc(),"symbol":symbol,"side":"BUY","price":price,
                        "quantity":qty,"value":val,"fee":fee,"confidence":conf,"regime":regime})
    return True,f"模拟买入 {val:.2f} USDT"

def paper_sell(a,symbol,price,reason,conf):
    p=a["positions"].get(symbol)
    if not p:return False
    q=p["quantity"];gross=q*price;ef=gross*FEE_RATE;ev=q*p["entry_price"]
    entry_fee=p.get("entry_fee",ev*FEE_RATE);pnl=gross-ev-entry_fee-ef
    a["cash"]+=gross-ef;a["fees_paid"]+=ef;a["realized_pnl"]+=pnl
    a["trades"].append({"time":now_utc(),"symbol":symbol,"side":"SELL","price":price,
                        "quantity":q,"value":gross,"fee":ef,"confidence":conf,
                        "reason":reason,"pnl":pnl})
    del a["positions"][symbol];return True

def manage_position(a,symbol,price,conf):
    p=a["positions"].get(symbol)
    if not p:return None
    p["highest_price"]=max(p.get("highest_price",price),price)
    stop=max(p.get("stop_loss",p["entry_price"]*(1-STOP_LOSS_PCT)),
             p["highest_price"]*(1-TRAILING_STOP_PCT))
    p['stop_loss']=stop
    if price<=stop:paper_sell(a,symbol,price,"止损/移动止损",conf);return "SELL：止损/移动止损"
    if price>=p.get("take_profit",p["entry_price"]*(1+TAKE_PROFIT_PCT)):
        paper_sell(a,symbol,price,"止盈",conf);return "SELL：止盈"
    if conf is not None and conf<=SELL_CONFIDENCE:paper_sell(a,symbol,price,"多维综合信号转弱",conf);return "SELL：多维综合信号转弱"
    return "HOLD：继续持仓"

def fast_risk(a):
    """Protect held positions before macro/history/research; never open here."""
    observations={};exited=set();errors=[]
    for symbol in list(a['positions']):
        try:
            q=runtime.execution_quote(symbol,'SELL','SPOT');observations[symbol]=q
            manage_position(a,symbol,q['reference_mid'],None)
            if symbol not in a['positions']:exited.add(symbol)
        except Exception as exc:errors.append({'symbol':symbol,'reason':str(exc)})
    runtime.snapshot(a,observations)
    a['runtime']['risk_errors']=errors
    save_account(a)
    return exited,observations

def refresh_valuation(a):
    observations={}
    for symbol in a['positions']:
        try:observations[symbol]=runtime.execution_quote(symbol,'SELL','SPOT')
        except Exception as exc:observations[symbol]={'available':False,'reason':str(exc)}
    return runtime.snapshot(a,observations)

# ---------------- Main ----------------
def run_paper():
    print("="*72);print("AI TRADING AGENT",VERSION);print("PAPER ONLY |",now_utc())
    print("币种：",", ".join(SYMBOLS));print("CoinGlass：","已配置Key" if COINGLASS_API_KEY else "未配置Key（专业ETF/资金流自动N/A）");print("="*72)
    if LIVE_TRADING:raise SystemExit("安全保护：禁止真实交易。")

    account=load_account()
    exited,observations=fast_risk(account)
    macro_raw=get_macro();macro_score,macro_reasons=macro_dimension(macro_raw)
    fg_value,_=get_fear_greed();sentiment_score,sentiment_reasons=sentiment_dimension(fg_value)

    try:markets=get_market_data()
    except Exception as e:raise SystemExit(f"CoinGecko主行情失败：{e}")

    market_map={}
    for m in markets:
        if m["id"] in COINS:
            market_map[COINS[m["id"]]]={"coin_id":m["id"],"price":float(m["current_price"]),
                "change24":float(m.get("price_change_percentage_24h",0) or 0)}
    eq0,current_prices=refresh_valuation(account)

    # 稳定币放在主行情之后拉，避免启动阶段集中打 CoinGecko。
    stable_raw=get_stablecoin_liquidity();stable_score,stable_reasons=stablecoin_dimension(stable_raw)

    dd=(account["peak_equity"]-eq0)/account["peak_equity"] if eq0 is not None and account["peak_equity"] else None
    trading_locked=dd is None or dd>=MAX_DRAWDOWN_PCT
    results={};failures=[]

    for symbol in SYMBOLS:
        data=market_map.get(symbol)
        if not data:failures.append(symbol+": 无主行情");continue
        print("\n"+"="*72);print(symbol);print("="*72)
        try:
            prices,volumes=get_history(data["coin_id"]);price=data["price"];ch=data["change24"]
            tech_score,tech_reasons,tech=technical_dimension(price,ch,prices,volumes)
            deriv_raw=get_derivatives(symbol,account);deriv_score,deriv_reasons=derivatives_dimension(ch,deriv_raw)
            etf_score,etf_reasons=free_etf_dimension(symbol)
            if etf_score is None and COINGLASS_API_KEY:
                etf_score,etf_reasons=etf_dimension(get_etf(symbol))
            flow_score,flow_reasons=flow_dimension(get_exchange_flow(symbol))
            dims={"technical":tech_score,"derivatives":deriv_score,"macro":macro_score,
                  "etf":etf_score,"flow":flow_score,"stablecoin":stable_score,
                  "sentiment":sentiment_score}
            conf,_=combine_dimensions(dims);coverage,avail=data_coverage(dims);label=confidence_label(conf)
            sup,res=support_resistance(prices)
            print(f"价格：${price:,.4f} | 24h {ch:+.2f}%")
            print(f"Market Regime：{tech['regime']}")
            print(f"综合置信度：{conf:+.1f} / 100 | {label}")
            print(f"数据覆盖率：{coverage:.1f}% | 可用维度：{len(avail)}/7")
            print(f"衍生品数据源：{deriv_raw['source']} | {deriv_raw['status']}")
            print("-"*72)
            for name,x in [("技术面",tech_score),("衍生品",deriv_score),("宏观",macro_score),
                           ("ETF机构资金",etf_score),("交易所资金流",flow_score),
                           ("稳定币流动性",stable_score),("市场情绪",sentiment_score)]:
                print(f"{name:<12}：{'N/A' if x is None else f'{x:+.1f}'}")
            print(f"RSI：{tech['rsi']:.2f}" if tech["rsi"] is not None else "RSI：N/A")
            print(f"30日支撑：${sup:,.4f} | 30日阻力：${res:,.4f}")
            if deriv_raw.get("funding_rate") is not None:print(f"Funding：{deriv_raw['funding_rate']:+.4f}%")
            if deriv_raw.get("oi_change_pct") is not None:print(f"OI变化：{deriv_raw['oi_change_pct']:+.2f}%")
            print("\n主要依据：")
            for r in tech_reasons+deriv_reasons+macro_reasons+etf_reasons+flow_reasons+stable_reasons+sentiment_reasons:print("•",r)

            if symbol in exited:action="HOLD：本轮已退出，禁止同轮重入"
            elif symbol in account["positions"]:
                quote=runtime.execution_quote(symbol,'SELL','SPOT')
                price=quote['reference_mid']
                action=manage_position(account,symbol,price,conf)
                if symbol not in account['positions']:exited.add(symbol)
            elif trading_locked:action="HOLD：账户最大回撤保护"
            elif coverage<55:action="HOLD：数据覆盖率不足55%，禁止新开仓"
            elif conf>=regime_entry_threshold(tech["regime"]):
                execution=assess_okx_orderbook(symbol,"BUY","SPOT")
                record_execution_ab(account,symbol,"BUY",price,conf,execution)
                print(f"订单流执行确认：{'PASS' if execution['passes'] else 'REJECT'} | {execution.get('reason')} | 质量分 {execution.get('quality_score','N/A')}")
                if not runtime.fresh(execution):
                    action="HOLD：无新鲜可执行报价，禁止用旧行情成交"
                elif ORDERFLOW_ENFORCEMENT and not execution["passes"]:
                    action="HOLD：订单流/微观结构确认未通过"
                else:
                    _,current_prices=refresh_valuation(account)
                    if not runtime.fresh(execution):execution=runtime.execution_quote(symbol,'BUY','SPOT')
                    price=execution['reference_mid']
                    if ORDERFLOW_ENFORCEMENT and not execution['passes']:
                        ok,msg=False,"刷新报价后订单流确认未通过"
                    else:ok,msg=paper_buy(account,symbol,price,conf,current_prices,tech["regime"])
                    shadow_note=" | 订单流影子拒绝已记录、不干预基准" if not execution["passes"] else ""
                    action=(("BUY：" if ok else "HOLD：")+msg+shadow_note)
            else:action=f"HOLD：未达到{tech['regime']}动态入场线 {regime_entry_threshold(tech['regime']):.0f}"
            print("\n模拟交易动作：",action)
            results[symbol]={"price":price,"confidence":conf,"label":label,
                             "regime":tech["regime"],"dimensions":dims,
                             "coverage":coverage,"action":action}
        except Exception as e:
            failures.append(f"{symbol}: {e}");print(symbol,"分析失败：",e)

    equity,current_prices=refresh_valuation(account)
    account["last_equity"]=equity;account["version"]=VERSION
    ret=(equity/INITIAL_BALANCE-1)*100 if equity is not None else None
    curdd=(account["peak_equity"]-equity)/account["peak_equity"]*100 if equity is not None and account["peak_equity"] else None
    save_account(account)
    runtime.finish_labels(account,save_account)

    print("\n"+"="*72);print("现货机器人模拟账户");print("="*72)
    print(f"现金：{account['cash']:,.2f} USDT");print(f"总资产：{runtime.format_amount(equity)} USDT")
    print(f"累计收益：{runtime.format_amount(ret)}%");print(f"已实现盈亏：{account['realized_pnl']:+.2f} USDT")
    print(f"手续费：{account['fees_paid']:.2f} USDT");print(f"当前回撤：{runtime.format_amount(curdd)}%")
    print("估值快照：",account['runtime']['valuation_at'],"|",account['runtime']['valuation_status'],"| 非连续实时行情")
    print(f"交易记录：{len(account['trades'])} 笔")
    print("\n五币总览")
    for symbol in SYMBOLS:
        r=results.get(symbol)
        if r:
            print(f"{symbol}: ${r['price']:,.4f} | {r['label']} | {r['regime']} | {r['confidence']:+.1f}/100")
            print("  →",r["action"])

    if failures or len(results)!=len(SYMBOLS):
        print("\n完整性检查失败：",failures)
        raise RuntimeError(f"现货机器人完整性检查失败：成功 {len(results)}/{len(SYMBOLS)} 币种")

    print("\n"+"="*72);print("现货机器人 PAPER 完成 | LIVE真实交易：关闭")
    print("状态文件：",STATE_FILE);print("="*72)


if __name__=='__main__':run_paper()
