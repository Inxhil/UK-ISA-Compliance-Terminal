import pytest

from engine.analyst_screener import AnalystSnapshot
from engine.config import WatchItem
from engine.income import TAX_NOTE, dividend_forecast, security_income
from engine.portfolio import Position, Portfolio


def pos(t, value, yld=None):
    return Position(t, t, "stock", 1, 1, 1, value, value, 0.1, 0.1, None, None, yld, {})


def pf(*positions):
    return Portfolio(tuple(positions), 0.0, sum(p.value_gbp for p in positions), sum(p.value_gbp for p in positions), 0, {}, None, None, 1.0)


def snap(t, y, est=False):
    return AnalystSnapshot(t, t, "stock", "GBP", 1, 1, 0, dividend_yield_pct=y, yield_is_estimate=est)


WATCH = {"AZN.L": WatchItem("AZN.L", "AZN", "stock"), "VWRP.L": WatchItem("VWRP.L", "VWRP", "etf", distribution="acc"),
         "ISF.L": WatchItem("ISF.L", "ISF", "etf", distribution="dist")}


def test_cash_income_is_value_times_trailing_yield():
    f = dividend_forecast(pf(pos("AZN.L", 5000), pos("ISF.L", 10000)), {"AZN.L": snap("AZN.L", 2.0), "ISF.L": snap("ISF.L", 3.5)}, WATCH)
    assert f.cash_gbp == pytest.approx(100 + 350) and f.reinvested_gbp == 0 and f.cash_per_month == pytest.approx(37.5)
    assert [l.ticker for l in f.lines] == ["ISF.L", "AZN.L"]                      # biggest contributor first


def test_accumulating_funds_are_reinvested_not_cash_income():
    f = dividend_forecast(pf(pos("VWRP.L", 20000), pos("AZN.L", 5000)), {"VWRP.L": snap("VWRP.L", 1.7, est=True), "AZN.L": snap("AZN.L", 2.0)}, WATCH)
    assert f.cash_gbp == pytest.approx(100) and f.reinvested_gbp == pytest.approx(340) and f.total_gbp == pytest.approx(440)
    assert next(l for l in f.lines if l.ticker == "VWRP.L").estimate and not next(l for l in f.lines if l.ticker == "VWRP.L").cash


def test_unknown_yield_is_excluded_not_counted_as_zero():
    f = dividend_forecast(pf(pos("AZN.L", 5000), pos("ISF.L", 1000)), {"AZN.L": snap("AZN.L", None)}, WATCH)
    assert set(f.unknown) == {"AZN.L", "ISF.L"} and f.total_gbp == 0
    assert all(l.annual_gbp is None for l in f.lines)


def test_falls_back_to_position_yield_and_ignores_unheld():
    f = dividend_forecast(pf(pos("AZN.L", 1000, yld=4.0), pos("ISF.L", 0)), {}, WATCH)
    assert f.cash_gbp == pytest.approx(40) and len(f.lines) == 1


def test_security_income_per_1000_and_holding():
    si = security_income("AZN.L", snap("AZN.L", 2.5), 4000.0, WATCH["AZN.L"])
    assert si.per_1000_gbp == pytest.approx(25) and si.held_annual_gbp == pytest.approx(100) and si.pays_cash
    acc = security_income("VWRP.L", snap("VWRP.L", 1.7, est=True), 0.0, WATCH["VWRP.L"])
    assert not acc.pays_cash and acc.estimate and acc.held_annual_gbp == 0
    none = security_income("X", None, 100.0, None)
    assert none.yield_pct is None and none.per_1000_gbp is None and none.held_annual_gbp is None


def test_tax_note_makes_no_rate_claims():
    assert "tax-free inside an isa" in TAX_NOTE.lower() and "£500" in TAX_NOTE and "%" not in TAX_NOTE
