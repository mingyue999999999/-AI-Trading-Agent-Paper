"""Funding/basis market-neutral PAPER experiment. No API keys and no order endpoints."""
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT_DIR=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path: sys.path.insert(0,ROOT_DIR)
from execution_quality import assess_okx_orderbook

LIVE_TRADING=False
INITIAL_BALANCE=10_000.0
STATE_FILE="合约/资金费率基差账户.json"
SYMBOLS=("BTC","ETH","XRP","SOL","BNB")
SPOT_FEE=0.001
PERP_FEE=0.0005
PERP_LEVERAGE=3.0
SLIPPAGE=0.0005
MAX_NOTIONAL_PCT=0.15
MIN_NOTIONAL=250.0
MIN_ENTRY_EV_PCT=0.10
MAX_BASIS_PCT=2.50
MAX_POSITION_LOSS_PCT=0.015
MAX_HOLD_HOURS=168

def now_utc(): return datetime.now(timezone.utc).isoformat(timespec="seconds")

def get_json(path,params):
    url="https://www.okx.com"+path+"?"+urllib.parse.urlencode(params)
    req=urllib.request.Request(url,headers={"User-Agent":"Funding-Basis-Paper/1.0","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=15) as r: return json.loads(r.read().decode("utf-8"))

def market(symbol):
    spot=f"{symbol}-USDT"; swap=f"{symbol}-USDT-SWAP"
    s=get_json("/api/v5/market/ticker",{"instId":spot})["data"][0]
    p=get_json("/api/v5/public/mark-price",{"instType":"SWAP","instId":swap})["data"][0]
    f=get_json("/api/v5/public/funding-rate",{"instId":swap})["data"][0]
    spot_px=float(s["last"]); perp_px=float(p["markPx"]); rate=float(f["fundingRate"])
    basis=(perp_px/spot_px-1)*100
    roundtrip_cost_pct=2*(SPOT_FEE+PERP_FEE+2*SLIPPAGE)*100
    projected_funding_pct=rate*3*100
    # Conservative: count only half of positive basis as likely 24h convergence.
    convergence_pct=max(0.0,basis)*0.50
    ev_pct=projected_funding_pct+convergence_pct-roundtrip_cost_pct
    return {"symbol":symbol,"spot":spot_px,"perp":perp_px,"funding_rate":rate,
            "basis_pct":basis,"roundtrip_cost_pct":roundtrip_cost_pct,
            "projected_24h_ev_pct":ev_pct,"next_funding_time":int(f.get("nextFundingTime") or 0)}

def new_account():
    return {"version":"Funding Basis Neutral PAPER v1","created_at":now_utc(),
            "initial_balance":INITIAL_BALANCE,"cash":INITIAL_BALANCE,"equity":INITIAL_BALANCE,
            "peak_equity":INITIAL_BALANCE,"realized_pnl":0.0,"funding_pnl":0.0,
            "fees_paid":0.0,"position":None,"trades":[],"observations":[]}

def load_account():
    if not os.path.exists(STATE_FILE): return new_account()
    try:
        with open(STATE_FILE,encoding="utf-8") as f: a=json.load(f)
        required={"initial_balance","cash","position","trades","realized_pnl","fees_paid","funding_pnl","peak_equity"}
        if (not isinstance(a,dict) or not required.issubset(a)
                or float(a["initial_balance"])!=INITIAL_BALANCE
                or not isinstance(a["trades"],list)
                or (a["position"] is not None and not isinstance(a["position"],dict))
                or any(not math.isfinite(float(a[k])) for k in ("cash","realized_pnl","fees_paid","funding_pnl","peak_equity"))):
            raise ValueError("invalid existing financial state; refusing defaults")
        d=new_account()
        for k,v in d.items(): a.setdefault(k,v)
        return a
    except Exception as e: raise RuntimeError(f"Funding/基差账户读取失败，拒绝重置：{e}") from e

def save_account(a):
    tmp=STATE_FILE+".tmp"
    with open(tmp,"w",encoding="utf-8") as f:
        json.dump(a,f,ensure_ascii=False,indent=2); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,STATE_FILE)

def unrealized(p,m):
    return p["spot_qty"]*(m["spot"]-p["entry_spot"])+p["perp_qty"]*(p["entry_perp"]-m["perp"])

def refresh_equity(a,m=None):
    eq=float(a["cash"])
    if a.get("position") and m: eq+=float(a["position"]["reserved_capital"])+unrealized(a["position"],m)
    a["equity"]=eq; a["peak_equity"]=max(float(a.get("peak_equity",eq)),eq)
    return eq

def latest_realized_funding(symbol):
    d=get_json("/api/v5/public/funding-rate-history",{"instId":f"{symbol}-USDT-SWAP","limit":1})["data"][0]
    return float(d.get("realizedRate") or d["fundingRate"]),int(d["fundingTime"])

def apply_funding(a,m):
    p=a.get("position")
    if not p: return 0.0
    try: rate,ts=latest_realized_funding(p["symbol"])
    except Exception: return 0.0
    if ts<=int(p.get("last_funding_ts",0)): return 0.0
    cashflow=p["perp_qty"]*m["perp"]*rate
    a["cash"]+=cashflow; a["funding_pnl"]+=cashflow; p["funding_pnl"]=float(p.get("funding_pnl",0.0))+cashflow; p["last_funding_ts"]=ts
    a["trades"].append({"time":now_utc(),"event":"FUNDING","symbol":p["symbol"],"rate":rate,"cashflow":cashflow})
    return cashflow

def close_position(a,m,reason):
    p=a["position"]; pnl=unrealized(p,m)
    exit_fee=p["reserved_notional"]*(SPOT_FEE+PERP_FEE)
    released=p["reserved_capital"]+pnl-exit_fee
    a["cash"]+=released; a["fees_paid"]+=exit_fee
    realized=pnl-p["entry_fee"]-exit_fee
    a["realized_pnl"]+=realized
    a["trades"].append({"time":now_utc(),"event":"CLOSE","symbol":p["symbol"],"reason":reason,
                        "spot":m["spot"],"perp":m["perp"],"basis_pct":m["basis_pct"],
                        "realized_trading_pnl":realized,"funding_pnl_position":p.get("funding_pnl",0.0)})
    a["position"]=None
    return realized

def open_position(a,m,spot_gate,perp_gate):
    edge=max(0.0,m["projected_24h_ev_pct"]-MIN_ENTRY_EV_PCT)
    size_mult=min(1.0,0.35+edge/1.5)
    fee_rate=SPOT_FEE+PERP_FEE
    capital_per_notional=1.0+1.0/PERP_LEVERAGE
    notional=min(a["equity"]*MAX_NOTIONAL_PCT*size_mult,a["cash"]/(capital_per_notional+fee_rate))
    if notional<MIN_NOTIONAL: return False,"资金不足"
    entry_spot=m["spot"]*(1+SLIPPAGE); entry_perp=m["perp"]*(1-SLIPPAGE)
    fee=notional*fee_rate
    reserved_capital=notional*capital_per_notional
    a["cash"]-=reserved_capital+fee; a["fees_paid"]+=fee
    try: _,last_ts=latest_realized_funding(m["symbol"])
    except Exception: last_ts=0
    a["position"]={"symbol":m["symbol"],"entry_time":now_utc(),"entry_epoch":int(time.time()),
                   "entry_spot":entry_spot,"entry_perp":entry_perp,
                   "spot_qty":notional/entry_spot,"perp_qty":notional/entry_perp,
                   "reserved_notional":notional,"perp_margin":notional/PERP_LEVERAGE,
                   "reserved_capital":reserved_capital,"entry_fee":fee,"last_funding_ts":last_ts,
                   "funding_pnl":0.0,"entry_ev_pct":m["projected_24h_ev_pct"],
                   "spot_execution":spot_gate,"perp_execution":perp_gate}
    a["trades"].append({"time":now_utc(),"event":"OPEN_DELTA_NEUTRAL","symbol":m["symbol"],
                        "notional_each_leg":notional,"funding_rate":m["funding_rate"],
                        "basis_pct":m["basis_pct"],"projected_24h_ev_pct":m["projected_24h_ev_pct"]})
    return True,f"模拟现货多 + 永续空，每腿 {notional:.2f} USDT"

def main():
    if LIVE_TRADING: raise SystemExit("安全保护：Funding/基差实验禁止真实交易")
    a=load_account(); observations=[]
    for s in SYMBOLS:
        try: observations.append(market(s))
        except Exception as e: print(s,"数据降级：",e)
    if not observations: raise RuntimeError("Funding/基差实验无可用市场数据")
    a["observations"]=(a.get("observations",[])+[{"time":now_utc(),"markets":observations}])[-200:]
    p=a.get("position")
    if p:
        m=next((x for x in observations if x["symbol"]==p["symbol"]),None)
        if not m: raise RuntimeError("持仓市场数据缺失，拒绝盲目改账")
        funding=apply_funding(a,m); eq=refresh_equity(a,m)
        loss=(INITIAL_BALANCE-eq)/INITIAL_BALANCE
        age=(time.time()-p["entry_epoch"])/3600
        reason=None
        if m["funding_rate"]<=0: reason="Funding翻转"
        elif m["basis_pct"]<0: reason="基差反向"
        elif m["basis_pct"]>MAX_BASIS_PCT: reason="基差尾部扩大"
        elif loss>=MAX_POSITION_LOSS_PCT: reason="组合止损"
        elif age>=MAX_HOLD_HOURS: reason="最长持有期"
        if reason: close_position(a,m,reason); refresh_equity(a)
        print(f"{p['symbol']} 中性持仓 | Funding {funding:+.4f} | 权益 {a['equity']:.2f} | {reason or '继续持有'}")
    else:
        refresh_equity(a)
        candidates=[m for m in observations if m["funding_rate"]>0 and 0<=m["basis_pct"]<=MAX_BASIS_PCT and m["projected_24h_ev_pct"]>=MIN_ENTRY_EV_PCT]
        candidates.sort(key=lambda x:x["projected_24h_ev_pct"],reverse=True)
        action="无扣费后正EV机会"
        for m in candidates:
            sg=assess_okx_orderbook(m["symbol"],"BUY","SPOT")
            pg=assess_okx_orderbook(m["symbol"],"SHORT","SWAP")
            if sg["passes"] and pg["passes"]:
                ok,action=open_position(a,m,sg,pg)
                if ok: break
            else: action=f"{m['symbol']} 执行质量不足"
        print("Funding/基差动作：",action)
    save_account(a)
    print(f"Funding/基差账户：权益 {a['equity']:.2f} | 已实现 {a['realized_pnl']:+.2f} | Funding {a['funding_pnl']:+.2f}")
    print("PAPER ONLY | 无钱包、无私钥、无真实下单")

if __name__=="__main__": main()
