import numpy as np
import pandas as pd
import pytest

from engine.technicals import (cross_state, drawdown_from_high, last_value, pct_vs_mean, rsi_wilder, sma,
                               trend_label)

# Classic Wilder/StockCharts worked example (14-period RSI).
CLOSES = [44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826, 45.8931,
          46.0328, 45.6140, 46.2820, 46.2820, 46.0028, 46.0328, 46.4116, 46.2222, 45.6439, 46.2122, 46.2521,
          45.7137, 46.4515, 45.7835, 45.3548, 44.0288, 44.1783, 44.2220, 44.5677, 43.4205, 42.6628, 43.1314]


def test_rsi_matches_stockcharts_reference():
    r = rsi_wilder(pd.Series(CLOSES), 14)
    assert r.iloc[:14].isna().all()
    assert r.iloc[14] == pytest.approx(70.53, abs=0.05)
    assert r.iloc[15] == pytest.approx(66.32, abs=0.05)
    assert r.iloc[16] == pytest.approx(66.55, abs=0.05)
    assert r.iloc[-1] == pytest.approx(37.77, abs=0.1)


def test_rsi_bounds_and_degenerate_cases():
    up = pd.Series(np.arange(1, 40, dtype=float))
    assert rsi_wilder(up).iloc[-1] == 100.0
    down = pd.Series(np.arange(40, 1, -1, dtype=float))
    assert rsi_wilder(down).iloc[-1] == pytest.approx(0.0, abs=1e-9)
    flat = pd.Series([5.0] * 30)
    assert rsi_wilder(flat).iloc[-1] == 50.0
    assert rsi_wilder(pd.Series([1.0, 2.0])).isna().all()


def test_rsi_preserves_index():
    idx = pd.date_range("2026-01-01", periods=33, freq="B")
    r = rsi_wilder(pd.Series(CLOSES, index=idx))
    assert list(r.index) == list(idx)


def test_sma_needs_full_window():
    s = pd.Series(range(1, 11), dtype=float)
    out = sma(s, 5)
    assert out.iloc[:4].isna().all()
    assert out.iloc[4] == 3.0 and out.iloc[-1] == 8.0


def test_drawdown_and_pct_vs_mean():
    s = pd.Series([100, 110, 120, 90], dtype=float)
    assert drawdown_from_high(s) == pytest.approx(-25.0)
    assert drawdown_from_high(pd.Series(dtype=float)) is None
    long = pd.Series([100.0] * 40 + [120.0])
    assert pct_vs_mean(long) == pytest.approx((120 / long.mean() - 1) * 100)
    assert pct_vs_mean(pd.Series([1.0, 2.0])) is None


def test_trend_and_cross():
    assert trend_label(110, 100, 90) == "UPTREND"
    assert trend_label(80, 90, 100) == "DOWNTREND"
    assert trend_label(95, 90, 100) == "MIXED"
    assert trend_label(None, 1, 1) == "N/A"
    assert trend_label(110, 100, None) == "UPTREND"
    assert cross_state(101, 100) == "GOLDEN" and cross_state(99, 100) == "DEATH" and cross_state(None, 1) is None
    assert last_value(pd.Series([1.0, float("nan")])) == 1.0 and last_value(None) is None
