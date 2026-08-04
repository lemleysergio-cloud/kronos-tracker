"""
Price Source — Coinbase Exchange (shared by every tracker module)
==================================================================
WHY THIS EXISTS
Previously each module fetched prices from Binance.US independently.
Binance.US has had thin liquidity since 2023, and its printed BTC price
can drift $100-500+ from the broader market during quiet hours.

Kalshi settles its BTC contracts on the CF Benchmarks Bitcoin Real-Time
Index (BRTI) — a blend of major REGULATED venues (Coinbase, Kraken,
Gemini, Bitstamp). Binance.US is not a BRTI constituent, so scoring
against it was structurally disconnected from what Kalshi actually pays.

Coinbase is a real BRTI constituent and has deep liquidity, so its price
tracks the settlement index far more closely. This module is now the
single source of truth for every price the system uses.

IMPORTANT: this is still an APPROXIMATION of BRTI, not BRTI itself.
BRTI is a licensed feed and blends multiple venues with a 60-second
trailing average. Expect Coinbase to sit within a few dollars of it
normally — versus the hundreds of dollars Binance.US could drift.

API NOTES (differs meaningfully from Binance):
  - Endpoint: api.exchange.coinbase.com/products/BTC-USD/candles
  - Candle shape: [time, low, high, open, close, volume]
      (Binance was [open_time, open, high, low, close, volume, ...])
  - Time is in SECONDS (Binance used milliseconds)
  - Results come back NEWEST FIRST (Binance was oldest first)
  - Max 300 candles per request (Binance allowed 1000) → we paginate
  - No quote-volume field, so "amount" is derived as close * volume
"""

import json
import math
import time
import urllib.request
from datetime import datetime, timezone, timedelta

COINBASE_CANDLES = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
COINBASE_SPOT    = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
GRANULARITY      = 3600          # 1-hour candles
MAX_PER_REQUEST  = 300           # Coinbase hard limit
PRODUCT          = "BTC-USD"

# Coinbase rejects requests without a User-Agent.
HEADERS = {
    "User-Agent": "kronos-tracker/1.0 (github.com/lemleysergio-cloud/kronos-tracker)",
    "Accept": "application/json",
}


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _iso(dt):
    """Coinbase wants ISO-8601 UTC timestamps."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── Core candle fetch ────────────────────────────────────────────────────────

def fetch_candles(end_dt=None, hours=25):
    """
    Fetch `hours` of 1h candles ending at `end_dt` (default: now).
    Handles Coinbase's 300-candle cap by paginating automatically.

    Returns a list of dicts, OLDEST FIRST (normalized to match the old
    Binance ordering so downstream math is unchanged):
      {timestamp(datetime), open, high, low, close, volume, amount}
    """
    if end_dt is None:
        end_dt = datetime.now(timezone.utc)
    end_dt = end_dt.replace(minute=0, second=0, microsecond=0)

    collected = {}
    remaining = hours
    cursor_end = end_dt

    while remaining > 0:
        chunk = min(remaining, MAX_PER_REQUEST)
        cursor_start = cursor_end - timedelta(hours=chunk)
        url = (f"{COINBASE_CANDLES}?granularity={GRANULARITY}"
               f"&start={_iso(cursor_start)}&end={_iso(cursor_end)}")
        try:
            raw = _get(url)
        except Exception as e:
            print(f"  Coinbase candle fetch failed: {e}")
            break

        if not raw:
            break

        for c in raw:
            # [time, low, high, open, close, volume]
            ts = datetime.fromtimestamp(c[0], tz=timezone.utc)
            close_px = float(c[4])
            vol      = float(c[5])
            collected[ts] = {
                "timestamps": ts,
                "low":    float(c[1]),
                "high":   float(c[2]),
                "open":   float(c[3]),
                "close":  close_px,
                "volume": vol,
                # Coinbase has no quote-volume field; approximate it so the
                # Kronos model still receives an "amount" column.
                "amount": close_px * vol,
            }

        remaining  -= chunk
        cursor_end  = cursor_start
        if remaining > 0:
            time.sleep(0.25)   # stay well under Coinbase's rate limit

    return [collected[k] for k in sorted(collected.keys())]


# ─── Convenience accessors (same signatures the old modules used) ─────────────

def get_price_at(dt_utc):
    """Close price of the 1h candle containing dt_utc. None on failure."""
    aligned = dt_utc.replace(minute=0, second=0, microsecond=0)
    now_h   = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    # Never ask for a candle that hasn't closed yet.
    if aligned >= now_h:
        aligned = now_h - timedelta(hours=1)

    candles = fetch_candles(end_dt=aligned + timedelta(hours=1), hours=2)
    if not candles:
        return None
    for c in candles:
        if c["timestamps"] == aligned:
            return c["close"]
    return candles[-1]["close"]


def get_price_now():
    """Latest spot price. Falls back to the last closed candle."""
    try:
        data = _get(COINBASE_SPOT, timeout=10)
        return float(data["data"]["amount"])
    except Exception:
        candles = fetch_candles(hours=2)
        return candles[-1]["close"] if candles else None


def get_range(start_dt, hours=24):
    """
    (low, high, close) across `hours` starting at start_dt.
    Used to simulate whether a stop/strike was touched intraday.
    """
    start_dt = start_dt.replace(minute=0, second=0, microsecond=0)
    candles = fetch_candles(end_dt=start_dt + timedelta(hours=hours), hours=hours)
    if not candles:
        return None, None, None
    return (min(c["low"] for c in candles),
            max(c["high"] for c in candles),
            candles[-1]["close"])


def compute_realized_vol(start_dt, hours=24):
    """Std dev of hourly log returns over the window. Raises on short data."""
    start_dt = start_dt.replace(minute=0, second=0, microsecond=0)
    candles = fetch_candles(end_dt=start_dt + timedelta(hours=hours), hours=hours + 1)
    closes = [c["close"] for c in candles]
    if len(closes) < 2:
        raise ValueError("Not enough candles to compute realized vol")
    lr   = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mean = sum(lr) / len(lr)
    return math.sqrt(sum((r - mean) ** 2 for r in lr) / len(lr))


def source_name():
    return "coinbase"


if __name__ == "__main__":
    print("Testing Coinbase price source...")
    px = get_price_now()
    print(f"  Spot: ${px:,.2f}" if px else "  Spot fetch failed")
    candles = fetch_candles(hours=5)
    print(f"  Fetched {len(candles)} candles")
    for c in candles:
        print(f"    {c['timestamps']:%Y-%m-%d %H:%M} close=${c['close']:,.2f}")
