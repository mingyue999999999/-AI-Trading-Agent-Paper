import csv
import io
import json
import math
import os
import sys
import random
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError
from datetime import datetime, timezone, timedelta

ROOT_DIR=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path: sys.path.insert(0,ROOT_DIR)
from execution_quality import assess_okx_orderbook, record_execution_ab, settle_execution_ab
import paper_runtime as runtime

# ============================================================
# AI TRADING AGENT V3 FULL - PAPER ONLY
# BTC / ETH / XRP / SOL / BNB
#
# 维度：
# 1) 技术面
# 2) Market Regime
# 3) 衍生品 Funding / OI
# 4) 宏观：FRED（2Y/10Y/Fed Funds/CPI）
# 5) ETF：CoinGlass（有 API Key 时启用）
# 6) 交易所/链上资金流：CoinGlass（有相应套餐权限时启用）
# 7) 稳定币流动性：CoinGecko USDT/USDC 市值变化
# 8) Fear & Greed
#
# 设计原则：
# - 永远 PAPER ONLY
# - 单一外部数据源失败不导致整机停止
# - 缺失维度不按利空处理，而是从综合权重中移除
# - 保留原 paper_account.json 兼容性
# ============================================================

VERSION = "合约机器人正式版"
LIVE_TRADING = False
ORDERFLOW_ENFORCEMENT = False

INITIAL_BALANCE = 10000.0
STATE_FILE = "合约/模拟账户.json"

FEE_RATE = 0.001
MAX_POSITION_PCT = 0.20
MAX_TOTAL_EXPOSURE = 0.50
RISK_PER_TRADE = 0.01
STOP_LOSS_PCT = 0.06
TAKE_PROFIT_PCT = 0.15
TRAILING_STOP_PCT = 0.07
MIN_TRADE_USDT = 50.0
MAX_DRAWDOWN_PCT = 0.15

# 综合置信度 -100 ~ +100
BUY_CONFIDENCE = 35.0
SELL_CONFIDENCE = -30.0

COINS = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "ripple": "XRP",
    "solana": "SOL",
    "binancecoin": "BNB",
}
SYMBOLS = list(COINS.values())

# 各大维度基础权重；数据缺失时自动重新归一化
WEIGHTS = {
    "technical": 0.25,
    "derivatives": 0.15,
    "macro": 0.15,
    "etf": 0.15,
    "flow": 0.12,
    "stablecoin": 0.08,
    "sentiment": 0.10,
}

# 可选：在 GitHub Actions Secrets 中添加 COINGLASS_API_KEY
# 没有 Key 时 ETF/专业资金流自动显示 N/A，不会误扣分
COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY", "").strip()
OI_STATE_FILE = "合约/持仓量记录.json"
FREE_ETF_BASE = "https://xoomar.com/api/markets/etf-flows"


# ============================================================
# 通用网络工具：429/5xx 自动退避重试
# ============================================================

def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def http_get(url, headers=None, timeout=25, retries=4):
    budget=90 if urllib.parse.urlsplit(url).hostname=='api.coingecko.com' else 12
    return runtime.bounded_http_get(url,headers,timeout,retries,budget_seconds=budget)


def get_json(url, headers=None, retries=4):
    return json.loads(http_get(url, headers=headers, retries=retries))


def safe_json(url, headers=None, retries=2, quiet=False):
    try:
        return get_json(url, headers=headers, retries=retries)
    except Exception as e:
        if not quiet:
            print("接口降级：", url.split("?")[0], "|", e)
        return None


# ============================================================
# CoinGecko：行情/历史
# ============================================================

def get_market_data():
    ids = ",".join(COINS.keys())
    return get_json(
        "https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=usd&ids={urllib.parse.quote(ids)}"
        "&price_change_percentage=24h", retries=5
    )


def get_history(coin_id):
    data = get_json(
        f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
        "?vs_currency=usd&days=365&interval=daily", retries=5
    )
    time.sleep(1.8)
    return (
        [float(x[1]) for x in data["prices"]],
        [float(x[1]) for x in data["total_volumes"]],
    )


# ============================================================
# 技术指标
# ============================================================

def ema_series(values, period):
    if len(values) < period:
        return []
    m = 2 / (period + 1)
    current = sum(values[:period]) / period
    out = [current]
    for value in values[period:]:
        current = (value - current) * m + current
        out.append(current)
    return out


def ema(values, period):
    x = ema_series(values, period)
    return x[-1] if x else None


def rsi(values, period=14):
    if len(values) <= period:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        c = values[i] - values[i - 1]
        gains.append(max(c, 0))
        losses.append(max(-c, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def macd(values):
    if len(values) < 35:
        return None, None, None
    fast = ema_series(values, 12)
    slow = ema_series(values, 26)
    fast = fast[len(fast) - len(slow):]
    line = [f - s for f, s in zip(fast, slow)]
    sig = ema_series(line, 9)
    if not sig:
        return None, None, None
    return line[-1], sig[-1], line[-1] - sig[-1]


def bollinger(values, period=20):
    if len(values) < period:
        return None, None, None
    x = values[-period:]
    mid = sum(x) / period
    std = math.sqrt(sum((v - mid) ** 2 for v in x) / period)
    return mid - 2 * std, mid, mid + 2 * std


def volatility(values, period=14):
    if len(values) <= period:
        return None
    x = [
        abs((values[i] - values[i - 1]) / values[i - 1])
        for i in range(1, len(values)) if values[i - 1]
    ]
    return sum(x[-period:]) / period * 100 if x else None


def momentum(values, days):
    if len(values) <= days or not values[-days - 1]:
        return None
    return (values[-1] / values[-days - 1] - 1) * 100


def volume_change(volumes):
    if len(volumes) < 8:
        return None
    avg = sum(volumes[-8:-1]) / 7
    return (volumes[-1] / avg - 1) * 100 if avg else 0.0


def support_resistance(values, days=30):
    x = values[-days:]
    return min(x), max(x)


def market_regime(price, prices):
    e20, e50, e200 = ema(prices, 20), ema(prices, 50), ema(prices, 200)
    vol = volatility(prices)
    if None in (e20, e50, e200):
        return "UNKNOWN"
    if price > e20 > e50 > e200:
        return "BULL_TREND"
    if price < e20 < e50 < e200:
        return "BEAR_TREND"
    if vol is not None and vol >= 4:
        return "HIGH_VOLATILITY"
    return "RANGE"


def technical_dimension(price, change24, prices, volumes):
    points, max_points = 0.0, 0.0
    reasons = []

    e20, e50, e200 = ema(prices, 20), ema(prices, 50), ema(prices, 200)
    rv = rsi(prices)
    mv, sv, hist = macd(prices)
    lower, middle, upper = bollinger(prices)
    m7, m30 = momentum(prices, 7), momentum(prices, 30)
    vc = volume_change(volumes)
    vol = volatility(prices)
    regime = market_regime(price, prices)

    if e20 and e50:
        max_points += 2
        if price > e20 > e50:
            points += 2; reasons.append("价格 > EMA20 > EMA50")
        elif price < e20 < e50:
            points -= 2; reasons.append("价格 < EMA20 < EMA50")

    if e200:
        max_points += 2
        points += 2 if price > e200 else -2
        reasons.append("价格在EMA200" + ("上方" if price > e200 else "下方"))

    if rv is not None:
        max_points += 2
        if 50 <= rv < 68:
            points += 1
        elif rv >= 75:
            points -= 2
        elif rv <= 30:
            points += 1
        elif rv < 45:
            points -= 1
        reasons.append(f"RSI={rv:.1f}")

    if mv is not None and sv is not None:
        max_points += 2
        points += 1 if mv > sv else -1
        if hist is not None:
            points += 1 if hist > 0 else -1
        reasons.append("MACD " + ("偏强" if mv > sv else "偏弱"))

    if m7 is not None:
        max_points += 1
        if m7 > 3: points += 1
        elif m7 < -3: points -= 1

    if m30 is not None:
        max_points += 1
        if m30 > 5: points += 1
        elif m30 < -5: points -= 1

    if middle is not None:
        max_points += 1
        points += 1 if price > middle else -1

    if vc is not None:
        max_points += 1
        if vc > 20 and change24 > 0: points += 1
        elif vc > 20 and change24 < 0: points -= 1

    max_points += 2
    if regime == "BULL_TREND":
        points += 2
    elif regime == "BEAR_TREND":
        points -= 2
    reasons.append("Regime=" + regime)

    score = 100 * points / max_points if max_points else None
    detail = {
        "ema20": e20, "ema50": e50, "ema200": e200, "rsi": rv,
        "macd": mv, "macd_signal": sv, "macd_hist": hist,
        "bollinger_lower": lower, "bollinger_middle": middle,
        "bollinger_upper": upper, "momentum7": m7, "momentum30": m30,
        "volume_change": vc, "volatility": vol, "regime": regime,
    }
    return score, reasons, detail


# ============================================================
# 衍生品：Bybit 主源 + OKX 备用源
# GitHub Actions 上 Binance Futures 可能返回 HTTP 451，因此 V3.1 不依赖 Binance
# ============================================================

def get_derivatives_bybit(symbol):
    pair = f"{symbol}USDT"
    out = {
        "funding_rate": None, "open_interest": None,
        "oi_change_pct": None, "source": "Bybit", "status": "N/A"
    }

    ticker = safe_json(
        "https://api.bybit.com/v5/market/tickers?"
        + urllib.parse.urlencode({"category": "linear", "symbol": pair}),
        retries=2,
    )
    try:
        row = ticker["result"]["list"][0]
        out["funding_rate"] = float(row["fundingRate"]) * 100
        out["open_interest"] = float(row["openInterest"])
    except Exception:
        pass

    hist = safe_json(
        "https://api.bybit.com/v5/market/open-interest?"
        + urllib.parse.urlencode({
            "category": "linear", "symbol": pair,
            "intervalTime": "1h", "limit": 3
        }),
        retries=2,
    )
    try:
        rows = hist["result"]["list"]
        rows = sorted(rows, key=lambda x: int(x["timestamp"]))
        if len(rows) >= 2:
            old_oi = float(rows[0]["openInterest"])
            new_oi = float(rows[-1]["openInterest"])
            if old_oi:
                out["oi_change_pct"] = (new_oi / old_oi - 1) * 100
    except Exception:
        pass

    if out["funding_rate"] is not None or out["open_interest"] is not None:
        out["status"] = "OK"
    return out


def load_oi_snapshots():
    try:
        with open(OI_STATE_FILE, "r", encoding="utf-8") as f:
            x = json.load(f)
            return x if isinstance(x, dict) else {}
    except Exception:
        return {}


def save_oi_snapshots(x):
    try:
        with open(OI_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(x, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("OI快照保存失败：", e)


def get_derivatives_okx(symbol):
    inst = f"{symbol}-USDT-SWAP"
    out = {
        "funding_rate": None, "open_interest": None,
        "oi_change_pct": None, "source": "OKX", "status": "N/A"
    }

    funding = safe_json(
        "https://www.okx.com/api/v5/public/funding-rate-history?"
        + urllib.parse.urlencode({"instId": inst, "limit": 1}),
        retries=2,
    )
    try:
        row = funding["data"][0]
        # Prefer realized rate when supplied; otherwise current/estimated rate.
        rate = row.get("realizedRate") or row.get("fundingRate")
        out["funding_rate"] = float(rate) * 100
    except Exception:
        pass

    oi = safe_json(
        "https://www.okx.com/api/v5/public/open-interest?"
        + urllib.parse.urlencode({"instType": "SWAP", "instId": inst}),
        retries=2,
    )
    try:
        row = oi["data"][0]
        # oiUsd is comparable across contract specification changes; fall back to oi.
        current_oi = float(row.get("oiUsd") or row["oi"])
        out["open_interest"] = current_oi

        snaps = load_oi_snapshots()
        prev = snaps.get(symbol)
        now_ts = int(time.time())
        if prev and float(prev.get("oi", 0)) > 0:
            age_h = (now_ts - int(prev.get("ts", now_ts))) / 3600
            # Compare with the previous persisted run. Display age so it is not
            # falsely described as a fixed 1h/24h metric.
            old_oi = float(prev["oi"])
            out["oi_change_pct"] = (current_oi / old_oi - 1) * 100
            out["oi_change_window_h"] = max(age_h, 0)
        snaps[symbol] = {"oi": current_oi, "ts": now_ts}
        save_oi_snapshots(snaps)
    except Exception:
        pass

    if out["funding_rate"] is not None or out["open_interest"] is not None:
        out["status"] = "FALLBACK"
    return out


def get_derivatives(symbol):
    d = get_derivatives_bybit(symbol)
    if d["status"] == "OK":
        return d
    return get_derivatives_okx(symbol)


def derivatives_dimension(change24, d):
    vals, reasons = [], []
    funding = d.get("funding_rate")
    oi_chg = d.get("oi_change_pct")

    if funding is not None:
        if funding >= 0.10: x = -80
        elif funding >= 0.05: x = -40
        elif funding <= -0.05: x = 35
        else: x = 0
        vals.append(x)
        reasons.append(f"{d.get('source')} Funding={funding:+.4f}%")

    if oi_chg is not None:
        if oi_chg >= 2 and change24 > 0: x = 55
        elif oi_chg >= 2 and change24 < 0: x = -55
        elif oi_chg <= -2 and change24 < 0: x = 15
        elif oi_chg <= -2 and change24 > 0: x = -10
        else: x = 0
        vals.append(x)
        window = d.get("oi_change_window_h")
        if window is not None:
            reasons.append(f"{d.get('source')} OI较上次运行({window:.1f}h)={oi_chg:+.2f}%")
        else:
            reasons.append(f"{d.get('source')} OI变化={oi_chg:+.2f}%")

    if not vals and d.get("open_interest") is not None:
        vals.append(0)
        reasons.append(f"{d.get('source')} OI当前值可用，但无变化基线")

    return (sum(vals) / len(vals) if vals else None), reasons


# ============================================================
# FRED 宏观（无需 FRED API Key 的 CSV 下载）
# ============================================================

def fred_recent(series_id, limit=12):
    # fredgraph CSV 是公开下载；若格式/网络改变则自动降级
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        text = http_get(url)
        rows = list(csv.reader(io.StringIO(text)))
        vals = []
        for row in rows[1:]:
            if len(row) >= 2 and row[1] not in ("", "."):
                try:
                    vals.append((row[0], float(row[1])))
                except Exception:
                    pass
        return vals[-limit:]
    except Exception as e:
        print("FRED降级：", series_id, e)
        return []


def get_macro():
    return {
        "dgs2": fred_recent("DGS2", 12),
        "dgs10": fred_recent("DGS10", 12),
        "fedfunds": fred_recent("FEDFUNDS", 6),
        "cpi": fred_recent("CPIAUCSL", 14),
    }


def pct_change(a, b):
    return ((b / a) - 1) * 100 if a else None


def macro_dimension(m):
    vals, reasons = [], []

    d2, d10 = m["dgs2"], m["dgs10"]
    if len(d2) >= 2:
        delta = d2[-1][1] - d2[0][1]
        vals.append(max(-100, min(100, -delta * 120)))
        reasons.append(f"美债2Y {d2[-1][1]:.2f}% / 近期变化 {delta:+.2f}pct")

    if len(d10) >= 2:
        delta = d10[-1][1] - d10[0][1]
        vals.append(max(-100, min(100, -delta * 100)))
        reasons.append(f"美债10Y {d10[-1][1]:.2f}% / 近期变化 {delta:+.2f}pct")

    ff = m["fedfunds"]
    if len(ff) >= 2:
        delta = ff[-1][1] - ff[-2][1]
        vals.append(max(-100, min(100, -delta * 80)))
        reasons.append(f"Fed Funds {ff[-1][1]:.2f}% / 月变 {delta:+.2f}pct")

    cpi = m["cpi"]
    if len(cpi) >= 13:
        yoy_now = pct_change(cpi[-13][1], cpi[-1][1])
        yoy_prev = pct_change(cpi[-14][1], cpi[-2][1]) if len(cpi) >= 14 else None
        if yoy_now is not None and yoy_prev is not None:
            delta = yoy_now - yoy_prev
            vals.append(max(-100, min(100, -delta * 35)))
            reasons.append(f"CPI同比约 {yoy_now:.2f}% / 变化 {delta:+.2f}pct")

    return (sum(vals) / len(vals) if vals else None), reasons


# ============================================================
# ETF：CoinGlass（可选 API Key）
# BTC/ETH/SOL/XRP 有接口时尝试；BNB无对应维度则 N/A
# ============================================================

ETF_PATH = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "xrp",
}


def coinglass_json(path, params=None):
    if not COINGLASS_API_KEY:
        return None
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    return safe_json(
        "https://open-api-v4.coinglass.com" + path + qs,
        headers={"CG-API-KEY": COINGLASS_API_KEY},
    )


def get_etf(symbol):
    name = ETF_PATH.get(symbol)
    if not name:
        return {"available": False, "latest": None, "sum3": None, "reason": "无适用ETF维度"}
    data = coinglass_json(f"/api/etf/{name}/flow-history")
    try:
        rows = data["data"]
        if not rows:
            raise ValueError("empty")
        rows = sorted(rows, key=lambda x: x.get("timestamp", 0))
        latest = float(rows[-1].get("flow_usd") or 0)
        sum3 = sum(float(x.get("flow_usd") or 0) for x in rows[-3:])
        return {"available": True, "latest": latest, "sum3": sum3, "reason": ""}
    except Exception:
        return {
            "available": False, "latest": None, "sum3": None,
            "reason": "未配置Key、套餐无权限或接口暂不可用",
        }


def etf_dimension(etf):
    if not etf.get("available"):
        return None, [etf.get("reason", "ETF N/A")]
    latest, sum3 = etf["latest"], etf["sum3"]
    # 用方向而非绝对美元值，避免不同资产规模直接硬比较
    x = 0
    if latest > 0: x += 35
    elif latest < 0: x -= 35
    if sum3 > 0: x += 45
    elif sum3 < 0: x -= 45
    return max(-100, min(100, x)), [
        f"ETF最新净流={latest/1e6:+.1f}M USD",
        f"ETF近3期合计={sum3/1e6:+.1f}M USD",
    ]


# ============================================================
# 交易所/资金流：CoinGlass（可选，套餐权限取决于账户）
# 注意：这里是真实资金流维度，不用普通成交量冒充链上净流入
# ============================================================

def get_exchange_flow(symbol):
    data = coinglass_json("/api/spot/coin/netflow", {"symbol": symbol})
    try:
        d = data["data"]
        return {
            "available": True,
            "net_1h": float(d.get("net_flow_usd_1h") or 0),
            "ratio_1h": float(d.get("net_flow_usd_1h_market_cap_ratio") or 0),
        }
    except Exception:
        return {"available": False, "net_1h": None, "ratio_1h": None}


def flow_dimension(flow):
    if not flow.get("available"):
        return None, ["专业交易所资金流 N/A"]
    net = flow["net_1h"]
    ratio = flow["ratio_1h"]
    # 该接口为现货主动买卖净流，不把它错误描述为“币流入交易所地址”
    if ratio >= 0.003: x = 70
    elif ratio >= 0.001: x = 35
    elif ratio <= -0.003: x = -70
    elif ratio <= -0.001: x = -35
    else: x = 0
    return x, [f"现货1h净流={net/1e6:+.2f}M USD / 市值比={ratio:+.5f}"]


# ============================================================
# 稳定币流动性：CoinGecko USDT + USDC 市值 7日变化
# ============================================================

def stablecoin_marketcap_history(coin_id):
    d = safe_json(
        f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
        "?vs_currency=usd&days=8&interval=daily", retries=5
    )
    time.sleep(1.5)
    try:
        return [float(x[1]) for x in d["market_caps"]]
    except Exception:
        return []


def get_stablecoin_liquidity():
    usdt = stablecoin_marketcap_history("tether")
    usdc = stablecoin_marketcap_history("usd-coin")
    if len(usdt) < 2 or len(usdc) < 2:
        return {"available": False, "change7": None}
    old, new = usdt[0] + usdc[0], usdt[-1] + usdc[-1]
    return {"available": True, "change7": (new / old - 1) * 100 if old else None}


def stablecoin_dimension(s):
    if not s.get("available") or s.get("change7") is None:
        return None, ["稳定币流动性 N/A"]
    c = s["change7"]
    if c >= 1.0: x = 65
    elif c >= 0.25: x = 30
    elif c <= -1.0: x = -65
    elif c <= -0.25: x = -30
    else: x = 0
    return x, [f"USDT+USDC市值7日变化={c:+.2f}%"]


# ============================================================
# Fear & Greed
# ============================================================

def get_fear_greed():
    d = safe_json("https://api.alternative.me/fng/?limit=1&format=json")
    try:
        x = d["data"][0]
        return int(x["value"]), x.get("value_classification", "")
    except Exception:
        return None, "N/A"


def sentiment_dimension(value):
    if value is None:
        return None, ["Fear & Greed N/A"]
    # 极端贪婪视为风险；极端恐惧只给有限反向正分
    if value >= 85: x = -65
    elif value >= 75: x = -30
    elif value <= 20: x = 35
    elif value <= 30: x = 15
    else: x = 0
    return x, [f"Fear & Greed={value}"]


# ============================================================
# 动态加权综合评分
# ============================================================

def combine_dimensions(dimensions):
    numerator = 0.0
    denominator = 0.0
    used = {}
    for name, score in dimensions.items():
        if score is None:
            continue
        w = WEIGHTS[name]
        numerator += score * w
        denominator += w
        used[name] = score
    return (numerator / denominator if denominator else 0.0), used


def data_coverage(dimensions):
    available = [k for k, v in dimensions.items() if v is not None]
    total_weight = sum(WEIGHTS.values())
    available_weight = sum(WEIGHTS[k] for k in available)
    pct = (available_weight / total_weight * 100) if total_weight else 0
    return pct, available


def confidence_label(x):
    if x >= 60: return "强势偏多"
    if x >= BUY_CONFIDENCE: return "偏多 / 达到入场线"
    if x >= 15: return "偏多观察"
    if x <= -60: return "强势偏空"
    if x <= SELL_CONFIDENCE: return "偏空"
    return "中性 / 等待"


# ============================================================
# 模拟账户
# ============================================================

def new_account():
    return {
        "version": VERSION, "created_at": now_utc(), "cash": INITIAL_BALANCE,
        "positions": {}, "trades": [], "realized_pnl": 0.0, "fees_paid": 0.0,
        "peak_equity": INITIAL_BALANCE, "last_equity": INITIAL_BALANCE,
    }


def load_account():
    if not os.path.exists(STATE_FILE):
        return new_account()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            a = json.load(f)
        a["version"] = VERSION
        return a
    except Exception as e:
        print("账户读取失败，建立新账户：", e)
        return new_account()


def save_account(a):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(a, f, ensure_ascii=False, indent=2)


def total_equity(a, prices):
    v = a["cash"]
    for s, p in a["positions"].items():
        v += p["quantity"] * prices.get(s, p["entry_price"])
    return v


def total_exposure(a, prices):
    return sum(
        p["quantity"] * prices.get(s, p["entry_price"])
        for s, p in a["positions"].items()
    )


def paper_buy(a, symbol, price, confidence, prices):
    if symbol in a["positions"]:
        return False, "已有持仓"
    equity = total_equity(a, prices)
    room = equity * MAX_TOTAL_EXPOSURE - total_exposure(a, prices)
    if room <= MIN_TRADE_USDT:
        return False, "总仓位达到限制"

    risk_budget = equity * RISK_PER_TRADE
    value_by_risk = risk_budget / STOP_LOSS_PCT

    # 置信度越高，允许接近上限；仍受20%单币限制
    confidence_factor = min(1.0, max(0.50, abs(confidence) / 70))
    trade_value = min(
        value_by_risk * confidence_factor,
        equity * MAX_POSITION_PCT,
        room,
        a["cash"] / (1 + FEE_RATE),
    )
    if trade_value < MIN_TRADE_USDT:
        return False, "可用资金不足"

    fee = trade_value * FEE_RATE
    qty = trade_value / price
    a["cash"] -= trade_value + fee
    a["fees_paid"] += fee
    a["positions"][symbol] = {
        "quantity": qty, "entry_price": price, "entry_time": now_utc(),
        "entry_confidence": confidence, "highest_price": price,
        "stop_loss": price * (1 - STOP_LOSS_PCT),
        "take_profit": price * (1 + TAKE_PROFIT_PCT),
        "entry_fee": fee,
    }
    a["trades"].append({
        "time": now_utc(), "symbol": symbol, "side": "BUY", "price": price,
        "quantity": qty, "value": trade_value, "fee": fee,
        "confidence": confidence,
    })
    return True, f"模拟买入 {trade_value:.2f} USDT"


def paper_sell(a, symbol, price, reason, confidence):
    p = a["positions"].get(symbol)
    if not p:
        return False
    qty = p["quantity"]
    gross = qty * price
    exit_fee = gross * FEE_RATE
    entry_value = qty * p["entry_price"]
    entry_fee = p.get("entry_fee", entry_value * FEE_RATE)
    pnl = gross - entry_value - entry_fee - exit_fee

    a["cash"] += gross - exit_fee
    a["fees_paid"] += exit_fee
    a["realized_pnl"] += pnl
    a["trades"].append({
        "time": now_utc(), "symbol": symbol, "side": "SELL", "price": price,
        "quantity": qty, "value": gross, "fee": exit_fee,
        "confidence": confidence, "reason": reason, "pnl": pnl,
    })
    del a["positions"][symbol]
    return True


def manage_position(a, symbol, price, confidence):
    p = a["positions"].get(symbol)
    if not p:
        return None
    p["highest_price"] = max(p.get("highest_price", price), price)
    stop = max(
        p.get("stop_loss", p["entry_price"] * (1 - STOP_LOSS_PCT)),
        p["highest_price"] * (1 - TRAILING_STOP_PCT),
    )
    if price <= stop:
        paper_sell(a, symbol, price, "止损/移动止损", confidence)
        return "SELL：止损/移动止损"
    if price >= p.get("take_profit", p["entry_price"] * (1 + TAKE_PROFIT_PCT)):
        paper_sell(a, symbol, price, "止盈", confidence)
        return "SELL：止盈"
    if confidence <= SELL_CONFIDENCE:
        paper_sell(a, symbol, price, "多维综合信号转弱", confidence)
        return "SELL：多维综合信号转弱"
    return "HOLD：继续持仓"


def free_etf_dimension(symbol):
    # Optional free source; unavailable data is N/A, never fabricated.
    if symbol not in ("BTC", "ETH"):
        return None, ["无适用免费ETF维度"]
    url = FREE_ETF_BASE + "?" + urllib.parse.urlencode({"asset": symbol.lower(), "days": 10})
    d = safe_json(url, retries=2)
    if not isinstance(d, dict):
        return None, ["免费ETF源不可用"]
    try:
        rows = d.get("data", [])
        if not isinstance(rows, list) or not rows:
            return None, ["免费ETF源暂无数据"]
        dates=[str(r.get("date","")) for r in rows if isinstance(r,dict) and r.get("date")]
        if not dates:
            return None, ["免费ETF源无日期字段"]
        newest=max(dates)
        vals=[]
        for r in rows:
            if str(r.get("date","")) != newest:
                continue
            for key in ("netFlowUsd","net_flow_usd","flowUsd","flow_usd"):
                if r.get(key) not in (None,""):
                    vals.append(float(r[key])); break
        if not vals:
            return None, [f"ETF {newest} 无可解析净流字段"]
        net=sum(vals)
        score=70 if net>=500_000_000 else 40 if net>=150_000_000 else 15 if net>0 else -70 if net<=-500_000_000 else -40 if net<=-150_000_000 else -15 if net<0 else 0
        return score,[f"免费ETF {newest} 净流≈${net/1e6:+.1f}M"]
    except Exception as e:
        return None,[f"免费ETF解析失败：{e}"]


# ============================================================
# V3 现货式执行入口已停用
# ============================================================
# 说明：本文件前半段仅保留 V4 合约引擎复用的数据获取/指标函数。
# 旧 V3 主程序曾错误地使用“合约/模拟账户.json”执行现货式买卖，随后才进入 V4，
# 会造成同一账户被两套不同账本模型连续读写，并让每次运行重复抓取整套外部数据。
# 合约机器人现在只执行下方 V4 FUTURES PAPER ENGINE。

# ============================================================
# V5 CLEAN FUTURES PAPER ENGINE
# USDT-margined perpetual, isolated-margin simulation.
# IMPORTANT: simulation only. No exchange order endpoint/API key is used.
# ============================================================

VERSION = "V5 CLEAN FUTURES PAPER + ORDERFLOW SHADOW A/B"
LIVE_TRADING = False
FUTURES_STATE_FILE = "合约/模拟账户.json"
FUTURES_CHALLENGER_MODE = os.getenv("FUTURES_CHALLENGER_MODE", "0") == "1"
THRESHOLD_MULTIPLIER = 0.875 if FUTURES_CHALLENGER_MODE else 1.0
if FUTURES_CHALLENGER_MODE:
    VERSION += " | 12.5% LOWER-THRESHOLD CHALLENGER"
    FUTURES_STATE_FILE = "合约/阈值挑战者账户.json"
    OI_STATE_FILE = "合约/阈值挑战者持仓量记录.json"

# Conservative paper defaults. These are strategy settings, not exchange promises.
LEVERAGE = 3.0
TAKER_FEE_RATE = 0.0005       # configurable paper assumption, 0.05% of notional
SLIPPAGE_RATE = 0.0005        # adverse simulated market-order slippage, 0.05%
MAINTENANCE_MARGIN_RATE = 0.005  # simplified first-tier paper MMR assumption
LIQUIDATION_FEE_RATE = 0.0005
FUTURES_RISK_PER_TRADE = 0.01
FUTURES_STOP_LOSS_PCT = 0.04
FUTURES_TAKE_PROFIT_PCT = 0.10
FUTURES_TRAILING_STOP_PCT = 0.05
MAX_MARGIN_PER_POSITION_PCT = 0.10
MAX_TOTAL_MARGIN_PCT = 0.30
MAX_CONCURRENT_POSITIONS = 3
MIN_MARGIN_USDT = 30.0
FUTURES_MAX_DRAWDOWN_PCT = 0.15
DAILY_REALIZED_LOSS_LIMIT_PCT = 0.03
MAX_CONSECUTIVE_LOSSES = 3
COOLDOWN_HOURS = 6
LONG_ENTRY_CONFIDENCE = 35.0
SHORT_ENTRY_CONFIDENCE = -30.0
LONG_EXIT_CONFIDENCE = -20.0
SHORT_EXIT_CONFIDENCE = 20.0
MIN_DATA_COVERAGE = 55.0


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def combine_dimensions(d):
    n=z=0.; used={}
    for k,v in d.items():
        if v is None: continue
        n += v*WEIGHTS[k]; z += WEIGHTS[k]; used[k]=v
    return (n/z if z else 0.0), used


def data_coverage(d):
    a=[k for k,v in d.items() if v is not None]
    return sum(WEIGHTS[k] for k in a)*100/sum(WEIGHTS.values()), a


def confidence_label(x):
    if x >= 60: return "强势偏多"
    if x >= LONG_ENTRY_CONFIDENCE: return "偏多 / LONG入场线"
    if x >= 15: return "偏多观察"
    if x <= -60: return "强势偏空"
    if x <= SHORT_ENTRY_CONFIDENCE: return "偏空 / SHORT入场线"
    if x <= -15: return "偏空观察"
    return "中性 / 等待"


def new_futures_account():
    return {
        "version": VERSION, "created_at": now_utc(), "cash": INITIAL_BALANCE,
        "positions": {}, "trades": [], "realized_pnl": 0.0,
        "fees_paid": 0.0, "funding_pnl": 0.0, "liquidations": 0,
        "peak_equity": INITIAL_BALANCE, "last_equity": INITIAL_BALANCE,
        "consecutive_losses": 0, "cooldown_until": None,
        "daily": {}, "_oi_snapshots": {}
    }


def load_futures_account():
    if not os.path.exists(FUTURES_STATE_FILE): return new_futures_account()
    try:
        with open(FUTURES_STATE_FILE,"r",encoding="utf-8") as f: a=json.load(f)
        required={"cash","positions","trades","realized_pnl","fees_paid","funding_pnl","peak_equity"}
        if (not isinstance(a,dict) or not required.issubset(a)
                or not isinstance(a["positions"],dict) or not isinstance(a["trades"],list)
                or not isinstance(a.get("daily",{}),dict)
                or any(not math.isfinite(float(a[k])) for k in ("cash","realized_pnl","fees_paid","funding_pnl","peak_equity"))):
            raise ValueError("invalid existing financial state; refusing defaults")
        d=new_futures_account()
        for k,v in d.items(): a.setdefault(k,v)
        a["version"]=VERSION
        return a
    except Exception as e:
        raise RuntimeError(f"V5账户读取失败，拒绝重置模拟账户：{e}") from e


def save_futures_account(a):
    tmp=FUTURES_STATE_FILE+".tmp"
    with open(tmp,"w",encoding="utf-8") as f:
        json.dump(a,f,ensure_ascii=False,indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp,FUTURES_STATE_FILE)


def unrealized_pnl(p, mark):
    q=float(p["quantity"]); e=float(p["entry_price"])
    return q*(mark-e) if p["side"]=="LONG" else q*(e-mark)


def futures_equity(a, marks):
    # cash already excludes isolated margin posted; equity adds remaining margin + unrealized PnL.
    return runtime.account_equity(a,marks,futures=True)


def used_margin(a):
    return sum(float(p.get("margin",0)) for p in a["positions"].values())


def adverse_fill(price, side, opening=True):
    # Buy fills higher, sell fills lower. LONG open/SHORT close are buys.
    is_buy = (side=="LONG" and opening) or (side=="SHORT" and not opening)
    return price*(1+SLIPPAGE_RATE if is_buy else 1-SLIPPAGE_RATE)


def liquidation_price(entry, margin, qty, side):
    # Simplified isolated USDT-linear estimate, including maintenance + liquidation fee buffer.
    r=MAINTENANCE_MARGIN_RATE + LIQUIDATION_FEE_RATE
    if qty<=0: return None
    if side=="LONG":
        den=qty*(r-1.0)
        return max(0.0,(margin-qty*entry)/den) if den else None
    den=qty*(r+1.0)
    return max(0.0,(margin+qty*entry)/den) if den else None


def get_okx_mark_price(symbol, fallback):
    # Legacy caller signature retained; never substitute spot for a missing mark.
    try:return runtime.mark_quote(symbol)['reference_mid']
    except Exception:return None


def get_latest_realized_funding(symbol):
    inst=f"{symbol}-USDT-SWAP"
    d=safe_json("https://www.okx.com/api/v5/public/funding-rate-history?"+
                urllib.parse.urlencode({"instId":inst,"limit":1}),retries=1,quiet=True)
    try:
        r=d["data"][0]
        rate=float(r.get("realizedRate") or r.get("fundingRate"))
        ts=int(r.get("fundingTime") or 0)
        if not (-0.05 <= rate <= 0.05): return None
        return {"rate":rate,"ts":ts}
    except Exception: return None


def apply_new_funding(a,symbol,mark):
    p=a["positions"].get(symbol)
    if not p: return 0.0
    f=get_latest_realized_funding(symbol)
    if not f or f["ts"]<=int(p.get("last_funding_ts",0)): return 0.0
    notional=float(p["quantity"])*mark
    # Positive funding: longs pay shorts. Negative funding: shorts pay longs.
    cashflow=(-1 if p["side"]=="LONG" else 1)*notional*f["rate"]
    p["margin"] += cashflow
    p["funding_pnl"]=float(p.get("funding_pnl",0))+cashflow
    p["last_funding_ts"]=f["ts"]
    a["funding_pnl"] += cashflow
    a["trades"].append({"time":now_utc(),"symbol":symbol,"event":"FUNDING",
                        "side":p["side"],"rate":f["rate"],"cashflow":cashflow,
                        "mark_price":mark})
    return cashflow


def utc_day():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def ensure_daily(a, eq):
    day=utc_day(); d=a["daily"].get(day)
    if not isinstance(d,dict):
        d={"start_equity":eq,"realized_pnl":0.0}; a["daily"][day]=d
    elif d.get('start_equity') is None and eq is not None:
        d['start_equity']=eq
        d['start_equity_note']='first valid valuation after missing marks; not midnight reconstruction'
    # retain only recent 14 keys
    for k in sorted(a["daily"].keys())[:-14]: a["daily"].pop(k,None)
    return d


def cooldown_active(a):
    x=a.get("cooldown_until")
    if not x: return False
    try: return datetime.now(timezone.utc) < datetime.fromisoformat(x)
    except Exception: return False


def set_cooldown(a):
    a["cooldown_until"]=(datetime.now(timezone.utc)+__import__('datetime').timedelta(hours=COOLDOWN_HOURS)).isoformat(timespec="seconds")


def entry_allowed(a, marks):
    eq=futures_equity(a,marks)
    if eq is None:return False,"持仓Mark缺失，无法可靠估值，禁止新开仓"
    peak=max(float(a.get("peak_equity",INITIAL_BALANCE)),eq)
    dd=(peak-eq)/peak if peak else 0
    if dd>=FUTURES_MAX_DRAWDOWN_PCT: return False,"最大回撤熔断"
    if cooldown_active(a): return False,"连续亏损冷却期"
    d=ensure_daily(a,eq)
    if float(d.get("realized_pnl",0)) <= -float(d.get("start_equity",eq))*DAILY_REALIZED_LOSS_LIMIT_PCT:
        return False,"当日已实现亏损达到限制"
    if len(a["positions"])>=MAX_CONCURRENT_POSITIONS: return False,"并发持仓达到限制"
    return True,"OK"


def futures_regime_rules(regime, side):
    # 挑战者只降低入场阈值12.5%；仓位、杠杆、止损及所有熔断与基准完全相同。
    if regime=="BULL_TREND": base,mult=(32.0 if side=="LONG" else 50.0, 1.00 if side=="LONG" else 0.45)
    elif regime=="BEAR_TREND": base,mult=(50.0 if side=="LONG" else 32.0, 0.45 if side=="LONG" else 1.00)
    elif regime=="HIGH_VOLATILITY": base,mult=48.0,0.50
    elif regime=="RANGE": base,mult=42.0,0.65
    else: base,mult=35.0,0.70
    return base*THRESHOLD_MULTIPLIER,mult

def open_futures(a,symbol,side,price,conf,coverage,marks,regime="UNKNOWN"):
    if symbol in a["positions"]: return False,"已有合约持仓"
    allowed,why=entry_allowed(a,marks)
    if not allowed: return False,why
    eq=futures_equity(a,marks)
    stop_pct=FUTURES_STOP_LOSS_PCT
    _,regime_mult=futures_regime_rules(regime,side)
    risk_budget=eq*FUTURES_RISK_PER_TRADE*clamp(abs(conf)/70.0,0.55,1.0)*regime_mult
    # Risk sizing is based on notional * stop distance, then capped by isolated margin limits.
    risk_notional=risk_budget/stop_pct
    max_margin=min(eq*MAX_MARGIN_PER_POSITION_PCT,
                   max(0.0,eq*MAX_TOTAL_MARGIN_PCT-used_margin(a)),a["cash"])
    margin=min(risk_notional/LEVERAGE,max_margin)
    if margin<MIN_MARGIN_USDT: return False,"可用保证金不足"
    fill=adverse_fill(price,side,True); notional=margin*LEVERAGE; qty=notional/fill
    fee=notional*TAKER_FEE_RATE
    if margin+fee>a["cash"]:
        margin=max(0,(a["cash"]-fee)); notional=margin*LEVERAGE; qty=notional/fill; fee=notional*TAKER_FEE_RATE
    if margin<MIN_MARGIN_USDT: return False,"扣除手续费后保证金不足"
    a["cash"]-=margin+fee; a["fees_paid"]+=fee
    f=get_latest_realized_funding(symbol); lastfts=f["ts"] if f else 0
    p={"side":side,"quantity":qty,"entry_price":fill,"entry_time":now_utc(),
       "entry_confidence":conf,"entry_coverage":coverage,"leverage":LEVERAGE,
       "margin":margin,"initial_margin":margin,"notional_entry":notional,
       "entry_fee":fee,"funding_pnl":0.0,"last_funding_ts":lastfts}
    if side=="LONG":
        p["best_price"]=fill; p["stop_price"]=fill*(1-FUTURES_STOP_LOSS_PCT); p["take_profit"]=fill*(1+FUTURES_TAKE_PROFIT_PCT)
    else:
        p["best_price"]=fill; p["stop_price"]=fill*(1+FUTURES_STOP_LOSS_PCT); p["take_profit"]=fill*(1-FUTURES_TAKE_PROFIT_PCT)
    p["liquidation_price"]=liquidation_price(fill,margin,qty,side)
    a["positions"][symbol]=p
    a["trades"].append({"time":now_utc(),"symbol":symbol,"event":"OPEN","side":side,
        "price":fill,"quantity":qty,"notional":notional,"margin":margin,"leverage":LEVERAGE,
        "fee":fee,"confidence":conf,"coverage":coverage,"regime":regime,"liquidation_price":p["liquidation_price"]})
    return True,f"模拟开{side} | 保证金 {margin:.2f} | 名义价值 {notional:.2f} | {LEVERAGE:.0f}x"


def close_futures(a,symbol,price,reason,conf,liquidated=False):
    p=a["positions"].get(symbol)
    if not p: return False,0.0
    fill=adverse_fill(price,p["side"],False)
    q=float(p["quantity"]); notional=q*fill
    pnl=q*(fill-p["entry_price"]) if p["side"]=="LONG" else q*(p["entry_price"]-fill)
    fee=notional*TAKER_FEE_RATE
    liq_fee=notional*LIQUIDATION_FEE_RATE if liquidated else 0.0
    returned=max(0.0,float(p["margin"])+pnl-fee-liq_fee)
    # If loss exceeds isolated margin, cap at margin: paper account cannot go negative from this position.
    realized=returned-float(p["initial_margin"])-float(p.get("funding_pnl",0))-float(p.get("entry_fee",0))
    # realized above includes trading PnL and exit/liq fee; funding is tracked separately to avoid double count.
    a["cash"]+=returned; a["fees_paid"]+=fee+liq_fee; a["realized_pnl"]+=realized
    if liquidated: a["liquidations"]+=1
    # Remove before valuing: returned cash must not be counted with the same
    # position's margin/PnL a second time when initializing a new UTC day.
    del a['positions'][symbol]
    d=ensure_daily(a,futures_equity(a,{})); d["realized_pnl"]=float(d.get("realized_pnl",0))+realized
    if realized<0:
        a["consecutive_losses"]=int(a.get("consecutive_losses",0))+1
        if a["consecutive_losses"]>=MAX_CONSECUTIVE_LOSSES: set_cooldown(a)
    else: a["consecutive_losses"]=0
    a["trades"].append({"time":now_utc(),"symbol":symbol,"event":"LIQUIDATION" if liquidated else "CLOSE",
        "side":p["side"],"price":fill,"quantity":q,"notional":notional,"fee":fee,
        "liquidation_fee":liq_fee,"confidence":conf,"reason":reason,"realized_pnl":realized,
        "funding_pnl":p.get("funding_pnl",0)})
    return True,realized


def manage_futures(a,symbol,last_price,mark,conf):
    p=a["positions"].get(symbol)
    if not p: return None
    funding=apply_new_funding(a,symbol,mark) if runtime.valid_price(mark) else 0.0
    p=a["positions"].get(symbol)
    if not p: return None
    liq=liquidation_price(p["entry_price"],p["margin"],p["quantity"],p["side"])
    p["liquidation_price"]=liq
    # Liquidation uses mark price, not spot/last price.
    hit_liq=runtime.valid_price(mark) and ((p["side"]=="LONG" and liq is not None and mark<=liq) or (p["side"]=="SHORT" and liq is not None and mark>=liq))
    if hit_liq:
        close_futures(a,symbol,mark,"保证金触发模拟强平",conf,True)
        return "LIQUIDATED：模拟强平"
    if p["side"]=="LONG":
        p["best_price"]=max(float(p.get("best_price",last_price)),last_price)
        trail=p["best_price"]*(1-FUTURES_TRAILING_STOP_PCT)
        stop=max(float(p["stop_price"]),trail)
        p['stop_price']=stop
        if last_price<=stop:
            close_futures(a,symbol,last_price,"止损/移动止损",conf); return "CLOSE LONG：止损/移动止损"
        if last_price>=float(p["take_profit"]):
            close_futures(a,symbol,last_price,"止盈",conf); return "CLOSE LONG：止盈"
        if conf is not None and conf<=LONG_EXIT_CONFIDENCE:
            close_futures(a,symbol,last_price,"多维信号转空",conf); return "CLOSE LONG：信号转空"
    else:
        p["best_price"]=min(float(p.get("best_price",last_price)),last_price)
        trail=p["best_price"]*(1+FUTURES_TRAILING_STOP_PCT)
        stop=min(float(p["stop_price"]),trail)
        p['stop_price']=stop
        if last_price>=stop:
            close_futures(a,symbol,last_price,"止损/移动止损",conf); return "CLOSE SHORT：止损/移动止损"
        if last_price<=float(p["take_profit"]):
            close_futures(a,symbol,last_price,"止盈",conf); return "CLOSE SHORT：止盈"
        if conf is not None and conf>=SHORT_EXIT_CONFIDENCE:
            close_futures(a,symbol,last_price,"多维信号转多",conf); return "CLOSE SHORT：信号转多"
    return ("HOLD LONG" if p["side"]=="LONG" else "HOLD SHORT") + (f" | Funding {funding:+.4f}" if funding else "")

def fast_risk(a):
    """No signal thresholds are evaluated in this early protective pass."""
    observations={};exited=set();errors=[]
    for symbol,p in list(a['positions'].items()):
        try:
            q=runtime.execution_quote(symbol,p['side'],'SWAP')
            try:
                m=runtime.mark_quote(symbol);observations[symbol]=m;mark=m['reference_mid']
            except Exception as exc:
                mark=None;errors.append({'symbol':symbol,'stage':'mark','reason':str(exc)})
            if not runtime.fresh(q):q=runtime.execution_quote(symbol,p['side'],'SWAP')
            manage_futures(a,symbol,q['reference_mid'],mark,None)
            if symbol not in a['positions']:exited.add(symbol)
        except Exception as exc:errors.append({'symbol':symbol,'stage':'risk','reason':str(exc)})
    eq,_=runtime.snapshot(a,observations,futures=True)
    ensure_daily(a,eq)
    a['runtime']['risk_errors']=errors
    save_futures_account(a)
    return exited,observations

def refresh_valuation(a):
    observations={}
    for symbol in a['positions']:
        try:observations[symbol]=runtime.mark_quote(symbol)
        except Exception as exc:observations[symbol]={'available':False,'reason':str(exc)}
    return runtime.snapshot(a,observations,futures=True)


# ---------------- V4 Main ----------------
def run_paper():
    print("="*72); print("AI TRADING AGENT",VERSION); print("FUTURES PAPER ONLY |",now_utc())
    print("币种：",", ".join(SYMBOLS)); print(f"模式：USDT永续模拟 | 逐仓 | {LEVERAGE:.0f}x | LONG/SHORT")
    print("CoinGlass：","已配置Key" if COINGLASS_API_KEY else "未配置Key（专业ETF/资金流自动N/A）"); print("="*72)
    if LIVE_TRADING: raise SystemExit("安全保护：合约机器人禁止真实交易。")

    account=load_futures_account()
    exited,observations=fast_risk(account)
    macro_raw=get_macro(); macro_score,macro_reasons=macro_dimension(macro_raw)
    fg_value,_=get_fear_greed(); sentiment_score,sentiment_reasons=sentiment_dimension(fg_value)
    try: markets=get_market_data()
    except Exception as e: raise SystemExit(f"CoinGecko主行情失败：{e}")
    market_map={}
    for m in markets:
        if m["id"] in COINS:
            market_map[COINS[m["id"]]]={"coin_id":m["id"],"price":float(m["current_price"]),
                "change24":float(m.get("price_change_percentage_24h",0) or 0)}
    current_prices={s:x["price"] for s,x in market_map.items()}
    eq0,marks=refresh_valuation(account)
    stable_raw=get_stablecoin_liquidity(); stable_score,stable_reasons=stablecoin_dimension(stable_raw)

    ensure_daily(account,eq0)
    results={}; failures=[]

    for symbol in SYMBOLS:
        data=market_map.get(symbol)
        if not data: failures.append(symbol+": 无主行情"); continue
        print("\n"+"="*72); print(symbol); print("="*72)
        try:
            prices,volumes=get_history(data["coin_id"]); price=data["price"]; ch=data["change24"]
            tech_score,tech_reasons,tech=technical_dimension(price,ch,prices,volumes)
            deriv_raw=get_derivatives(symbol); deriv_score,deriv_reasons=derivatives_dimension(ch,deriv_raw)
            etf_score,etf_reasons=free_etf_dimension(symbol)
            if etf_score is None and COINGLASS_API_KEY: etf_score,etf_reasons=etf_dimension(get_etf(symbol))
            flow_score,flow_reasons=flow_dimension(get_exchange_flow(symbol))
            dims={"technical":tech_score,"derivatives":deriv_score,"macro":macro_score,"etf":etf_score,
                  "flow":flow_score,"stablecoin":stable_score,"sentiment":sentiment_score}
            conf,_=combine_dimensions(dims); coverage,avail=data_coverage(dims); label=confidence_label(conf)
            sup,res=support_resistance(prices); mark=marks.get(symbol)
            print(f"信号价格：${price:,.4f} | Mark：{runtime.format_amount(mark)} | 24h {ch:+.2f}%")
            print(f"Regime：{tech['regime']} | 综合置信度：{conf:+.1f}/100 | {label}")
            print(f"数据覆盖率：{coverage:.1f}% | 可用维度：{len(avail)}/7 | 衍生品源：{deriv_raw['source']}")
            print("-"*72)
            for name,x in [("技术面",tech_score),("衍生品",deriv_score),("宏观",macro_score),("ETF机构资金",etf_score),
                           ("交易所资金流",flow_score),("稳定币流动性",stable_score),("市场情绪",sentiment_score)]:
                print(f"{name:<12}：{'N/A' if x is None else f'{x:+.1f}'}")
            print(f"RSI：{tech['rsi']:.2f}" if tech["rsi"] is not None else "RSI：N/A")
            print(f"30日支撑：${sup:,.4f} | 30日阻力：${res:,.4f}")
            if deriv_raw.get("funding_rate") is not None: print(f"Funding：{deriv_raw['funding_rate']:+.4f}%")
            if deriv_raw.get("oi_change_pct") is not None: print(f"OI变化：{deriv_raw['oi_change_pct']:+.2f}%")
            print("\n主要依据：")
            for r in tech_reasons+deriv_reasons+macro_reasons+etf_reasons+flow_reasons+stable_reasons+sentiment_reasons: print("•",r)

            if symbol in exited:
                action="HOLD：本轮已退出，禁止同轮重入"
            elif symbol in account["positions"]:
                execution=runtime.execution_quote(symbol,account['positions'][symbol]['side'],'SWAP')
                try:mark=runtime.mark_quote(symbol)['reference_mid']
                except Exception:mark=None
                if not runtime.fresh(execution):execution=runtime.execution_quote(symbol,account['positions'][symbol]['side'],'SWAP')
                price=execution['reference_mid']
                action=manage_futures(account,symbol,price,mark,conf)
                if symbol not in account['positions']:exited.add(symbol)
            elif coverage<MIN_DATA_COVERAGE:
                action="HOLD：数据覆盖率不足，禁止新开仓"
            else:
                regime=tech["regime"]
                long_thr,_=futures_regime_rules(regime,"LONG"); short_thr,_=futures_regime_rules(regime,"SHORT")
                if conf>=long_thr:
                    execution=assess_okx_orderbook(symbol,"LONG","SWAP")
                    record_execution_ab(account,symbol,"LONG",price,conf,execution)
                    print(f"订单流执行确认：{'PASS' if execution['passes'] else 'REJECT'} | {execution.get('reason')} | 质量分 {execution.get('quality_score','N/A')}")
                    if not runtime.fresh(execution):
                        action="HOLD：无新鲜可执行报价"
                    elif ORDERFLOW_ENFORCEMENT and not execution["passes"]:
                        action="HOLD：订单流/微观结构确认未通过"
                    else:
                        _,marks=refresh_valuation(account)
                        if not runtime.fresh(execution):execution=runtime.execution_quote(symbol,'LONG','SWAP')
                        price=execution['reference_mid']
                        if ORDERFLOW_ENFORCEMENT and not execution['passes']:
                            ok,msg=False,"刷新报价后订单流确认未通过"
                        else:ok,msg=open_futures(account,symbol,"LONG",price,conf,coverage,marks,regime)
                        shadow_note=" | 订单流影子拒绝已记录、不干预基准" if not execution["passes"] else ""
                        action=(("OPEN LONG：" if ok else "HOLD：")+msg+shadow_note)
                elif conf<=-short_thr:
                    execution=assess_okx_orderbook(symbol,"SHORT","SWAP")
                    record_execution_ab(account,symbol,"SHORT",price,conf,execution)
                    print(f"订单流执行确认：{'PASS' if execution['passes'] else 'REJECT'} | {execution.get('reason')} | 质量分 {execution.get('quality_score','N/A')}")
                    if not runtime.fresh(execution):
                        action="HOLD：无新鲜可执行报价"
                    elif ORDERFLOW_ENFORCEMENT and not execution["passes"]:
                        action="HOLD：订单流/微观结构确认未通过"
                    else:
                        _,marks=refresh_valuation(account)
                        if not runtime.fresh(execution):execution=runtime.execution_quote(symbol,'SHORT','SWAP')
                        price=execution['reference_mid']
                        if ORDERFLOW_ENFORCEMENT and not execution['passes']:
                            ok,msg=False,"刷新报价后订单流确认未通过"
                        else:ok,msg=open_futures(account,symbol,"SHORT",price,conf,coverage,marks,regime)
                        shadow_note=" | 订单流影子拒绝已记录、不干预基准" if not execution["passes"] else ""
                        action=(("OPEN SHORT：" if ok else "HOLD：")+msg+shadow_note)
                else: action=f"HOLD：未达到{regime}动态阈值 LONG +{long_thr:.0f} / SHORT -{short_thr:.0f}"
            print("\n合约模拟动作：",action)
            p=account["positions"].get(symbol)
            if p: print(f"持仓：{p['side']} | {p['leverage']:.0f}x | 保证金 {p['margin']:.2f} | 预估强平 ${p['liquidation_price']:,.4f}")
            results[symbol]={"price":price,"mark":mark,"confidence":conf,"label":label,"regime":tech["regime"],
                             "coverage":coverage,"action":action}
        except Exception as e:
            failures.append(f"{symbol}: {e}"); print(symbol,"分析失败：",e)

    equity,marks=refresh_valuation(account)
    account["last_equity"]=equity; account["version"]=VERSION
    ret=(equity/INITIAL_BALANCE-1)*100 if equity is not None else None
    curdd=(account["peak_equity"]-equity)/account["peak_equity"]*100 if equity is not None and account["peak_equity"] else None
    save_futures_account(account)
    runtime.finish_labels(account,save_futures_account)

    print("\n"+"="*72); print("合约机器人模拟账户"); print("="*72)
    print(f"可用现金：{account['cash']:,.2f} USDT | 已用逐仓保证金：{used_margin(account):,.2f} USDT")
    print(f"总权益：{runtime.format_amount(equity)} USDT | 累计收益：{runtime.format_amount(ret)}% | 当前回撤：{runtime.format_amount(curdd)}%")
    print("估值快照：",account['runtime']['valuation_at'],"|",account['runtime']['valuation_status'],"| 非连续实时行情")
    print(f"已实现交易盈亏：{account['realized_pnl']:+.2f} | Funding累计：{account['funding_pnl']:+.2f} | 手续费：{account['fees_paid']:.2f}")
    print(f"当前持仓：{len(account['positions'])} | 强平次数：{account['liquidations']} | 连续亏损：{account['consecutive_losses']}")
    if account.get("cooldown_until"): print("冷却至：",account["cooldown_until"])
    print("\n五币总览")
    for symbol in SYMBOLS:
        r=results.get(symbol)
        if r:
            print(f"{symbol}: ${r['price']:,.4f} | {r['label']} | {r['regime']} | {r['confidence']:+.1f}/100")
            print("  →",r["action"])
    if failures or len(results)!=len(SYMBOLS):
        print("\n完整性检查失败：",failures)
        raise RuntimeError(f"合约机器人完整性检查失败：成功 {len(results)}/{len(SYMBOLS)} 币种")
    print("\n"+"="*72); print("合约机器人 PAPER 完成 | LIVE真实交易：关闭")
    print("状态文件：",FUTURES_STATE_FILE); print("="*72)


if __name__=='__main__':run_paper()
