#!/usr/bin/env python3
"""Polymarket public-data research and PAPER-ONLY execution simulator.

No private keys, signatures, wallet connections, or order endpoints exist here.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import re
import statistics
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

DATA = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
USER_AGENT = "Polymarket-Paper-Research/1.0 (public-data; no-ordering)"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def number(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def parse_jsonish(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        out = json.loads(value)
        return out if isinstance(out, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class PublicAPI:
    def __init__(self, timeout: int = 25, retries: int = 3):
        self.timeout = timeout
        self.retries = retries

    def get(self, base: str, path: str, params: dict[str, Any] | None = None) -> Any:
        query = urllib.parse.urlencode(params or {}, doseq=True)
        url = f"{base}{path}" + (f"?{query}" if query else "")
        error: Exception | None = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    return json.load(response)
            except Exception as exc:  # network fallback is part of normal operation
                error = exc
                time.sleep(0.4 * (2**attempt))
        raise RuntimeError(f"GET failed after {self.retries} attempts: {url}: {error}")


@dataclasses.dataclass
class WalletScore:
    address: str
    name: str
    score: float
    pnl: float
    volume: float
    trades: int
    markets: int
    active_days: int
    active_weeks: int
    concentration: float
    two_sided_ratio: float
    late_trade_ratio: float
    recent_activity: float
    flags: list[str]
    flows: dict[str, dict[str, float]]


def discover_wallets(api: PublicAPI, cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    merged: dict[str, dict[str, Any]] = {}
    rows_seen = 0
    jobs = [(category, period, page) for category in cfg["leaderboard_categories"]
            for period in cfg["leaderboard_periods"]
            for page in range(int(cfg["leaderboard_pages_per_slice"]))]
    def page(job: tuple[str, str, int]) -> list[dict[str, Any]]:
        category, period, index = job
        try:
            return api.get(DATA, "/v1/leaderboard", {"category":category, "timePeriod":period, "orderBy":"PNL",
                                                       "limit":50, "offset":index*50})
        except Exception:
            return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, int(cfg["wallet_workers"]))) as pool:
        pages = list(pool.map(page, jobs))
    for rows in pages:
        rows_seen += len(rows)
        for row in rows:
            address = str(row.get("proxyWallet", "")).lower()
            if not re.fullmatch(r"0x[0-9a-f]{40}", address):
                continue
            old = merged.get(address, {})
            merged[address] = {"address":address, "name":row.get("userName") or old.get("name") or address[:10],
                               "pnl":max(number(row.get("pnl")), number(old.get("pnl"))),
                               "volume":max(number(row.get("vol")), number(old.get("volume"))),
                               "appearances":int(old.get("appearances", 0))+1}
    ranked = sorted(merged.values(), key=lambda x: (x["appearances"], math.log1p(max(0, x["pnl"]))), reverse=True)
    return ranked, rows_seen


def wallet_quality(candidate: dict[str, Any], trades: list[dict[str, Any]], now_ts: int, cfg: dict[str, Any]) -> WalletScore:
    address = candidate["address"]
    if not trades:
        return WalletScore(address, candidate["name"], 0, candidate["pnl"], candidate["volume"], 0, 0, 0, 0, 1, 0, 0, 0, ["no_90d_trades"], {})
    by_market: dict[str, float] = defaultdict(float)
    directions: dict[str, set[str]] = defaultdict(set)
    days, weeks = set(), set()
    late = 0
    recent_notional = 0.0
    total_notional = 0.0
    flows: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for t in trades:
        ts = int(number(t.get("timestamp")))
        day = dt.datetime.fromtimestamp(ts, dt.timezone.utc).date()
        days.add(day.isoformat())
        weeks.add(f"{day.isocalendar().year}-{day.isocalendar().week}")
        cid = str(t.get("conditionId", ""))
        asset = str(t.get("asset", ""))
        outcome = str(t.get("outcome", ""))
        key = f"{cid}|{asset}|{outcome}"
        notional = max(0.0, number(t.get("size")) * number(t.get("price")))
        side = str(t.get("side", "BUY")).upper()
        sign = 1.0 if side == "BUY" else -1.0
        by_market[cid] += notional
        directions[cid].add(side)
        total_notional += notional
        if now_ts - ts <= 7 * 86400:
            recent_notional += notional
        if number(t.get("price")) >= 0.94:
            late += 1
        f = flows[key]
        f["net_shares"] += sign * number(t.get("size"))
        f["net_notional"] += sign * notional
        f["last_ts"] = max(f.get("last_ts", 0), ts)
        f["price_notional"] += number(t.get("price")) * notional
        f["weight"] += notional
        f["title"] = t.get("title", "")
        f["event_slug"] = t.get("eventSlug", "")
        f["condition_id"] = cid
        f["asset"] = asset
        f["outcome"] = outcome
    notionals = sorted(by_market.values(), reverse=True)
    concentration = notionals[0] / total_notional if total_notional else 1.0
    two_sided = sum(1 for s in directions.values() if len(s) > 1) / max(1, len(directions))
    late_ratio = late / max(1, len(trades))
    active_days = len(days)
    active_weeks = len(weeks)
    roi_proxy = candidate["pnl"] / max(candidate["volume"], 1.0)
    sample = 1 - math.exp(-len(trades) / 80)
    breadth = clamp(len(by_market) / 30)
    cadence = clamp(active_weeks / 8)
    persistence = clamp(candidate["appearances"] / 5)
    plausible_roi = math.exp(-max(0.0, roi_proxy - 0.35) * 4)
    concentration_penalty = clamp((concentration - 0.35) / 0.55)
    late_penalty = clamp((late_ratio - 0.35) / 0.55)
    churn_penalty = clamp((two_sided - 0.75) / 0.25)
    score = clamp(0.23 * sample + 0.20 * breadth + 0.20 * cadence + 0.17 * persistence + 0.20 * plausible_roi
                  - 0.18 * concentration_penalty - 0.12 * late_penalty - 0.08 * churn_penalty)
    flags: list[str] = []
    if concentration > 0.65: flags.append("one_event_dominance")
    if late_ratio > 0.60: flags.append("late_odds_specialist")
    if two_sided > 0.85: flags.append("possible_market_making_or_churn")
    if roi_proxy > 0.65: flags.append("extreme_return_needs_review")
    if active_days < cfg["min_wallet_active_days"]: flags.append("short_history")
    if len(by_market) < cfg["min_wallet_markets"]: flags.append("low_breadth")
    if len(trades) < cfg["min_wallet_trades"]: flags.append("small_sample")
    clean_flows = {k: dict(v) for k, v in flows.items() if v.get("net_notional", 0) > 0 and now_ts - v.get("last_ts", 0) <= 7 * 86400}
    return WalletScore(address, candidate["name"], round(score, 6), candidate["pnl"], candidate["volume"],
                       len(trades), len(by_market), active_days, active_weeks, round(concentration, 4),
                       round(two_sided, 4), round(late_ratio, 4), round(recent_notional, 2), flags, clean_flows)


def deep_audit(api: PublicAPI, candidates: list[dict[str, Any]], cfg: dict[str, Any], now_ts: int) -> tuple[list[WalletScore], int]:
    start = now_ts - int(cfg["lookback_days"]) * 86400
    selected = candidates[: int(cfg["deep_wallet_limit"])]
    def one(c: dict[str, Any]) -> WalletScore:
        try:
            # Stratified windows prevent a high-frequency wallet's latest trades from
            # crowding the whole 90-day sample into one recent day.
            window = 30 * 86400
            per_window = max(50, int(cfg["wallet_trade_limit"]) // 3)
            trades = []
            cursor = start
            while cursor < now_ts:
                end = min(now_ts, cursor + window)
                trades.extend(api.get(DATA, "/trades", {"user":c["address"], "start":cursor, "end":end,
                                                        "limit":per_window, "takerOnly":"false"}))
                cursor = end
            return wallet_quality(c, trades, now_ts, cfg)
        except Exception:
            return wallet_quality(c, [], now_ts, cfg)
    with concurrent.futures.ThreadPoolExecutor(max_workers=int(cfg["wallet_workers"])) as pool:
        scores = list(pool.map(one, selected))
    scores.sort(key=lambda x: x.score, reverse=True)
    return scores, len(selected)


def fetch_markets(api: PublicAPI, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    remaining = int(cfg["market_scan_limit"])
    offset = 0
    while remaining > 0:
        limit = min(100, remaining)
        rows = api.get(GAMMA, "/markets", {"active": "true", "closed": "false", "limit": limit,
                                            "offset": offset, "order": "volume24hr", "ascending": "false"})
        markets.extend(rows)
        if len(rows) < limit: break
        remaining -= len(rows); offset += len(rows)
    return markets


def consensus_signals(wallets: list[WalletScore], market_by_condition: dict[str, dict[str, Any]], cfg: dict[str, Any], now_ts: int) -> list[dict[str, Any]]:
    aggregate: dict[str, dict[str, Any]] = {}
    qualified = [w for w in wallets if w.score >= cfg["min_wallet_score"] and not {"small_sample", "low_breadth", "short_history"} & set(w.flags)]
    for w in qualified:
        for key, flow in w.flows.items():
            cid = flow["condition_id"]
            market = market_by_condition.get(cid)
            if not market: continue
            rec = aggregate.setdefault(key, {"wallets": [], "weight": 0.0, "notional": 0.0, **flow})
            freshness = math.exp(-(now_ts - flow["last_ts"]) / (3 * 86400))
            weight = w.score * math.log1p(max(0.0, flow["net_notional"])) * freshness
            rec["wallets"].append({"address": w.address, "name": w.name, "score": w.score,
                                   "net_notional": round(flow["net_notional"], 2), "last_ts": int(flow["last_ts"])})
            rec["weight"] += weight
            rec["notional"] += flow["net_notional"]
    signals = []
    for rec in aggregate.values():
        independent = len({x["address"] for x in rec["wallets"]})
        if independent < 3: continue
        market = market_by_condition[rec["condition_id"]]
        tokens = parse_jsonish(market.get("clobTokenIds")); outcomes = parse_jsonish(market.get("outcomes")); prices = parse_jsonish(market.get("outcomePrices"))
        try: idx = tokens.index(rec["asset"])
        except ValueError: continue
        price = number(prices[idx] if idx < len(prices) else 0)
        if not 0.03 < price < 0.97: continue
        conviction = clamp((math.log1p(rec["weight"]) - 3.0) / 7.0)
        independence = clamp((independent - 2) / 8)
        # A wallet signal is evidence, not a price oracle. Use a small, varying
        # premium and cap it at 5.5 points instead of manufacturing a fixed edge.
        premium = 0.018 + 0.037 * (0.65 * conviction + 0.35 * independence)
        fair = clamp(price + min(0.055, premium), 0.01, 0.99)
        signals.append({"type": "wallet_consensus", "condition_id": rec["condition_id"], "asset": rec["asset"],
                        "title": market.get("question", rec["title"]), "event_slug": rec["event_slug"],
                        "outcome": outcomes[idx] if idx < len(outcomes) else rec["outcome"], "price": price,
                        "fair_probability": fair, "gross_edge": fair - price, "wallet_count": independent,
                        "wallet_notional": round(rec["notional"], 2), "wallets": sorted(rec["wallets"], key=lambda x:x["score"], reverse=True)[:8],
                        "liquidity": number(market.get("liquidityNum") or market.get("liquidity")),
                        "volume24h": number(market.get("volume24hr")), "category": market_category(market)})
    return signals


def orderbook(api: PublicAPI, token: str) -> tuple[float, float, float]:
    book = api.get(CLOB, "/book", {"token_id": token})
    bids = [(number(x.get("price")), number(x.get("size"))) for x in book.get("bids", [])]
    asks = [(number(x.get("price")), number(x.get("size"))) for x in book.get("asks", [])]
    best_bid = max((x[0] for x in bids), default=0.0)
    valid_asks = [x for x in asks if x[0] > 0]
    best_ask = min((x[0] for x in valid_asks), default=1.0)
    ask_depth = sum(p * s for p, s in valid_asks if p <= best_ask + 0.01)
    return best_bid, best_ask, ask_depth


def structural_arbs(api: PublicAPI, markets: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    eligible = [m for m in markets if len(parse_jsonish(m.get("clobTokenIds"))) == 2 and number(m.get("liquidityNum") or m.get("liquidity")) >= cfg["min_market_liquidity"]]
    eligible = eligible[: int(cfg["orderbook_scan_limit"])]
    def inspect(m: dict[str, Any]) -> dict[str, Any] | None:
        try:
            tokens = parse_jsonish(m.get("clobTokenIds")); outcomes = parse_jsonish(m.get("outcomes"))
            books = [orderbook(api, str(t)) for t in tokens]
            cost = sum(x[1] for x in books)
            edge = 1.0 - cost - cfg["estimated_roundtrip_cost"] - cfg["slippage_buffer"]
            if edge < cfg["structural_arb_min_edge"]: return None
            return {"type": "complete_set_arb", "condition_id": m.get("conditionId"), "title": m.get("question"),
                    "tokens": tokens, "outcomes": outcomes, "asks": [x[1] for x in books], "depth_usdt": min(x[2] for x in books),
                    "gross_edge": 1.0 - cost, "net_edge": edge, "liquidity": number(m.get("liquidityNum") or m.get("liquidity")),
                    "volume24h": number(m.get("volume24hr")), "category": market_category(m)}
        except Exception: return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        return [x for x in pool.map(inspect, eligible) if x]


STOPWORDS = {"will","the","a","an","be","to","of","in","on","by","before","after","for","and","or","is","at","this","that","with","win"}


def news_context(title: str, enabled: bool, recency_hours: int) -> dict[str, Any]:
    if not enabled: return {"score": 0.5, "headlines": [], "status": "disabled"}
    terms = [x for x in re.findall(r"[A-Za-z0-9]{3,}", title) if x.lower() not in STOPWORDS][:7]
    if len(terms) < 2: return {"score": 0.5, "headlines": [], "status": "insufficient_terms"}
    query = " ".join(terms)
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": f'"{query}" when:{max(1, recency_hours//24)}d', "hl":"en-US", "gl":"US", "ceid":"US:en"})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=12) as response: root = ET.fromstring(response.read())
        headlines = [i.findtext("title", "") for i in root.findall(".//item")[:6]]
        overlap = []
        wanted = {x.lower() for x in terms}
        for h in headlines:
            got = set(re.findall(r"[a-z0-9]{3,}", h.lower()))
            overlap.append(len(wanted & got) / max(1, len(wanted)))
        return {"score": round(clamp(statistics.mean(overlap) if overlap else 0.35), 4), "headlines": headlines[:3], "status": "ok"}
    except Exception as exc:
        return {"score": 0.5, "headlines": [], "status": f"unavailable:{type(exc).__name__}"}


def market_category(market: dict[str, Any]) -> str:
    tags = market.get("tags") or []
    if tags and isinstance(tags[0], dict): return str(tags[0].get("label") or tags[0].get("slug") or "OTHER").upper()
    return str(market.get("category") or "OTHER").upper()


def load_account(path: Path, initial: float) -> dict[str, Any]:
    if path.exists():
        try:
            a=json.loads(path.read_text(encoding="utf-8"))
            required={"initial_cash_usdt","cash_usdt","positions","closed_trades",
                      "realized_pnl_usdt","fees_and_slippage_usdt","high_watermark_usdt"}
            if (not isinstance(a,dict) or not required.issubset(a)
                    or float(a["initial_cash_usdt"])!=float(initial)
                    or not isinstance(a["positions"],list) or not isinstance(a["closed_trades"],list)
                    or any(not math.isfinite(float(a[k])) for k in
                           ("cash_usdt","realized_pnl_usdt","fees_and_slippage_usdt","high_watermark_usdt"))):
                raise ValueError("invalid existing financial state")
            return a
        except (ValueError, TypeError, OSError) as e:
            raise RuntimeError(f"Polymarket account unreadable; refusing history reset: {e}") from e
    now = utc_now().isoformat()
    return {"mode":"PAPER_ONLY", "initial_cash_usdt":initial, "cash_usdt":initial, "equity_usdt":initial,
            "high_watermark_usdt":initial, "positions":[], "closed_trades":[], "created_at":now, "updated_at":now,
            "day":utc_now().date().isoformat(), "day_start_equity_usdt":initial,
            "realized_pnl_usdt":0.0, "fees_and_slippage_usdt":0.0}


def mark_and_exit(account: dict[str, Any], price_map: dict[str, float], cfg: dict[str, Any], now_ts: int) -> None:
    kept = []
    for p in account["positions"]:
        mark = price_map.get(p["asset"], p["mark_price"])
        p["mark_price"] = mark
        age_days = (now_ts - p["opened_ts"]) / 86400
        reason = None
        if p["signal_type"] == "wallet_consensus" and cfg.get("retire_uncalibrated_positions", True):
            if not cfg.get("wallet_consensus_execution_enabled", False):
                reason = "wallet_consensus_shadow_only"
            elif not p.get("strategy_version"):
                reason = "legacy_uncalibrated_signal"
        if reason is None and p["signal_type"] == "wallet_consensus" and p.get("fair_probability", mark) - mark < cfg["estimated_roundtrip_cost"]: reason = "edge_converged"
        elif age_days >= cfg["max_holding_days"]: reason = "max_holding_time"
        elif mark <= p["entry_price"] * 0.72: reason = "risk_stop"
        if reason:
            proceeds = p["shares"] * mark * (1 - cfg["estimated_roundtrip_cost"] / 2)
            pnl = proceeds - p["cost_usdt"]
            account["cash_usdt"] += proceeds
            account["realized_pnl_usdt"] += pnl
            account["fees_and_slippage_usdt"] += p["shares"] * mark * cfg["estimated_roundtrip_cost"] / 2
            account["closed_trades"].append({**p, "closed_ts":now_ts, "exit_price":mark, "pnl_usdt":round(pnl, 4), "reason":reason})
        else: kept.append(p)
    account["positions"] = kept
    account["closed_trades"] = account["closed_trades"][-300:]


def size_order(equity: float, price: float, fair: float, confidence: float, liquidity: float, cfg: dict[str, Any]) -> float:
    if not 0 < price < 1 or fair <= price: return 0.0
    b = (1 - price) / price
    kelly = max(0.0, (b * fair - (1 - fair)) / max(b, 1e-9))
    fraction = min(cfg["max_trade_fraction"], cfg["fractional_kelly"] * kelly * clamp(confidence))
    liquidity_cap = max(0.0, liquidity * 0.002)
    return round(min(equity * fraction, liquidity_cap), 2)


def execute_paper(account: dict[str, Any], signals: list[dict[str, Any]], cfg: dict[str, Any], now_ts: int) -> list[dict[str, Any]]:
    equity = account["cash_usdt"] + sum(p["shares"] * p["mark_price"] for p in account["positions"])
    hwm = max(account.get("high_watermark_usdt", equity), equity)
    drawdown = 1 - equity / max(hwm, 1)
    day_start = number(account.get("day_start_equity_usdt"), equity)
    daily_loss = 1 - equity / max(day_start, 1)
    if drawdown >= cfg["max_drawdown_fraction"] or daily_loss >= cfg["max_daily_loss_fraction"]: return []
    exposure = sum(p["shares"] * p["mark_price"] for p in account["positions"])
    existing = {p["asset"] for p in account["positions"]}
    category_exposure = Counter()
    for p in account["positions"]: category_exposure[p.get("category", "OTHER")] += p["shares"] * p["mark_price"]
    fills = []
    for s in sorted(signals, key=lambda x: x.get("net_edge", x.get("gross_edge", 0)), reverse=True):
        if len(account["positions"]) >= cfg["max_open_positions"]: break
        if s["type"] != "wallet_consensus": continue  # paired arbitrage requires atomic legs; observe only in this simulator
        if not cfg.get("wallet_consensus_execution_enabled", False): continue  # uncalibrated statistical signal stays shadow-only
        asset = s["asset"]
        if asset in existing: continue
        net_edge = s["fair_probability"] - s["price"] - cfg["estimated_roundtrip_cost"] - cfg["slippage_buffer"]
        if net_edge < cfg["min_net_edge"]: continue
        confidence = clamp(0.35 + 0.08 * min(6, s["wallet_count"]) + 0.20 * s.get("news", {}).get("score", 0.5))
        amount = size_order(equity, s["price"] + cfg["slippage_buffer"] / 2, s["fair_probability"], confidence, s["liquidity"], cfg)
        amount = min(amount, equity * cfg["max_total_exposure_fraction"] - exposure,
                     equity * cfg["max_category_fraction"] - category_exposure[s["category"]], account["cash_usdt"])
        if amount < cfg["min_order_usdt"]: continue
        entry = min(0.995, s["price"] + cfg["slippage_buffer"] / 2)
        fee = amount * cfg["estimated_roundtrip_cost"] / 2
        shares = (amount - fee) / entry
        p = {"id": hashlib.sha256(f"{asset}:{now_ts}".encode()).hexdigest()[:16], "signal_type":s["type"],
             "strategy_version":"consensus_v2",
             "condition_id":s["condition_id"], "asset":asset, "title":s["title"], "outcome":s["outcome"],
             "category":s["category"], "entry_price":entry, "mark_price":s["price"], "fair_probability":s["fair_probability"],
             "shares":shares, "cost_usdt":amount, "opened_ts":now_ts, "confidence":confidence,
             "wallet_count":s["wallet_count"], "net_edge_at_entry":net_edge}
        account["positions"].append(p); account["cash_usdt"] -= amount; account["fees_and_slippage_usdt"] += fee
        exposure += amount; category_exposure[s["category"]] += amount; existing.add(asset)
        fills.append({"id":p["id"], "title":p["title"], "outcome":p["outcome"], "amount_usdt":amount,
                      "entry_price":round(entry,4), "confidence":round(confidence,4), "net_edge":round(net_edge,4)})
    return fills


def run(config_path: Path) -> dict[str, Any]:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    if cfg.get("paper_only") is not True: raise SystemExit("Safety lock: paper_only must be true")
    base = config_path.parent; state_dir = base / cfg["state_dir"]
    api = PublicAPI(); now = utc_now(); now_ts = int(now.timestamp())
    candidates, rows_seen = discover_wallets(api, cfg)
    wallets, audited = deep_audit(api, candidates, cfg, now_ts)
    markets = fetch_markets(api, cfg)
    market_by_condition = {str(m.get("conditionId")): m for m in markets}
    consensus = consensus_signals(wallets, market_by_condition, cfg, now_ts)
    arbs = structural_arbs(api, markets, cfg)
    for s in sorted(consensus, key=lambda x:x["gross_edge"], reverse=True)[:12]:
        s["news"] = news_context(s["title"], cfg["news_enabled"], int(cfg["news_recency_hours"]))
    account_path = state_dir / "paper_account.json"; account = load_account(account_path, cfg["initial_cash_usdt"])
    price_map = {}
    for m in markets:
        for token, price in zip(parse_jsonish(m.get("clobTokenIds")), parse_jsonish(m.get("outcomePrices"))): price_map[str(token)] = number(price)
    mark_and_exit(account, price_map, cfg, now_ts)
    marked_equity = account["cash_usdt"] + sum(p["shares"] * p["mark_price"] for p in account["positions"])
    today = now.date().isoformat()
    if account.get("day") != today:
        account["day"] = today
        account["day_start_equity_usdt"] = marked_equity
    fills = execute_paper(account, consensus, cfg, now_ts)
    account["equity_usdt"] = round(account["cash_usdt"] + sum(p["shares"] * p["mark_price"] for p in account["positions"]), 4)
    account["high_watermark_usdt"] = round(max(account.get("high_watermark_usdt", account["equity_usdt"]), account["equity_usdt"]), 4)
    account["updated_at"] = now.isoformat(); atomic_json(account_path, account)
    qualified = [dataclasses.asdict(w) for w in wallets if w.score >= cfg["min_wallet_score"]]
    for w in qualified: w.pop("flows", None)
    report = {"generated_at":now.isoformat(), "mode":"PAPER_ONLY", "lookback_days":cfg["lookback_days"],
              "coverage":{"leaderboard_rows_seen":rows_seen, "unique_wallet_candidates":len(candidates), "wallets_deep_audited":audited,
                          "qualified_wallets":len(qualified), "active_markets_scanned":len(markets), "orderbooks_scanned":min(len(markets),cfg["orderbook_scan_limit"])},
              "account":{"initial_cash_usdt":account["initial_cash_usdt"], "cash_usdt":round(account["cash_usdt"],4),
                         "equity_usdt":account["equity_usdt"], "realized_pnl_usdt":round(account["realized_pnl_usdt"],4),
                         "open_positions":len(account["positions"]), "fees_and_slippage_usdt":round(account["fees_and_slippage_usdt"],4)},
              "top_wallets":qualified[:30], "structural_arbitrage":arbs[:20],
              "mispricing_signals":sorted(consensus,key=lambda x:x["gross_edge"],reverse=True)[:30], "new_paper_fills":fills,
              "safety":{"live_order_code_present":False, "wallet_or_key_required":False,
                        "wallet_consensus_execution_enabled":bool(cfg.get("wallet_consensus_execution_enabled", False)),
                        "daily_loss_guard_enabled":True,
                        "note":"Complete-set opportunities and uncalibrated wallet-consensus signals are observation-only. No statistical signal is booked until forward calibration establishes usable edge."}}
    atomic_json(state_dir / "latest_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    args = parser.parse_args(); run(Path(args.config).resolve())


if __name__ == "__main__": main()
