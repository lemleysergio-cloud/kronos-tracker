"""
Kronos BTC Accuracy Tracker — Hourly Edition
=============================================
Scrapes the Kronos live demo every hour, records probabilistic predictions,
then 24 hours later fetches real BTC/USDT data from Binance and scores them.

Run modes:
  python kronos_tracker.py --scrape    # record current prediction
  python kronos_tracker.py --score     # score any pending predictions 24h+ old
  python kronos_tracker.py --report    # print human-readable accuracy summary
  python kronos_tracker.py --all       # score + scrape in one pass (GitHub Actions)
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
PENDING_FILE = REPO_ROOT / "pending.json"  # now a list of pending predictions

DEMO_URL = "https://shiyu-coder.github.io/Kronos-demo/"
BINANCE_KLINES_URL = "https://api.binance.us/api/v3/klines"
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
    resp = requests.get(DEMO_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    page_text = soup.get_text(separator="\n")

    upside_match = re.search(
        r"Upside Probability.*?(\d+\.?\d*)\s*%",
        page_text, re.DOTALL | re.IGNORECASE,
    )
    if not upside_match:
        raise ValueError("Could not parse upside probability from demo page.")
    upside_prob = float(upside_match.group(1)) / 100.0

    vol_match = re.search(
        r"Volatility Amplification.*?(\d+\.?\d*)\s*%",
        page_text, re.DOTALL | re.IGNORECASE,
    )
    if not vol_match:
        raise ValueError("Could not parse volatility amplification from demo page.")
    vol_prob = float(vol_match.group(1)) / 100.0

    updated_match = re.search(
        r"Last Updated.*?(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})",
        page_text, re.DOTALL | re.IGNORECASE,
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
        candles.append({
            "open_time": datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc).isoformat(),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]),
        })
    return candles


def get_price_at(dt_utc: datetime) -> float:
    aligned = dt_utc.replace(minute=0, second=0, microsecond=0)
    now_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    if aligned >= now_hour:
        aligned = now_hour - timedelta(hours=1)
    candles = fetch_btc_klines(aligned, count=2)
    if not candles:
        raise ValueError(f"No Binance data returned for {dt_utc}")
    return candles[0]["close"]


def compute_realized_vol(start_utc: datetime, hours: int = 24) -> float:
    import math
    candles = fetch_btc_klines(start_utc, count=hours + 1)
    closes = [c["close"] for c in candles]
    if len(closes) < 2:
        raise ValueError("Not enough candles to compute realized vol.")
    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / len(log_returns)
    return math.sqrt(variance)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_prediction(pending: dict) -> dict:
    raw_ts = pending["prediction_timestamp"]
    try:
        pred_dt = datetime.strptime(raw_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        pred_dt = datetime.fromisoformat(raw_ts)
        if pred_dt.tzinfo is None:
            pred_dt = pred_dt.replace(tzinfo=timezone.utc)

    price_t0 = get_price_at(pred_dt)
    price_t24 = get_price_at(pred_dt + timedelta(hours=24))

    price_change_pct = (price_t24 - price_t0) / price_t0 * 100.0
    went_up = price_t24 > price_t0

    hist_vol = compute_realized_vol(pred_dt - timedelta(hours=24), hours=24)
    realized_vol = compute_realized_vol(pred_dt, hours=24)
    realized_vol_ratio = realized_vol / hist_vol if hist_vol > 0 else 1.0
    vol_amplified = realized_vol_ratio > 1.0

    upside_prob = pending["upside_prob"]
    direction_correct = (upside_prob > 0.5) == went_up
    brier_score = (upside_prob - (1.0 if went_up else 0.0)) ** 2

    vol_prob = pending["vol_amplification_prob"]
    vol_correct = (vol_prob > 0.5) == vol_amplified
    vol_brier = (vol_prob - (1.0 if vol_amplified else 0.0)) ** 2

    return {
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

def compute_stats(records: list[dict]) -> dict:
    if not records:
        return {}
    n = len(records)
    dir_correct = sum(1 for r in records if r.get("direction_correct"))
    vol_correct = sum(1 for r in records if r.get("vol_correct"))
    avg_brier = sum(r.get("brier_score", 0.5) for r in records) / n
    avg_vol_brier = sum(r.get("vol_brier_score", 0.5) for r in records) / n
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
    }


def group_by_day(records: list[dict]) -> dict:
    """Group scored records by UTC date string (YYYY-MM-DD)."""
    days = {}
    for r in records:
        date = r.get("prediction_timestamp", "")[:10]
        if date not in days:
            days[date] = []
        days[date].append(r)
    return days


def print_report(records: list[dict]):
    if not records:
        print("No scored records yet.")
        return

    print("\n" + "=" * 60)
    print("  KRONOS BTC ACCURACY REPORT (HOURLY)")
    print(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    all_stats = compute_stats(records)
    print(f"\n  All-time (n={all_stats['n']} predictions)")
    print(f"    Direction accuracy : {all_stats['direction_accuracy_pct']}%")
    print(f"    Volatility accuracy: {all_stats['vol_accuracy_pct']}%")
    print(f"    Avg Brier score    : {all_stats['avg_brier_score']}")
    print(f"    Correct streak     : {all_stats['correct_streak']} predictions")

    days = group_by_day(records)
    print("\n  Per-day breakdown:")
    for date in sorted(days.keys(), reverse=True)[:7]:
        day_records = days[date]
        stats = compute_stats(day_records)
        print(f"\n  {date}  —  {stats['direction_accuracy_pct']}% ({sum(1 for r in day_records if r.get('direction_correct'))}/{len(day_records)})  Avg Brier: {stats['avg_brier_score']}")

    print("\n" + "=" * 60 + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_scrape():
    """Record current Kronos prediction, deduplicating by prediction_timestamp."""
    print("Scraping Kronos demo...")
    prediction = scrape_demo()
    print(f"  Upside prob       : {prediction['upside_prob']*100:.1f}%")
    print(f"  Vol amplification : {prediction['vol_amplification_prob']*100:.1f}%")
    print(f"  Demo last updated : {prediction['demo_last_updated']}")

    pending = load_json(PENDING_FILE, [])
    # Deduplicate — don't add if we already have this prediction_timestamp
    existing_ts = {p["prediction_timestamp"] for p in pending}
    if prediction["prediction_timestamp"] in existing_ts:
        print(f"  Already have prediction for {prediction['prediction_timestamp']} — skipping.")
        return

    pending.append(prediction)
    save_json(PENDING_FILE, pending)
    print(f"  Saved. Total pending: {len(pending)}")


def cmd_score():
    """Score any pending predictions that are 24h+ old."""
    pending = load_json(PENDING_FILE, [])
    if not pending:
        print("No pending predictions to score.")
        return

    now = datetime.now(timezone.utc)
    scored_count = 0
    still_pending = []

    scores = load_json(SCORES_FILE, [])

    for p in pending:
        scrape_ts = datetime.fromisoformat(p["scrape_timestamp"])
        if scrape_ts.tzinfo is None:
            scrape_ts = scrape_ts.replace(tzinfo=timezone.utc)
        age = now - scrape_ts

        if age >= timedelta(hours=24):
            print(f"  Scoring prediction from {p['prediction_timestamp']}...")
            try:
                scored = score_prediction(p)
                scores.append(scored)
                scored_count += 1
                result = "CORRECT ✓" if scored["direction_correct"] else "WRONG ✗"
                print(f"    Direction: {result}  Brier: {scored['brier_score']}")
            except Exception as e:
                print(f"    ERROR scoring {p['prediction_timestamp']}: {e}")
                still_pending.append(p)
        else:
            still_pending.append(p)

    if scored_count > 0:
        save_json(SCORES_FILE, scores)

    save_json(PENDING_FILE, still_pending)
    print(f"Scored {scored_count} predictions. {len(still_pending)} still pending.")


def cmd_report():
    scores = load_json(SCORES_FILE, [])
    print_report(scores)


def cmd_all():
    pending = load_json(PENDING_FILE, [])
    if pending:
        print("--- Scoring mature pending predictions ---")
        cmd_score()
    else:
        print("No pending predictions to score yet.")

    print("\n--- Scraping current prediction ---")
    cmd_scrape()

    print("\n--- Current report ---")
    cmd_report()


def main():
    parser = argparse.ArgumentParser(description="Kronos BTC Accuracy Tracker")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scrape", action="store_true")
    group.add_argument("--score", action="store_true")
    group.add_argument("--report", action="store_true")
    group.add_argument("--all", action="store_true")
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
