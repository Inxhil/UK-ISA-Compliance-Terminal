import numpy as np
import pandas as pd
import pytest

from engine.analyst_screener import (AnalystScreener, consensus_label, mean_from_counts, trailing_yield_pct,
                                     valuation_label)
from engine.config import HandbookRules, WatchItem
from engine.market_intel import DataUnavailable, MarketIntel, RawQuote


def series(n, start, end, divs=None):
    idx = pd.bdate_range(end="2026-11-18", periods=n)
    px = np.linspace(start, end, n)
    df = pd.DataFrame({"Open": px, "High": px, "Low": px, "Close": px, "Volume": 1e6, "Dividends": 0.0}, index=idx)
    for off, amt in (divs or {}).items():
        df.iloc[off, df.columns.get_loc("Dividends")] = amt
    return df


class Fake:
    name = "fake"
    META = {"AZN.L": ("GBp", "EQUITY"), "MSFT": ("USD", "EQUITY"), "VWRP.L": ("GBP", "ETF"), "GBPUSD=X": ("USD", "CURRENCY")}
    PX = {"AZN.L": 13628.0, "MSFT": 501.69, "VWRP.L": 138.3, "GBPUSD=X": 1.3154}

    def __init__(self):
        self.targets = {"AZN.L": {"current": 13628, "high": 16500, "low": 11000, "mean": 15000.0, "median": 14800},
                        "MSFT": {"current": 501.69, "high": 870, "low": 440, "mean": 636.12, "median": 631}}
        self.recs = {"MSFT": [{"period": "0m", "strongBuy": 12, "buy": 41, "hold": 5, "sell": 0, "strongSell": 0}]}
        self.infos = {"AZN.L": {"forwardPE": 17.5, "trailingPegRatio": 1.34, "payoutRatio": 0.58, "sector": "Healthcare",
                                "recommendationMean": 1.9, "numberOfAnalystOpinions": 22, "marketCap": 2.1e11, "beta": 0.5},
                      "MSFT": {"forwardPE": 20.7, "trailingPegRatio": 1.5, "dividendYield": 0.77,
                               "trailingAnnualDividendYield": 0.0073, "payoutRatio": 0.24, "sector": "Technology"},
                      "VWRP.L": {"quoteType": "ETF", "longName": "Vanguard FTSE All-World"}}
        self.hist_days = 1305

    def closes(self, ts):
        return {t: RawQuote(self.PX[t], self.PX[t] * 0.99) for t in ts if t in self.PX}

    def metadata(self, t):
        c, ty = self.META[t]
        return {"currency": c, "instrumentType": ty}

    def history(self, t, period="5y"):
        px = self.PX[t]
        # AZN (pence): two dividends 100p and 207p in the last year (=> 307/13628 = 2.25%)
        divs = {-100: 100.0, -30: 207.0} if t == "AZN.L" else ({-60: 0.91} if t == "MSFT" else None)
        return series(self.hist_days, px * 0.7, px, divs)

    def info(self, t):
        return dict(self.infos.get(t, {}))

    def analyst(self, t):
        return {"targets": self.targets.get(t, {}), "recs": self.recs.get(t, [])}

    def news(self, t):
        raise DataUnavailable("none")

    def gilt(self):
        return None


WATCH = {
    "AZN.L": WatchItem("AZN.L", "AstraZeneca", "stock", 10, sector="Healthcare"),
    "MSFT": WatchItem("MSFT", "Microsoft", "stock", 12, sector="Technology"),
    "VWRP.L": WatchItem("VWRP.L", "Vanguard FTSE All-World", "etf", 35, ter_pct=0.22, distribution="acc",
                        yield_override_pct=1.7),
}


@pytest.fixture
def env(tmp_path):
    fake = Fake()
    mi = MarketIntel(tmp_path, live=fake, mode="live")
    sc = AnalystScreener(mi, HandbookRules(), WATCH)
    return sc, mi, fake


def snap(env, t):
    sc, mi, _ = env
    return sc.snapshot(t, mi.quotes([t])[t])


def test_pence_stock_targets_and_upside_in_pounds(env):
    s = snap(env, "AZN.L")
    assert s.currency == "GBP" and s.price == pytest.approx(136.28) and s.price_gbp == pytest.approx(136.28)
    assert s.target_mean == pytest.approx(150.0) and s.target_high == pytest.approx(165.0) and s.target_low == pytest.approx(110.0)
    assert s.upside_pct == pytest.approx((150 / 136.28 - 1) * 100)
    assert s.upside_low_pct < 0 < s.upside_high_pct
    assert s.rating_label == "Buy" and s.rating_mean == 1.9 and s.n_analysts == 22 and s.peg == pytest.approx(1.34)


def test_usd_stock_ratios_in_native_units_gbp_for_display(env):
    s = snap(env, "MSFT")
    assert s.currency == "USD" and s.price == pytest.approx(501.69) and s.price_gbp == pytest.approx(501.69 / 1.3154)
    assert s.upside_pct == pytest.approx((636.12 / 501.69 - 1) * 100)         # FX cancels: native / native
    # no recommendationMean in info => derived from the 12/41/5 count distribution: 109/58 = 1.88 => "Buy"
    assert s.rating_mean == pytest.approx(109 / 58) and s.rating_label == "Buy"
    assert s.n_analysts == 58 and s.rec_counts["strongBuy"] == 12


def test_unit_mismatched_target_is_reconciled_with_a_note(env):
    env[2].targets["AZN.L"] = {"mean": 150.0, "high": 165.0, "low": 110.0}     # pounds, though the ticker is pence
    s = snap(env, "AZN.L")
    assert s.target_mean == pytest.approx(150.0) and s.upside_pct == pytest.approx((150 / 136.28 - 1) * 100)
    assert any("unit mismatch" in n for n in s.notes)


def test_yield_from_dividend_history_same_unit(env):
    s = snap(env, "AZN.L")
    assert s.dividend_yield_pct == pytest.approx(307 / 13628 * 100, rel=1e-3) and not s.yield_is_estimate
    m = snap(env, "MSFT")
    assert m.dividend_yield_pct == pytest.approx(0.91 / 501.69 * 100, rel=1e-3)   # NOT vendor's 0.77 / 100x-wrong field


def test_vendor_percent_yield_is_not_mistaken_for_a_fraction(env):
    env[2].hist_days = 120                                                        # too short for a trailing-12m sum
    s = snap(env, "MSFT")
    assert s.dividend_yield_pct == pytest.approx(0.73, abs=1e-6)                  # trailingAnnualDividendYield(0.0073) * 100, not dividendYield(0.77)/100
    assert any("vendor field" in n for n in s.notes)


def test_dividend_cover_from_payout(env):
    s = snap(env, "AZN.L")
    assert s.dividend_cover == pytest.approx(1 / 0.58) and s.is_dividend_payer


def test_accumulating_etf_uses_override_and_has_no_analyst_data(env):
    s = snap(env, "VWRP.L")
    assert s.dividend_yield_pct == 1.7 and s.yield_is_estimate and s.ter_pct == 0.22
    assert s.target_mean is None and s.upside_pct is None and s.rating_label == "—" and not s.has_analyst_coverage
    assert s.kind == "etf" and s.currency == "GBP"


def test_technicals_and_52w_range(env):
    s = snap(env, "MSFT")
    assert s.sma50 and s.sma200 and s.sma50 > s.sma200 and s.trend == "UPTREND" and s.cross == "GOLDEN"
    assert s.rsi14 == pytest.approx(100.0)                                        # monotone rising series
    assert s.high_52w == pytest.approx(501.69) and 0 < s.range_pos <= 1 and s.drawdown_52w_pct == pytest.approx(0, abs=1e-6)
    assert s.vs_5y_avg_pct > 0 and s.avg_5y < s.price


def test_throttled_info_degrades_gracefully(env):
    env[2].infos["AZN.L"] = {}
    env[2].targets["AZN.L"] = {}
    s = snap(env, "AZN.L")
    assert s.upside_pct is None and s.rating_label == "—" and s.peg is None and s.price == pytest.approx(136.28)
    assert any("unavailable" in n for n in s.notes)
    assert s.rsi14 is not None                                                    # technicals still work


def test_screen_all_survives_one_bad_ticker(env):
    sc, mi, fake = env
    q = mi.quotes(["AZN.L", "MSFT"])
    orig = fake.history
    fake.history = lambda t, p="5y": (_ for _ in ()).throw(RuntimeError("boom")) if t == "MSFT" else orig(t, p)
    out = sc.screen_all(["AZN.L", "MSFT"], q)
    assert out["AZN.L"].upside_pct is not None
    assert out["MSFT"].price == pytest.approx(501.69) or out["MSFT"].notes   # degraded, not crashed


@pytest.mark.parametrize("mean,label", [(1.0, "Strong Buy"), (1.5, "Strong Buy"), (1.51, "Buy"), (2.5, "Buy"),
                                        (3.0, "Hold"), (3.51, "Sell"), (4.6, "Strong Sell"), (None, "—")])
def test_consensus_label_bands(mean, label):
    assert consensus_label(mean) == label


def test_mean_from_counts():
    assert mean_from_counts({"strongBuy": 2, "buy": 2}) == (pytest.approx(1.5), 4)
    assert mean_from_counts({}) == (None, 0) and mean_from_counts(None) == (None, 0)


def test_valuation_label_thresholds():
    r = HandbookRules()
    assert valuation_label(45, r) == "RICH" and valuation_label(40, r) == "RICH" and valuation_label(39.9, r) == "FAIR"
    assert valuation_label(-15, r) == "CHEAP" and valuation_label(None, r) == "—"


def test_trailing_yield_needs_enough_history():
    assert trailing_yield_pct(series(100, 1, 2), 2.0) is None
    assert trailing_yield_pct(series(300, 100, 100, {-10: 1.0}), 100.0) == pytest.approx(1.0)
    assert trailing_yield_pct(series(300, 100, 100, {-10: 1.0}), None) is None


def test_live_price_outside_cached_52w_range_is_clamped(env):
    env[2].PX["MSFT"] = 501.69
    sc, mi, fake = env
    q = mi.quotes(["MSFT"])["MSFT"]
    import dataclasses
    below = dataclasses.replace(q, price_native=300.0, price_gbp=300 / 1.3154)       # fresh print under every cached close
    s = sc.snapshot("MSFT", below)
    assert s.low_52w == pytest.approx(300.0) and s.range_pos == pytest.approx(0.0) and 0 <= s.range_pos <= 1
