"""Normalize optional FOMO/GMGN/DeBot research feeds.

External vendor labels are discovery evidence only. They can block a candidate or
change its review priority, but can never bypass the independent Dex/on-chain gate.
No trading, wallet, signing, or order functionality exists here.
"""
from __future__ import annotations
import os
from urllib.parse import urlparse

SOURCE_URL_ENV = {
    "fomo": "MEME_FOMO_FEED_URL",
    "gmgn": "MEME_GMGN_FEED_URL",
    "debot": "MEME_DEBOT_FEED_URL",
}
DEFAULT_ALLOWED_HOSTS = {"gmgn.ai", "www.gmgn.ai", "debot.ai", "www.debot.ai"}

def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)

def _items(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "items", "tokens", "signals", "result"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for nested in ("items", "list", "tokens", "signals"):
                if isinstance(value.get(nested), list):
                    return value[nested]
    return []

def normalize(raw, source):
    if not isinstance(raw, dict):
        return None
    token = raw.get("token") if isinstance(raw.get("token"), dict) else {}
    wallet = raw.get("wallet") if isinstance(raw.get("wallet"), dict) else {}
    risk = raw.get("risk") if isinstance(raw.get("risk"), dict) else {}
    chain = str(raw.get("chain") or raw.get("chainId") or token.get("chain") or "").lower()
    address = str(raw.get("address") or raw.get("tokenAddress") or
                  raw.get("contract_address") or token.get("address") or "")
    if not chain or not address:
        return None
    return {
        "source": source,
        "chain": chain,
        "address": address,
        "smart_buyers": int(_num(raw.get("smart_buyers", wallet.get("buyers", 0)))),
        "smart_holders": int(_num(raw.get("smart_holders", wallet.get("holders", 0)))),
        "smart_sellers": int(_num(raw.get("smart_sellers", wallet.get("sellers", 0)))),
        "wallet_history_days": _num(raw.get("wallet_history_days", wallet.get("history_days", 0))),
        "wallet_sample_tokens": int(_num(raw.get("wallet_sample_tokens", wallet.get("sample_tokens", 0)))),
        "wallet_cross_cycle": _bool(raw.get("wallet_cross_cycle", wallet.get("cross_cycle", False))),
        "paid_promotion": _bool(raw.get("paid_promotion", risk.get("paid_promotion", False))),
        "honeypot": _bool(raw.get("honeypot", risk.get("honeypot", False))),
        "can_sell": raw.get("can_sell", risk.get("can_sell")),
        "top10_pct": _num(raw.get("top10_pct", risk.get("top10_pct", 0))),
        "dev_pct": _num(raw.get("dev_pct", risk.get("dev_pct", 0))),
        "insider_pct": _num(raw.get("insider_pct", risk.get("insider_pct", 0))),
        "organic_volume_score": _num(raw.get("organic_volume_score", risk.get("organic_volume_score", 1)), 1),
    }

def load_optional_feeds(fetcher, environ=None):
    environ = environ or os.environ
    allowed = set(DEFAULT_ALLOWED_HOSTS)
    allowed.update(x.strip().lower() for x in environ.get("MEME_ALLOWED_SIGNAL_HOSTS", "").split(",") if x.strip())
    records, status = [], {}
    for source, env_name in SOURCE_URL_ENV.items():
        url = environ.get(env_name, "").strip()
        if not url:
            status[source] = "NOT_CONFIGURED"
            continue
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in allowed:
            status[source] = "REJECTED_URL"
            continue
        try:
            payload = fetcher(url)
            normalized = [normalize(x, source) for x in _items(payload)]
            normalized = [x for x in normalized if x]
            records.extend(normalized)
            status[source] = f"OK:{len(normalized)}"
        except Exception as exc:
            status[source] = f"ERROR:{type(exc).__name__}"
    return records, status

def merge(records):
    merged = {}
    for row in records:
        key = f"{row['chain']}:{row['address']}"
        item = merged.setdefault(key, {"sources": set(), "records": []})
        item["sources"].add(row["source"])
        item["records"].append(row)
    for item in merged.values():
        item["sources"] = sorted(item["sources"])
    return merged

def evaluate(item):
    if not item:
        return {"sources": [], "bonus": 0.0, "blockers": [], "qualified_wallet_sources": []}
    records = item.get("records", [])
    blockers = []
    if any(x.get("paid_promotion") for x in records): blockers.append("PAID_PROMOTION")
    if any(x.get("honeypot") for x in records): blockers.append("HONEYPOT_FLAG")
    if any(x.get("can_sell") is False for x in records): blockers.append("SELLABILITY_FAILED")
    if any(x.get("top10_pct", 0) > 50 for x in records): blockers.append("TOP10_CONCENTRATION")
    if any(x.get("dev_pct", 0) > 10 for x in records): blockers.append("DEV_CONCENTRATION")
    if any(x.get("insider_pct", 0) > 20 for x in records): blockers.append("INSIDER_CONCENTRATION")
    if any(x.get("organic_volume_score", 1) < .35 for x in records): blockers.append("LIKELY_WASH_VOLUME")
    qualified = sorted({
        x["source"] for x in records
        if x.get("wallet_history_days", 0) >= 30
        and x.get("wallet_sample_tokens", 0) >= 20
        and x.get("wallet_cross_cycle")
        and x.get("smart_holders", 0) >= 2
        and x.get("can_sell") is not False
    })
    sources = sorted(item.get("sources", []))
    bonus = min(8.0, 2.0 * len(sources)) + min(4.0, 2.0 * len(qualified))
    if blockers:
        bonus = 0.0
    return {
        "sources": sources,
        "bonus": bonus,
        "blockers": blockers,
        "qualified_wallet_sources": qualified,
        "discovery_only": True,
    }
