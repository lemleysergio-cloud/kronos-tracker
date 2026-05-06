"""
Kronos BTC Accuracy Tracker
============================
Scrapes the Kronos live demo (shiyu-coder.github.io/Kronos-demo/) once per day,
records its probabilistic predictions, then 24 hours later fetches real BTC/USDT
data from Binance and scores the prediction.

Scoring metrics per record:
  - direction_correct  : bool  — did price go up when upside_prob > 0.5?
  - brier_score        : float — (prob - outcome)^2, lower is better (0.0 perfect)
  - vol_correct        : bool  — did vol_amplification_prob predict realized vol?
  - price_change_pct   : float — actual 24h price change %
  - realized_vol_ratio : float — realized vol / historical vol (>1 means amplified)

Run modes:
  python kronos_tracker.py --scrape    # record today's prediction (run at ~16:30 UTC)
  python kronos_tracker.py --score     # score yesterday's prediction (run 24h later)
  python kronos_tracker.py --report    # print human-readable accuracy summary
  python kronos_tracker.py --all       # scrape + score in one pass (for GitHub Actions)
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).parent.parent
SCORES_FILE = REPO_ROOT / "scores.json"
PENDING_FILE = REPO_ROOT / "pending.json"

DEMO_URL = "https://shiyu-coder.github.io/Kronos-demo/"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
SYMBOL = "BTCUSDT"
INTERVAL = "1h"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def scrape_demo() -> dict:
    """
    Fetch the Kronos live demo page and extract prediction signals.

    Returns a dict with:
        upside_prob          (float 0-1)
        vol_amplification_prob (float 0-1)
        prediction_timestamp (ISO 8601 UTC string)
        scrape_timestamp     (ISO 8601 UTC string)
        demo_last_updated    (raw string from page)
    """
    resp = requests.get(DEMO_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    page_text = soup.get_text(separator="\n")

    # --- Upside probability ---
    upside_match = re.search(
        r"Upside Probability.*?(\d+\.?\d*)\s*%",
        page_text,
        re.DOTALL | re.IGNORECASE,
    )
    if not upside_match:
        raise ValueError("Could not parse upside probability from demo page.")
    upside_prob = float(upside_match.group(1)) / 100.0

    # --- Volatility amplification ---
    vol_match = re.search(
        r"Volatility Amplification.*?(\d+\.?\d*)\s*%",
        page_text,
        re.DOTALL | re.IGNORECASE,
    )
    if not vol_match:
        raise ValueError("Could not parse volatility amplification from demo page.")
    vol_prob = float(vol_match.group(1)) / 100.0

    # --- Last updated timestamp ---
    updated_match = re.search(
        r"Last Updated.*?(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})",
        page_text,
        re.DOTALL | re.IGNORECASE,
    )
    demo_last_updated = updated_match.group(1).strip() if updated_match else "unknown"

    now_utc = datetime.now(timezone.utc).isoformat()

    return {
        "upside_prob": upside_prob,
        "vol_amplification_prob": vol_prob,
        "demo_last_updated": demo_last_updated,
        "prediction_timestamp": demo_last_updated,
        "scrape_timestamp": now_utc,
    }


# ---------------------------------------------------------------------------
# Binance data fetching
# ---------------------------------------------------------------------------

def fetch_btc_klines(start_utc: datetime, count: int = 25) -> list[dict]:
    """
    Fetch hourly BTC/USDT klines from Binance starting at start_utc.

    Returns list of dicts with keys: open_time, open, high, low, close, volume.
    """
    start_ms = int(start_utc.timestamp() * 1000)
    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "startTime": start_ms,
        "limit": count,
    }
    resp = requests.get(BINANCE_KLINES_URL, params=params, timeout=20)
    resp.raise_for_status()
    raw = resp.json()

    candles = []
    for c in raw:
        candles.append(
            {
                "open_time": datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc).isoformat(),
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
            }
        )
    return candles


def get_price_at(dt_utc: datetime) -> float:
    """Return the closing price of the 1h candle that contains dt_utc."""
    # Align to the start of the hour
    aligned = dt_utc.replace(minute=0, second=0, microsecond=0)
    candles = fetch_btc_klines(aligned, count=2)
    if not candles:
        raise ValueError(f"No Binance data returned for {dt_utc}")
    return candles[0]["close"]


def compute_realized_vol(start_utc: datetime, hours: int = 24) -> float:
    """
    Compute the realized (standard deviation of log returns) volatility
    over `hours` 1h candles starting at start_utc.
    Returns annualized vol as a ratio (not percent).
    """
    import math

    candles = fetch_btc_klines(start_utc, count=hours + 1)
    closes = [c["close"] for c in candles]
    if len(closes) < 2:
        raise ValueError("Not enough candles to compute realized vol.")
    log_returns = [
        math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
    ]
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / len(log_returns)
    return math.sqrt(variance)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_prediction(pending: dict) -> dict:
    """
    Given a pending prediction record, fetch actual BTC data and score it.

    Returns the original record augmented with scoring fields.
    """
    # Parse prediction timestamp — demo uses "YYYY-MM-DD HH:MM:SS" UTC
    raw_ts = pending["prediction_timestamp"]
    try:
        pred_dt = datetime.strptime(raw_ts, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        # Fallback: try ISO format
        pred_dt = datetime.fromisoformat(raw_ts)
        if pred_dt.tzinfo is None:
            pred_dt = pred_dt.replace(tzinfo=timezone.utc)

    # T+0 and T+24h prices
    price_t0 = get_price_at(pred_dt)
    price_t24 = get_price_at(pred_dt + timedelta(hours=24))

    price_change_pct = (price_t24 - price_t0) / price_t0 * 100.0
    went_up = price_t24 > price_t0

    # Historical vol: 24h BEFORE prediction
    hist_vol = compute_realized_vol(pred_dt - timedelta(hours=24), hours=24)
    # Realized vol: 24h AFTER prediction
    realized_vol = compute_realized_vol(pred_dt, hours=24)
    realized_vol_ratio = realized_vol / hist_vol if hist_vol > 0 else 1.0
    vol_amplified = realized_vol_ratio > 1.0

    # Brier score: (probability - binary_outcome)^2
    upside_prob = pending["upside_prob"]
    direction_correct = (upside_prob > 0.5) == went_up
    brier_score = (upside_prob - (1.0 if went_up else 0.0)) ** 2

    vol_prob = pending["vol_amplification_prob"]
    vol_correct = (vol_prob > 0.5) == vol_amplified
    vol_brier = (vol_prob - (1.0 if vol_amplified else 0.0)) ** 2

    scored = {
        **pending,
        "score_timestamp": datetime.now(timezone.utc).isoformat(),
        "price_t0": price_t0,
        "price_t24": price_t24,
        "price_change_pct": round(price_change_pct, 4),
        "went_up": went_up,
        "direction_correct": direction_correct,
        "brier_score": round(brier_score, 6),
        "hist_vol": round(hist_vol, 8),
        "realized_vol": round(realized_vol, 8),
        "realized_vol_ratio": round(realized_vol_ratio, 4),
        "vol_amplified": vol_amplified,
        "vol_correct": vol_correct,
        "vol_brier_score": round(vol_brier, 6),
    }
    return scored


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path: Path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Saved: {path}")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def compute_stats(records: list[dict], window: int | None = None) -> dict:
    """Compute accuracy statistics over records (optionally last N days)."""
    if window is not None:
        records = records[-window:]
    if not records:
        return {}

    n = len(records)
    dir_correct = sum(1 for r in records if r.get("direction_correct"))
    vol_correct = sum(1 for r in records if r.get("vol_correct"))
    avg_brier = sum(r.get("brier_score", 0.5) for r in records) / n
    avg_vol_brier = sum(r.get("vol_brier_score", 0.5) for r in records) / n
    avg_change = sum(r.get("price_change_pct", 0) for r in records) / n

    # Streak
    streak = 0
    for r in reversed(records):
        if r.get("direction_correct"):
            streak += 1
        else:
            break

    return {
        "n": n,
        "direction_accuracy_pct": round(dir_correct / n * 100, 1),
        "vol_accuracy_pct": round(vol_correct / n * 100, 1),
        "avg_brier_score": round(avg_brier, 4),
        "avg_vol_brier_score": round(avg_vol_brier, 4),
        "correct_streak": streak,
        "avg_price_change_pct": round(avg_change, 2),
    }


def print_report(records: list[dict]):
    """Print a human-readable accuracy report."""
    if not records:
        print("No scored records yet. Run --scrape today and --score tomorrow.")
        return

    print("\n" + "=" * 60)
    print("  KRONOS BTC ACCURACY REPORT")
    print(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    for label, window in [("All-time", None), ("Last 30 days", 30), ("Last 7 days", 7)]:
        stats = compute_stats(records, window)
        if not stats:
            continue
        print(f"\n  {label} (n={stats['n']})")
        print(f"    Direction accuracy : {stats['direction_accuracy_pct']}%  (random baseline = 50%)")
        print(f"    Volatility accuracy: {stats['vol_accuracy_pct']}%")
        print(f"    Avg Brier score    : {stats['avg_brier_score']}  (perfect = 0.0, random = 0.25)")
        print(f"    Avg vol Brier      : {stats['avg_vol_brier_score']}")
        print(f"    Correct streak     : {stats['correct_streak']} days")
        print(f"    Avg daily BTC move : {stats['avg_price_change_pct']:+.2f}%")

    print("\n  Recent records (last 10):")
    print(f"  {'Date':<12} {'Upside%':>8} {'Direction':>10} {'Brier':>7} {'ΔPrice':>8}")
    print("  " + "-" * 50)
    for r in records[-10:]:
        date = r.get("prediction_timestamp", "")[:10]
        prob = f"{r['upside_prob']*100:.0f}%"
        correct = "✓" if r.get("direction_correct") else "✗"
        brier = f"{r['brier_score']:.3f}"
        delta = f"{r.get('price_change_pct', 0):+.2f}%"
        print(f"  {date:<12} {prob:>8} {correct:>10} {brier:>7} {delta:>8}")

    print("\n" + "=" * 60 + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_scrape():
    """Record today's Kronos prediction."""
    print("Scraping Kronos demo...")
    prediction = scrape_demo()
    print(f"  Upside prob          : {prediction['upside_prob']*100:.1f}%")
    print(f"  Vol amplification    : {prediction['vol_amplification_prob']*100:.1f}%")
    print(f"  Demo last updated    : {prediction['demo_last_updated']}")

    # Save as pending (to be scored tomorrow)
    save_json(PENDING_FILE, prediction)
    print("Prediction saved to pending.json — run --score in 24h.")


def cmd_score():
    """Score yesterday's pending prediction."""
    pending = load_json(PENDING_FILE, None)
    if pending is None:
        print("No pending prediction found. Run --scrape first.")
        sys.exit(1)

    print(f"Scoring prediction from {pending['prediction_timestamp']}...")
    scored = score_prediction(pending)

    result = "CORRECT ✓" if scored["direction_correct"] else "WRONG ✗"
    print(f"  Direction: {result}  (prob={scored['upside_prob']*100:.1f}%, actual={'UP' if scored['went_up'] else 'DOWN'} {scored['price_change_pct']:+.2f}%)")
    print(f"  Brier score: {scored['brier_score']}")
    print(f"  Vol amplification: {'CORRECT ✓' if scored['vol_correct'] else 'WRONG ✗'}  (ratio={scored['realized_vol_ratio']:.2f}x)")

    # Append to scores log
    scores = load_json(SCORES_FILE, [])
    scores.append(scored)
    save_json(SCORES_FILE, scores)

    # Clear pending
    PENDING_FILE.unlink(missing_ok=True)
    print("Scored record appended to scores.json.")


def cmd_report():
    """Print accuracy report from scores.json."""
    scores = load_json(SCORES_FILE, [])
    print_report(scores)


def cmd_all():
    """
    Combined mode for GitHub Actions:
    1. Score any pending prediction from yesterday
    2. Scrape today's new prediction
    """
    pending = load_json(PENDING_FILE, None)
    if pending is not None:
        print("--- Scoring yesterday's prediction ---")
        cmd_score()
    else:
        print("No pending prediction to score yet (first run).")

    print("\n--- Scraping today's prediction ---")
    cmd_scrape()

    print("\n--- Current report ---")
    cmd_report()


def main():
    parser = argparse.ArgumentParser(description="Kronos BTC Accuracy Tracker")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scrape", action="store_true", help="Record today's Kronos prediction")
    group.add_argument("--score", action="store_true", help="Score yesterday's prediction")
    group.add_argument("--report", action="store_true", help="Print accuracy report")
    group.add_argument("--all", action="store_true", help="Score + scrape in one pass (GitHub Actions)")
    args = parser.parse_args()

    if args.scrape:
        cmd_scrape()
    elif args.score:
        cmd_score()
    elif args.report:
        cmd_report()
    elif args.all:
        cmd_all()


if __name__ == "__main__":
    main()
