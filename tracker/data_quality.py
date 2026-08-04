"""
Data-quality tagging for the Binance -> Coinbase price source migration
and the two bugs that followed it (see DATA_CHANGELOG.md at the repo root
for the full narrative). Nothing here deletes or rewrites any historical
record's own values — every record just gets a few additional fields so
future analysis (calibration, backtests, anything else) can filter down to
only internally-consistent data instead of silently mixing price sources.

Timeline these cutoffs encode:
  - Aug 3 2026 ~23:26 ET (Aug 4 03:26 UTC): Binance -> Coinbase migration
    lands (commits 4a2d872..acdaded). Anything predicted before this has no
    `price_source` field and used Binance for its entry price.
  - Same window, commit d39b290: kronos_tracker.py's get_price_at() starts
    delegating to Coinbase. Any score computed after this uses Coinbase for
    the settlement/exit price, even when the underlying prediction is older
    — that mismatch is exactly what made most of the pre-migration backlog
    "mixed" once it matured and got scored post-migration.
  - Aug 3 23:27 ET -> Aug 4 10:29 ET: the migration accidentally broke every
    scrape (ModuleNotFoundError: price_source) — no new predictions were
    generated in this window, so there's a gap but nothing to tag.
  - Aug 4 10:29 ET, commit ee682b5: scrapes fixed.
  - Aug 4 10:50 ET, commit 34efd69: current_price anchored to the true top
    of the hour instead of "whenever the run happened to fire." Before this,
    a late-running scrape (e.g. a manual button click well into the hour)
    could record a live-drifted price under an hour label it didn't match.
"""

from datetime import datetime, timezone, timedelta

EXIT_SOURCE_CUTOFF = datetime(2026, 8, 4, 3, 27, 0, tzinfo=timezone.utc)   # commit d39b290
ANCHOR_FIX_CUTOFF  = datetime(2026, 8, 4, 15, 0, 0, tzinfo=timezone.utc)  # commit 34efd69 (first clean scrape landed 15:24 UTC)
DRIFT_TOLERANCE    = timedelta(minutes=5)  # a scrape this close to the hour boundary is fine even pre-fix


def _parse(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _hour_of(prediction_timestamp):
    dt = _parse(prediction_timestamp)
    return dt.replace(minute=0, second=0, microsecond=0) if dt else None


def classify_entry(record):
    """Tag entry_price_source + entry_anchored on a prediction record (pending or scored)."""
    entry_source = "coinbase" if record.get("price_source") == "coinbase" else "binance"

    anchored = False
    if entry_source == "coinbase":
        scrape_ts = _parse(record.get("scrape_timestamp"))
        hour      = _hour_of(record.get("prediction_timestamp"))
        if scrape_ts and hour:
            if scrape_ts - hour <= DRIFT_TOLERANCE:
                anchored = True          # ran close enough to :00 that drift is negligible either way
            elif scrape_ts >= ANCHOR_FIX_CUTOFF:
                anchored = True          # ran late, but the anchor fix was live so it's still correct

    record["entry_price_source"] = entry_source
    record["entry_anchored"]     = anchored
    return record


def classify_exit(record):
    """Tag exit_price_source + price_regime on a SCORED record (needs classify_entry first)."""
    score_ts    = _parse(record.get("score_timestamp"))
    exit_source = "coinbase" if (score_ts and score_ts >= EXIT_SOURCE_CUTOFF) else "binance"
    record["exit_price_source"] = exit_source

    entry_source = record.get("entry_price_source")
    if entry_source == "coinbase" and exit_source == "coinbase" and record.get("entry_anchored"):
        regime = "coinbase_clean"
    elif entry_source == "coinbase" and exit_source == "coinbase":
        regime = "coinbase_mixed"        # both post-migration, but entry captured late pre-anchor-fix
    elif entry_source == "binance" and exit_source == "coinbase":
        regime = "binance_to_coinbase_mixed"   # the bulk of the pre-migration backlog
    else:
        regime = "binance_legacy"        # predicted and scored entirely before the migration

    record["price_regime"]       = regime
    record["price_regime_clean"] = (regime == "coinbase_clean")
    return record


def tag_pending(records):
    for r in records:
        classify_entry(r)
    return records


def tag_scores(records):
    for r in records:
        classify_entry(r)
        classify_exit(r)
    return records


if __name__ == "__main__":
    # One-off (idempotent — safe to rerun) backfill of existing pending.json /
    # scores.json with the fields above. New records get tagged automatically
    # by kronos_predictor.py / kronos_tracker.py going forward.
    import json
    import collections
    from pathlib import Path

    REPO_ROOT    = Path(__file__).parent.parent
    PENDING_FILE = REPO_ROOT / "pending.json"
    SCORES_FILE  = REPO_ROOT / "scores.json"

    for path, tagger in [(PENDING_FILE, tag_pending), (SCORES_FILE, tag_scores)]:
        if not path.exists():
            continue
        records = json.loads(path.read_text())
        tagger(records)
        path.write_text(json.dumps(records, indent=2))
        counts = collections.Counter(r.get("price_regime", r.get("entry_price_source")) for r in records)
        print(f"  Tagged {len(records)} records in {path.name}: {dict(counts)}")
