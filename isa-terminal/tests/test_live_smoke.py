"""Opt-in smoke test against the REAL providers.   RUN_LIVE=1 python -m pytest -q tests/test_live_smoke.py

Skipped by default (the normal suite is fully offline). Uses a throw-away cache so it always hits the network.
Asserts *plausibility*, not exact prices.
"""
import os

import pytest

from engine.config import load_config, load_handbook_rules
from engine.analyst_screener import AnalystScreener
from engine.market_intel import MarketIntel

pytestmark = pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="set RUN_LIVE=1 to run live network tests")


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    cfg = load_config()
    mi = MarketIntel(tmp_path_factory.mktemp("livecache"), mode="live", price_units={w.ticker: w.price_unit for w in cfg.watchlist})
    mi.begin_cycle()
    return cfg, mi, mi.quotes(cfg.tickers)


def test_pence_and_pound_tickers_land_on_sane_gbp_prices(live):
    _, mi, q = live
    assert mi.status().label == "LIVE"
    # AZN.L is quoted in pence by Yahoo (GBp) -> must be pounds here; VWRP.L is already pounds
    assert q["AZN.L"].raw_currency == "GBp" and 20 < q["AZN.L"].price_gbp < 400
    assert q["VWRP.L"].raw_currency == "GBP" and 50 < q["VWRP.L"].price_gbp < 400
    assert q["SMT.L"].raw_currency == "GBp" and 2 < q["SMT.L"].price_gbp < 60


def test_usd_stock_converted_with_live_fx(live):
    _, _, q = live
    assert q["MSFT"].currency == "USD" and 1.0 < q["MSFT"].fx_rate < 2.0
    assert q["MSFT"].price_gbp == pytest.approx(q["MSFT"].price_native / q["MSFT"].fx_rate)


def test_tape_has_indices_fx_and_gilt(live):
    cfg, mi, _ = live
    items = {t.label: t for t in mi.tape(list(cfg.tape))}
    assert items["FTSE 100"].value > 1000 and items["S&P 500"].value > 1000 and 0.8 < items["GBP/USD"].value < 2.0
    assert items["UK 10Y GILT"].value is None or 0 < items["UK 10Y GILT"].value < 15   # BoE can throttle: n/a is acceptable


def test_screener_end_to_end_on_a_us_stock_and_an_etf(live):
    cfg, mi, q = live
    sc = AnalystScreener(mi, load_handbook_rules(), {w.ticker: w for w in cfg.watchlist})
    msft, vwrp = sc.snapshot("MSFT", q["MSFT"]), sc.snapshot("VWRP.L", q["VWRP.L"])
    assert msft.rsi14 is not None and 0 <= msft.rsi14 <= 100 and msft.sma200 is not None
    assert msft.upside_pct is None or -60 < msft.upside_pct < 200            # unit sanity: never a 100x error
    assert msft.dividend_yield_pct is None or 0 <= msft.dividend_yield_pct < 5
    assert vwrp.kind == "etf" and vwrp.target_mean is None and vwrp.dividend_yield_pct == 1.7


# =============================================================================================== v0.2 keyed providers
# Uses the keys in .env (or the environment). Skipped when absent. Spends a handful of Twelve Data credits (free tier: 8/min).
import json as _json

from engine.config import ROOT as _ROOT, load_env
from engine.models import DataUnavailable
from engine.providers import PlanLimit, build_chain


@pytest.fixture(scope="module")
def keyed(tmp_path_factory):
    load_env(_ROOT / ".env")
    td, fh = os.environ.get("TWELVEDATA_API_KEY"), os.environ.get("FINNHUB_API_KEY")
    if not (td or fh):
        pytest.skip("no TWELVEDATA_API_KEY / FINNHUB_API_KEY configured")
    d = tmp_path_factory.mktemp("keyed")
    chain = build_chain(d, td_key=td, finnhub_key=fh)
    return chain, d, bool(td), bool(fh)


def test_finnhub_us_quote_recommendations_search_and_news(keyed):
    chain, _, _, has_fh = keyed
    if not has_fh:
        pytest.skip("no Finnhub key")
    q = chain.fh.quote("MSFT")
    assert q.currency == "USD" and q.provider == "FH" and 20 < q.last < 5000 and q.prev
    assert chain.fh.recommendations("MSFT")[0]["buy"] >= 0
    assert any(r["symbol"] == "BARC.L" for r in chain.fh.search("BARC"))
    news = chain.news("MSFT")
    assert news and news[0]["link"].startswith("http") and news[0]["related"][0] == "MSFT"
    macro = chain.market_news()
    assert len(macro) >= 5 and all(m["scope"] == "macro" for m in macro)
    with pytest.raises(DataUnavailable):
        chain.fh.quote("AZN.L")                                               # UK symbols are refused locally, never sent


def test_finnhub_fundamentals_units(keyed):
    chain, _, _, has_fh = keyed
    if not has_fh:
        pytest.skip("no Finnhub key")
    info = chain.info("MSFT")
    assert info["currency"] == "USD" and info["marketCap"] > 1e10
    assert 0 <= info.get("dividendYield", 0.5) < 5 and 0 <= info.get("payoutRatio", 0.2) < 2          # percent / fraction conventions


def test_twelvedata_forex_and_us_history(keyed):
    chain, _, has_td, _ = keyed
    if not has_td:
        pytest.skip("no Twelve Data key")
    q, errs = chain.td.quotes(["GBPUSD=X"])
    assert 0.8 < q["GBPUSD=X"].last < 2.0 and q["GBPUSD=X"].kind == "CURRENCY", errs
    df = chain.td.history("MSFT")
    assert len(df) > 200 and df.attrs["currency"] == "USD" and "Dividends" not in df.columns


def test_twelvedata_lse_is_either_cleanly_refused_or_served_in_pence_that_normalises(keyed):
    chain, d, has_td, _ = keyed
    if not has_td:
        pytest.skip("no Twelve Data key")
    mi = MarketIntel(d / "lse", live=chain, mode="live")
    mi.begin_cycle()
    q = mi.quotes(["AZN.L"])["AZN.L"]
    assert 20 < q.price_gbp < 400, f"AZN.L normalised to £{q.price_gbp} via {q.provider} ({q.raw_currency})"      # never 100x off, whichever source
    if chain.td.lse_blocked():
        assert q.provider != "TD" and chain.td.supports("AZN.L") is False       # free plan: refused once, remembered, YF served it
    else:
        assert q.provider == "TD" and q.raw_currency in ("GBX", "GBp", "GBP")


def test_full_chain_through_marketintel_reports_its_sources(keyed):
    chain, d, has_td, has_fh = keyed
    mi = MarketIntel(d / "mi", live=chain, mode="live")
    mi.begin_cycle()
    q = mi.quotes(["MSFT", "VWRP.L", "GBPUSD=X"])
    assert q["MSFT"].price_gbp == pytest.approx(q["MSFT"].price_native / q["GBPUSD=X"].price_native, rel=1e-6)
    assert 50 < q["VWRP.L"].price_gbp < 400 and q["GBPUSD=X"].price_gbp is None and q["GBPUSD=X"].price_native > 0     # FX shown as a rate
    st = mi.status()
    assert st.label == "LIVE" and (not has_fh or "FH" in st.sources) and "YF" in st.sources
    assert "·" in st.badge
