"""
Kalshi Barbell Strike Tracker
==============================
Replaces the old BTC spot paper trader.

What it does:
  1. Reads the newest Kronos prediction for each horizon (1h, 24h)
  2. Computes SUGGESTED Kalshi strikes for a barbell entry:
       - Conservative leg: cushioned strike, needs Kronos only roughly right
       - Aggressive leg:   strike at Kronos's actual dollar target
  3. Logs any barbell trades you actually took (barbell_trades.json)
  4. Scores those trades once the horizon matures, using real Binance data

What it deliberately does NOT do:
  Auto-size or auto-execute. Kalshi/Robinhood contract prices move
  constantly, so sizing has to happen live when you're looking at the
  real odds. This computes the strikes and tracks outcomes; you decide
  the dollars in the moment.

barbell_trades.json record shape (you add these manually or via helper):
  {
    "id": "BB-0001",
    "horizon": "1h",
    "kronos_timestamp": "2026-08-02 20:00:00",
    "status": "open",
    "direction": "up",
    "entry_price": 63402.35,
    "conservative": {"strike": 63300, "cost_cents": 68, "stake_usd": 40},
    "aggressive":   {"strike": 63600, "cost_cents": 35, "stake_usd": 10}
  }
"""

import json
import math
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT     = Path(__file__).parent.parent
PENDING_FILE  = REPO_ROOT / "pending.json"
SCORES_FILE   = REPO_ROOT / "scores.json"
TRADES_FILE   = REPO_ROOT / "barbell_trades.json"

BINANCE_URL   = "https://api.binance.us/api/v3/klines"
FG_URL        = "https://api.alternative.me/fng/?limit=1"

HORIZON_HOURS = {"1h": 1, "24h": 24}

# Kalshi and Robinhood BTC strike contracts are listed in flat $100
# increments (e.g. $63,200, $63,300 — never $63,250).
def strike_increment(price):
    return 100

# How much cushion the conservative leg gets, as a fraction of the
# predicted move. 0.30 = conservative strike sits 30% of the move
# BEHIND current price (i.e. price can drift against you and still win).
CONSERVATIVE_CUSHION = 0.30

# Minimum predicted move (%) for a barbell to be worth considering.
# Below this the two strikes collapse onto each other and there's no
# meaningful spread between the legs.
MIN_MOVE_PCT = 0.15

# Kalshi public market data — no API key needed, read-only endpoints only.
KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_BTC_SERIES = "KXBTCD"   # hourly BTC up/down series

# How much vol_amplification_prob widens or tightens the barbell spread.
# 0.0 = vol has no effect · 1.0 = vol can double the cushion/stretch
VOL_WIDEN_FACTOR = 0.6


# ─── I/O ──────────────────────────────────────────────────────────────────────

def load_json(path, default):
    if path.exists():
        with open(path) as f: return json.load(f)
    return default

def save_json(path, data):
    with open(path, "w") as f: json.dump(data, f, indent=2)
    print(f"  Saved {path.name}")


# ─── Market data ──────────────────────────────────────────────────────────────

def fetch_fear_greed():
    try:
        with urllib.request.urlopen(FG_URL, timeout=8) as r:
            d = json.loads(r.read())
        return int(d["data"][0]["value"]), d["data"][0]["value_classification"]
    except Exception:
        return None, None

def get_price_at(dt_utc):
    aligned = dt_utc.replace(minute=0, second=0, microsecond=0)
    now_h   = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    if aligned >= now_h:
        aligned = now_h - timedelta(hours=1)
    url = f"{BINANCE_URL}?symbol=BTCUSDT&interval=1h&limit=2&startTime={int(aligned.timestamp()*1000)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            raw = json.loads(r.read())
        return float(raw[0][4]) if raw else None
    except Exception:
        return None


# ─── Strike math ──────────────────────────────────────────────────────────────

def snap(price, inc, mode="nearest"):
    """Snap a price to a Kalshi strike increment.
    mode="down" always rounds down, mode="up" always rounds up.
    Used so the conservative leg never accidentally snaps to a
    HARDER strike than intended (which would erase the cushion)."""
    if mode == "down":
        return int(math.floor(price / inc) * inc)
    if mode == "up":
        return int(math.ceil(price / inc) * inc)
    return int(round(price / inc) * inc)


def suggest_barbell(current_price, forecast_price, upside_prob, horizon, vol_amplification_prob=None):
    """
    Returns a dict of suggested strikes, or None if the move is too small
    to build a meaningful barbell around.

    Conservative leg  → strike sits CUSHION behind current price.
                        Wins even if Kronos overshoots or price drifts slightly
                        against you. Higher win rate, lower payout.
    Aggressive leg    → strike sits AT (or past) Kronos's dollar target.
                        Only wins if Kronos is roughly precise.
                        Lower win rate, higher payout.

    Volatility adjustment:
      vol_amplification_prob is Kronos's own estimate of how choppy the
      window will be. High vol → both legs widen (more room needed to
      survive noise, more room to justify reaching further). Low vol →
      both legs tighten (a calm, controlled move needs less cushion and
      doesn't justify reaching far on the aggressive leg).
      A neutral 0.5 leaves the base cushion/stretch unchanged.
    """
    move      = forecast_price - current_price
    move_pct  = abs(move) / current_price * 100
    direction = "up" if move > 0 else "down"

    if move_pct < MIN_MOVE_PCT:
        return None

    inc = strike_increment(current_price)

    # Vol multiplier: 0.5 (neutral) → 1.0x · 1.0 (max vol) → 1 + VOL_WIDEN_FACTOR
    # · 0.0 (min vol) → 1 - VOL_WIDEN_FACTOR, floored so it never goes negative.
    vol = 0.5 if vol_amplification_prob is None else vol_amplification_prob
    vol_mult = 1.0 + (vol - 0.5) * 2 * VOL_WIDEN_FACTOR
    vol_mult = max(0.3, vol_mult)

    cushion_frac = CONSERVATIVE_CUSHION * vol_mult
    # Aggressive leg's "stretch" scales the same way — in high vol we reach
    # further past the target; in low vol we sit closer to the target itself.
    stretch_frac = 1.0 + (vol_mult - 1.0) * 0.5

    if direction == "up":
        conservative_raw = current_price - abs(move) * cushion_frac
        aggressive_raw    = current_price + abs(move) * stretch_frac
        side              = "above"
    else:
        conservative_raw = current_price + abs(move) * cushion_frac
        aggressive_raw    = current_price - abs(move) * stretch_frac
        side              = "below"

    # Snap so the conservative leg always lands on the EASIER side:
    #   up bet   → round strike DOWN (lower bar to clear)
    #   down bet → round strike UP   (higher ceiling to stay under)
    if direction == "up":
        conservative = snap(conservative_raw, inc, "down")
        aggressive   = snap(aggressive_raw,   inc, "down")
    else:
        conservative = snap(conservative_raw, inc, "up")
        aggressive   = snap(aggressive_raw,   inc, "up")

    cushion_pct = abs(current_price - conservative) / current_price * 100
    stretch_pct = abs(aggressive - current_price) / current_price * 100

    vol_label = "high" if vol >= 0.65 else "low" if vol <= 0.35 else "moderate"

    return {
        "direction":        direction,
        "side":             side,
        "move_pct":         round(move_pct, 3),
        "conservative_strike": conservative,
        "aggressive_strike":   aggressive,
        "cushion_pct":      round(cushion_pct, 3),
        "stretch_pct":      round(stretch_pct, 3),
        "kronos_prob":      round(upside_prob * 100, 1),
        "horizon":          horizon,
        "vol_amplification_prob": round(vol * 100, 1),
        "vol_label":        vol_label,
        "vol_mult":         round(vol_mult, 3),
    }


def barbell_economics(cons_cost_cents, cons_stake, aggr_cost_cents, aggr_stake):
    """
    Given live Kalshi contract prices (in cents) and your stake in dollars,
    compute the payoff structure. Call this when you're actually looking at
    live odds — not baked into the email, since prices move constantly.

    Returns profit for each outcome and whether the conservative win alone
    covers the aggressive stake (the "free shot" test).
    """
    def contracts(stake, cost_cents):
        return stake / (cost_cents / 100.0) if cost_cents > 0 else 0

    c_contracts = contracts(cons_stake, cons_cost_cents)
    a_contracts = contracts(aggr_stake, aggr_cost_cents)

    c_payout = c_contracts * 1.0     # each contract pays $1
    a_payout = a_contracts * 1.0

    c_profit = c_payout - cons_stake
    a_profit = a_payout - aggr_stake
    total_stake = cons_stake + aggr_stake

    return {
        "total_staked":        round(total_stake, 2),
        "both_win":            round(c_profit + a_profit, 2),
        "only_conservative":   round(c_profit - aggr_stake, 2),
        "both_lose":           round(-total_stake, 2),
        "conservative_profit": round(c_profit, 2),
        "aggressive_profit":   round(a_profit, 2),
        # "Free shot": does the conservative win cover the aggressive stake?
        "free_shot":           c_profit >= aggr_stake,
        "implied_prob_cons":   cons_cost_cents,
        "implied_prob_aggr":   aggr_cost_cents,
    }


def edge_vs_market(kronos_prob_pct, kalshi_cost_cents):
    """
    Kalshi's contract price IS the market's probability estimate.
    The gap between Kronos and that price is your edge (in percentage points).
    Positive = Kronos more bullish than the market.
    """
    return round(kronos_prob_pct - kalshi_cost_cents, 1)


# ─── Kalshi live snapshot (public, no API key needed) ─────────────────────────

def fetch_kalshi_btc_markets(limit=50):
    """
    Pull the currently open KXBTCD (hourly BTC up/down) markets from Kalshi's
    public, unauthenticated market data endpoint.
    Returns a list of {ticker, strike, yes_bid, yes_ask, no_bid, no_ask, ...}
    or [] on failure — never raises, since this is a nice-to-have snapshot,
    not something that should break the scoring run if Kalshi is down.
    """
    url = f"{KALSHI_API_BASE}/markets"
    params = f"series_ticker={KALSHI_BTC_SERIES}&status=open&limit={limit}"
    try:
        with urllib.request.urlopen(f"{url}?{params}", timeout=10) as r:
            data = json.loads(r.read())
        markets = data.get("markets", [])
        out = []
        for m in markets:
            out.append({
                "ticker":  m.get("ticker"),
                "title":   m.get("title"),
                "yes_bid": m.get("yes_bid"),
                "yes_ask": m.get("yes_ask"),
                "no_bid":  m.get("no_bid"),
                "no_ask":  m.get("no_ask"),
                "close_time": m.get("close_time"),
            })
        return out
    except Exception as e:
        print(f"  Kalshi fetch failed (non-fatal): {e}")
        return []


def find_closest_kalshi_market(markets, target_strike):
    """From a list of Kalshi markets, find the one whose strike is closest
    to target_strike. Kalshi tickers embed the strike as e.g. '...-T63500'."""
    best, best_dist = None, None
    for m in markets:
        ticker = m.get("ticker", "")
        if "-T" not in ticker:
            continue
        try:
            strike = int(ticker.split("-T")[-1])
        except ValueError:
            continue
        dist = abs(strike - target_strike)
        if best_dist is None or dist < best_dist:
            best, best_dist = {**m, "strike": strike}, dist
    return best


def snapshot_kalshi_for_suggestion(suggestion):
    """
    Given one barbell suggestion (with conservative/aggressive strikes),
    pull the live Kalshi price for the closest real market to each strike
    and compute the edge vs Kronos. This is a point-in-time snapshot —
    call it right when Kronos makes its call, and log the result immediately.
    Returns the suggestion dict enriched with a "kalshi" key, or unchanged
    if Kalshi data isn't available.
    """
    markets = fetch_kalshi_btc_markets()
    if not markets:
        return suggestion

    kronos_prob = suggestion["kronos_prob"]
    snap_data = {}

    for leg_name, strike in [("conservative", suggestion["conservative_strike"]),
                              ("aggressive",   suggestion["aggressive_strike"])]:
        m = find_closest_kalshi_market(markets, strike)
        if not m:
            continue
        # yes_ask is what you'd actually pay to buy YES right now, in cents
        yes_price = m.get("yes_ask")
        if yes_price is None:
            continue
        snap_data[leg_name] = {
            "matched_ticker":  m["ticker"],
            "matched_strike":  m["strike"],
            "yes_price_cents": yes_price,
            "edge_pts":        edge_vs_market(kronos_prob, yes_price),
            "snapshot_time":   datetime.now(timezone.utc).isoformat(),
        }

    if snap_data:
        suggestion["kalshi"] = snap_data
    return suggestion


# ─── Scoring open barbell trades ──────────────────────────────────────────────

def score_open_trades(trades):
    now    = datetime.now(timezone.utc)
    closed = 0

    for t in trades:
        if t.get("status") != "open":
            continue

        horizon = t.get("horizon", "24h")
        hours   = HORIZON_HOURS.get(horizon, 24)

        try:
            entry_dt = datetime.strptime(t["kronos_timestamp"], "%Y-%m-%d %H:%M:%S")
            entry_dt = entry_dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        if (now - entry_dt).total_seconds()/3600 < hours:
            continue

        settle_price = get_price_at(entry_dt + timedelta(hours=hours))
        if settle_price is None:
            print(f"  {t['id']}: could not fetch settle price — leaving open")
            continue

        direction = t.get("direction", "up")

        def leg_won(strike):
            return settle_price > strike if direction == "up" else settle_price < strike

        cons = t.get("conservative", {})
        aggr = t.get("aggressive", {})
        cons_won = leg_won(cons.get("strike", 0)) if cons else None
        aggr_won = leg_won(aggr.get("strike", 0)) if aggr else None

        # P&L per leg using the stored contract cost
        def leg_pnl(leg, won):
            if not leg or won is None:
                return 0.0
            stake = leg.get("stake_usd", 0)
            cost  = leg.get("cost_cents", 50) / 100.0
            if cost <= 0:
                return 0.0
            n = stake / cost
            return round((n * 1.0 - stake) if won else -stake, 2)

        cons_pnl = leg_pnl(cons, cons_won)
        aggr_pnl = leg_pnl(aggr, aggr_won)

        t.update({
            "status":            "closed",
            "settle_price":      round(settle_price, 2),
            "conservative_won":  cons_won,
            "aggressive_won":    aggr_won,
            "conservative_pnl":  cons_pnl,
            "aggressive_pnl":    aggr_pnl,
            "total_pnl":         round(cons_pnl + aggr_pnl, 2),
            "close_timestamp":   now.isoformat(),
        })

        c_icon = "✅" if cons_won else "❌"
        a_icon = "✅" if aggr_won else "❌"
        print(f"  {t['id']} [{horizon}] settled ${settle_price:,.0f} | "
              f"cons {c_icon} {cons_pnl:+.2f} | aggr {a_icon} {aggr_pnl:+.2f} | "
              f"net {t['total_pnl']:+.2f}")
        closed += 1

    return closed


# ─── Suggestions from latest predictions ──────────────────────────────────────

def build_suggestions(with_kalshi_snapshot=True):
    """Pull the newest prediction per horizon, compute vol-adjusted barbell
    strikes, and (optionally) snapshot live Kalshi prices for those strikes."""
    pending = load_json(PENDING_FILE, [])
    if not pending:
        return {}

    newest = {}
    for p in pending:
        h = p.get("horizon", "24h")
        if h not in newest or p.get("prediction_timestamp","") > newest[h].get("prediction_timestamp",""):
            newest[h] = p

    suggestions = {}
    for horizon, p in newest.items():
        cur   = p.get("current_price")
        tgt   = p.get("mean_forecast_close")
        up    = p.get("upside_prob")
        vol_p = p.get("vol_amplification_prob")
        if not (cur and tgt and up is not None):
            continue
        s = suggest_barbell(cur, tgt, up, horizon, vol_amplification_prob=vol_p)
        if s:
            s["entry_price"]     = cur
            s["forecast_price"]  = tgt
            s["kronos_timestamp"] = p.get("prediction_timestamp")
            s["vol_prob"]        = round((vol_p or 0)*100, 1)
            s["forecast_low"]    = p.get("forecast_low")
            s["forecast_high"]   = p.get("forecast_high")

            if with_kalshi_snapshot:
                s = snapshot_kalshi_for_suggestion(s)

            suggestions[horizon] = s
    return suggestions


def log_kalshi_snapshots(suggestions):
    """
    Append this run's Kalshi vs Kronos snapshots to a permanent history file.
    Each snapshot goes stale within minutes as a live number, but stacked
    over weeks this becomes the dataset that answers: when Kronos and
    Kalshi disagree by X points, which one tends to be right more often.
    """
    snap_file = REPO_ROOT / "kalshi_snapshots.json"
    history = load_json(snap_file, [])
    now = datetime.now(timezone.utc).isoformat()

    for horizon, s in suggestions.items():
        kalshi = s.get("kalshi")
        if not kalshi:
            continue
        for leg_name, leg in kalshi.items():
            history.append({
                "logged_at":        now,
                "horizon":          horizon,
                "leg":              leg_name,
                "kronos_timestamp": s.get("kronos_timestamp"),
                "kronos_prob":      s["kronos_prob"],
                "strike":           s["conservative_strike"] if leg_name=="conservative" else s["aggressive_strike"],
                "matched_ticker":   leg["matched_ticker"],
                "matched_strike":   leg["matched_strike"],
                "kalshi_price_cents": leg["yes_price_cents"],
                "edge_pts":         leg["edge_pts"],
                "entry_btc_price":  s["entry_price"],
            })

    if history:
        save_json(snap_file, history)
        print(f"  Logged {sum(len(s.get('kalshi',{})) for s in suggestions.values())} Kalshi snapshot(s) "
              f"→ kalshi_snapshots.json ({len(history)} total)")


# ─── Portfolio ────────────────────────────────────────────────────────────────

def portfolio_summary(trades):
    closed = [t for t in trades if t.get("status") == "closed"]
    open_t = [t for t in trades if t.get("status") == "open"]

    total_pnl   = sum(t.get("total_pnl", 0) or 0 for t in closed)
    cons_wins   = sum(1 for t in closed if t.get("conservative_won"))
    aggr_wins   = sum(1 for t in closed if t.get("aggressive_won"))
    n           = len(closed)
    open_stake  = sum(
        (t.get("conservative",{}).get("stake_usd",0) or 0) +
        (t.get("aggressive",{}).get("stake_usd",0) or 0)
        for t in open_t
    )

    return {
        "closed": n,
        "open": len(open_t),
        "open_stake": round(open_stake, 2),
        "total_pnl": round(total_pnl, 2),
        "conservative_win_pct": round(cons_wins/n*100) if n else None,
        "aggressive_win_pct":   round(aggr_wins/n*100) if n else None,
    }


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("\n=== Kalshi Barbell Tracker ===")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n")

    trades = load_json(TRADES_FILE, [])
    print(f"  Loaded {len(trades)} barbell trades")

    print("\n[1] Scoring matured trades...")
    closed = score_open_trades(trades)
    print(f"  Closed {closed}")

    print("\n[2] Market context...")
    fg_val, fg_label = fetch_fear_greed()
    print(f"  Fear & Greed: {fg_val} ({fg_label})" if fg_val else "  F&G unavailable")

    print("\n[3] Barbell suggestions from latest Kronos calls (vol-adjusted)...")
    suggestions = build_suggestions(with_kalshi_snapshot=True)
    if not suggestions:
        print("  No qualifying signals (move too small or no predictions yet).")
    for horizon, s in suggestions.items():
        print(f"\n  ── {horizon.upper()} ── vol: {s['vol_label']} ({s['vol_prob']}%, x{s['vol_mult']}) ──")
        print(f"     Kronos: {s['kronos_prob']}% upside | target ${s['forecast_price']:,.0f} "
              f"({'+' if s['direction']=='up' else ''}{s['move_pct']:.2f}% {s['direction']})")
        print(f"     Entry price: ${s['entry_price']:,.0f}")
        print(f"     CONSERVATIVE → {s['side']} ${s['conservative_strike']:,} "
              f"({s['cushion_pct']:.2f}% cushion)")
        print(f"     AGGRESSIVE   → {s['side']} ${s['aggressive_strike']:,} "
              f"({s['stretch_pct']:.2f}% stretch)")
        if s.get("forecast_low") and s.get("forecast_high"):
            print(f"     MC range: ${s['forecast_low']:,.0f} – ${s['forecast_high']:,.0f}")
        if s.get("kalshi"):
            for leg, k in s["kalshi"].items():
                sign = "+" if k["edge_pts"] >= 0 else ""
                print(f"     Kalshi [{leg}] {k['matched_ticker']} @ {k['yes_price_cents']}¢ "
                      f"→ edge {sign}{k['edge_pts']}pts")

    print("\n[4] Logging Kalshi snapshots to history...")
    log_kalshi_snapshots(suggestions)

    print("\n[5] Saving...")
    save_json(TRADES_FILE, trades)

    p = portfolio_summary(trades)
    print(f"""
=== Barbell Summary ===
  Closed trades      : {p['closed']}
  Open trades        : {p['open']} (${p['open_stake']:.2f} staked)
  Realized P&L       : ${p['total_pnl']:+.2f}
  Conservative win % : {p['conservative_win_pct'] if p['conservative_win_pct'] is not None else '—'}
  Aggressive win %   : {p['aggressive_win_pct'] if p['aggressive_win_pct'] is not None else '—'}
  Fear & Greed       : {fg_val} ({fg_label})
""")


if __name__ == "__main__":
    main()
