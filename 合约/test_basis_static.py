import ast
from pathlib import Path

s=Path("合约/资金费率基差实验.py").read_text(encoding="utf-8")
t=ast.parse(s)
values={}
for n in t.body:
    if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name):
        try: values[n.targets[0].id]=ast.literal_eval(n.value)
        except Exception: pass
assert values["LIVE_TRADING"] is False
assert values["INITIAL_BALANCE"]==10_000.0
assert values["STATE_FILE"]=="合约/资金费率基差账户.json"
assert values["MAX_NOTIONAL_PCT"]<=0.15
assert values["PERP_LEVERAGE"]==3.0
assert '"reserved_capital":reserved_capital' in s
assert 'notional/PERP_LEVERAGE' in s
assert "OPEN_DELTA_NEUTRAL" in s
assert 'assess_okx_orderbook(m["symbol"],"BUY","SPOT")' in s
assert 'assess_okx_orderbook(m["symbol"],"SHORT","SWAP")' in s
for forbidden in ("/api/v3/order","/v5/order/create","/api/v5/trade/order","create_order(","place_order("):
    assert forbidden not in s
print("Funding/基差中性实验静态安全检查通过")
