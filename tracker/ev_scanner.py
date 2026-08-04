"""
Kronos EV Scanner — standalone, optional module
==================================================
Separate from barbell_tracker.py by design. Toggle on/off independently,
delete it entirely, or extend it without touching anything else that's
already working.

What it does, every hour:
  1. Reads the latest Kronos prediction (uses raw Monte Carlo paths if
     available, falls back to a normal-curve approximation if not)
  2. Pulls the FULL live Kalshi contract chain for the matching hour
     (every strike Kalshi lists, not just pre-picked ones)
  3. For every strike, computes:
       - Kronos's raw implied probability (% of MC paths above/below strike)
       - A CALIBRATED probability, adjusted using your own historical
         accuracy at that confidence level (from scores.json)
       - Kalshi's market-implied probability (its own YES price)
       - Expected value of buying YES or NO at Kalshi's current price
  4. Also scores 2-leg combos (directional barbell, range bet) using the
     same calibrated per-strike probabilities
  5. Ranks every single-leg and combo play by EV, surfaces the best one
  6. Logs whatever play scored highest to ev_trades.json
  7. Scores that play once the hour settles, using real Binance data

Nothing here talks to barbell_tracker.py, auto_trader.py, or the email
builder's existing sections. It's called from the workflow as its own
step and writes its own files.

Files:
  ev_trades.json     — logged EV-ranked plays + outcomes (this module owns it)
  Reads (read-only):  pending.json, scores.json
"""

import json
import math
import statistics
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT    = Path(__file__).parent.parent
PENDING_FILE = REPO_ROOT / "pending.json"
SCORES_FILE  = REPO_ROOT / "scores.json"
EV_FILE      = REPO_ROOT / "ev_trades.json"

KALSHI_API_BASE   = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_BTC_SERIES = "KXBTCD"

HORIZON_HOURS = {"1h": 1, "24h": 24}

# Minimum edge (percentage points, calibrated prob − Kalshi price) before
# a play is even considered. Below this, transaction costs / spread likely
# eat the whole "edge" — not worth logging as a real play.
MIN_EDGE_PTS = 5.0

# ─── I/O ──────────────────────────────────────────────────────────────────────

def load_json(path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  Saved {path.name}")


# ─── Kronos: distribution + implied probability at any strike ─────────────────

def get_latest_prediction(horizon):
    """Newest pending prediction for a given horizon, or None."""
    pending = load_json(PENDING_FILE, [])
    matches = [p for p in pending if p.get("horizon") == horizon]
    if not matches:
        return None
    return max(matches, key=lambda p: p.get("prediction_timestamp", ""))


def kronos_prob_above(prediction, strike):
    """
    P(BTC settles above `strike`) according to Kronos.
    Uses raw Monte Carlo paths if present (mc_final_prices), otherwise
    falls back to a normal-distribution approximation from mean + range.
    """
    paths = prediction.get("mc_final_prices")
    if paths:
        return sum(1 for p in paths if p > strike) / len(paths)

    # Fallback: approximate as normal(mean, std) using the 5th/95th
    # percentile range Kronos already reports (older records only).
    mean = prediction.get("mean_forecast_close")
    lo   = prediction.get("forecast_low")
    hi   = prediction.get("forecast_high")
    if mean is None or lo is None or hi is None:
        return None
    # 90% CI width ≈ 3.29 std devs for a normal distribution
    std = max((hi - lo) / 3.29, 1e-6)
    z = (strike - mean) / std
    # standard normal CDF via erf
    cdf = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    return 1 - cdf  # P(above strike)


# ─── Historical calibration ────────────────────────────────────────────────────

def load_calibration_curve():
    """
    Bucket all scored predictions by their stated upside_prob, and compute
    the ACTUAL hit rate in each bucket. This is what turns Kronos's raw
    probability into a calibrated one — e.g. if Kronos says 70% but your
    own history shows 70%-ish calls only hit 58% of the time, we trust 58%.

    Returns a list of (bucket_low, bucket_high, actual_hit_rate, n) tuples,
    sorted by bucket_low. Empty list if not enough data yet.
    """
    scores = load_json(SCORES_FILE, [])
    if len(scores) < 20:
        return []  # not enough history to calibrate reliably yet

    buckets = [(0.0,0.2), (0.2,0.4), (0.4,0.6), (0.6,0.8), (0.8,1.01)]
    curve = []
    for lo, hi in buckets:
        in_bucket = [s for s in scores if lo <= s.get("upside_prob", 0.5) < hi]
        if len(in_bucket) < 5:
            continue
        hits = sum(1 for s in in_bucket if s.get("went_up"))
        curve.append((lo, hi, hits/len(in_bucket), len(in_bucket)))
    return curve


def calibrate(raw_prob, curve):
    """
    Map a raw Kronos probability through the historical calibration curve.
    If no curve data exists yet (cold start), returns raw_prob unchanged
    and flags it so callers know the number is un-calibrated.
    """
    if not curve:
        return raw_prob, False
    for lo, hi, actual, n in curve:
        if lo <= raw_prob < hi:
            return actual, True
    return raw_prob, False


# ─── Kalshi: full chain for the hour ───────────────────────────────────────────

def fetch_kalshi_chain(limit=100):
    """All currently open KXBTCD markets — the full strike ladder, not
    just pre-picked strikes. Public endpoint, no auth."""
    url = f"{KALSHI_API_BASE}/markets"
    params = f"series_ticker={KALSHI_BTC_SERIES}&status=open&limit={limit}"
    try:
        with urllib.request.urlopen(f"{url}?{params}", timeout=10) as r:
            data = json.loads(r.read())
        out = []
        for m in data.get("markets", []):
            ticker = m.get("ticker", "")
            if "-T" not in ticker:
                continue
            try:
                strike = int(ticker.split("-T")[-1])
            except ValueError:
                continue
            out.append({
                "ticker": ticker,
                "strike": strike,
                "yes_ask": m.get("yes_ask"),
                "yes_bid": m.get("yes_bid"),
                "no_ask":  m.get("no_ask"),
                "no_bid":  m.get("no_bid"),
                "close_time": m.get("close_time"),
            })
        return sorted(out, key=lambda x: x["strike"])
    except Exception as e:
        print(f"  Kalshi chain fetch failed: {e}")
        return []


# ─── EV math ──────────────────────────────────────────────────────────────────

def contract_ev(calibrated_prob_yes, price_cents, side="yes"):
    """
    Expected value of buying one contract at price_cents (in cents),
    given our calibrated true probability of YES.
    Returns EV in cents per $1 contract (positive = good bet).
    """
    if price_cents is None or price_cents <= 0 or price_cents >= 100:
        return None
    p_yes = calibrated_prob_yes
    if side == "no":
        p_win = 1 - p_yes
        cost  = 100 - price_cents  # NO costs (100 - yes_ask) implicitly via no_ask normally; caller passes correct price
    else:
        p_win = p_yes
        cost  = price_cents
    payout_if_win = 100  # contract pays $1 = 100 cents
    ev = p_win * payout_if_win - cost
    return round(ev, 2)


def scan_single_legs(chain, prediction, calib_curve):
    """Score every strike in the chain, both YES and NO sides."""
    results = []
    for m in chain:
        raw_p = kronos_prob_above(prediction, m["strike"])
        if raw_p is None:
            continue
        cal_p, was_calibrated = calibrate(raw_p, calib_curve)

        for side, price in [("yes", m["yes_ask"]), ("no", m["no_ask"])]:
            if price is None:
                continue
            ev = contract_ev(cal_p, price, side)
            if ev is None:
                continue
            market_implied = price if side == "yes" else (100 - price)
            edge = round(cal_p * 100 - market_implied, 1) if side == "yes" else round((1-cal_p)*100 - market_implied, 1)
            results.append({
                "play_type":       "single",
                "ticker":          m["ticker"],
                "strike":          m["strike"],
                "side":            side,
                "price_cents":     price,
                "raw_kronos_prob": round(raw_p*100, 1),
                "calibrated_prob": round(cal_p*100, 1),
                "was_calibrated":  was_calibrated,
                "ev_cents":        ev,
                "edge_pts":        edge,
            })
    return results


def scan_range_combos(chain, prediction, calib_curve, max_width_strikes=3):
    """
    Score NO-above-X + YES-above-Y range combos (i.e. betting price stays
    between two nearby strikes), for strikes reasonably close together.
    """
    results = []
    n = len(chain)
    for i in range(n):
        for j in range(i+1, min(i+1+max_width_strikes, n)):
            lower, upper = chain[i], chain[j]
            if lower["strike"] >= upper["strike"]:
                continue
            # YES on lower strike (price ends above lower) + NO on upper strike (price ends below upper)
            p_above_lower = kronos_prob_above(prediction, lower["strike"])
            p_above_upper = kronos_prob_above(prediction, upper["strike"])
            if p_above_lower is None or p_above_upper is None:
                continue
            cal_lower, _ = calibrate(p_above_lower, calib_curve)
            cal_upper, _ = calibrate(p_above_upper, calib_curve)
            p_in_range = max(cal_lower - cal_upper, 0.0)

            yes_price = lower.get("yes_ask")
            no_price_upper = (100 - upper.get("yes_ask")) if upper.get("yes_ask") else None
            if yes_price is None or no_price_upper is None:
                continue

            total_cost = yes_price + no_price_upper
            ev_both_win = p_in_range * 100  # both legs pay out $1 combined... actually each leg pays separately
            # Correct combo EV: profit if both win = (100-yes_price)+(100-no_price_upper); lose both stakes otherwise
            profit_both_win = (100 - yes_price) + (100 - no_price_upper)
            ev = round(p_in_range * profit_both_win - (1 - p_in_range) * total_cost, 2)

            if ev is None:
                continue

            results.append({
                "play_type":    "range",
                "lower_ticker": lower["ticker"], "lower_strike": lower["strike"],
                "upper_ticker": upper["ticker"], "upper_strike": upper["strike"],
                "total_cost_cents": total_cost,
                "calibrated_prob_in_range": round(p_in_range*100, 1),
                "ev_cents":     ev,
                "edge_pts":     round(p_in_range*100 - 50, 1),  # rough reference vs coin flip
            })
    return results


# ─── Best-play selection ───────────────────────────────────────────────────────

def find_best_play(chain, prediction, calib_curve):
    singles = scan_single_legs(chain, prediction, calib_curve)
    combos  = scan_range_combos(chain, prediction, calib_curve)

    all_plays = [p for p in singles if p["ev_cents"] is not None] + \
                [p for p in combos  if p["ev_cents"] is not None]

    # Only POSITIVE edge (our calibrated prob exceeds what the market charges)
    # AND positive EV — a large negative edge means a confidently bad bet,
    # not an opportunity, so it must be excluded, not just have a big magnitude.
    qualifying = [p for p in all_plays
                  if p.get("edge_pts", 0) >= MIN_EDGE_PTS and p.get("ev_cents", -1) > 0]
    ranked = sorted(qualifying, key=lambda p: p["ev_cents"], reverse=True)

    return ranked[:5]  # top 5 candidates, best first


# ─── Scoring open EV trades ─────────────────────────────────────────────────────

def get_price_at(dt_utc):
    """Settlement price lookup — delegated to Coinbase price_source."""
    import price_source
    return price_source.get_price_at(dt_utc)


def score_open_ev_trades(trades):
    now = datetime.now(timezone.utc)
    closed = 0
    for t in trades:
        if t.get("status") != "open":
            continue
        try:
            entry_dt = datetime.strptime(t["kronos_timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        hours = HORIZON_HOURS.get(t.get("horizon","1h"), 1)
        if (now - entry_dt).total_seconds()/3600 < hours:
            continue

        settle = get_price_at(entry_dt + timedelta(hours=hours))
        if settle is None:
            continue

        play = t["play"]
        if play["play_type"] == "single":
            won = settle > play["strike"] if play["side"] == "yes" else settle < play["strike"]
            pnl_cents = (100 - play["price_cents"]) if won else -play["price_cents"]
        else:  # range
            won = play["lower_strike"] < settle < play["upper_strike"]
            profit = (100 - play.get("lower_price_cents", 50)) + (100 - play.get("upper_price_cents", 50))
            pnl_cents = profit if won else -play.get("total_cost_cents", 0)

        t.update({
            "status": "closed",
            "settle_price": round(settle, 2),
            "won": won,
            "pnl_cents": round(pnl_cents, 2),
            "close_timestamp": now.isoformat(),
        })
        icon = "✅" if won else "❌"
        print(f"  {t['id']} {icon} settled ${settle:,.0f} | P&L {pnl_cents:+.1f}¢")
        closed += 1
    return closed


# ─── Main ─────────────────────────────────────────────────────────────────────

def run_horizon_scan(horizon, trades):
    prediction = get_latest_prediction(horizon)
    if prediction is None:
        print(f"  [{horizon}] No prediction available.")
        return

    print(f"  [{horizon}] Fetching full Kalshi chain...")
    chain = fetch_kalshi_chain()
    if not chain:
        print(f"  [{horizon}] No Kalshi chain data — skipping.")
        return

    calib_curve = load_calibration_curve()
    calib_status = f"{len(calib_curve)} buckets calibrated" if calib_curve else "cold start (raw Kronos probs, no calibration yet)"
    print(f"  [{horizon}] Calibration: {calib_status}")

    best = find_best_play(chain, prediction, calib_curve)
    if not best:
        print(f"  [{horizon}] No play clears the {MIN_EDGE_PTS}pt minimum edge.")
        return

    top = best[0]
    print(f"  [{horizon}] BEST PLAY: {top['play_type']} | EV {top['ev_cents']:+.1f}¢/contract | edge {top.get('edge_pts')}pts")

    trade_id = f"EV-{len(trades)+1:04d}"
    trades.append({
        "id": trade_id,
        "horizon": horizon,
        "status": "open",
        "kronos_timestamp": prediction["prediction_timestamp"],
        "entry_price": prediction.get("current_price"),
        "play": top,
        "candidates_considered": len(best),
        "calibration_status": calib_status,
        "logged_at": datetime.now(timezone.utc).isoformat(),
    })
    print(f"  [{horizon}] Logged as {trade_id}")


def main():
    print("\n=== Kronos EV Scanner (standalone) ===")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n")

    trades = load_json(EV_FILE, [])
    print(f"  Loaded {len(trades)} EV trades")

    print("\n[1] Scoring matured trades...")
    closed = score_open_ev_trades(trades)
    print(f"  Closed {closed}")

    print("\n[2] Scanning for best play per horizon...")
    for horizon in ["1h", "24h"]:
        run_horizon_scan(horizon, trades)

    save_json(EV_FILE, trades)

    closed_trades = [t for t in trades if t.get("status") == "closed"]
    wins = sum(1 for t in closed_trades if t.get("won"))
    n = len(closed_trades)
    print(f"""
=== EV Scanner Summary ===
  Total trades  : {len(trades)}
  Closed        : {n}
  Win rate      : {round(wins/n*100) if n else '—'}%
""")


if __name__ == "__main__":
    main()
