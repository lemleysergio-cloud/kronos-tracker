"""
Kronos Native Predictor — v2 (Dual Horizon)
============================================
Generates BOTH a 1-hour and a 24-hour forecast every run.

Why two horizons:
  - 1h  → matches Kalshi's hourly BTC strike ladder (KXBTCD)
  - 24h → matches daily contracts + gives trend context

Each prediction is written to pending.json as its own record with a
"horizon" field ("1h" or "24h"), so they score independently and we
can measure which timeframe Kronos is actually good at.

Output schema (per record):
  horizon                   "1h" | "24h"
  upside_prob               P(price at horizon > current price)
  vol_amplification_prob    P(realized vol > historical vol)
  current_price             BTC price when the call was made
  mean_forecast_close       Kronos's actual dollar target
  mean_forecast_change_pct  % move implied by that target
  forecast_low / forecast_high   5th/95th percentile of MC paths
"""

import json
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT    = Path(__file__).parent.parent
PENDING_FILE = REPO_ROOT / "pending.json"
SCORES_FILE  = REPO_ROOT / "scores.json"

LOOKBACK     = 360    # hours of context fed to the model
SAMPLE_COUNT = 30     # Monte Carlo paths per horizon

# Price data now comes from Coinbase (a real CF Benchmarks BRTI constituent)
# instead of Binance.US, so our numbers track Kalshi's actual settlement
# index far more closely. See price_source.py for the full rationale.
# price_source.py lives at the repo root, so it needs the root on sys.path
# (not true by default when this script is run as `python tracker/kronos_predictor.py`).
sys.path.insert(0, str(REPO_ROOT))
import price_source

HORIZONS = {
    "1h":  1,
    "24h": 24,
}

MODEL_NAME     = "NeoQuasar/Kronos-mini"
TOKENIZER_NAME = "NeoQuasar/Kronos-Tokenizer-2k"


# ─── Price data (Coinbase) ──────────────────────────────────────────────────────────────────

def fetch_btc_history(lookback=LOOKBACK):
    print(f"  Fetching {lookback}h of BTC-USD from Coinbase...")
    candles = price_source.fetch_candles(hours=lookback)
    if not candles:
        print("  Coinbase fetch returned nothing.")
        return None
    df = pd.DataFrame(candles)
    print(f"  Got {len(df)} candles. Latest close: ${df['close'].iloc[-1]:,.2f}")
    return df


def compute_hist_vol(df, window=24):
    closes = df["close"].values[-window-1:]
    if len(closes) < 2:
        return None
    return float(np.std(np.diff(np.log(closes))))


# ─── Model loading ────────────────────────────────────────────────────────────

def load_kronos():
    print(f"  Loading {MODEL_NAME}...")
    kronos_dir = REPO_ROOT / "kronos_model"
    if not kronos_dir.exists():
        print("  Cloning Kronos model code...")
        import subprocess
        res = subprocess.run(
            ["git", "clone", "--depth=1",
             "https://github.com/shiyu-coder/Kronos.git", str(kronos_dir)],
            capture_output=True, text=True
        )
        if res.returncode != 0:
            print(f"  Clone failed: {res.stderr}")
            return None
    sys.path.insert(0, str(kronos_dir))

    try:
        from model import Kronos, KronosTokenizer, KronosPredictor
        tokenizer = KronosTokenizer.from_pretrained(TOKENIZER_NAME)
        model     = Kronos.from_pretrained(MODEL_NAME)
        predictor = KronosPredictor(model, tokenizer, device="cpu", max_context=2048)
        print("  Model loaded.")
        return predictor
    except Exception as e:
        print(f"  Model load failed: {e}")
        import traceback; traceback.print_exc()
        return None


# ─── Inference ────────────────────────────────────────────────────────────────

def run_horizon(predictor, df, pred_len, label):
    """Run SAMPLE_COUNT Monte Carlo paths for one horizon."""
    print(f"\n  [{label}] Running {SAMPLE_COUNT} paths (pred_len={pred_len}h)...")

    x_df        = df[["open","high","low","close","volume","amount"]].copy()
    x_timestamp = df["timestamps"]
    last_ts     = df["timestamps"].iloc[-1]
    y_timestamp = pd.Series([last_ts + timedelta(hours=i+1) for i in range(pred_len)])

    paths = []
    for i in range(SAMPLE_COUNT):
        try:
            pred = predictor.predict(
                df=x_df, x_timestamp=x_timestamp, y_timestamp=y_timestamp,
                pred_len=pred_len, T=1.0, top_p=0.9, sample_count=1, verbose=False,
            )
            paths.append(pred)
        except Exception as e:
            if i == 0:
                print(f"    Path error: {e}")
            continue

    print(f"  [{label}] {len(paths)}/{SAMPLE_COUNT} paths succeeded")
    return paths


def derive_signals(paths, current_price, hist_vol, pred_len):
    """Turn Monte Carlo paths into probability + target metrics."""
    if not paths:
        return None

    final_closes = []
    path_vols    = []

    for p in paths:
        closes = p["close"].values
        if len(closes) == 0:
            continue
        final_closes.append(float(closes[-1]))
        if len(closes) > 1:
            path_vols.append(float(np.std(np.diff(np.log(closes)))))
        elif len(closes) == 1:
            # 1h horizon — single step, derive vol from the move itself
            path_vols.append(abs(float(np.log(closes[0] / current_price))))

    if not final_closes:
        return None

    upside_prob = sum(1 for c in final_closes if c > current_price) / len(final_closes)

    vol_amp_prob = 0.5
    if path_vols and hist_vol and hist_vol > 0:
        # scale hist_vol to the horizon length for a fair comparison
        scaled_hist = hist_vol * np.sqrt(max(pred_len, 1) / 24.0) if pred_len < 24 else hist_vol
        vol_amp_prob = sum(1 for v in path_vols if v > scaled_hist) / len(path_vols)

    mean_final = float(np.mean(final_closes))

    return {
        "upside_prob":            round(upside_prob, 4),
        "vol_amplification_prob": round(vol_amp_prob, 4),
        "mean_forecast_close":    round(mean_final, 2),
        "mean_forecast_change_pct": round((mean_final - current_price)/current_price*100, 4),
        "forecast_low":           round(float(np.percentile(final_closes, 5)), 2),
        "forecast_high":          round(float(np.percentile(final_closes, 95)), 2),
        "paths_generated":        len(paths),
        "mc_final_prices":        [round(c, 2) for c in final_closes],
    }


# ─── Dedup ────────────────────────────────────────────────────────────────────

def already_predicted(now_utc, horizon):
    """Skip if we already have this horizon for the current hour."""
    hour_prefix = now_utc.strftime("%Y-%m-%d %H:")
    for path in (PENDING_FILE, SCORES_FILE):
        if not path.exists():
            continue
        with open(path) as f:
            records = json.load(f)
        for r in records:
            if (r.get("prediction_timestamp","").startswith(hour_prefix)
                    and r.get("horizon", "24h") == horizon):
                return True
    return False


# ─── Main ─────────────────────────────────────────────────────────────────────

def generate_prediction():
    now_utc  = datetime.now(timezone.utc)
    hour_str = now_utc.strftime("%Y-%m-%d %H:00:00")

    print(f"\n=== Kronos Native Predictor (dual horizon) ===")
    print(f"  {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")

    todo = [h for h in HORIZONS if not already_predicted(now_utc, h)]
    if not todo:
        print("  Both horizons already predicted this hour — skipping.")
        return None
    print(f"  Horizons to generate: {', '.join(todo)}")

    df = fetch_btc_history()
    if df is None or len(df) < 48:
        print("  Insufficient data — aborting.")
        return None

    current_price = float(df["close"].iloc[-1])
    hist_vol      = compute_hist_vol(df, window=24)
    print(f"  Current BTC: ${current_price:,.2f} | 24h hist vol: {hist_vol:.6f}")

    predictor = load_kronos()
    if predictor is None:
        return None

    pending = json.load(open(PENDING_FILE)) if PENDING_FILE.exists() else []
    results = {}

    for horizon in todo:
        pred_len = HORIZONS[horizon]
        paths    = run_horizon(predictor, df, pred_len, horizon)
        signals  = derive_signals(paths, current_price, hist_vol, pred_len)

        if signals is None:
            print(f"  [{horizon}] No valid signals — skipping.")
            continue

        record = {
            "horizon":              horizon,
            "prediction_timestamp": hour_str,
            "scrape_timestamp":     now_utc.isoformat(),
            "current_price":        round(current_price, 2),
            "source":               "kronos-mini-local",
            "price_source":         price_source.source_name(),
            **signals,
        }
        pending.append(record)
        results[horizon] = record

        print(f"\n  [{horizon}] Upside: {signals['upside_prob']*100:.1f}%"
              f" | Vol amp: {signals['vol_amplification_prob']*100:.1f}%")
        print(f"  [{horizon}] Target: ${signals['mean_forecast_close']:,.2f}"
              f" ({signals['mean_forecast_change_pct']:+.2f}%)")
        print(f"  [{horizon}] Range:  ${signals['forecast_low']:,.0f} – ${signals['forecast_high']:,.0f}")

    if not results:
        return None

    with open(PENDING_FILE, "w") as f:
        json.dump(pending, f, indent=2)
    print(f"\n  Saved to pending.json ({len(pending)} total pending)")

    return results.get("24h") or results.get("1h")


if __name__ == "__main__":
    generate_prediction()
