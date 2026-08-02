"""
Kronos Native Predictor
========================
Replaces demo scraping with direct Kronos-mini inference.
Pulls live BTC/USDT 1h data from Binance, runs Kronos-mini,
generates 30 Monte Carlo paths, derives upside probability
and vol amplification — same output schema as the old scraper.

Designed to run inside GitHub Actions on CPU — no GPU needed.
Model weights (~16MB) are cached via HF_HOME after first download.

Output written to pending.json in the same format as before,
so scores.json, auto_trader.py, and send_email.py need zero changes.
"""

import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import torch

REPO_ROOT    = Path(__file__).parent.parent
PENDING_FILE = REPO_ROOT / "pending.json"
SCORES_FILE  = REPO_ROOT / "scores.json"

BINANCE_URL  = "https://api.binance.us/api/v3/klines"
SYMBOL       = "BTCUSDT"
INTERVAL     = "1h"
LOOKBACK     = 360   # hours of history to feed the model (~15 days)
PRED_LEN     = 24    # predict 24 hours ahead
SAMPLE_COUNT = 30    # Monte Carlo paths

MODEL_NAME     = "NeoQuasar/Kronos-mini"
TOKENIZER_NAME = "NeoQuasar/Kronos-Tokenizer-2k"


# ─── Binance data ─────────────────────────────────────────────────────────────

def fetch_btc_history(lookback=LOOKBACK):
    """Fetch the last `lookback` 1h BTC/USDT candles from Binance."""
    print(f"  Fetching {lookback}h of BTC/USDT data from Binance...")
    url = f"{BINANCE_URL}?symbol={SYMBOL}&interval={INTERVAL}&limit={lookback}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            raw = json.loads(r.read())
    except Exception as e:
        print(f"  Binance fetch failed: {e}")
        return None

    records = []
    for c in raw:
        ts = datetime.fromtimestamp(c[0]/1000, tz=timezone.utc)
        records.append({
            "timestamps": ts,
            "open":   float(c[1]),
            "high":   float(c[2]),
            "low":    float(c[3]),
            "close":  float(c[4]),
            "volume": float(c[5]),
            "amount": float(c[7]),  # quote asset volume
        })

    df = pd.DataFrame(records)
    print(f"  Got {len(df)} candles. Latest: {df['timestamps'].iloc[-1]} close=${df['close'].iloc[-1]:,.2f}")
    return df


def get_future_timestamps(last_ts, pred_len=PRED_LEN):
    """Generate future 1h timestamps for the prediction window."""
    return pd.Series([
        last_ts + timedelta(hours=i+1)
        for i in range(pred_len)
    ])


# ─── Kronos inference ─────────────────────────────────────────────────────────

def load_kronos():
    """Load Kronos-mini model and tokenizer from Hugging Face."""
    print(f"  Loading {MODEL_NAME} from Hugging Face...")

    # Add Kronos model directory to path
    kronos_model_dir = REPO_ROOT / "kronos_model"
    if kronos_model_dir.exists():
        sys.path.insert(0, str(kronos_model_dir))
    else:
        # Clone the model code if not present
        print("  Downloading Kronos model code...")
        import subprocess
        result = subprocess.run(
            ["git", "clone", "--depth=1",
             "https://github.com/shiyu-coder/Kronos.git",
             str(kronos_model_dir)],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"  Git clone failed: {result.stderr}")
            return None, None
        sys.path.insert(0, str(kronos_model_dir))

    try:
        from model import Kronos, KronosTokenizer, KronosPredictor

        tokenizer = KronosTokenizer.from_pretrained(TOKENIZER_NAME)
        model     = Kronos.from_pretrained(MODEL_NAME)
        predictor = KronosPredictor(model, tokenizer, device="cpu", max_context=2048)
        print("  Model loaded successfully.")
        return predictor, None
    except Exception as e:
        print(f"  Model load failed: {e}")
        import traceback; traceback.print_exc()
        return None, str(e)


def run_inference(predictor, df):
    """
    Run Kronos-mini inference with Monte Carlo sampling.
    Returns list of pred_df DataFrames (one per sample path).
    """
    print(f"  Running {SAMPLE_COUNT} Monte Carlo paths (pred_len={PRED_LEN}h)...")

    x_df        = df[["open","high","low","close","volume","amount"]].copy()
    x_timestamp = df["timestamps"]
    y_timestamp = get_future_timestamps(df["timestamps"].iloc[-1], PRED_LEN)

    paths = []
    for i in range(SAMPLE_COUNT):
        try:
            pred = predictor.predict(
                df=x_df,
                x_timestamp=x_timestamp,
                y_timestamp=y_timestamp,
                pred_len=PRED_LEN,
                T=1.0,
                top_p=0.9,
                sample_count=1,
            )
            paths.append(pred)
        except Exception as e:
            print(f"    Path {i+1} failed: {e}")
            continue

    print(f"  Got {len(paths)} valid paths out of {SAMPLE_COUNT}")
    return paths


# ─── Derive prediction signals ────────────────────────────────────────────────

def derive_signals(paths, current_price):
    """
    From Monte Carlo paths, derive:
      upside_prob          — P(price in 24h > current price)
      vol_amplification_prob — P(realized vol in 24h > historical vol)
    """
    if not paths:
        return None, None

    # 24h close prices across all paths
    final_closes = []
    path_vols    = []

    for pred_df in paths:
        closes = pred_df["close"].values
        if len(closes) == 0:
            continue

        # Final price after 24h
        final_closes.append(closes[-1])

        # Realized vol — std of log returns across the 24h path
        if len(closes) > 1:
            log_returns = np.diff(np.log(closes))
            path_vols.append(np.std(log_returns))

    if not final_closes:
        return None, None

    # Upside probability
    upside_count = sum(1 for p in final_closes if p > current_price)
    upside_prob  = upside_count / len(final_closes)

    # Vol amplification — compare predicted vol to historical vol
    vol_amplification_prob = 0.5  # default neutral
    if path_vols:
        # Historical vol from the input data (last 24 candles)
        # We'll compute this from the Binance data passed in
        vol_amplification_prob = 0.5  # placeholder — set from caller

    return round(upside_prob, 4), path_vols


def compute_hist_vol(df, window=24):
    """Compute historical realized vol from last `window` candles."""
    closes = df["close"].values[-window-1:]
    if len(closes) < 2:
        return None
    log_returns = np.diff(np.log(closes))
    return float(np.std(log_returns))


# ─── Staleness check ──────────────────────────────────────────────────────────

def get_latest_pending_ts():
    """Return the most recent prediction_timestamp in pending.json."""
    data = []
    if PENDING_FILE.exists():
        with open(PENDING_FILE) as f:
            data = json.load(f)
    if not data:
        return None
    tss = [p.get("prediction_timestamp","") for p in data]
    return max(tss) if tss else None


def already_predicted_this_hour(now_utc):
    """Check if we already have a prediction for the current hour."""
    current_hour = now_utc.strftime("%Y-%m-%d %H:")
    latest = get_latest_pending_ts()
    if latest and latest.startswith(current_hour):
        return True
    # Also check scores.json
    if SCORES_FILE.exists():
        with open(SCORES_FILE) as f:
            scores = json.load(f)
        for s in scores:
            if s.get("prediction_timestamp","").startswith(current_hour):
                return True
    return False


# ─── Main prediction function ─────────────────────────────────────────────────

def generate_prediction():
    """
    Full pipeline: fetch data → run Kronos → derive signals → save to pending.json
    Returns the prediction dict or None on failure.
    """
    now_utc = datetime.now(timezone.utc)
    hour_str = now_utc.strftime("%Y-%m-%d %H:00:00")

    print(f"\n=== Kronos Native Predictor ===")
    print(f"  Time: {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")

    # Skip if already predicted this hour
    if already_predicted_this_hour(now_utc):
        print(f"  Already have prediction for this hour — skipping")
        return None

    # Fetch BTC data
    df = fetch_btc_history(LOOKBACK)
    if df is None or len(df) < 48:
        print("  Insufficient data — aborting")
        return None

    current_price = float(df["close"].iloc[-1])
    hist_vol      = compute_hist_vol(df, window=24)
    print(f"  Current BTC price: ${current_price:,.2f}")
    print(f"  Historical vol (24h): {hist_vol:.6f}" if hist_vol else "  Historical vol: N/A")

    # Load model
    predictor, err = load_kronos()
    if predictor is None:
        print(f"  Cannot load model: {err}")
        return None

    # Run inference
    paths = run_inference(predictor, df)
    if not paths:
        print("  No valid paths generated — aborting")
        return None

    # Derive signals
    upside_prob, path_vols = derive_signals(paths, current_price)
    if upside_prob is None:
        print("  Could not derive signals — aborting")
        return None

    # Vol amplification probability
    vol_amplification_prob = 0.5
    if path_vols and hist_vol and hist_vol > 0:
        amplified_count = sum(1 for v in path_vols if v > hist_vol)
        vol_amplification_prob = round(amplified_count / len(path_vols), 4)

    # Mean forecast stats
    final_closes = [p["close"].values[-1] for p in paths if len(p) > 0]
    mean_final   = float(np.mean(final_closes)) if final_closes else current_price
    mean_change  = (mean_final - current_price) / current_price * 100

    print(f"\n  === Prediction Results ===")
    print(f"  Upside probability     : {upside_prob*100:.1f}%")
    print(f"  Vol amplification prob : {vol_amplification_prob*100:.1f}%")
    print(f"  Mean forecast close    : ${mean_final:,.2f} ({mean_change:+.2f}%)")
    print(f"  Paths sampled          : {len(paths)}")

    prediction = {
        "upside_prob":              upside_prob,
        "vol_amplification_prob":   vol_amplification_prob,
        "demo_last_updated":        hour_str,
        "prediction_timestamp":     hour_str,
        "scrape_timestamp":         now_utc.isoformat(),
        "current_price":            round(current_price, 2),
        "mean_forecast_close":      round(mean_final, 2),
        "mean_forecast_change_pct": round(mean_change, 4),
        "paths_generated":          len(paths),
        "source":                   "kronos-mini-local",
    }

    # Append to pending.json
    pending = []
    if PENDING_FILE.exists():
        with open(PENDING_FILE) as f:
            pending = json.load(f)

    pending.append(prediction)

    with open(PENDING_FILE, "w") as f:
        json.dump(pending, f, indent=2)

    print(f"  Saved to pending.json ({len(pending)} total pending)")
    return prediction


if __name__ == "__main__":
    result = generate_prediction()
    if result:
        print(f"\n  Done. Upside: {result['upside_prob']*100:.1f}% | Vol: {result['vol_amplification_prob']*100:.1f}%")
    else:
        print("\n  No prediction generated this run.")
