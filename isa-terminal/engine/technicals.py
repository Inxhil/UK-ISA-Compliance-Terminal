"""Technical indicators (pure pandas/numpy). All ratios are unit-free, so compute on native prices."""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(close: pd.Series, window: int) -> pd.Series:
    """Simple moving average; NaN until ``window`` observations exist."""
    return close.rolling(window=window, min_periods=window).mean()


def rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI.

    Seed = simple average of the first ``period`` gains/losses, then Wilder smoothing
    ``avg = (prev*(period-1) + cur) / period`` (this matches StockCharts / most charting packages).
    """
    c = pd.Series(close, dtype="float64").dropna()
    out = pd.Series(np.nan, index=c.index, dtype="float64")
    if len(c) <= period:
        return out.reindex(close.index)
    delta = c.diff().to_numpy()
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_g = gain[1:period + 1].mean()
    avg_l = loss[1:period + 1].mean()

    def _rsi(g: float, l: float) -> float:
        if l == 0:
            return 50.0 if g == 0 else 100.0
        return 100.0 - 100.0 / (1.0 + g / l)

    vals = np.full(len(c), np.nan)
    vals[period] = _rsi(avg_g, avg_l)
    for i in range(period + 1, len(c)):
        avg_g = (avg_g * (period - 1) + gain[i]) / period
        avg_l = (avg_l * (period - 1) + loss[i]) / period
        vals[i] = _rsi(avg_g, avg_l)
    out[:] = vals
    return out.reindex(close.index)


def last_value(s: pd.Series | None) -> float | None:
    if s is None or len(s) == 0:
        return None
    v = s.dropna()
    return float(v.iloc[-1]) if len(v) else None


def drawdown_from_high(close: pd.Series, window: int = 252) -> float | None:
    """Current drawdown (%, <= 0) from the trailing ``window``-bar high."""
    c = close.dropna()
    if len(c) == 0:
        return None
    hi = c.iloc[-window:].max()
    return float((c.iloc[-1] / hi - 1.0) * 100.0) if hi > 0 else None


def pct_vs_mean(close: pd.Series, window: int | None = None) -> float | None:
    """% premium (+) / discount (-) of the last close to the mean close of the trailing window (None = all)."""
    c = close.dropna()
    if len(c) < 20:
        return None
    ref = c.iloc[-window:] if window else c
    m = ref.mean()
    return float((c.iloc[-1] / m - 1.0) * 100.0) if m > 0 else None


def trend_label(price: float | None, sma50: float | None, sma200: float | None) -> str:
    """UPTREND / DOWNTREND / MIXED / N/A from price vs SMA50 vs SMA200."""
    if price is None or sma50 is None:
        return "N/A"
    if sma200 is None:
        return "UPTREND" if price > sma50 else "DOWNTREND"
    if price > sma50 > sma200:
        return "UPTREND"
    if price < sma50 < sma200:
        return "DOWNTREND"
    return "MIXED"


def cross_state(sma50: float | None, sma200: float | None) -> str | None:
    """'GOLDEN' when SMA50 > SMA200, 'DEATH' when below (state, not event)."""
    if sma50 is None or sma200 is None:
        return None
    return "GOLDEN" if sma50 > sma200 else "DEATH"
