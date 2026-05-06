"""
Tests for kronos_tracker.py
Run: pytest tests/ -v
"""

import json
import math
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add tracker dir to path
sys.path.insert(0, str(Path(__file__).parent.parent / "tracker"))
from kronos_tracker import (
    compute_realized_vol,
    compute_stats,
    score_prediction,
    scrape_demo,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DEMO_HTML_TEMPLATE = """
<html><body>
<p>Last Updated (UTC): <strong>{last_updated}</strong></p>
<h3>Upside Probability (Next 24h)</h3>
<p>{upside}%</p>
<h3>Volatility Amplification (Next 24h)</h3>
<p>{vol}%</p>
</body></html>
"""


def make_demo_html(upside: float, vol: float, last_updated: str = "2026-05-06 16:00:25") -> str:
    return DEMO_HTML_TEMPLATE.format(upside=upside, vol=vol, last_updated=last_updated)


def make_kline(close: float, open_: float = None, high: float = None, low: float = None,
               volume: float = 1000.0, ts_ms: int = 1714000000000) -> list:
    """Return a Binance kline array."""
    open_ = open_ or close * 0.999
    high = high or close * 1.001
    low = low or close * 0.998
    return [ts_ms, str(open_), str(high), str(low), str(close), str(volume),
            ts_ms + 3_600_000, "0", 100, "500", "0", "0"]


def make_kline_series(closes: list[float], base_ts_ms: int = 1714000000000) -> list[list]:
    """Return a series of kline arrays with sequential timestamps."""
    return [
        make_kline(c, ts_ms=base_ts_ms + i * 3_600_000)
        for i, c in enumerate(closes)
    ]


SAMPLE_PENDING = {
    "upside_prob": 0.7,
    "vol_amplification_prob": 0.6,
    "demo_last_updated": "2026-05-05 16:00:25",
    "prediction_timestamp": "2026-05-05 16:00:25",
    "scrape_timestamp": "2026-05-05T16:05:00+00:00",
}


# ---------------------------------------------------------------------------
# scrape_demo tests
# ---------------------------------------------------------------------------

class TestScrapDemo:
    def _mock_response(self, html: str) -> MagicMock:
        resp = MagicMock()
        resp.text = html
        resp.raise_for_status = MagicMock()
        return resp

    @patch("kronos_tracker.requests.get")
    def test_parses_upside_probability(self, mock_get):
        mock_get.return_value = self._mock_response(make_demo_html(13.3, 66.7))
        result = scrape_demo()
        assert abs(result["upside_prob"] - 0.133) < 0.001

    @patch("kronos_tracker.requests.get")
    def test_parses_vol_amplification(self, mock_get):
        mock_get.return_value = self._mock_response(make_demo_html(13.3, 66.7))
        result = scrape_demo()
        assert abs(result["vol_amplification_prob"] - 0.667) < 0.001

    @patch("kronos_tracker.requests.get")
    def test_parses_timestamp(self, mock_get):
        mock_get.return_value = self._mock_response(
            make_demo_html(50.0, 50.0, "2026-05-06 16:00:25")
        )
        result = scrape_demo()
        assert result["demo_last_updated"] == "2026-05-06 16:00:25"

    @patch("kronos_tracker.requests.get")
    def test_upside_100_percent(self, mock_get):
        mock_get.return_value = self._mock_response(make_demo_html(100.0, 0.0))
        result = scrape_demo()
        assert result["upside_prob"] == 1.0
        assert result["vol_amplification_prob"] == 0.0

    @patch("kronos_tracker.requests.get")
    def test_upside_0_percent(self, mock_get):
        mock_get.return_value = self._mock_response(make_demo_html(0.0, 100.0))
        result = scrape_demo()
        assert result["upside_prob"] == 0.0
        assert result["vol_amplification_prob"] == 1.0

    @patch("kronos_tracker.requests.get")
    def test_raises_on_missing_upside(self, mock_get):
        mock_get.return_value = self._mock_response("<html><body>broken</body></html>")
        with pytest.raises(ValueError, match="upside probability"):
            scrape_demo()

    @patch("kronos_tracker.requests.get")
    def test_raises_on_http_error(self, mock_get):
        import requests as req
        resp = MagicMock()
        resp.raise_for_status.side_effect = req.exceptions.HTTPError("403")
        mock_get.return_value = resp
        with pytest.raises(req.exceptions.HTTPError):
            scrape_demo()

    @patch("kronos_tracker.requests.get")
    def test_scrape_timestamp_is_utc_iso(self, mock_get):
        mock_get.return_value = self._mock_response(make_demo_html(30.0, 70.0))
        result = scrape_demo()
        # Should parse as a valid ISO datetime
        dt = datetime.fromisoformat(result["scrape_timestamp"])
        assert dt.tzinfo is not None


# ---------------------------------------------------------------------------
# Brier score math
# ---------------------------------------------------------------------------

class TestBrierScore:
    """The Brier score is (prob - outcome)^2. Verify arithmetic is correct."""

    def _brier(self, prob, went_up):
        outcome = 1.0 if went_up else 0.0
        return (prob - outcome) ** 2

    def test_perfect_bullish_prediction(self):
        assert self._brier(1.0, True) == pytest.approx(0.0)

    def test_perfect_bearish_prediction(self):
        assert self._brier(0.0, False) == pytest.approx(0.0)

    def test_random_baseline(self):
        assert self._brier(0.5, True) == pytest.approx(0.25)
        assert self._brier(0.5, False) == pytest.approx(0.25)

    def test_worst_case_bullish(self):
        # 100% confident UP but price went DOWN
        assert self._brier(1.0, False) == pytest.approx(1.0)

    def test_worst_case_bearish(self):
        assert self._brier(0.0, True) == pytest.approx(1.0)

    def test_moderate_confidence_correct(self):
        # 70% confident UP, price went UP
        score = self._brier(0.7, True)
        assert score == pytest.approx(0.09)

    def test_moderate_confidence_wrong(self):
        # 70% confident UP, price went DOWN
        score = self._brier(0.7, False)
        assert score == pytest.approx(0.49)


# ---------------------------------------------------------------------------
# score_prediction tests
# ---------------------------------------------------------------------------

class TestScorePrediction:
    # 2026-05-05 16:00:00 UTC — must match SAMPLE_PENDING's prediction_timestamp
    PRED_DT = datetime(2026, 5, 5, 16, 0, 0, tzinfo=timezone.utc)
    PRED_TS_MS = int(PRED_DT.timestamp() * 1000)

    def _make_binance_mock(self, prices_t0_region: list[float], prices_hist_region: list[float],
                           prices_future_region: list[float]):
        """
        Route Binance mock responses by startTime relative to the prediction timestamp.

        Four call sites in score_prediction:
          1. get_price_at(pred_dt)        → limit=2, start ≈ pred_ts        → prices_t0_region
          2. get_price_at(pred_dt + 24h)  → limit=2, start ≈ pred_ts+24h    → prices_future_region
          3. compute_realized_vol(hist)   → limit=25, start ≈ pred_ts-24h   → prices_hist_region
          4. compute_realized_vol(future) → limit=25, start ≈ pred_ts        → prices_future_region
        """
        ONE_HOUR_MS = 3_600_000
        pred_ts = self.PRED_TS_MS
        t24_ts = pred_ts + 24 * ONE_HOUR_MS
        # threshold: anything within 1h before pred_ts is "hist region start"
        hist_ts = pred_ts - 24 * ONE_HOUR_MS

        def mock_get(url, params=None, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            start = params.get("startTime", 0) if params else 0
            limit = params.get("limit", 1) if params else 1

            # Call 2: t24 price fetch
            if start >= t24_ts - ONE_HOUR_MS:
                data = make_kline_series(prices_future_region[-2:], t24_ts)
            # Call 1: t0 price fetch (limit ≤ 2, start near pred_ts)
            elif start >= pred_ts - ONE_HOUR_MS and limit <= 2:
                data = make_kline_series(prices_t0_region, pred_ts)
            # Call 4: future realized vol (limit > 2, start near pred_ts)
            elif start >= pred_ts - ONE_HOUR_MS and limit > 2:
                data = make_kline_series(prices_future_region, pred_ts)
            # Call 3: historical vol (start near pred_ts - 24h)
            else:
                data = make_kline_series(prices_hist_region, hist_ts)

            resp.json.return_value = data[:limit]
            return resp

        return mock_get

    @patch("kronos_tracker.requests.get")
    def test_direction_correct_up(self, mock_get):
        """Kronos says bullish (0.7), price goes up — should be correct."""
        # t0=50000, t24=51000 → went up
        hist_closes = [49000.0] * 25
        t0_closes = [50000.0, 50000.0]
        future_closes = [50500.0] * 26  # rising

        mock_get.side_effect = self._make_binance_mock(t0_closes, hist_closes, future_closes)

        pending = {**SAMPLE_PENDING, "upside_prob": 0.7, "vol_amplification_prob": 0.5}
        scored = score_prediction(pending)

        assert scored["went_up"] is True
        assert scored["direction_correct"] is True
        assert scored["brier_score"] == pytest.approx((0.7 - 1.0) ** 2)

    @patch("kronos_tracker.requests.get")
    def test_direction_correct_down(self, mock_get):
        """Kronos says bearish (0.2), price goes down — should be correct."""
        hist_closes = [51000.0] * 25
        t0_closes = [50000.0, 50000.0]
        future_closes = [49000.0] * 26

        mock_get.side_effect = self._make_binance_mock(t0_closes, hist_closes, future_closes)

        pending = {**SAMPLE_PENDING, "upside_prob": 0.2, "vol_amplification_prob": 0.5}
        scored = score_prediction(pending)

        assert scored["went_up"] is False
        assert scored["direction_correct"] is True
        assert scored["brier_score"] == pytest.approx((0.2 - 0.0) ** 2)

    @patch("kronos_tracker.requests.get")
    def test_direction_wrong(self, mock_get):
        """Kronos says bullish (0.8), price goes down — should be wrong."""
        hist_closes = [50000.0] * 25
        t0_closes = [50000.0, 50000.0]
        future_closes = [48000.0] * 26

        mock_get.side_effect = self._make_binance_mock(t0_closes, hist_closes, future_closes)

        pending = {**SAMPLE_PENDING, "upside_prob": 0.8, "vol_amplification_prob": 0.5}
        scored = score_prediction(pending)

        assert scored["went_up"] is False
        assert scored["direction_correct"] is False
        assert scored["brier_score"] == pytest.approx((0.8 - 0.0) ** 2)

    @patch("kronos_tracker.requests.get")
    def test_price_change_pct_calculation(self, mock_get):
        hist_closes = [50000.0] * 25
        t0_closes = [50000.0, 50000.0]
        future_closes = [51000.0] * 26

        mock_get.side_effect = self._make_binance_mock(t0_closes, hist_closes, future_closes)

        scored = score_prediction({**SAMPLE_PENDING})
        assert scored["price_change_pct"] == pytest.approx(2.0, abs=0.01)

    @patch("kronos_tracker.requests.get")
    def test_scored_record_has_all_keys(self, mock_get):
        hist_closes = [50000.0] * 25
        t0_closes = [50000.0, 50000.0]
        future_closes = [50100.0] * 26

        mock_get.side_effect = self._make_binance_mock(t0_closes, hist_closes, future_closes)

        scored = score_prediction({**SAMPLE_PENDING})
        required_keys = [
            "direction_correct", "brier_score", "vol_correct", "vol_brier_score",
            "price_t0", "price_t24", "price_change_pct", "went_up",
            "hist_vol", "realized_vol", "realized_vol_ratio", "vol_amplified",
            "score_timestamp",
        ]
        for key in required_keys:
            assert key in scored, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# compute_stats tests
# ---------------------------------------------------------------------------

class TestComputeStats:
    def _make_record(self, direction_correct: bool, brier: float,
                     vol_correct: bool, vol_brier: float,
                     price_change: float = 1.0) -> dict:
        return {
            "direction_correct": direction_correct,
            "brier_score": brier,
            "vol_correct": vol_correct,
            "vol_brier_score": vol_brier,
            "price_change_pct": price_change,
        }

    def test_empty_records(self):
        assert compute_stats([]) == {}

    def test_perfect_accuracy(self):
        records = [self._make_record(True, 0.0, True, 0.0) for _ in range(10)]
        stats = compute_stats(records)
        assert stats["direction_accuracy_pct"] == 100.0
        assert stats["avg_brier_score"] == 0.0
        assert stats["vol_accuracy_pct"] == 100.0

    def test_zero_accuracy(self):
        records = [self._make_record(False, 1.0, False, 1.0) for _ in range(10)]
        stats = compute_stats(records)
        assert stats["direction_accuracy_pct"] == 0.0
        assert stats["avg_brier_score"] == 1.0

    def test_50_percent_accuracy(self):
        records = [
            self._make_record(True, 0.09, True, 0.09),
            self._make_record(False, 0.49, False, 0.49),
        ] * 5
        stats = compute_stats(records)
        assert stats["direction_accuracy_pct"] == 50.0
        assert stats["n"] == 10

    def test_window_slices_last_n(self):
        records = (
            [self._make_record(False, 1.0, False, 1.0)] * 20
            + [self._make_record(True, 0.0, True, 0.0)] * 7
        )
        stats = compute_stats(records, window=7)
        assert stats["direction_accuracy_pct"] == 100.0
        assert stats["n"] == 7

    def test_streak_all_correct(self):
        records = [self._make_record(True, 0.0, True, 0.0) for _ in range(5)]
        stats = compute_stats(records)
        assert stats["correct_streak"] == 5

    def test_streak_broken(self):
        records = [
            self._make_record(True, 0.0, True, 0.0),
            self._make_record(True, 0.0, True, 0.0),
            self._make_record(False, 1.0, False, 1.0),  # break
            self._make_record(True, 0.0, True, 0.0),
            self._make_record(True, 0.0, True, 0.0),
        ]
        stats = compute_stats(records)
        assert stats["correct_streak"] == 2  # only last 2 are correct

    def test_streak_zero(self):
        records = [self._make_record(False, 1.0, False, 1.0)]
        stats = compute_stats(records)
        assert stats["correct_streak"] == 0

    def test_avg_price_change(self):
        records = [
            self._make_record(True, 0.0, True, 0.0, price_change=2.0),
            self._make_record(True, 0.0, True, 0.0, price_change=4.0),
        ]
        stats = compute_stats(records)
        assert stats["avg_price_change_pct"] == pytest.approx(3.0)

    def test_window_larger_than_records(self):
        records = [self._make_record(True, 0.0, True, 0.0) for _ in range(3)]
        stats = compute_stats(records, window=30)
        assert stats["n"] == 3  # returns all available


# ---------------------------------------------------------------------------
# Vol computation sanity checks
# ---------------------------------------------------------------------------

class TestRealizedVol:
    @patch("kronos_tracker.requests.get")
    def test_zero_vol_flat_prices(self, mock_get):
        """Completely flat prices → zero variance."""
        closes = [50000.0] * 25
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = make_kline_series(closes)
        mock_get.return_value = resp

        vol = compute_realized_vol(datetime(2026, 5, 5, 16, tzinfo=timezone.utc), hours=24)
        assert vol == pytest.approx(0.0, abs=1e-10)

    @patch("kronos_tracker.requests.get")
    def test_vol_increases_with_larger_moves(self, mock_get):
        """Larger price swings → higher realized vol."""
        closes_low_vol = [50000.0 + (i % 2) * 10 for i in range(25)]
        closes_high_vol = [50000.0 + (i % 2) * 1000 for i in range(25)]

        resp_low = MagicMock()
        resp_low.raise_for_status = MagicMock()
        resp_low.json.return_value = make_kline_series(closes_low_vol)
        mock_get.return_value = resp_low

        vol_low = compute_realized_vol(datetime(2026, 5, 5, 16, tzinfo=timezone.utc), hours=24)

        resp_high = MagicMock()
        resp_high.raise_for_status = MagicMock()
        resp_high.json.return_value = make_kline_series(closes_high_vol)
        mock_get.return_value = resp_high

        vol_high = compute_realized_vol(datetime(2026, 5, 5, 16, tzinfo=timezone.utc), hours=24)
        assert vol_high > vol_low

    @patch("kronos_tracker.requests.get")
    def test_raises_on_insufficient_data(self, mock_get):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = make_kline_series([50000.0])  # only 1 candle
        mock_get.return_value = resp

        with pytest.raises(ValueError, match="Not enough candles"):
            compute_realized_vol(datetime(2026, 5, 5, 16, tzinfo=timezone.utc), hours=24)
