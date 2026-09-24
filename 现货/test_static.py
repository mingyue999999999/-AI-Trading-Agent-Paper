import ast
import pathlib

p = pathlib.Path("现货/现货机器人.py")
s = p.read_text(encoding="utf-8")
tree = ast.parse(s)

last_assign = {}
for node in tree.body:
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        name = node.targets[0].id
        if name in {"LIVE_TRADING", "ORDERFLOW_ENFORCEMENT", "STATE_FILE", "WEIGHTS"}:
            try:
                last_assign[name] = ast.literal_eval(node.value)
            except Exception:
                pass

assert last_assign.get("LIVE_TRADING") is False
assert last_assign.get("ORDERFLOW_ENFORCEMENT") is False
assert last_assign.get("STATE_FILE") == "现货/模拟账户.json"
assert last_assign.get("WEIGHTS") == {
    "technical": 0.25, "derivatives": 0.15, "macro": 0.15,
    "etf": 0.15, "flow": 0.12, "stablecoin": 0.08, "sentiment": 0.10,
}
assert "def paper_buy" in s and "def paper_sell" in s and "def manage_position" in s
assert "拒绝重置模拟账户" in s
assert 'if not a.get("created_at")' in s and "min(trade_times)" in s
assert 'if ORDERFLOW_ENFORCEMENT and not execution["passes"]' in s
for forbidden in (
    "/api/v3/order", "/v5/order/create", "/api/v5/trade/order",
    "create_order(", "place_order(",
):
    assert forbidden not in s
print("现货静态安全检查通过：指标权重未变、PAPER ONLY、账户防重置")
