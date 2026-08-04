# Data Changelog

Backend-only record of changes to how `pending.json` / `scores.json` are
tagged, scored, or calibrated. Nothing in this file is shown in the email —
it exists so a future look at the historical data (or at why some number
moved) has a paper trail. Newest entries first.

---

## 2026-08-04 — Data-quality tagging + probability calibration introduced

**Why:** The Aug 3 Binance → Coinbase price-source migration, plus two bugs
that followed it, left the accuracy history built from an inconsistent mix
of price sources. Before trusting any "X% accurate" number or building a
calibration layer on top of it, the history needed to be tagged so clean
and mixed-source data can be told apart.

**What happened, chronologically:**
- Aug 3 ~23:26 ET — Binance → Coinbase migration lands (commits
  `4a2d872`..`acdaded`). Everything predicted before this has no
  `price_source` field and used Binance for its entry price.
- Aug 3 ~23:27 ET, commit `d39b290` — `kronos_tracker.py`'s scorer starts
  using Coinbase for the settlement/exit price. Any prediction that was
  still pending at this point and matured afterward got scored with a
  Coinbase exit price against a Binance entry price — an apples-to-oranges
  mismatch baked into ~7 historical records.
- Aug 3 23:27 ET → Aug 4 10:29 ET — the migration accidentally broke every
  scrape (`ModuleNotFoundError: price_source`); no new predictions were
  generated in this window.
- Aug 4 10:29 ET, commit `ee682b5` — scrapes fixed.
- Aug 4 10:50 ET, commit `34efd69` — `current_price` anchored to the true
  top of the hour instead of whenever the run happened to fire. Before
  this, a late-running scrape (e.g. a manual "Run Latest Prediction" click
  well into the hour) could record a live-drifted price under an hour
  label it didn't actually match. One historical 1h record was captured
  this way (~$165 off from the true top-of-hour price).

**What was added** (`tracker/data_quality.py`, `tracker/calibration.py`):
- Every `pending.json` / `scores.json` record now carries
  `entry_price_source`, `entry_anchored`, and (once scored)
  `exit_price_source` + `price_regime`. `price_regime` is one of:
  `binance_legacy` (predicted and scored entirely pre-migration — internally
  consistent, just an old regime), `binance_to_coinbase_mixed` (the mismatch
  above), `coinbase_mixed` (post-migration but captured before the anchor
  fix), or `coinbase_clean` (fully consistent, current-regime data).
  `price_regime_clean` is `True` only for `coinbase_clean`.
- **Nothing historical was deleted or altered** — this is purely additive
  tagging. Run `python tracker/data_quality.py` any time to (re)tag; it's
  idempotent.
- New predictions (`kronos_predictor.py`) now also compute
  `upside_prob_calibrated` / `vol_amplification_prob_calibrated`: the raw
  model probability adjusted against the realized hit rate at that
  confidence level, fit ONLY on `price_regime_clean==True` history. This
  does **not** retrain the Kronos model — it's a post-hoc lookup layer.
  Below 30 clean samples for a horizon it's a no-op (calibrated == raw,
  `..._calibration_n` is `0`) rather than fitting a curve to noise.
- Calibration is **not** retroactively backfilled onto historical records —
  doing so would be circular (using data the calibration curve was itself
  fit on to "predict" its own inputs). Only predictions generated after
  this change carry calibrated fields.
- `kronos_tracker.py --report` now prints a calibrated accuracy line
  alongside the raw one (once calibrated records exist) and a data-regime
  breakdown at the bottom. Backend/CLI only — the email is unchanged.

**Regime counts at the time of this change** (from the backfill pass):
- `pending.json` (11 records): 7 `binance` entry / 4 `coinbase` entry
- `scores.json` (442 records): 433 `binance_legacy`, 7
  `binance_to_coinbase_mixed`, 1 `coinbase_mixed`, 1 `coinbase_clean`

So as of this change, there is exactly **1** fully clean scored record.
Calibration will stay a passthrough for a while — expect it to start
actually adjusting numbers once `coinbase_clean` accumulates past 30
records per horizon. Check `upside_prob_calibration_n` on any given
prediction to see whether it was calibrated or passed through.
