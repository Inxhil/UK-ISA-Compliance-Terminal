from pathlib import Path

import pytest

from engine.analyst_screener import AnalystSnapshot
from engine.config import ConfigError, load_config
from engine.market_intel import Quote
from engine.portfolio import HoldingRow, build_portfolio, load_holdings_csv, sector_split, simulate_buy

ROOT = Path(__file__).resolve().parent.parent
PX = {"VWRP.L": 140.0, "VUAG.L": 100.0, "SMT.L": 15.0, "AZN.L": 140.0, "MSFT": 400.0, "NVDA": 120.0, "ISF.L": 10.0, "ULVR.L": 48.0}


def q(t, px):
    return Quote(t, px, px, 0.0, px, px, "GBP", "GBP", None, None, "live")


@pytest.fixture(scope="module")
def cfg():
    return load_config(ROOT / "config.yaml")


@pytest.fixture
def pf(cfg):
    rows = load_holdings_csv(cfg.holdings_csv, cfg.accounts)
    return build_portfolio(rows, cfg.accounts, {t: q(t, p) for t, p in PX.items()}, {w.ticker: w for w in cfg.watchlist})


def test_sample_csv_loads(cfg):
    rows = load_holdings_csv(cfg.holdings_csv, cfg.accounts)
    assert len(rows) == 12 and {r.account for r in rows} == {"T212_ISA", "HL_ISA", "HL_LISA", "VG_ISA", "HL_GIA"}


def test_bad_holdings_fail_loudly(tmp_path, cfg):
    p = tmp_path / "h.csv"
    p.write_text("account,ticker,quantity,avg_cost_gbp\nNOPE,VWRP.L,1,1\n")
    with pytest.raises(ConfigError, match="line 2.*unknown account"):
        load_holdings_csv(p, cfg.accounts)
    p.write_text("account,ticker,quantity,avg_cost_gbp\nHL_ISA,VWRP.L,-1,1\n")
    with pytest.raises(ConfigError, match=">= 0"):
        load_holdings_csv(p, cfg.accounts)
    p.write_text("account,ticker\nHL_ISA,VWRP.L\n")
    with pytest.raises(ConfigError, match="need columns"):
        load_holdings_csv(p, cfg.accounts)


def test_aggregation_across_accounts_and_totals(pf):
    v = pf.get("VWRP.L")
    assert v.quantity == 158 and v.value_gbp == pytest.approx(158 * 140)
    assert v.avg_cost_gbp == pytest.approx((60 * 107.2 + 58 * 111.4 + 40 * 116.5) / 158)
    assert set(v.accounts) == {"T212_ISA", "HL_ISA", "HL_LISA"}
    invested = 158 * 140 + 104 * 100 + 615 * 15 + 36 * 140 + 14 * 400 + 92 * 120
    assert pf.invested_gbp == pytest.approx(invested) and pf.cash_gbp == 1900 and pf.total_gbp == pytest.approx(invested + 1900)
    assert sum(p.weight for p in pf.positions) + pf.cash_weight == pytest.approx(1.0)


def test_gia_is_excluded_from_isa_weights_and_kept_for_bed_and_isa(pf):
    assert {g.ticker for g in pf.gia} == {"MSFT", "SMT.L"}
    msft_gia = next(g for g in pf.gia if g.ticker == "MSFT")
    assert msft_gia.quantity == 12 and msft_gia.unrealised_gain == pytest.approx((400 - 268) * 12)
    assert pf.get("MSFT").quantity == 14                       # ISA MSFT only, not 26


def test_pnl(pf):
    nv = pf.get("NVDA")
    assert nv.pnl_gbp == pytest.approx(92 * (120 - 47.3)) and nv.pnl_pct == pytest.approx((120 / 47.3 - 1) * 100)


def test_drift_vs_target(pf):
    nv = pf.get("NVDA")
    assert nv.target_weight == pytest.approx(0.06) and nv.drift_pp == pytest.approx((nv.weight - 0.06) * 100)
    assert nv.drift_pp > 0 and pf.get("AZN.L").drift_pp < 0


def test_sector_exposure_uses_look_through_and_sums_to_one(pf):
    sx = pf.sector_exposure
    assert sum(sx.values()) == pytest.approx(1.0) and sx["Cash"] == pytest.approx(pf.cash_weight)
    tech_direct = (14 * 400 + 92 * 120) / pf.total_gbp
    assert sx["Technology"] > tech_direct + 0.05                # + VWRP / VUAG / SMT constituents
    assert pf.top_sector[0] == "Technology" and "Cash" != pf.top_sector[0]
    assert sx["Healthcare"] > 36 * 140 / pf.total_gbp           # AZN direct + fund look-through


def test_sector_split_variants(cfg):
    w = {x.ticker: x for x in cfg.watchlist}
    assert sector_split("AZN.L", w["AZN.L"], None) == {"Healthcare": 1.0}
    assert sum(sector_split("VWRP.L", w["VWRP.L"], None).values()) == pytest.approx(1.0)
    assert sector_split("???", None, None) == {"Unclassified": 1.0}
    snap = AnalystSnapshot("X", "X", "stock", "GBP", 1, 1, 0, sector="Utilities")
    assert sector_split("X", None, snap) == {"Utilities": 1.0}


def test_blended_ter_and_yield(pf):
    # funds: VWRP 0.22, VUAG 0.07, SMT 0.31 ; stocks 0.  Weighted over invested value.
    v = {"VWRP.L": 158 * 140, "VUAG.L": 104 * 100, "SMT.L": 615 * 15}
    inv = pf.invested_gbp
    expect = (v["VWRP.L"] * 0.22 + v["VUAG.L"] * 0.07 + v["SMT.L"] * 0.31) / inv
    assert pf.blended_ter_pct == pytest.approx(expect)
    assert pf.yield_pct is not None and 0 < pf.yield_pct < 3    # from config yield overrides (no snapshots passed)


def test_no_price_means_valued_at_cost_with_warning(cfg):
    rows = [HoldingRow("T212_ISA", "AZN.L", 10, 100.0)]
    p = build_portfolio(rows, cfg.accounts, {}, {w.ticker: w for w in cfg.watchlist})
    assert p.get("AZN.L").valued_at_cost and p.invested_gbp == 1000 and any("valued at cost" in w for w in p.warnings)


def test_unheld_target_names_are_listed_with_zero_weight(cfg):
    p = build_portfolio([HoldingRow("T212_ISA", "VWRP.L", 1, 100)], cfg.accounts, {t: q(t, x) for t, x in PX.items()},
                        {w.ticker: w for w in cfg.watchlist})
    nv = p.get("NVDA")
    assert nv is not None and nv.weight == 0 and nv.target_weight == pytest.approx(0.06) and p.get("ISF.L") is None


def test_effective_n_between_1_and_n(pf):
    assert 1 < pf.effective_n < 6


# ----------------------------------------------------------------------------- simulator
W = None


@pytest.fixture
def watch(cfg):
    return {w.ticker: w for w in cfg.watchlist}


def test_simulate_new_money_into_core_etf_lowers_cost_and_concentration(pf, watch):
    r = simulate_buy(pf, "VUAG.L", 2000, 100.0, watch, funded_from="new", isa_headroom_gbp=7500)
    assert r.units == pytest.approx(20) and r.amount_gbp == 2000
    assert r.after.total_gbp == pytest.approx(pf.total_gbp + 2000)
    assert r.after.blended_ter_pct < r.before.blended_ter_pct          # 0.07% fund dilutes OCF
    assert r.after.ticker_weight > r.before.ticker_weight
    assert r.allowance_after_gbp == pytest.approx(5500)
    # VUAG is a broad ETF => exempt from the single-position cap, but it pushes Technology look-through over 40%
    assert not any("R-CAP-01" in b for b in r.breaches) and any("Technology" in b and "R-CAP-02" in b for b in r.breaches)


def test_simulate_from_cash_leaves_total_unchanged(pf, watch):
    r = simulate_buy(pf, "AZN.L", 1500, 140.0, watch, funded_from="cash")
    assert r.after.total_gbp == pytest.approx(r.before.total_gbp) and r.after.cash_gbp == pytest.approx(400)
    r2 = simulate_buy(pf, "AZN.L", 99999, 140.0, watch, funded_from="cash")
    assert r2.amount_gbp == pf.cash_gbp                                   # cannot spend cash you do not have


def test_simulate_flags_position_cap_and_allowance_breach(pf, watch):
    r = simulate_buy(pf, "NVDA", 5000, 120.0, watch, isa_headroom_gbp=3000)
    assert any("R-CAP-01" in b for b in r.breaches) and any("allowance" in b for b in r.breaches)
    assert r.after.ticker_weight * 100 > 15


def test_simulate_yield_moves_toward_the_bought_assets_yield(pf, watch):
    r = simulate_buy(pf, "ISF.L", 5000, 10.0, watch)
    assert r.after.ticker_weight > 0 and r.after.sector_exposure["Financial Services"] > r.before.sector_exposure["Financial Services"]


def test_simulate_rejects_negative(pf, watch):
    with pytest.raises(ValueError):
        simulate_buy(pf, "VWRP.L", -1, 100.0, watch)


def test_cap_applies_to_stocks_not_core_etfs_unless_configured(pf, watch):
    big_etf = simulate_buy(pf, "VWRP.L", 30000, 140.0, watch)                 # VWRP would be ~50% of the pot
    assert not any("R-CAP-01" in b for b in big_etf.breaches)
    strict = simulate_buy(pf, "VWRP.L", 30000, 140.0, watch, cap_applies_to=("stock", "trust", "etf"))
    assert any("R-CAP-01" in b for b in strict.breaches)
