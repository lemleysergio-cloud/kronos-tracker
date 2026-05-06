# Kronos BTC Accuracy Tracker

Automatically tracks the prediction accuracy of the [Kronos live demo](https://shiyu-coder.github.io/Kronos-demo/) against real BTC/USDT market data from Binance. Runs daily via GitHub Actions, zero cost, no model download needed.

## What it does

Every day at **16:30 UTC**, the GitHub Actions workflow:

1. **Scores yesterday's prediction** — fetches real BTC price from Binance 24h after the prediction was made, then calculates direction accuracy, Brier score, and volatility accuracy
2. **Records today's prediction** — scrapes the live demo's upside probability % and volatility amplification % 
3. **Commits** the updated `scores.json` back to this repo
4. **Prints a summary** in the Actions run log (visible in the GitHub UI)

All data lives in `scores.json` in this repo — fully auditable and versioned.

## Setup (5 minutes)

### 1. Fork or create this repo

Create a new **private** GitHub repo and push these files to it:

```
.github/
  workflows/
    daily_tracker.yml
tracker/
  kronos_tracker.py
requirements.txt
scores.json          ← empty array, already initialized
```

### 2. Enable GitHub Actions

Go to your repo → **Actions** tab → click **"I understand my workflows, go ahead and enable them"** if prompted.

### 3. Trigger the first run manually

Go to **Actions → Kronos Daily Tracker → Run workflow** (top right). Select mode `all`. This first run won't have a prediction to score yet — that's expected. It will scrape today's prediction and save it to `pending.json`.

After that, the daily cron handles everything automatically.

### 4. No secrets or API keys needed

The Binance klines endpoint used here is public and unauthenticated. The Kronos demo page is public. Nothing in this repo requires credentials.

---

## Metrics tracked

| Metric | Description | Perfect score |
|--------|-------------|---------------|
| `direction_correct` | Did price move the way Kronos predicted? | `true` |
| `brier_score` | Calibration of the upside probability | `0.0` |
| `vol_correct` | Did vol amplification prediction match reality? | `true` |
| `vol_brier_score` | Calibration of vol probability | `0.0` |
| `price_change_pct` | Actual 24h BTC price change % | — |
| `realized_vol_ratio` | Realized vol ÷ historical vol | — |

Random baseline: direction accuracy = 50%, Brier score = 0.25.

---

## Claude routine prompt

Use this in **Claude.ai → Routines** to get a daily narrative report. Set it to run daily, then paste in the contents of your `scores.json` each time (or attach the file).

---

```
You are my daily Kronos accuracy analyst. I'm tracking whether the Kronos financial foundation model (https://shiyu-coder.github.io/Kronos-demo/) makes accurate BTC predictions. Below is my scores.json — each record is one day's scored prediction.

Please produce a structured accuracy report with these sections:

**1. Overall summary**
- Direction accuracy % (all-time, last 30 days, last 7 days)
- Avg Brier score (all-time, last 30 days, last 7 days) — remind me that 0.0 is perfect, 0.25 is random
- Volatility accuracy % (all-time)
- Current correct streak

**2. Trend analysis**
- Is accuracy improving, declining, or stable over time?
- Any correlation between Kronos's stated confidence level and actual correctness? (i.e. when upside_prob is extreme like <20% or >80%, is it more or less accurate?)
- Most recent 5 predictions with outcome

**3. Calibration check**
- Bin predictions by upside_prob (0–30%, 30–50%, 50–70%, 70–100%) and tell me what % of each bin actually went up. A well-calibrated model should show alignment.

**4. Verdict**
- Is Kronos performing better than a coin flip on direction?
- Is its probability calibration meaningful or noise?
- What would you want to see over the next 30 days to feel confident this model has real signal?

scores.json:
[PASTE CONTENTS HERE]
```

---

## Local usage

```bash
pip install -r requirements.txt

# Record today's prediction
python tracker/kronos_tracker.py --scrape

# Score yesterday's prediction (run 24h later)
python tracker/kronos_tracker.py --score

# Print report
python tracker/kronos_tracker.py --report

# Combined (what GitHub Actions runs)
python tracker/kronos_tracker.py --all
```

---

## Connecting to local Kronos model (later)

When you eventually download `Kronos-mini` or `Kronos-small`, you can swap the demo scraper for a local `KronosPredictor` call. The `scores.json` schema is identical — your compounding accuracy history continues uninterrupted.

The key fields to populate from local inference:
- `upside_prob` — P(price_t24 > price_t0) derived from your Monte Carlo paths
- `vol_amplification_prob` — P(realized_vol > hist_vol)
- `prediction_timestamp` — UTC timestamp when prediction was made

---

## License

MIT
