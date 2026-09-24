"""Early-launch meme shadow paper portfolio. No wallet, signing or orders."""
from __future__ import annotations
import json, os, tempfile, time
from datetime import datetime, timezone
from pathlib import Path

LIVE_TRADING=False
INITIAL_BALANCE=10_000.0
STATE_FILE=Path("链上Meme/新币影子账户.json")
MAX_POSITIONS=3
POSITION_PCT=.01
MAX_EXPOSURE_PCT=.03
ENTRY_COST_PCT=.02
EXIT_COST_PCT=.03
STOP_LOSS_PCT=.15
TRAILING_STOP_PCT=.12
MIN_OBSERVATIONS=4
MIN_WATCH_AGE_SECONDS=45*60
MAX_HOLD_SECONDS=72*3600

def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def numeric(x,default=0.0):
    try: return float(x)
    except (TypeError,ValueError): return default

def new_account():
    return {"version":1,"mode":"PAPER_ONLY_NEW_LAUNCH_SHADOW","initial_balance":INITIAL_BALANCE,
        "cash":INITIAL_BALANCE,"positions":{},"watchlist":{},"trades":[],"realized_pnl":0.0,
        "fees_slippage":0.0,"peak_equity":INITIAL_BALANCE,"last_equity":INITIAL_BALANCE,
        "max_drawdown_pct":0.0,"created_at":now()}

def load_account(path=STATE_FILE):
    if not path.exists(): return new_account()
    try:
        a=json.loads(path.read_text(encoding="utf-8"))
        if float(a.get("initial_balance",0))!=INITIAL_BALANCE: raise ValueError("invalid initial balance")
        for k in ("cash","positions","watchlist","trades"):
            if k not in a: raise ValueError(f"missing {k}")
        return a
    except Exception as e: raise RuntimeError(f"new-launch paper account corrupt; refusing reset: {e}") from e

def save_account(a,path=STATE_FILE):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=path.name+".",dir=path.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as f:
            json.dump(a,f,ensure_ascii=False,indent=2); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def score_launch(m, min_liq_fdv=.08):
    ratio=m["buys"]/max(1,m["sells"])
    hard=(m["price"]>0 and .25<=m["age_h"]<=72 and m["liquidity"]>=75_000 and
        m["volume24"]>=100_000 and 200_000<=m["fdv"]<=30_000_000 and m["liq_fdv"]>=min_liq_fdv and
        m["buys"]+m["sells"]>=200 and .70<=ratio<=3.50 and -10<=m["h1"]<=35 and -30<=m["h24"]<=200)
    if not hard: return None
    score=min(30,m["liquidity"]/75_000*8)+min(25,m["volume24"]/100_000*6)
    score+=min(20,(m["buys"]+m["sells"])/200*4)+min(15,m["liq_fdv"]/.08*5)
    score+=10 if 0<=m["h1"]<=18 else 3
    return round(score,2)

def equity(a,prices):
    return a["cash"]+sum(p["quantity"]*prices.get(k,p["entry_price"]) for k,p in a["positions"].items())

def sell_fraction(a,key,price,fraction,reason):
    p=a["positions"][key]; qty=p["quantity"]*fraction; gross=qty*price; cost=gross*EXIT_COST_PCT
    allocated=p["invested_usdt"]*fraction
    a["cash"]+=gross-cost; a["realized_pnl"]+=gross-cost-allocated; a["fees_slippage"]+=cost
    a["trades"].append({"time":now(),"side":"SELL","key":key,"price":price,"quantity":qty,
                        "pnl":gross-cost-allocated,"reason":reason})
    p["quantity"]-=qty; p["invested_usdt"]-=allocated
    if p["quantity"]<=1e-12 or fraction>=.999999: del a["positions"][key]

def run(a,candidates,prices,epoch=None, all_metrics=None):
    epoch=int(epoch or time.time()); by_key={f"{c['chain']}:{c['address']}":c for c in candidates}
    for key,p in list(a["positions"].items()):
        px=numeric(prices.get(key))
        if px<=0: continue
        metrics=(all_metrics or {}).get(key) or (by_key.get(key) or {}).get("metrics",{})
        p["highest_price"]=max(p["highest_price"],px); held=epoch-p["entry_epoch"]
        broken=bool(metrics) and (metrics.get("liquidity",0)<p["entry_liquidity"]*.60 or
                                 metrics.get("buys",0)/max(1,metrics.get("sells",0))<.35)
        if px<=p["entry_price"]*(1-STOP_LOSS_PCT): sell_fraction(a,key,px,1.0,"HARD_STOP")
        elif broken: sell_fraction(a,key,px,1.0,"LIQUIDITY_OR_FLOW_BREAK")
        elif held>=MAX_HOLD_SECONDS: sell_fraction(a,key,px,1.0,"MAX_HOLD_72H")
        else:
            gain=px/p["entry_price"]-1
            if gain>=.60 and p["profit_stage"]<2:
                sell_fraction(a,key,px,.25/.65,"TAKE_PROFIT_60")
                if key in a["positions"]: a["positions"][key]["profit_stage"]=2
            elif gain>=.30 and p["profit_stage"]<1:
                sell_fraction(a,key,px,.35,"TAKE_PROFIT_30")
                if key in a["positions"]: a["positions"][key]["profit_stage"]=1
            if key in a["positions"]:
                p=a["positions"][key]; trail=p["highest_price"]*(1-TRAILING_STOP_PCT)
                break_even=p["entry_price"]*(1+ENTRY_COST_PCT+EXIT_COST_PCT)
                floor=max(trail,break_even) if p["profit_stage"] else trail
                if px<=floor: sell_fraction(a,key,px,1.0,"TRAILING_STOP")
        if key not in a["positions"]:
            a["watchlist"].setdefault(key,{"first_seen":epoch,"observations":0})["last_exit_epoch"]=epoch
    for c in candidates:
        key=f"{c['chain']}:{c['address']}"
        evidence=c.get("external_evidence") or {"sources":[],"bonus":0.0,"blockers":[],"qualified_wallet_sources":[]}
        w=a["watchlist"].setdefault(key,{"first_seen":epoch,"observations":0})
        w.update({"last_seen":epoch,"score":c["score"],"observations":w["observations"]+1,
                  "contract_risk":"UNVERIFIED_SHADOW_ONLY","external_evidence":evidence})
        # Defence in depth: discovery vendors may veto obvious risk but never bypass the independent gate.
        if evidence.get("blockers"):
            w["decision"]="REJECT_EXTERNAL_RISK_BLOCKER"
            continue
        w["decision"]="WATCH"
        if key in a["positions"]:
            w["decision"]="ALREADY_HELD"; continue
        if len(a["positions"])>=MAX_POSITIONS:
            w["decision"]="POSITION_LIMIT"; continue
        if epoch-w.get("last_exit_epoch",0)<6*3600:
            w["decision"]="EXIT_COOLDOWN"; continue
        if w["observations"]<MIN_OBSERVATIONS or epoch-w["first_seen"]<MIN_WATCH_AGE_SECONDS:
            w["decision"]="WAIT_OBSERVATIONS_OR_AGE"; continue
        eq=equity(a,prices)
        exposure=sum(p["quantity"]*prices.get(k,p["entry_price"]) for k,p in a["positions"].items())
        budget=min(eq*POSITION_PCT,max(0,eq*MAX_EXPOSURE_PCT-exposure),a["cash"])
        if budget<50:
            w["decision"]="BUDGET_BELOW_MINIMUM"; continue
        px=c["metrics"]["price"]; cost=budget*ENTRY_COST_PCT; qty=(budget-cost)/px
        a["cash"]-=budget; a["fees_slippage"]+=cost
        a["positions"][key]={"chain":c["chain"],"address":c["address"],"symbol":c.get("symbol"),
            "quantity":qty,"entry_price":px,"highest_price":px,"invested_usdt":budget,
            "entry_liquidity":c["metrics"]["liquidity"],"entry_epoch":epoch,"entry_time":now(),
            "score":c["score"],"independent_score":c.get("independent_score",c["score"]),
            "external_evidence":evidence,"profit_stage":0,"contract_risk":"UNVERIFIED_SHADOW_ONLY"}
        a["trades"].append({"time":now(),"side":"BUY","key":key,"price":px,"budget":budget,"cost":cost})
        prices[key]=px
        w["decision"]="BUY"
    total=equity(a,prices); a["peak_equity"]=max(a["peak_equity"],total); a["last_equity"]=total
    dd=(a["peak_equity"]-total)/a["peak_equity"]*100 if a["peak_equity"] else 0
    a["max_drawdown_pct"]=max(a.get("max_drawdown_pct",0),dd); a["last_run_at"]=now()
    return a

if __name__=="__main__": raise SystemExit("Run through 链上Meme机器人.py; PAPER ONLY")

