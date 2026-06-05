"""
Kronos Auto Paper Trader — v2
==============================
Key changes from v1:
  - Vol amplification NO LONGER blocks entry — only affects take profit width
  - Bear market mode: F&G < 35 + BTC falling = ride the downtrend directly
  - Bull market mode: F&G < 40 + Kronos bullish = bounce plays
  - Conviction score simplified — direction confidence + regime only
  - Paper trading section always writes to paper_trades.json even if empty
  - Take profit scales with vol: low vol = 1.0%, high vol = 2.0%

Entry rules:
  BEAR MODE  (F&G < 35, trend = down):
    - Kronos upside_prob <= 0.35 (bearish signal)
    - Conviction >= 5
    - Ride the trend, no overextension required

  BULL REVERSAL MODE (F&G < 40, trend = down):
    - Kronos upside_prob >= 0.80 (strong bullish)
    - Conviction >= 6
    - Bounce play against oversold trend

  BULL TREND MODE (F&G > 60, trend = up):
    - Kronos upside_prob >= 0.80
    - Conviction >= 6
    - Ride the uptrend

Position sizing (conviction-based):
  Score 5–6  → $50
  Score 7    → $75
  Score 8    → $100
  Score 9–10 → $150

Take profit (vol-adjusted):
  Vol amp < 30%  → TP = 1.0%
  Vol amp 30–60% → TP = 1.5%
  Vol amp > 60%  → TP = 2.0%

Stop loss: fixed 1.0%
Fees: 0.1% per side (0.2% round trip)
Max simultaneous exposure: $1,000
"""

import json
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT   = Path(__file__).parent.parent
SCORES_FILE = REPO_ROOT / "scores.json"
PENDING_FILE= REPO_ROOT / "pending.json"
TRADES_FILE = REPO_ROOT / "paper_trades.json"

STARTING_BALANCE = 1000.0
FEE_RATE         = 0.001   # 0.1% per side
STOP_LOSS_PCT    = 1.0
MAX_EXPOSURE     = 1000.0
BINANCE_URL      = "https://api.binance.us/api/v3/klines"
FG_URL           = "https://api.alternative.me/fng/?limit=1"


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
    except Exception as e:
        print(f"  F&G fetch failed: {e}"); return None, None

def fetch_closes(limit=170, start_ms=None):
    try:
        url = f"{BINANCE_URL}?symbol=BTCUSDT&interval=1h&limit={limit}"
        if start_ms: url += f"&startTime={start_ms}"
        with urllib.request.urlopen(url, timeout=10) as r:
            return [float(c[4]) for c in json.loads(r.read())]
    except Exception as e:
        print(f"  Binance fetch failed: {e}"); return []

def get_btc_price_now():
    c = fetch_closes(limit=2)
    return c[-1] if c else None

def get_btc_7day_trend():
    c = fetch_closes(limit=170)
    if len(c) < 10: return "unknown"
    chg = (c[-1] - c[0]) / c[0] * 100
    return "up" if chg > 2 else "down" if chg < -2 else "sideways"

def get_24h_range(kronos_ts):
    """Fetch 24h low, high, close from a prediction timestamp string."""
    try:
        dt = datetime.strptime(kronos_ts[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc, minute=0, second=0)
        ms = int(dt.timestamp() * 1000)
        url = f"{BINANCE_URL}?symbol=BTCUSDT&interval=1h&limit=25&startTime={ms}"
        with urllib.request.urlopen(url, timeout=10) as r:
            raw = json.loads(r.read())[:24]
        return (
            min(float(c[3]) for c in raw),
            max(float(c[2]) for c in raw),
            float(raw[-1][4])
        )
    except Exception as e:
        print(f"  24h range failed for {kronos_ts}: {e}"); return None, None, None


# ─── Signal logic ─────────────────────────────────────────────────────────────

def get_signal(prob):
    if prob <= 0.35: return "bearish"
    if prob >= 0.80: return "bullish"
    return "neutral"

def calc_conviction(prob, trend_aligned):
    """
    Simplified conviction — direction strength + regime alignment.
    Vol amplification NO LONGER affects entry conviction.
    """
    s = 0
    d = abs(prob - 0.5)
    if d >= 0.4:   s += 4   # 90%+ or 10%-
    elif d >= 0.3: s += 3   # 80%+ or 20%-
    elif d >= 0.2: s += 2   # 70%+ or 30%-
    elif d >= 0.15:s += 1   # 65%+ or 35%-
    if trend_aligned: s += 2
    if prob <= 0.10 or prob >= 0.90: s += 1  # extreme bonus
    return max(1, min(10, s))

def get_take_profit(vol_prob):
    """Vol amplification determines how wide to set the take profit."""
    if vol_prob is None: return 1.5
    if vol_prob >= 0.60: return 2.0   # high vol → bigger expected move
    if vol_prob >= 0.30: return 1.5   # moderate
    return 1.0                         # low vol → small calm move, take it quick

def get_position_size(score):
    if score >= 9:  return 150.0
    if score >= 8:  return 100.0
    if score >= 7:  return 75.0
    return 50.0  # score 5–6

def determine_market_mode(fg_val, trend):
    """
    Returns the trading mode based on market conditions.

    BEAR_TREND    — ride the downtrend (F&G < 35, trend down)
    BULL_REVERSAL — bounce play (F&G < 40, trend down, strong Kronos bullish)
    BULL_TREND    — ride the uptrend (F&G > 60, trend up)
    NO_TRADE      — sideways or conflicting signals
    """
    if fg_val is None or trend == "unknown":
        return "NO_TRADE", "Market data unavailable"

    if fg_val <= 35 and trend == "down":
        return "BEAR_TREND", f"Bear mode: F&G={fg_val} (fear) + BTC falling — riding downtrend"

    if fg_val <= 40 and trend == "down":
        return "BULL_REVERSAL", f"Bull reversal: F&G={fg_val} (fear) + oversold — watching for bounce"

    if fg_val >= 60 and trend == "up":
        return "BULL_TREND", f"Bull trend: F&G={fg_val} (greed) + BTC rising — riding uptrend"

    if trend == "sideways":
        return "NO_TRADE", f"Sideways market (F&G={fg_val}) — no clear edge, sitting out"

    return "NO_TRADE", f"Conflicting signals: F&G={fg_val}, trend={trend} — no trade"

def evaluate_signal(prob, vol_prob, mode):
    """
    Returns (should_trade, signal, conviction, take_profit_pct, reason)
    """
    signal = get_signal(prob)
    vol = vol_prob if vol_prob is not None else 0.5
    tp = get_take_profit(vol_prob)

    if mode == "NO_TRADE":
        return False, signal, 0, tp, "No trade mode active"

    if mode == "BEAR_TREND":
        if signal != "bearish":
            return False, signal, 0, tp, f"Bear mode needs bearish signal, got {signal} ({prob*100:.0f}%)"
        aligned = True
        conv = calc_conviction(prob, aligned)
        if conv < 5:
            return False, signal, conv, tp, f"Conviction {conv}/10 too low for bear mode (need 5+)"
        return True, signal, conv, tp, f"Bear trend entry: {prob*100:.0f}% upside prob, conv={conv}/10, TP={tp}%"

    if mode == "BULL_REVERSAL":
        if signal != "bullish":
            return False, signal, 0, tp, f"Bull reversal needs bullish signal, got {signal} ({prob*100:.0f}%)"
        aligned = False  # going against the downtrend
        conv = calc_conviction(prob, aligned)
        if conv < 6:
            return False, signal, conv, tp, f"Conviction {conv}/10 too low for reversal (need 6+)"
        return True, signal, conv, tp, f"Bull reversal entry: {prob*100:.0f}% upside prob, conv={conv}/10, TP={tp}%"

    if mode == "BULL_TREND":
        if signal != "bullish":
            return False, signal, 0, tp, f"Bull mode needs bullish signal, got {signal} ({prob*100:.0f}%)"
        aligned = True
        conv = calc_conviction(prob, aligned)
        if conv < 6:
            return False, signal, conv, tp, f"Conviction {conv}/10 too low for bull mode (need 6+)"
        return True, signal, conv, tp, f"Bull trend entry: {prob*100:.0f}% upside prob, conv={conv}/10, TP={tp}%"

    return False, signal, 0, tp, "Unknown mode"


# ─── Trade simulation ─────────────────────────────────────────────────────────

def simulate_outcome(signal, entry, low24, high24, close24, size, tp_pct):
    sl_price = entry*(1+STOP_LOSS_PCT/100) if signal=="bearish" else entry*(1-STOP_LOSS_PCT/100)
    tp_price = entry*(1-tp_pct/100)        if signal=="bearish" else entry*(1+tp_pct/100)

    sl_hit = high24 >= sl_price if signal=="bearish" else low24 <= sl_price
    tp_hit = low24  <= tp_price if signal=="bearish" else high24 >= tp_price

    if sl_hit and tp_hit:
        outcome, exit_p = "stop-loss", sl_price
    elif sl_hit:
        outcome, exit_p = "stop-loss", sl_price
    elif tp_hit:
        outcome, exit_p = "take-profit", tp_price
    else:
        exit_p  = close24
        chg = (close24-entry)/entry*100
        raw = (-chg if signal=="bearish" else chg)/100*size
        outcome = "win" if raw >= 0 else "loss"

    if outcome == "stop-loss":
        raw_pnl = -(size*STOP_LOSS_PCT/100)
    elif outcome == "take-profit":
        raw_pnl = size*tp_pct/100
    else:
        chg = (exit_p-entry)/entry*100
        raw_pnl = (-chg if signal=="bearish" else chg)/100*size

    fees    = round(size*FEE_RATE*2, 4)
    net_pnl = round(raw_pnl - fees, 4)
    return net_pnl, outcome, round(exit_p, 2), fees


# ─── Portfolio helpers ────────────────────────────────────────────────────────

def calc_portfolio(trades):
    balance  = STARTING_BALANCE
    exposure = 0.0
    for t in trades:
        if t.get("status") == "closed":
            balance += t.get("net_pnl", 0) or 0
        elif t.get("status") == "open":
            exposure += t.get("size", 0) or 0
    return round(balance, 2), round(exposure, 2)

def already_entered(trades, kronos_ts):
    return any(t.get("kronos_timestamp") == kronos_ts for t in trades)


# ─── Score open trades ────────────────────────────────────────────────────────

def score_open_trades(trades):
    now = datetime.now(timezone.utc)
    closed = 0
    for t in trades:
        if t.get("status") != "open": continue
        try:
            entry_dt = datetime.fromisoformat(t["entry_timestamp"])
            if entry_dt.tzinfo is None: entry_dt = entry_dt.replace(tzinfo=timezone.utc)
        except: continue

        age_h = (now - entry_dt).total_seconds()/3600
        if age_h < 24:
            print(f"  Trade {t['id']} still open ({age_h:.1f}h) — waiting")
            continue

        print(f"  Scoring {t['id']} ({t['signal']} @ ${t['entry_price']:,.0f})...")
        low24, high24, close24 = get_24h_range(t["kronos_timestamp"])
        if low24 is None:
            print(f"    Price data unavailable — leaving open"); continue

        net_pnl, outcome, exit_p, fees = simulate_outcome(
            t["signal"], t["entry_price"],
            low24, high24, close24,
            t["size"], t.get("take_profit_pct", 1.5)
        )

        t.update({
            "status":          "closed",
            "outcome":         outcome,
            "exit_price":      exit_p,
            "low_24h":         round(low24,2),
            "high_24h":        round(high24,2),
            "close_24h":       round(close24,2),
            "fees":            fees,
            "net_pnl":         net_pnl,
            "close_timestamp": now.isoformat(),
        })

        icon = "✅" if outcome in("win","take-profit") else "⛔" if outcome=="stop-loss" else "❌"
        print(f"    {icon} {outcome.upper()} | net P&L: {'+' if net_pnl>=0 else ''}${net_pnl:.4f} | fees: ${fees:.4f}")
        closed += 1
    return closed


# ─── Open new trades ─────────────────────────────────────────────────────────

def open_new_trades(trades, mode, mode_reason, fg_val, trend, btc_price):
    pending = load_json(PENDING_FILE, [])
    if not pending:
        print("  No pending predictions found."); return 0

    now = datetime.now(timezone.utc)
    # Only evaluate predictions scraped in the last 2 hours
    recent = []
    for p in pending:
        try:
            ts = datetime.fromisoformat(p.get("scrape_timestamp",""))
            if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
            if (now-ts).total_seconds() < 7200: recent.append(p)
        except: pass

    if not recent:
        print("  No recent (<2h) predictions to evaluate."); return 0

    balance, exposure = calc_portfolio(trades)
    opened = 0

    for p in recent:
        prob    = p.get("upside_prob", 0.5)
        vol     = p.get("vol_amplification_prob")
        kronos_ts = p.get("prediction_timestamp","")

        if already_entered(trades, kronos_ts):
            continue

        should_trade, signal, conv, tp_pct, reason = evaluate_signal(prob, vol, mode)
        print(f"  {kronos_ts} | {signal} {prob*100:.0f}% | conv={conv} | {reason}")

        if not should_trade: continue

        size = get_position_size(conv)
        if exposure + size > MAX_EXPOSURE:
            print(f"  Exposure limit: ${exposure:.0f} + ${size} > ${MAX_EXPOSURE} — skip")
            continue

        if btc_price is None:
            print("  No BTC price — cannot enter"); continue

        entry_fee = round(size*FEE_RATE, 4)
        sl_price  = round(btc_price*(1+STOP_LOSS_PCT/100) if signal=="bearish" else btc_price*(1-STOP_LOSS_PCT/100), 2)
        tp_price  = round(btc_price*(1-tp_pct/100)        if signal=="bearish" else btc_price*(1+tp_pct/100), 2)

        trade_id = f"PT-{len(trades)+1:04d}"
        trade = {
            "id":               trade_id,
            "status":           "open",
            "market_mode":      mode,
            "signal":           signal,
            "prob":             round(prob*100,1),
            "vol_prob":         round(vol*100,1) if vol else None,
            "conviction_score": conv,
            "size":             size,
            "entry_price":      round(btc_price,2),
            "entry_fee":        entry_fee,
            "sl_price":         sl_price,
            "tp_price":         tp_price,
            "stop_loss_pct":    STOP_LOSS_PCT,
            "take_profit_pct":  tp_pct,
            "fear_greed":       fg_val,
            "btc_trend_7d":     trend,
            "mode_reason":      mode_reason,
            "trade_reason":     reason,
            "kronos_timestamp": kronos_ts,
            "entry_timestamp":  now.isoformat(),
            "outcome":          None,
            "exit_price":       None,
            "net_pnl":          None,
            "fees":             None,
            "low_24h":          None,
            "high_24h":         None,
            "close_24h":        None,
        }

        trades.append(trade)
        exposure += size
        opened   += 1
        print(f"  ✅ OPENED {trade_id} | {signal.upper()} | ${size} | Entry ${btc_price:,.2f} | SL ${sl_price:,.2f} | TP ${tp_price:,.2f} | TP%={tp_pct}%")

    return opened


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("\n=== Kronos Auto Paper Trader v2 ===")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n")

    trades = load_json(TRADES_FILE, [])
    print(f"  Loaded {len(trades)} trades")

    # 1 — Score open trades
    print("\n[1] Scoring open trades...")
    closed = score_open_trades(trades)
    print(f"  Closed {closed} trade(s)")

    # 2 — Market data
    print("\n[2] Fetching market data...")
    fg_val, fg_label = fetch_fear_greed()
    print(f"  F&G: {fg_val} ({fg_label})" if fg_val else "  F&G: unavailable")
    trend = get_btc_7day_trend()
    print(f"  BTC 7d trend: {trend}")
    btc_price = get_btc_price_now()
    print(f"  BTC price: ${btc_price:,.2f}" if btc_price else "  BTC price: unavailable")

    # 3 — Determine market mode
    mode, mode_reason = determine_market_mode(fg_val, trend)
    print(f"\n[3] Market mode: {mode}")
    print(f"  {mode_reason}")

    # 4 — Open new trades
    print("\n[4] Evaluating signals...")
    opened = open_new_trades(trades, mode, mode_reason, fg_val, trend, btc_price)
    print(f"  Opened {opened} new trade(s)")

    # 5 — Save (always save even if no changes — ensures file exists)
    print("\n[5] Saving...")
    save_json(TRADES_FILE, trades)

    # 6 — Summary
    balance, exposure = calc_portfolio(trades)
    closed_trades = [t for t in trades if t.get("status")=="closed"]
    open_trades   = [t for t in trades if t.get("status")=="open"]
    wins = sum(1 for t in closed_trades if t.get("outcome") in("win","take-profit"))
    total_c = len(closed_trades)
    wr = round(wins/total_c*100) if total_c else 0
    total_fees = sum(t.get("fees",0) or 0 for t in closed_trades)

    print(f"""
=== Portfolio Summary ===
  Mode              : {mode}
  Balance           : ${balance:,.2f}
  Open exposure     : ${exposure:,.2f}
  Open trades       : {len(open_trades)}
  Closed trades     : {total_c}
  Win rate          : {wr}%
  Total fees paid   : ${total_fees:.4f}
  F&G               : {fg_val} ({fg_label})
  BTC trend         : {trend}
""")


if __name__ == "__main__":
    main()
