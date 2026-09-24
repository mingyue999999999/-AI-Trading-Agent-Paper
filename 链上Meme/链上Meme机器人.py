"""On-chain meme discovery and paper portfolio. No wallet, signing or orders."""
from __future__ import annotations
import json, math, os, tempfile, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path
import gate_diagnostics as diagnostics

LIVE_TRADING = False
INITIAL_BALANCE = 10_000.0
STATE_FILE = Path("链上Meme/模拟账户.json")
CHAINS = {"solana", "bsc"}
MAX_POSITIONS = 3
POSITION_PCT = 0.025
MAX_EXPOSURE_PCT = 0.075
ENTRY_COST_PCT = 0.015
EXIT_COST_PCT = 0.015
STOP_LOSS_PCT = 0.12
TAKE_PROFIT_PCT = 0.25
TRAILING_STOP_PCT = 0.10
MIN_OBSERVATIONS = 3
MIN_WATCH_AGE_SECONDS = 3600

def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def get_json(url):
    req=urllib.request.Request(url,headers={"User-Agent":"Meme-Paper-Research/1.0"})
    last=None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req,timeout=30) as r: return json.loads(r.read().decode())
        except Exception as e:
            last=e; time.sleep(2**attempt)
    raise RuntimeError(f"data source unavailable: {last}")

def new_account():
    return {"version":1,"mode":"PAPER_ONLY","initial_balance":INITIAL_BALANCE,
            "cash":INITIAL_BALANCE,"positions":{},"watchlist":{},"trades":[],
            "realized_pnl":0.0,"fees_slippage":0.0,"peak_equity":INITIAL_BALANCE,
            "last_equity":INITIAL_BALANCE,"max_drawdown_pct":0.0,"created_at":now()}

def load_account(path=STATE_FILE):
    if not path.exists(): return new_account()
    try:
        x=json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(x,dict) or float(x.get("initial_balance",0))!=INITIAL_BALANCE:
            raise ValueError("invalid initial balance or account structure")
        for k in ("cash","positions","watchlist","trades"): 
            if k not in x: raise ValueError(f"missing {k}")
        return x
    except Exception as e: raise RuntimeError(f"Meme paper account corrupt; refusing reset: {e}") from e

def save_account(a,path=STATE_FILE):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=path.name+".",dir=path.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as f:
            json.dump(a,f,ensure_ascii=False,indent=2); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def numeric(x,default=0.0):
    try: return float(x)
    except (TypeError,ValueError): return default

def pair_metrics(p):
    liq=numeric((p.get("liquidity") or {}).get("usd")); fdv=numeric(p.get("fdv") or p.get("marketCap"))
    vol=numeric((p.get("volume") or {}).get("h24")); tx=(p.get("txns") or {}).get("h24") or {}
    buys,sells=int(tx.get("buys") or 0),int(tx.get("sells") or 0)
    pc=p.get("priceChange") or {}; h1=numeric(pc.get("h1")); h24=numeric(pc.get("h24"))
    created=int(p.get("pairCreatedAt") or 0); age_h=(time.time()*1000-created)/3_600_000 if created else 1e9
    return {"price":numeric(p.get("priceUsd")),"liquidity":liq,"fdv":fdv,"volume24":vol,
            "buys":buys,"sells":sells,"h1":h1,"h24":h24,"age_h":age_h,
            "liq_fdv":liq/fdv if fdv else 0.0}

def score_pair(p):
    m=pair_metrics(p); reasons=[]
    hard=(m["price"]>0 and 2<=m["age_h"]<=720 and m["liquidity"]>=250_000 and
          m["volume24"]>=500_000 and 500_000<=m["fdv"]<=100_000_000 and
          m["liq_fdv"]>=.05 and m["buys"]+m["sells"]>=500 and
          .8<=m["buys"]/max(1,m["sells"])<=2.5 and -5<=m["h1"]<=25 and -15<=m["h24"]<=100)
    if not hard: return None
    score=min(25,math.log10(m["liquidity"]/250_000+1)*20)
    score+=min(25,math.log10(m["volume24"]/500_000+1)*20)
    score+=min(20,(m["buys"]+m["sells"])/1000*10)
    score+=20*min(1,m["liq_fdv"]/.20)
    score+=10 if 0<=m["h1"]<=12 else 4
    reasons=["liquidity","organic activity","balanced flow","non-parabolic momentum"]
    return round(score,2),m,reasons

def best_pair(rows):
    valid=[p for p in rows if p.get("chainId") in CHAINS and numeric(p.get("priceUsd"))>0]
    return max(valid,key=lambda p:numeric((p.get("liquidity") or {}).get("usd")),default=None)

def discover():
    seeds=get_json("https://api.dexscreener.com/token-profiles/latest/v1")
    out=[]; reviews=[]
    for x in seeds[:30]:
        chain,address=x.get("chainId"),x.get("tokenAddress")
        if chain not in CHAINS or not address: continue
        rows=get_json(f"https://api.dexscreener.com/token-pairs/v1/{chain}/{address}")
        p=best_pair(rows if isinstance(rows,list) else [])
        if p:
            snapshot=diagnostics.snapshot(p,time.time())
            reviews.append({"key":f"{chain}:{address}","symbol":(p.get("baseToken") or {}).get("symbol"),
                            "metrics":snapshot,"pair":p,"gate":diagnostics.evaluate(snapshot,"base")})
            s=score_pair(p)
            reviews[-1]["execution_gate_passed"] = s is not None
            reviews[-1]["diagnostic_disagreement"] = (s is not None) != reviews[-1]["gate"]["passed"]
            if s: out.append({"chain":chain,"address":address,"pair":p,"score":s[0],"metrics":s[1],"reasons":s[2]})
        time.sleep(.12)
    discover.report={"time":now(),"mode":"PAPER_ONLY_BASE_DIAGNOSTICS",
        "seed_count":len(seeds),"seed_limit":30,"reviewed":len(reviews),"accepted_for_watch":len(out),
        "reviews":reviews,"summary":diagnostics.summarize(reviews)}
    return sorted(out,key=lambda x:x["score"],reverse=True)

def held_prices(a):
    out={}
    for key,p in a["positions"].items():
        rows=get_json(f"https://api.dexscreener.com/token-pairs/v1/{p['chain']}/{p['address']}")
        pair=best_pair(rows if isinstance(rows,list) else [])
        if pair: out[key]=numeric(pair.get("priceUsd"))
        time.sleep(.12)
    return out

def equity(a,prices):
    return a["cash"]+sum(p["quantity"]*prices.get(k,p["entry_price"]) for k,p in a["positions"].items())

def run(a,candidates,ts=None):
    epoch=int(ts or time.time()); prices=held_prices(a) if a["positions"] else {}
    for key,p in list(a["positions"].items()):
        px=prices.get(key); 
        if not px: continue
        p["highest_price"]=max(p["highest_price"],px)
        reason=None
        if px<=p["entry_price"]*(1-STOP_LOSS_PCT): reason="STOP"
        elif px>=p["entry_price"]*(1+TAKE_PROFIT_PCT): reason="TAKE_PROFIT"
        elif px<=p["highest_price"]*(1-TRAILING_STOP_PCT): reason="TRAILING_STOP"
        if reason:
            gross=p["quantity"]*px; cost=gross*EXIT_COST_PCT; net=gross-cost
            pnl=net-p["invested_usdt"]
            a["cash"]+=net; a["realized_pnl"]+=pnl; a["fees_slippage"]+=cost
            a["trades"].append({"time":now(),"side":"SELL","key":key,"price":px,"pnl":pnl,"reason":reason})
            del a["positions"][key]
    for c in candidates:
        key=f"{c['chain']}:{c['address']}"; w=a["watchlist"].setdefault(key,{"first_seen":epoch,"observations":0})
        w["observations"]+=1; w["last_seen"]=epoch; w["score"]=c["score"]
        if key in a["positions"]:
            w["decision"]="ALREADY_HELD"; continue
        if len(a["positions"])>=MAX_POSITIONS:
            w["decision"]="POSITION_LIMIT"; continue
        if w["observations"]<MIN_OBSERVATIONS or epoch-w["first_seen"]<MIN_WATCH_AGE_SECONDS:
            w["decision"]="WAIT_OBSERVATIONS_OR_AGE"; continue
        px=c["metrics"]["price"]; eq=equity(a,prices); exposure=sum(p["quantity"]*prices.get(k,p["entry_price"]) for k,p in a["positions"].items())
        budget=min(eq*POSITION_PCT,max(0,eq*MAX_EXPOSURE_PCT-exposure),a["cash"])
        if budget<50:
            w["decision"]="BUDGET_BELOW_MINIMUM"; continue
        cost=budget*ENTRY_COST_PCT; qty=(budget-cost)/px
        a["cash"]-=budget; a["fees_slippage"]+=cost
        a["positions"][key]={"chain":c["chain"],"address":c["address"],"symbol":c["pair"]["baseToken"].get("symbol"),
            "quantity":qty,"entry_price":px,"highest_price":px,"invested_usdt":budget,"score":c["score"],"entry_time":now()}
        a["trades"].append({"time":now(),"side":"BUY","key":key,"price":px,"budget":budget,"cost":cost})
        prices[key]=px
        w["decision"]="BUY"
    eq=equity(a,prices); a["peak_equity"]=max(a["peak_equity"],eq); a["last_equity"]=eq
    dd=(a["peak_equity"]-eq)/a["peak_equity"]*100 if a["peak_equity"] else 0
    a["max_drawdown_pct"]=max(a.get("max_drawdown_pct",0),dd); a["last_run_at"]=now()
    return a

def main():
    if LIVE_TRADING: raise SystemExit("PAPER ONLY safety stop")
    a=load_account(); c=discover(); a=run(a,c); save_account(a)
    save_account(discover.report,Path("链上Meme/基准候选诊断.json"))
    print(json.dumps({"mode":"PAPER_ONLY","candidates":len(c),"top":c[:10],"account":a},ensure_ascii=False,indent=2))

if __name__=="__main__": main()

