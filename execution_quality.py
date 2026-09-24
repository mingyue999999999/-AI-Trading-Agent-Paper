"""Read-only OKX market measurements and timestamped PAPER shadow labels."""
import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from functools import lru_cache

LABEL_VERSION = 2
_label_requests = 0


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "Paper-Shadow/2.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def okx(path, **params):
    payload = _get_json("https://www.okx.com" + path + "?" + urllib.parse.urlencode(params))
    if str(payload.get("code", "0")) != "0":
        raise ValueError("OKX " + str(payload.get("code")) + ": " + str(payload.get("msg")))
    if not payload.get("data"):
        raise ValueError("empty OKX data: " + path)
    return payload["data"]


def positive(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("non-positive or non-finite market value")
    return value


@lru_cache(maxsize=32)
def contract_units(symbol):
    """OKX linear SWAP book/trade sizes are contracts, not base currency."""
    row = okx("/api/v5/public/instruments", instType="SWAP", instId=f"{symbol}-USDT-SWAP")[0]
    if row.get("ctType") != "linear" or row.get("ctValCcy") != symbol or row.get("settleCcy") != "USDT":
        raise ValueError("unsupported contract denomination")
    return positive(row["ctVal"]) * positive(row.get("ctMult") or 1)


def book_snapshot(symbol, market="SPOT", levels=20):
    inst = f"{symbol}-USDT" + ("-SWAP" if market == "SWAP" else "")
    units = contract_units(symbol) if market == "SWAP" else 1.0
    row = okx("/api/v5/market/books", instId=inst, sz=max(5, levels))[0]
    ts = int(row["ts"])
    age = time.time() * 1000 - ts
    if not -2000 <= age <= 30000:
        raise ValueError("stale or future order book")
    bids = [[positive(x[0]), positive(x[1]) * units] for x in row["bids"][:levels]]
    asks = [[positive(x[0]), positive(x[1]) * units] for x in row["asks"][:levels]]
    if not bids or not asks or bids[0][0] >= asks[0][0]:
        raise ValueError("empty or crossed book")
    if bids != sorted(bids, key=lambda x: -x[0]) or asks != sorted(asks, key=lambda x: x[0]):
        raise ValueError("unsorted order book")
    return {"bids": bids, "asks": asks, "mid": (bids[0][0] + asks[0][0]) / 2,
            "price_epoch_ms": ts, "instrument": inst, "base_units_per_contract": units}


def assess_okx_orderbook(symbol, side, market="SPOT", depth_levels=10):
    direction = 1.0 if side in ("BUY", "LONG") else -1.0
    base = {"schema_version": 2, "symbol": symbol, "side": side, "market": market, "observed_at": _now()}
    try:
        book = book_snapshot(symbol, market, depth_levels)
        bids, asks, mid = book["bids"], book["asks"], book["mid"]
        bid_depth = sum(p * q for p, q in bids)
        ask_depth = sum(p * q for p, q in asks)
        imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)
        spread = (asks[0][0] - bids[0][0]) / mid * 10000
        micro = (asks[0][0] * bids[0][1] + bids[0][0] * asks[0][1]) / (bids[0][1] + asks[0][1])
        micro_bps = (micro - mid) / mid * 10000
        checks = {"spread": spread <= (8 if market == "SPOT" else 5),
                  "depth": min(bid_depth, ask_depth) >= (20000 if symbol in ("BTC", "ETH") else 8000),
                  "pressure": direction * imbalance >= -.18, "microprice": direction * micro_bps >= -2}
        return {**base, "available": True, "passes": all(checks.values()),
                "reason": ",".join(k for k, v in checks.items() if not v) or "OK",
                "spread_bps": spread, "bid_depth_usd": bid_depth, "ask_depth_usd": ask_depth,
                "imbalance": imbalance, "microprice_bps": micro_bps,
                "quality_score": max(0, min(100, 50 + 70 * direction * imbalance + 5 * direction * micro_bps - 2 * max(0, spread - 1))),
                "reference_mid": mid, "price_epoch_ms": book["price_epoch_ms"],
                "base_units_per_contract": book["base_units_per_contract"]}
    except Exception as e:
        return {**base, "available": False, "passes": False, "reason": f"orderbook unavailable: {e}"}


@lru_cache(maxsize=4096)
def historical_close(symbol, market, target_epoch):
    """Last CLOSED 1m candle at/before target; <60s lag, no future/current-price fallback."""
    close_ms = (int(target_epoch) // 60) * 60000
    open_ms = close_ms - 60000
    inst = f"{symbol}-USDT" + ("-SWAP" if market == "SWAP" else "")
    rows = okx("/api/v5/market/history-candles", instId=inst, bar="1m", after=open_ms + 1, limit=3)
    matches = [x for x in rows if int(x[0]) == open_ms and len(x) >= 9 and str(x[8]) == "1"]
    if len(matches) != 1 or close_ms > time.time() * 1000:
        raise ValueError("target candle missing, duplicated, unclosed, or future")
    return {"price": positive(matches[0][4]), "price_epoch_ms": close_ms,
            "candle_open_epoch_ms": open_ms, "target_epoch_ms": int(target_epoch) * 1000,
            "lag_seconds": int(target_epoch) - close_ms / 1000,
            "source": "OKX:" + inst + ":1m:closed", "method": "last_closed_minute_at_or_before_target"}


def record_execution_ab(account, symbol, side, reference_price, confidence, gate):
    epoch = int(time.time())
    sample = {"label_version": LABEL_VERSION, "time": _now(), "epoch": epoch,
              "symbol": symbol, "side": side, "reference_price": float(reference_price),
              "confidence": float(confidence), "filter_passed": bool(gate.get("passes")),
              "gate": dict(gate), "outcomes": {}, "mode": "shadow"}
    if gate.get("available") and gate.get("schema_version") == 2:
        sample["evaluation_reference"] = {"price": positive(gate["reference_mid"]),
            "price_epoch_ms": gate["price_epoch_ms"], "source": "OKX:book_mid", "method": "observed_mid"}
    samples = account.setdefault("execution_ab_signals", [])
    samples.append(sample)
    account["execution_ab_signals"] = samples[-500:]


def migrate_legacy(sample):
    if sample.get("label_version") == LABEL_VERSION:
        return
    sample["invalidated_legacy_outcomes"] = sample.pop("outcomes", {})
    sample["outcomes"] = {}
    sample["label_version"] = LABEL_VERSION
    sample["legacy_invalidated_at"] = _now()
    sample["legacy_invalid_reason"] = "late polling price reused across horizons; gate units/timestamps unverified"


def settle_execution_ab(account, symbol, current_price=None, max_requests=None, deadline=None):
    """Mutates shadow records only. Financial balances/positions are untouched.

    current_price is deliberately unused; retained for existing caller compatibility.
    Global request budget and stop-on-source-error protect the core bot's runtime.
    """
    global _label_requests
    limit=36 if max_requests is None else min(36,_label_requests+max(0,int(max_requests)))
    now = int(time.time())
    samples = [s for s in account.get("execution_ab_signals", []) if s.get("symbol") == symbol]
    for s in samples:
        migrate_legacy(s)
    for s in reversed(samples):
        if now < s.get("next_label_retry_epoch", 0):
            continue
        market = s.get("gate", {}).get("market", "SPOT")
        for minutes in (5, 15, 30):
            key = f"{minutes}m"
            target = int(s.get("epoch", now)) + minutes * 60
            if key in s["outcomes"] or now < target + 5 or _label_requests >= limit:
                continue
            if deadline is not None and time.monotonic()>=deadline:return account
            try:
                if not s.get("evaluation_reference"):
                    _label_requests += 1
                    s["evaluation_reference"] = dict(historical_close(symbol, market, int(s["epoch"])))
                if _label_requests>=limit or (deadline is not None and time.monotonic()>=deadline):return account
                _label_requests += 1
                price = historical_close(symbol, market, target)
                ref = s["evaluation_reference"]
                direction = 1 if s.get("side") in ("BUY", "LONG") else -1
                s["outcomes"][key] = {**price, "valid": True, "label_version": LABEL_VERSION,
                    "directional_return_pct": direction * (price["price"] / ref["price"] - 1) * 100,
                    "reference_epoch_ms": ref["price_epoch_ms"], "settled_at": _now(),
                    "observed_after_minutes": (now - int(s["epoch"])) / 60,
                    "ab_eligible": bool(s.get("gate", {}).get("available") and
                                        s.get("gate", {}).get("schema_version") == 2 and
                                        ref.get("method") == "observed_mid")}
                s.pop("label_error", None)
                s.pop("next_label_retry_epoch", None)
            except Exception as e:
                s["label_error"] = {"time": _now(), "horizon": key, "reason": str(e)}
                s["next_label_retry_epoch"] = now + 1800
                return account
    return account
