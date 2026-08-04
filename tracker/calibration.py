"""
Post-hoc probability calibration for Kronos's upside_prob / vol_amplification_prob.

This does NOT retrain or touch the Kronos model — it's a lookup that maps
the model's raw stated confidence to what that confidence level has
actually delivered historically. Fit ONLY on price_regime_clean==True
scores (see data_quality.py) so the Binance/Coinbase source mismatch in
the pre-migration backlog can't leak into it.

Deliberately conservative: below MIN_SAMPLES clean records for a given
horizon, calibration is a no-op (calibrated == raw) rather than fitting a
curve to noise. It activates on its own as clean data accumulates — every
record this touches carries a `..._calibration_n` field recording exactly
how many clean samples backed the number, so it's always visible whether a
given calibrated value is real or just a passthrough.
"""

MIN_SAMPLES = 30
MAX_BINS    = 5


def _clean(records, horizon, prob_field, outcome_field):
    return [r for r in records
            if r.get("horizon", "24h") == horizon
            and r.get("price_regime_clean") is True
            and r.get(prob_field) is not None
            and r.get(outcome_field) is not None]


def build_curve(records, horizon, prob_field="upside_prob", outcome_field="direction_correct"):
    """Bucket clean history into quantile bins of the raw probability.
    Returns (sorted [(bin_midpoint_raw_prob, realized_hit_rate), ...], n_clean)."""
    clean = _clean(records, horizon, prob_field, outcome_field)
    if len(clean) < MIN_SAMPLES:
        return None, len(clean)

    clean.sort(key=lambda r: r[prob_field])
    n_bins   = max(2, min(MAX_BINS, len(clean) // 10))
    bin_size = len(clean) / n_bins
    curve = []
    for i in range(n_bins):
        chunk = clean[int(i * bin_size): int((i + 1) * bin_size)] or clean[-1:]
        mid   = sum(r[prob_field] for r in chunk) / len(chunk)
        hit   = sum(1 for r in chunk if r[outcome_field]) / len(chunk)
        curve.append((mid, hit))
    return curve, len(clean)


def _interp(curve, x):
    if x <= curve[0][0]:
        return curve[0][1]
    if x >= curve[-1][0]:
        return curve[-1][1]
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y0
            frac = (x - x0) / (x1 - x0)
            return y0 + frac * (y1 - y0)
    return x  # unreachable given the bounds checks above


def calibrate(raw_prob, horizon, clean_history, prob_field="upside_prob", outcome_field="direction_correct"):
    """Returns (calibrated_prob, n_samples_used). n_samples_used == 0 means
    there wasn't enough clean history yet, so calibrated_prob == raw_prob."""
    curve, n = build_curve(clean_history, horizon, prob_field, outcome_field)
    if curve is None:
        return raw_prob, 0
    return round(_interp(curve, raw_prob), 4), n
