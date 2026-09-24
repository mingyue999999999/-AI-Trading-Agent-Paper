"""Lightweight PAPER-ONLY valuation refresher.

Reads trading account state and writes a separate valuation snapshot.
It never mutates trading account ledgers, positions, trades, strategy state,
risk settings, wallets, keys, signatures, or order paths.
"""
from __future__ import annotations
import json, math, os, tempfile, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "state/valuations/latest.json"

def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Paper-Valuation-Heartbeat/2.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))

def okx_spot(symbol):
    inst=f"{symbol}-USDT"
    d=get_json("https://www.okx.com/api/v5/market/ticker?"+urllib.parse.urlencode({"instId":inst}))
    px=float(d["data"][0]["last"])
    if not math.isfinite(px) or px<=0: raise ValueError(f"invalid OKX spot quote {symbol}: {px}")
    return px

def okx_swap_mark(symbol):
    inst=f"{symbol}-USDT-SWAP"
    d=get_json("https://www.okx.com/api/v5/public/mark-price?"+urllib.parse.urlencode({"instId":inst}))
    px=float(d["data"][0]["markPx"])
    if not math.isfinite(px) or px<=0: raise ValueError(f"invalid OKX mark quote {symbol}: {px}")
    return px

def load(rel):
    return json.loads((ROOT/rel).read_text(encoding="utf-8"))

def atomic_save(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=path.name+".",dir=path.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as f:
            json.dump(obj,f,ensure_ascii=False,indent=2); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def spot_snapshot(rel):
    a=load(rel); prices={}; eq=float(a["cash"]); unreal=0.0
    for s,p in a.get("positions",{}).items():
        px=okx_spot(s); prices[s]=px
        qty=float(p["quantity"]); entry=float(p.get("entry_price",px))
        eq+=qty*px; unreal+=qty*(px-entry)
    return {"equity":eq,"cash":float(a["cash"]),"unrealized_pnl":unreal,
            "positions":len(a.get("positions",{})),"prices":prices}

def quant_snapshot(rel):
    a=load(rel); prices={}; eq=float(a["cash"]); unreal=0.0
    for pair,p in a.get("positions",{}).items():
        symbol=pair[:-4] if pair.endswith("USDT") else pair
        px=okx_spot(symbol); prices[pair]=px
        qty=float(p["qty"]); entry=float(p.get("entry",px))
        eq+=qty*px; unreal+=qty*(px-entry)
    return {"equity":eq,"cash":float(a["cash"]),"unrealized_pnl":unreal,
            "positions":len(a.get("positions",{})),"prices":prices}

def futures_snapshot(rel):
    a=load(rel); prices={}; eq=float(a["cash"]); unreal=0.0
    for s,p in a.get("positions",{}).items():
        mark=okx_swap_mark(s); prices[s]=mark
        qty=float(p["quantity"]); entry=float(p["entry_price"])
        pnl=qty*(mark-entry) if p["side"]=="LONG" else qty*(entry-mark)
        unreal+=pnl; eq+=float(p.get("margin",0.0))+pnl
    return {"equity":eq,"cash":float(a["cash"]),"unrealized_pnl":unreal,
            "positions":len(a.get("positions",{})),"prices":prices}

def simple_snapshot(rel,cash_key="cash"):
    a=load(rel); pos=a.get("positions",{})
    cash=float(a.get(cash_key,0.0))
    return {"equity":cash if not pos else None,"cash":cash,"unrealized_pnl":0.0 if not pos else None,
            "positions":len(pos),"prices":{},"status":"OBSERVED" if not pos else "SPECIALIZED_NONEMPTY"}

def main():
    observed=now_utc()
    accounts={
      "spot":spot_snapshot("现货/模拟账户.json"),
      "futures":futures_snapshot("合约/模拟账户.json"),
      "futures_challenger":futures_snapshot("合约/阈值挑战者账户.json"),
      "quant":quant_snapshot("量化/state/paper_account.json"),
      "meme":simple_snapshot("链上Meme/模拟账户.json"),
      "meme_new_launch":simple_snapshot("链上Meme/新币影子账户.json"),
      "meme_liquidity_5":simple_snapshot("链上Meme/流动性5对照账户.json"),
      "meme_liquidity_8":simple_snapshot("链上Meme/流动性8对照账户.json"),
      "polymarket":simple_snapshot("Polymarket套利机器人/state/paper_account.json","cash_usdt"),
    }
    for v in accounts.values():
        v.setdefault("status","OBSERVED")
    atomic_save(OUT,{"schema_version":2,"paper_only":True,"observed_at":observed,
                     "source":"OKX public market data + repository paper ledgers",
                     "accounts":accounts})
    print(json.dumps({"observed_at":observed,"accounts":{k:v["equity"] for k,v in accounts.items()}},ensure_ascii=False))

if __name__=="__main__":
    main()
