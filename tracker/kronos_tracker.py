"""
Kronos BTC Accuracy Tracker — v3 (Dual Horizon)
================================================
Scores 1h predictions after 1 hour, 24h predictions after 24 hours.
Reports accuracy separately per horizon so you can see which
timeframe Kronos is actually good at.

Run modes:
  --scrape   generate 1h + 24h predictions via Kronos-mini
  --score    score any matured predictions
  --report   print accuracy summary (split by horizon)
  --all      score + scrape (GitHub Actions mode)
"""

import argparse, json, math, subprocess, sys, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT    = Path(__file__).parent.parent.resolve()
SCORES_FILE  = REPO_ROOT / "scores.json"
PENDING_FILE = REPO_ROOT / "pending.json"

# Price data comes from Coinbase (a CF Benchmarks BRTI constituent), not
# Binance.US — keeps our scoring aligned with Kalshi's real settlement.
# price_source.py lives at the repo root, not in tracker/, so it's only
# importable once the root is on sys.path (not true by default when this
# script is invoked as `python tracker/kronos_tracker.py`).
sys.path.insert(0, str(REPO_ROOT))
import price_source
import data_quality

HORIZON_HOURS = {"1h": 1, "24h": 24}


# ─── Price helpers (delegated to Coinbase price_source) ──────────────────────

def get_price_at(dt_utc):
    return price_source.get_price_at(dt_utc)

def compute_realized_vol(dt_utc, hours=24):
    return price_source.compute_realized_vol(dt_utc, hours=hours)


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
    horizon = pending.get("horizon", "24h")
    hours   = HORIZON_HOURS.get(horizon, 24)

    raw_ts = pending["prediction_timestamp"]
    try:
        pred_dt = datetime.strptime(raw_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        pred_dt = datetime.fromisoformat(raw_ts)
        if pred_dt.tzinfo is None: pred_dt = pred_dt.replace(tzinfo=timezone.utc)

    price_t0 = pending.get("current_price") or get_price_at(pred_dt)
    price_tN = get_price_at(pred_dt + timedelta(hours=hours))
    if price_t0 is None or price_tN is None:
        raise ValueError("Could not fetch BTC prices")

    change_pct = (price_tN - price_t0) / price_t0 * 100
    went_up    = price_tN > price_t0

    # Vol comparison over the matching window
    vol_window = max(hours, 2)
    hist_vol     = compute_realized_vol(pred_dt - timedelta(hours=vol_window), hours=vol_window)
    realized_vol = compute_realized_vol(pred_dt, hours=vol_window)
    vol_ratio    = realized_vol / hist_vol if hist_vol > 0 else 1.0
    vol_amplified = vol_ratio > 1.0

    up_p  = pending["upside_prob"]
    vol_p = pending["vol_amplification_prob"]

    # How close was the dollar target?
    target = pending.get("mean_forecast_close")
    target_error_pct = None
    if target:
        target_error_pct = round(abs(target - price_tN) / price_tN * 100, 4)

    result = {
        **pending,
        "score_timestamp":    datetime.now(timezone.utc).isoformat(),
        "price_t0":           price_t0,
        "price_t24":          price_tN,   # kept as price_t24 for schema compat
        "price_at_horizon":   price_tN,
        "price_change_pct":   round(change_pct, 4),
        "went_up":            went_up,
        "direction_correct":  (up_p > 0.5) == went_up,
        "brier_score":        round((up_p - (1.0 if went_up else 0.0))**2, 6),
        "hist_vol":           round(hist_vol, 8),
        "realized_vol":       round(realized_vol, 8),
        "realized_vol_ratio": round(vol_ratio, 4),
        "vol_amplified":      vol_amplified,
        "vol_correct":        (vol_p > 0.5) == vol_amplified,
        "vol_brier_score":    round((vol_p - (1.0 if vol_amplified else 0.0))**2, 6),
        "target_error_pct":   target_error_pct,
    }

    # Data-quality tags (see data_quality.py) + calibrated-probability scoring,
    # so future analysis can filter to consistent data and compare raw vs.
    # calibrated accuracy side by side. classify_entry() is idempotent —
    # rerunning it here just re-confirms tags already set at prediction time.
    data_quality.classify_entry(result)
    data_quality.classify_exit(result)

    up_cal = result.get("upside_prob_calibrated")
    if up_cal is not None:
        result["direction_correct_calibrated"] = (up_cal > 0.5) == went_up
        result["brier_score_calibrated"]       = round((up_cal - (1.0 if went_up else 0.0))**2, 6)

    return result


# ─── Stats ────────────────────────────────────────────────────────────────────

def compute_stats(records, window=None):
    if window: records = records[-window:]
    if not records: return {}
    n  = len(records)
    dc = sum(1 for r in records if r.get("direction_correct"))
    vc = sum(1 for r in records if r.get("vol_correct"))
    ab = sum(r.get("brier_score",0.5) for r in records)/n
    errs = [r["target_error_pct"] for r in records if r.get("target_error_pct") is not None]
    streak = 0
    for r in reversed(records):
        if r.get("direction_correct"): streak += 1
        else: break

    # Calibrated side-by-side, where available (only records generated after
    # calibration.py was wired in carry these — see DATA_CHANGELOG.md).
    cal_recs = [r for r in records if r.get("direction_correct_calibrated") is not None]
    cal_stats = None
    if cal_recs:
        cn  = len(cal_recs)
        cdc = sum(1 for r in cal_recs if r.get("direction_correct_calibrated"))
        cab = sum(r.get("brier_score_calibrated", 0.5) for r in cal_recs) / cn
        cal_stats = {
            "n": cn,
            "direction_accuracy_pct": round(cdc/cn*100, 1),
            "avg_brier_score":        round(cab, 4),
        }

    return {
        "n": n, "dir_correct": dc,
        "direction_accuracy_pct": round(dc/n*100,1),
        "vol_accuracy_pct":       round(vc/n*100,1),
        "avg_brier_score":        round(ab,4),
        "avg_target_error_pct":   round(sum(errs)/len(errs),3) if errs else None,
        "correct_streak":         streak,
        "calibrated":             cal_stats,
    }

def split_by_horizon(records):
    out = {"1h": [], "24h": []}
    for r in records:
        out.setdefault(r.get("horizon","24h"), []).append(r)
    return out

def print_data_quality(records):
    """Backend-only visibility into which price regime backs each scored
    record — see data_quality.py / DATA_CHANGELOG.md. Not shown in the email."""
    import collections
    counts = collections.Counter(r.get("price_regime", "untagged") for r in records)
    print("\n  ── Data regime (backend only, see DATA_CHANGELOG.md) ──")
    for regime, n in counts.most_common():
        print(f"    {regime:<28} {n}")

def print_report(records):
    if not records:
        print("No scored records yet."); return
    print("\n" + "="*62)
    print("  KRONOS ACCURACY REPORT — dual horizon")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("="*62)

    for horizon, recs in split_by_horizon(records).items():
        if not recs: continue
        print(f"\n  ── {horizon.upper()} HORIZON ({len(recs)} scored) ──")
        for label, w in [("All-time",None),("Last 30",30),("Last 7",7)]:
            s = compute_stats(recs, w)
            if not s: continue
            tgt = f" | Avg target error: {s['avg_target_error_pct']}%" if s['avg_target_error_pct'] is not None else ""
            print(f"    {label:<9} n={s['n']:<4} dir={s['direction_accuracy_pct']}%"
                  f"  vol={s['vol_accuracy_pct']}%  brier={s['avg_brier_score']}"
                  f"  streak={s['correct_streak']}{tgt}")
            if s.get("calibrated"):
                c = s["calibrated"]
                print(f"      └─ calibrated  n={c['n']:<4} dir={c['direction_accuracy_pct']}%"
                      f"  brier={c['avg_brier_score']}")

    print_data_quality(records)
    print()


# ─── Commands ─────────────────────────────────────────────────────────────────

def find_predictor():
    this_dir  = Path(__file__).parent.resolve()
    for p in [this_dir / "kronos_predictor.py",
              REPO_ROOT / "tracker" / "kronos_predictor.py",
              Path.cwd() / "tracker" / "kronos_predictor.py"]:
        if p.exists():
            print(f"  Found predictor: {p}")
            return p
    print("  kronos_predictor.py not found.")
    return None

def cmd_scrape():
    path = find_predictor()
    if path is None:
        sys.exit(1)
    sys.path.insert(0, str(path.parent))
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("kronos_predictor", path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.generate_prediction()
    except Exception as e:
        print(f"  Import failed ({e}) — trying subprocess...")
        res = subprocess.run([sys.executable, str(path)], capture_output=True, text=True)
        print(res.stdout)
        if res.returncode != 0:
            print(f"  Error: {res.stderr}"); sys.exit(1)

def cmd_score():
    pending = load_json(PENDING_FILE, [])
    if not pending:
        print("No pending predictions."); return

    now       = datetime.now(timezone.utc)
    scored    = load_json(SCORES_FILE, [])
    remaining = []
    counts    = {"1h": 0, "24h": 0}

    for p in pending:
        horizon = p.get("horizon", "24h")
        needed  = HORIZON_HOURS.get(horizon, 24)

        ts = datetime.fromisoformat(p["scrape_timestamp"])
        if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
        age = (now - ts).total_seconds()/3600

        if age >= needed:
            try:
                result = score_prediction(p)
                scored.append(result)
                counts[horizon] = counts.get(horizon, 0) + 1
                icon = "✅" if result["direction_correct"] else "❌"
                tgt  = f" | target off by {result['target_error_pct']}%" if result.get("target_error_pct") is not None else ""
                print(f"  {icon} [{horizon}] {p['prediction_timestamp']} "
                      f"Δ{result['price_change_pct']:+.2f}% brier={result['brier_score']:.3f}{tgt}")
            except Exception as e:
                print(f"  Error scoring [{horizon}] {p['prediction_timestamp']}: {e}")
                remaining.append(p)
        else:
            remaining.append(p)

    if sum(counts.values()) > 0:
        save_json(SCORES_FILE, scored)
    save_json(PENDING_FILE, remaining)
    print(f"  Scored {counts.get('1h',0)} × 1h, {counts.get('24h',0)} × 24h. "
          f"{len(remaining)} still pending.")

def cmd_report():
    print_report(load_json(SCORES_FILE, []))

def cmd_all():
    print("--- Scoring matured predictions ---")
    cmd_score()
    print("\n--- Generating new predictions ---")
    cmd_scrape()
    print("\n--- Current report ---")
    cmd_report()


def main():
    parser = argparse.ArgumentParser(description="Kronos BTC Tracker v3 (dual horizon)")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--scrape", action="store_true")
    g.add_argument("--score",  action="store_true")
    g.add_argument("--report", action="store_true")
    g.add_argument("--all",    action="store_true")
    a = parser.parse_args()
    if a.scrape: cmd_scrape()
    elif a.score: cmd_score()
    elif a.report: cmd_report()
    elif a.all: cmd_all()

if __name__ == "__main__":
    main()
