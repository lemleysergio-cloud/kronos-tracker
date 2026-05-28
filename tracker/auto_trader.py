"""
Kronos Auto Paper Trader
========================
Runs daily via GitHub Actions alongside kronos_tracker.py.

Three-filter entry logic:
  1. Kronos signal: upside_prob <= 0.30 (bearish) or >= 0.80 (bullish), conviction >= 7
  2. Fear & Greed confirmation:
       bearish signal → F&G >= 60 (crowd greedy, drop likely)
       bullish signal → F&G <= 40 (crowd fearful, bounce likely)
  3. 7-day BTC trend from Binance:
       bearish signal → BTC up over last 7 days (overextended, due pullback)
       bullish signal → BTC down over last 7 days (oversold, due bounce)

Position sizing (conviction-based):
  Score 7   → $50
  Score 8   → $100
  Score 9   → $125
  Score 10  → $150

Fees: 0.1% per side (0.2% round trip) — Binance standard maker fee.
Stop loss: 1.0% | Take profit: 1.5%
Max simultaneous exposure: $1,000 total open positions.

Files managed:
  paper_trades.json  — completed + open trades log
  pending.json       — read-only (Kronos predictions)
  scores.json        — read-only (scored Kronos predictions)
"""

import json
import math
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT      = Path(__file__).parent.parent
SCORES_FILE    = REPO_ROOT / "scores.json"
PENDING_FILE   = REPO_ROOT / "pending.json"
TRADES_FILE    = REPO_ROOT / "paper_trades.json"

STARTING_BALANCE = 1000.0
FEE_RATE         = 0.001   # 0.1% per side
STOP_LOSS_PCT    = 1.0     # %
TAKE_PROFIT_PCT  = 1.5     # %
MAX_EXPOSURE     = 1000.0  # never exceed this in open positions

BINANCE_URL = "https://api.binance.us/api/v3/klines"
FG_URL      = "https://api.alternative.me/fng/?limit=1"


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_json(path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  Saved {path.name}")


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

def fetch_fear_greed():
    """Return (value:int, label:str) or (None, None)."""
    try:
        with urllib.request.urlopen(FG_URL, timeout=8) as r:
            data = json.loads(r.read())
        v = int(data["data"][0]["value"])
        return v, data["data"][0]["value_classification"]
    except Exception as e:
        print(f"  Fear & Greed fetch failed: {e}")
        return None, None


def fetch_klines(symbol="BTCUSDT", interval="1h", limit=25, start_ms=None):
    """Fetch Binance klines. Returns list of close prices."""
    params = f"symbol={symbol}&interval={interval}&limit={limit}"
    if start_ms:
        params += f"&startTime={start_ms}"
    try:
        with urllib.request.urlopen(f"{BINANCE_URL}?{params}", timeout=10) as r:
            raw = json.loads(r.read())
        return [float(c[4]) for c in raw]  # close prices
    except Exception as e:
        print(f"  Binance fetch failed: {e}")
        return []


def get_btc_price_now():
    """Current BTC price — last closed 1h candle."""
    closes = fetch_klines(limit=2)
    return closes[-1] if closes else None


def get_btc_7day_trend():
    """
    Returns 'up', 'down', or 'sideways'.
    Compares price 7 days ago to current price.
    """
    closes = fetch_klines(limit=170)  # ~7 days of 1h candles
    if len(closes) < 10:
        return "unknown"
    price_7d_ago = closes[0]
    price_now    = closes[-1]
    change_pct   = (price_now - price_7d_ago) / price_7d_ago * 100
    if change_pct > 2.0:
        return "up"
    if change_pct < -2.0:
        return "down"
    return "sideways"


def get_btc_price_at(timestamp_str):
    """
    Fetch BTC close price at a specific UTC timestamp string
    e.g. '2026-05-27 14:00:25'
    """
    try:
        dt = datetime.strptime(timestamp_str[:19], "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=timezone.utc, minute=0, second=0)
        ms = int(dt.timestamp() * 1000)
        closes = fetch_klines(limit=2, start_ms=ms)
        return closes[0] if closes else None
    except Exception as e:
        print(f"  Price fetch failed for {timestamp_str}: {e}")
        return None


def get_24h_range(timestamp_str):
    """
    Fetch the 24h high, low, and close starting from timestamp.
    Returns (low, high, close) or (None, None, None).
    """
    try:
        dt = datetime.strptime(timestamp_str[:19], "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=timezone.utc, minute=0, second=0)
        ms = int(dt.timestamp() * 1000)
        with urllib.request.urlopen(
            f"{BINANCE_URL}?symbol=BTCUSDT&interval=1h&limit=25&startTime={ms}",
            timeout=10
        ) as r:
            raw = json.loads(r.read())
        candles = raw[:24]
        lows    = [float(c[3]) for c in candles]
        highs   = [float(c[2]) for c in candles]
        closes  = [float(c[4]) for c in candles]
        return min(lows), max(highs), closes[-1]
    except Exception as e:
        print(f"  24h range fetch failed: {e}")
        return None, None, None


# ---------------------------------------------------------------------------
# Signal evaluation
# ---------------------------------------------------------------------------

def get_signal(prob):
    if prob <= 0.30: return "bearish"
    if prob >= 0.80: return "bullish"
    return "neutral"


def calc_conviction(prob, vol):
    s = 0
    d = abs(prob - 0.5)
    if d >= 0.4:   s += 3
    elif d >= 0.3: s += 2
    elif d >= 0.2: s += 1
    if vol is not None:
        if prob <= 0.30:   # bearish
            s += 2 if vol >= 0.7 else 1 if vol >= 0.5 else -1
        else:              # bullish
            s += 2 if vol >= 0.7 else 1 if vol >= 0.5 else -1
    if prob <= 0.10 or prob >= 0.90:
        s += 1
    return max(1, min(10, s))


def get_position_size(score):
    if score >= 10: return 150.0
    if score >= 9:  return 125.0
    if score >= 8:  return 100.0
    return 50.0   # score 7


def check_three_filters(signal, fg_val, trend):
    """
    Returns (passed:bool, reason:str)
    """
    reasons = []
    passed  = True

    # Filter 1 — handled before calling this (signal already confirmed)
    reasons.append(f"Kronos: {signal}")

    # Filter 2 — Fear & Greed
    if fg_val is None:
        reasons.append("F&G: unavailable — skipping trade")
        return False, " · ".join(reasons)
    if signal == "bearish":
        if fg_val >= 60:
            reasons.append(f"F&G: {fg_val} (greed ✓)")
        else:
            reasons.append(f"F&G: {fg_val} — need ≥60 for bearish entry")
            passed = False
    else:  # bullish
        if fg_val <= 40:
            reasons.append(f"F&G: {fg_val} (fear ✓)")
        else:
            reasons.append(f"F&G: {fg_val} — need ≤40 for bullish entry")
            passed = False

    # Filter 3 — 7-day trend
    if trend == "unknown":
        reasons.append("Trend: unknown — skipping trade")
        return False, " · ".join(reasons)
    if signal == "bearish":
        if trend == "up":
            reasons.append("Trend: up 7d — overextended, bearish aligns ✓")
        elif trend == "sideways":
            reasons.append("Trend: sideways — weaker bearish setup, skip")
            passed = False
        else:
            reasons.append("Trend: already down — counter-trend bearish, skip")
            passed = False
    else:  # bullish
        if trend == "down":
            reasons.append("Trend: down 7d — oversold, bullish aligns ✓")
        elif trend == "sideways":
            reasons.append("Trend: sideways — weaker bullish setup, skip")
            passed = False
        else:
            reasons.append("Trend: already up — counter-trend bullish, skip")
            passed = False

    return passed, " · ".join(reasons)


# ---------------------------------------------------------------------------
# Trade simulation (intraday SL/TP)
# ---------------------------------------------------------------------------

def simulate_outcome(signal, entry, low24, high24, close24, size):
    """
    Returns (pnl_after_fees, outcome_str, exit_price)
    """
    sl_price = entry * (1 + STOP_LOSS_PCT/100)    if signal == "bearish" else entry * (1 - STOP_LOSS_PCT/100)
    tp_price = entry * (1 - TAKE_PROFIT_PCT/100)  if signal == "bearish" else entry * (1 + TAKE_PROFIT_PCT/100)

    sl_hit = high24 >= sl_price if signal == "bearish" else low24 <= sl_price
    tp_hit = low24  <= tp_price if signal == "bearish" else high24 >= tp_price

    if sl_hit and tp_hit:
        outcome    = "stop-loss"
        exit_price = sl_price
    elif sl_hit:
        outcome    = "stop-loss"
        exit_price = sl_price
    elif tp_hit:
        outcome    = "take-profit"
        exit_price = tp_price
    else:
        exit_price = close24
        chg = (close24 - entry) / entry * 100
        raw_pnl = (-chg if signal == "bearish" else chg) / 100 * size
        outcome = "win" if raw_pnl >= 0 else "loss"

    # Raw P&L
    if outcome == "stop-loss":
        raw_pnl = -(size * STOP_LOSS_PCT / 100)
    elif outcome == "take-profit":
        raw_pnl = size * TAKE_PROFIT_PCT / 100
    # else already set above

    # Fees: 0.1% entry + 0.1% exit, applied to position size
    fees = size * FEE_RATE * 2
    net_pnl = round(raw_pnl - fees, 4)

    return net_pnl, outcome, round(exit_price, 2)


# ---------------------------------------------------------------------------
# Portfolio state
# ---------------------------------------------------------------------------

def calc_portfolio(trades):
    """Returns (balance, open_exposure)."""
    balance  = STARTING_BALANCE
    exposure = 0.0
    for t in trades:
        if t.get("status") == "open":
            exposure += t.get("size", 0)
        elif t.get("status") == "closed":
            balance += t.get("net_pnl", 0)
    return round(balance, 2), round(exposure, 2)


def already_have_signal(trades, prediction_timestamp):
    """Avoid double-entering the same Kronos hourly signal."""
    for t in trades:
        if t.get("kronos_timestamp") == prediction_timestamp:
            return True
    return False


# ---------------------------------------------------------------------------
# Core: score open trades
# ---------------------------------------------------------------------------

def score_open_trades(trades):
    """
    For any open trade that is 24h+ old, fetch real Binance data and close it.
    Modifies trades list in place. Returns count of trades closed.
    """
    now = datetime.now(timezone.utc)
    closed = 0

    for t in trades:
        if t.get("status") != "open":
            continue

        entry_dt = datetime.fromisoformat(t["entry_timestamp"])
        if entry_dt.tzinfo is None:
            entry_dt = entry_dt.replace(tzinfo=timezone.utc)

        age_hours = (now - entry_dt).total_seconds() / 3600
        if age_hours < 24:
            print(f"  Trade {t['id']} still open ({age_hours:.1f}h old) — skipping")
            continue

        print(f"  Scoring trade {t['id']} ({t['signal']} @ ${t['entry_price']:,.0f})...")
        low24, high24, close24 = get_24h_range(t["kronos_timestamp"])

        if low24 is None:
            print(f"    Could not fetch price data — leaving open")
            continue

        net_pnl, outcome, exit_price = simulate_outcome(
            t["signal"], t["entry_price"], low24, high24, close24, t["size"]
        )

        t["status"]          = "closed"
        t["outcome"]         = outcome
        t["exit_price"]      = exit_price
        t["low_24h"]         = round(low24, 2)
        t["high_24h"]        = round(high24, 2)
        t["close_24h"]       = round(close24, 2)
        t["raw_pnl"]         = round(net_pnl + t["size"] * FEE_RATE * 2, 4)
        t["fees"]            = round(t["size"] * FEE_RATE * 2, 4)
        t["net_pnl"]         = net_pnl
        t["close_timestamp"] = now.isoformat()

        icon = "✅" if outcome in ("win","take-profit") else "⛔" if outcome == "stop-loss" else "❌"
        print(f"    {icon} {outcome.upper()} · net P&L: {'+' if net_pnl>=0 else ''}${net_pnl:.4f} (fees: ${t['fees']:.4f})")
        closed += 1

    return closed


# ---------------------------------------------------------------------------
# Core: find and enter new trades
# ---------------------------------------------------------------------------

def find_new_trades(trades, fg_val, trend, btc_price):
    """
    Scan the most recent Kronos pending predictions for high-confidence signals.
    Opens new paper trades if all three filters pass.
    Returns count of trades opened.
    """
    pending = load_json(PENDING_FILE, [])
    if not pending:
        print("  No pending Kronos predictions found.")
        return 0

    # Only look at predictions from the last 2 hours (freshest signal)
    now = datetime.now(timezone.utc)
    recent = []
    for p in pending:
        try:
            ts = datetime.fromisoformat(p.get("scrape_timestamp",""))
            if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
            if (now - ts).total_seconds() < 7200:
                recent.append(p)
        except:
            pass

    if not recent:
        print("  No recent (< 2h) Kronos predictions to evaluate.")
        return 0

    balance, exposure = calc_portfolio(trades)
    opened = 0

    for p in recent:
        prob = p.get("upside_prob", 0.5)
        vol  = p.get("vol_amplification_prob")
        ts   = p.get("prediction_timestamp","")

        signal = get_signal(prob)
        if signal == "neutral":
            continue

        score = calc_conviction(prob, vol)
        if score < 7:
            print(f"  Signal {signal} {prob*100:.0f}% — conviction {score}/10 too low (need 7+)")
            continue

        if already_have_signal(trades, ts):
            print(f"  Signal at {ts} already has an open/closed trade — skipping")
            continue

        size = get_position_size(score)

        # Check max exposure
        if exposure + size > MAX_EXPOSURE:
            print(f"  Would exceed max exposure (${exposure:.0f} open + ${size} = ${exposure+size:.0f}) — skip")
            continue

        # Check three filters
        passed, reason = check_three_filters(signal, fg_val, trend)
        print(f"  Evaluating {signal} {prob*100:.0f}% (score {score}/10): {reason}")

        if not passed:
            continue

        if btc_price is None:
            print(f"  Cannot get BTC price — skipping entry")
            continue

        # Entry fees
        entry_fee = round(size * FEE_RATE, 4)
        sl_price  = round(btc_price * (1 + STOP_LOSS_PCT/100)   if signal == "bearish" else btc_price * (1 - STOP_LOSS_PCT/100), 2)
        tp_price  = round(btc_price * (1 - TAKE_PROFIT_PCT/100) if signal == "bearish" else btc_price * (1 + TAKE_PROFIT_PCT/100), 2)

        trade_id = f"PT-{len(trades)+1:04d}"
        trade = {
            "id":                 trade_id,
            "status":             "open",
            "signal":             signal,
            "prob":               round(prob * 100, 1),
            "vol_prob":           round(vol * 100, 1) if vol else None,
            "conviction_score":   score,
            "size":               size,
            "entry_price":        round(btc_price, 2),
            "entry_fee":          entry_fee,
            "sl_price":           sl_price,
            "tp_price":           tp_price,
            "stop_loss_pct":      STOP_LOSS_PCT,
            "take_profit_pct":    TAKE_PROFIT_PCT,
            "fear_greed":         fg_val,
            "btc_trend_7d":       trend,
            "filter_reason":      reason,
            "kronos_timestamp":   ts,
            "entry_timestamp":    now.isoformat(),
            "outcome":            None,
            "exit_price":         None,
            "net_pnl":            None,
            "fees":               None,
        }

        trades.append(trade)
        exposure += size
        opened   += 1

        print(f"  ✅ TRADE OPENED: {trade_id} · {signal.upper()} · ${size} · Entry ${btc_price:,.2f} · SL ${sl_price:,.2f} · TP ${tp_price:,.2f} · Fee ${entry_fee:.4f}")

    return opened


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("\n=== Kronos Auto Paper Trader ===")
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"  Running at {now_str}\n")

    trades = load_json(TRADES_FILE, [])
    print(f"  Loaded {len(trades)} existing trades")

    # Step 1 — score any open trades that are 24h+ old
    print("\n[1] Scoring open trades...")
    closed = score_open_trades(trades)
    print(f"  Closed {closed} trade(s)")

    # Step 2 — fetch market data
    print("\n[2] Fetching market data...")
    fg_val, fg_label = fetch_fear_greed()
    print(f"  Fear & Greed: {fg_val} ({fg_label})" if fg_val else "  Fear & Greed: unavailable")

    trend = get_btc_7day_trend()
    print(f"  BTC 7-day trend: {trend}")

    btc_price = get_btc_price_now()
    print(f"  BTC current price: ${btc_price:,.2f}" if btc_price else "  BTC price: unavailable")

    # Step 3 — evaluate signals and open new trades
    print("\n[3] Evaluating Kronos signals...")
    opened = find_new_trades(trades, fg_val, trend, btc_price)
    print(f"  Opened {opened} new trade(s)")

    # Step 4 — save
    print("\n[4] Saving...")
    save_json(TRADES_FILE, trades)

    # Step 5 — summary
    balance, exposure = calc_portfolio(trades)
    closed_trades = [t for t in trades if t["status"] == "closed"]
    open_trades   = [t for t in trades if t["status"] == "open"]
    wins = sum(1 for t in closed_trades if t["outcome"] in ("win","take-profit"))
    total_closed = len(closed_trades)
    wr = round(wins/total_closed*100) if total_closed else 0
    total_fees = sum(t.get("fees",0) or 0 for t in closed_trades)

    print(f"""
=== Summary ===
  Portfolio balance : ${balance:,.2f}
  Open exposure     : ${exposure:,.2f}
  Open trades       : {len(open_trades)}
  Closed trades     : {total_closed}
  Win rate          : {wr}%
  Total fees paid   : ${total_fees:.4f}
""")


if __name__ == "__main__":
    main()
