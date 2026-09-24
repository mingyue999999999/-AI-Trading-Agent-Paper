import ast
import pathlib

p = pathlib.Path("合约/合约机器人.py")
s = p.read_text(encoding="utf-8")
tree = ast.parse(s)

last_assign = {}
for node in tree.body:
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        name = node.targets[0].id
        if name in {
            "LIVE_TRADING", "ORDERFLOW_ENFORCEMENT", "FUTURES_STATE_FILE", "WEIGHTS", "LEVERAGE",
            "FUTURES_RISK_PER_TRADE", "FUTURES_STOP_LOSS_PCT",
            "FUTURES_TAKE_PROFIT_PCT", "MAX_CONCURRENT_POSITIONS"
        }:
            try:
                last_assign[name] = ast.literal_eval(node.value)
            except Exception:
                pass

assert last_assign.get("LIVE_TRADING") is False
assert last_assign.get("ORDERFLOW_ENFORCEMENT") is False
assert last_assign.get("FUTURES_STATE_FILE") == "合约/模拟账户.json"
assert last_assign.get("WEIGHTS") == {
    "technical": 0.25, "derivatives": 0.15, "macro": 0.15,
    "etf": 0.15, "flow": 0.12, "stablecoin": 0.08, "sentiment": 0.10,
}
assert last_assign.get("LEVERAGE") == 3.0
assert last_assign.get("FUTURES_RISK_PER_TRADE") == 0.01
assert last_assign.get("FUTURES_STOP_LOSS_PCT") == 0.04
assert last_assign.get("FUTURES_TAKE_PROFIT_PCT") == 0.10
assert last_assign.get("MAX_CONCURRENT_POSITIONS") == 3
assert "def open_futures" in s and "def manage_futures" in s and "def close_futures" in s
assert "V3 现货式执行入口已停用" in s
assert "拒绝重置模拟账户" in s
assert s.count('if ORDERFLOW_ENFORCEMENT and not execution["passes"]') == 2
assert 'FUTURES_CHALLENGER_MODE = os.getenv("FUTURES_CHALLENGER_MODE", "0") == "1"' in s
assert 'THRESHOLD_MULTIPLIER = 0.875 if FUTURES_CHALLENGER_MODE else 1.0' in s
assert 'FUTURES_STATE_FILE = "合约/阈值挑战者账户.json"' in s
assert 'OI_STATE_FILE = "合约/阈值挑战者持仓量记录.json"' in s
for forbidden in (
    "/api/v3/order", "/v5/order/create", "/api/v5/trade/order",
    "create_order(", "place_order(",
):
    assert forbidden not in s
print("合约静态安全检查通过：指标权重未变、PAPER ONLY、账户防重置")
