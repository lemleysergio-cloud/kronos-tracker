"""
Kronos BTC Accuracy Tracker — v2 (Native Inference Edition)
============================================================
Replaced demo scraping with direct Kronos-mini inference.
All scoring, reporting, and pending/scores.json schemas
remain identical — nothing downstream needs to change.

Run modes:
  python kronos_tracker.py --scrape    # generate prediction via Kronos-mini
  python kronos_tracker.py --score     # score pending predictions 24h+ old
  python kronos_tracker.py --report    # print accuracy summary
  python kronos_tracker.py --all       # score + scrape (GitHub Actions mode)
"""

import argparse
import json
import math
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT    = Path(__file__).parent.parent.resolve()
SCORES_FILE  = REPO_ROOT / "scores.json"
PENDING_FILE = REPO_ROOT / "pending.json"

BINANCE_URL = "https://api.binance.us/api/v3/klines"
SYMBOL      = "BTCUSDT"
INTERVAL    = "1h"


# ─── Binance helpers ──────────────────────────────────────────────────────────

def fetch_klines(limit=25, start_ms=None):
    url = f"{BINANCE_URL}?symbol={SYMBOL}&interval={INTERVAL}&limit={limit}"
    if start_ms: url += f"&startTime={start_ms}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  Binance error: {e}"); return []

def get_price_at(dt_utc):
    aligned = dt_utc.replace(minute=0, second=0, microsecond=0)
    now_h   = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    if aligned >= now_h:
        aligned = now_h - timedelta(hours=1)
    ms  = int(aligned.timestamp()*1000)
    raw = fetch_klines(limit=2, start_ms=ms)
    return float(raw[0][4]) if raw else None

def compute_realized_vol(dt_utc, hours=24):
    ms  = int(dt_utc.replace(minute=0,second=0,microsecond=0).timestamp()*1000)
    raw = fetch_klines(limit=hours+1, start_ms=ms)
    closes = [float(c[4]) for c in raw]
    if len(closes) < 2: raise ValueError("Not enough candles")
    lr   = [math.log(closes[i]/closes[i-1]) for i in range(1,len(closes))]
    mean = sum(lr)/len(lr)
    var  = sum((r-mean)**2 for r in lr)/len(lr)
    return math.sqrt(var)


# ─── I/O ──────────────────────────────────────────────────────────────────────

def load_json(path, default):
    if path.exists():
        with open(path) as f: return json.load(f)
    return default

def save_json(path, data):
    with open(path,"w") as f: json.dump(data, f, indent=2)
    print(f"  Saved {path.name}")


# ─── Scoring ──────────────────────────────────────────────────────────────────

def score_prediction(pending):
    raw_ts = pending["prediction_timestamp"]
    try:
        pred_dt = datetime.strptime(raw_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        pred_dt = datetime.fromisoformat(raw_ts)
        if pred_dt.tzinfo is None: pred_dt = pred_dt.replace(tzinfo=timezone.utc)

    price_t0  = get_price_at(pred_dt)
    price_t24 = get_price_at(pred_dt + timedelta(hours=24))
    if price_t0 is None or price_t24 is None:
        raise ValueError("Could not fetch BTC prices")

    price_change_pct = (price_t24 - price_t0) / price_t0 * 100
    went_up          = price_t24 > price_t0

    hist_vol     = compute_realized_vol(pred_dt - timedelta(hours=24), hours=24)
    realized_vol = compute_realized_vol(pred_dt, hours=24)
    vol_ratio    = realized_vol / hist_vol if hist_vol > 0 else 1.0
    vol_amplified= vol_ratio > 1.0

    upside_prob      = pending["upside_prob"]
    direction_correct= (upside_prob > 0.5) == went_up
    brier_score      = (upside_prob - (1.0 if went_up else 0.0))**2

    vol_prob    = pending["vol_amplification_prob"]
    vol_correct = (vol_prob > 0.5) == vol_amplified
    vol_brier   = (vol_prob - (1.0 if vol_amplified else 0.0))**2

    return {
        **pending,
        "score_timestamp":    datetime.now(timezone.utc).isoformat(),
        "price_t0":           price_t0,
        "price_t24":          price_t24,
        "price_change_pct":   round(price_change_pct, 4),
        "went_up":            went_up,
        "direction_correct":  direction_correct,
        "brier_score":        round(brier_score, 6),
        "hist_vol":           round(hist_vol, 8),
        "realized_vol":       round(realized_vol, 8),
        "realized_vol_ratio": round(vol_ratio, 4),
        "vol_amplified":      vol_amplified,
        "vol_correct":        vol_correct,
        "vol_brier_score":    round(vol_brier, 6),
    }


# ─── Stats & report ───────────────────────────────────────────────────────────

def compute_stats(records, window=None):
    if window: records = records[-window:]
    if not records: return {}
    n  = len(records)
    dc = sum(1 for r in records if r.get("direction_correct"))
    vc = sum(1 for r in records if r.get("vol_correct"))
    ab = sum(r.get("brier_score",0.5) for r in records)/n
    streak = 0
    for r in reversed(records):
        if r.get("direction_correct"): streak+=1
        else: break
    return {
        "n": n,
        "direction_accuracy_pct": round(dc/n*100,1),
        "vol_accuracy_pct":       round(vc/n*100,1),
        "avg_brier_score":        round(ab,4),
        "correct_streak":         streak,
        "dir_correct":            dc,
    }

def print_report(records):
    if not records:
        print("No scored records yet."); return
    print("\n" + "="*60)
    print("  KRONOS BTC ACCURACY REPORT (Native Inference Edition)")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("="*60)
    for label, window in [("All-time",None),("Last 30",30),("Last 7",7)]:
        s = compute_stats(records, window)
        if not s: continue
        print(f"\n  {label} (n={s['n']})")
        print(f"    Direction accuracy: {s['direction_accuracy_pct']}%")
        print(f"    Vol accuracy      : {s['vol_accuracy_pct']}%")
        print(f"    Avg Brier score   : {s['avg_brier_score']}")
        print(f"    Streak            : {s['correct_streak']}")
    print()


# ─── CLI commands ─────────────────────────────────────────────────────────────

def find_predictor():
    """Find kronos_predictor.py — works locally and in GitHub Actions."""
    this_dir  = Path(__file__).parent.resolve()
    repo_root = this_dir.parent.resolve()
    cwd       = Path.cwd().resolve()

    candidates = [
        this_dir / "kronos_predictor.py",
        repo_root / "tracker" / "kronos_predictor.py",
        cwd / "tracker" / "kronos_predictor.py",
        cwd / "kronos_predictor.py",
    ]

    for path in candidates:
        if path.exists():
            print(f"  Found kronos_predictor.py at: {path}")
            return path

    print("  kronos_predictor.py not found. Searched:")
    for p in candidates:
        print(f"    {p}")
    return None


def cmd_scrape():
    """Generate a new prediction using Kronos-mini."""
    predictor_path = find_predictor()
    if predictor_path is None:
        print("  ERROR: Cannot find kronos_predictor.py")
        print("  Make sure tracker/kronos_predictor.py exists in your repo.")
        sys.exit(1)

    # Add its directory to sys.path so it can be imported
    pred_dir = str(predictor_path.parent)
    if pred_dir not in sys.path:
        sys.path.insert(0, pred_dir)

    # Also add repo root and cwd
    for extra in [str(REPO_ROOT), str(Path.cwd())]:
        if extra not in sys.path:
            sys.path.insert(0, extra)

    try:
        # Force fresh import in case of stale cache
        if "kronos_predictor" in sys.modules:
            del sys.modules["kronos_predictor"]

        import importlib.util
        spec   = importlib.util.spec_from_file_location("kronos_predictor", predictor_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        result = module.generate_prediction()
        if result:
            print(f"\n  Upside: {result['upside_prob']*100:.1f}% | Vol: {result['vol_amplification_prob']*100:.1f}%")
            print(f"  Mean forecast: ${result.get('mean_forecast_close',0):,.2f} ({result.get('mean_forecast_change_pct',0):+.2f}%)")
        else:
            print("  No prediction generated (already have one for this hour or error).")

    except Exception as e:
        print(f"  Import/run failed: {e}")
        print("  Falling back to subprocess...")
        result = subprocess.run(
            [sys.executable, str(predictor_path)],
            capture_output=True, text=True
        )
        print(result.stdout)
        if result.returncode != 0:
            print(f"  Subprocess error: {result.stderr}")
            sys.exit(1)


def cmd_score():
    """Score pending predictions that are 24h+ old."""
    pending = load_json(PENDING_FILE, [])
    if not pending:
        print("No pending predictions."); return

    now      = datetime.now(timezone.utc)
    scored   = load_json(SCORES_FILE, [])
    remaining= []
    count    = 0

    for p in pending:
        scrape_ts = datetime.fromisoformat(p["scrape_timestamp"])
        if scrape_ts.tzinfo is None: scrape_ts = scrape_ts.replace(tzinfo=timezone.utc)
        age = (now - scrape_ts).total_seconds()/3600

        if age >= 24:
            print(f"  Scoring {p['prediction_timestamp']}...")
            try:
                result = score_prediction(p)
                scored.append(result)
                count += 1
                icon = "✅" if result["direction_correct"] else "❌"
                print(f"    {icon} direction={'UP' if result['went_up'] else 'DOWN'} | Δ{result['price_change_pct']:+.2f}% | Brier={result['brier_score']:.3f}")
            except Exception as e:
                print(f"    Error: {e} — keeping in pending")
                remaining.append(p)
        else:
            remaining.append(p)

    if count > 0:
        save_json(SCORES_FILE, scored)
    save_json(PENDING_FILE, remaining)
    print(f"  Scored {count} predictions. {len(remaining)} still pending.")


def cmd_report():
    scores = load_json(SCORES_FILE, [])
    print_report(scores)


def cmd_all():
    pending = load_json(PENDING_FILE, [])
    if pending:
        print("--- Scoring pending predictions ---")
        cmd_score()
    else:
        print("No pending predictions to score yet.")
    print("\n--- Generating new prediction ---")
    cmd_scrape()
    print("\n--- Current report ---")
    cmd_report()


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Kronos BTC Tracker v2")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--scrape", action="store_true")
    g.add_argument("--score",  action="store_true")
    g.add_argument("--report", action="store_true")
    g.add_argument("--all",    action="store_true")
    args = parser.parse_args()

    if args.scrape:   cmd_scrape()
    elif args.score:  cmd_score()
    elif args.report: cmd_report()
    elif args.all:    cmd_all()

if __name__ == "__main__":
    main()
