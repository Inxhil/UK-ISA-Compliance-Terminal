"""Projected annual dividend income inside the ISA (pure arithmetic on already-computed numbers).

Basis: **trailing** 12-month yield × current value — a projection, not a promise: dividends are cut, raised and skipped.
Accumulating funds don't pay cash out; their income is reinvested inside the fund, so it is reported separately and is
*not* counted as cash income. Inside an ISA dividends are tax-free; outside it, dividends above the £500 allowance are taxed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from engine.analyst_screener import AnalystSnapshot
from engine.config import WatchItem
from engine.portfolio import Portfolio

TAX_NOTE = "Tax-free inside an ISA. Outside one, dividends above the £500 dividend allowance are taxable."


@dataclass(frozen=True)
class IncomeLine:
    ticker: str
    value_gbp: float
    yield_pct: float | None
    annual_gbp: float | None           # None = yield unknown
    cash: bool                         # False = accumulating fund (reinvested inside the fund)
    estimate: bool = False             # yield came from a config estimate (accumulating funds)


@dataclass(frozen=True)
class IncomeForecast:
    lines: tuple[IncomeLine, ...]
    cash_gbp: float                    # paid out as cash
    reinvested_gbp: float              # earned inside accumulating funds
    unknown: tuple[str, ...]           # holdings with no yield data (excluded, never counted as 0)

    @property
    def total_gbp(self) -> float:
        return self.cash_gbp + self.reinvested_gbp

    @property
    def cash_per_month(self) -> float:
        return self.cash_gbp / 12.0


def dividend_forecast(pf: Portfolio, snaps: Mapping[str, AnalystSnapshot], watch: Mapping[str, WatchItem]) -> IncomeForecast:
    lines: list[IncomeLine] = []
    cash = reinv = 0.0
    unknown: list[str] = []
    for p in pf.positions:
        if p.value_gbp <= 0:
            continue
        s, w = snaps.get(p.ticker), watch.get(p.ticker)
        y = s.dividend_yield_pct if s and s.dividend_yield_pct is not None else p.yield_pct
        acc = bool(w and w.distribution == "acc")
        if y is None:
            unknown.append(p.ticker)
            lines.append(IncomeLine(p.ticker, p.value_gbp, None, None, not acc))
            continue
        amt = p.value_gbp * y / 100.0
        lines.append(IncomeLine(p.ticker, p.value_gbp, y, amt, not acc, bool(s and s.yield_is_estimate)))
        if acc:
            reinv += amt
        else:
            cash += amt
    lines.sort(key=lambda l: -(l.annual_gbp or 0))
    return IncomeForecast(tuple(lines), cash, reinv, tuple(unknown))


@dataclass(frozen=True)
class SecurityIncome:
    ticker: str
    yield_pct: float | None
    held_value_gbp: float
    held_annual_gbp: float | None
    per_1000_gbp: float | None
    pays_cash: bool
    estimate: bool


def security_income(ticker: str, snap: AnalystSnapshot | None, held_value_gbp: float, watch: WatchItem | None) -> SecurityIncome:
    y = snap.dividend_yield_pct if snap else None
    acc = bool(watch and watch.distribution == "acc")
    return SecurityIncome(ticker, y, held_value_gbp, None if y is None else held_value_gbp * y / 100.0,
                          None if y is None else 1000.0 * y / 100.0, not acc, bool(snap and snap.yield_is_estimate))
