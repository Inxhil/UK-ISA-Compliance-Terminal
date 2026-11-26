"""Portfolio valuation, look-through sector exposure, blended OCF / yield and the ISA Impact Simulator.

Weights are fractions of **total ISA value including ISA cash** (the handbook's 15% cap is measured against it).
GIA holdings are tracked separately — they exist only as Bed & ISA candidates.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from engine.analyst_screener import AnalystSnapshot
from engine.config import AccountCfg, ConfigError, WatchItem
from engine.isa_engine import GIAPosition
from engine.market_intel import Quote

CASH = "CASH"


@dataclass(frozen=True)
class HoldingRow:
    account: str
    ticker: str
    quantity: float
    avg_cost_gbp: float
    as_of: str = ""
    note: str = ""


def load_holdings_csv(path: str | Path, accounts: Mapping[str, AccountCfg] | None = None) -> list[HoldingRow]:
    """Read ``holdings.csv`` (account,ticker,quantity,avg_cost_gbp,as_of,note). Fails loudly on bad rows."""
    rows: list[HoldingRow] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rd = csv.DictReader(fh)
        need = {"account", "ticker", "quantity", "avg_cost_gbp"}
        if not rd.fieldnames or not need <= {h.strip() for h in rd.fieldnames}:
            raise ConfigError(f"{path}: need columns {sorted(need)}, found {rd.fieldnames}")
        for n, r in enumerate(rd, start=2):
            try:
                acc = r["account"].strip()
                if accounts is not None and acc not in accounts:
                    raise ValueError(f"unknown account '{acc}' (known: {sorted(accounts)})")
                qty, cost = float(r["quantity"]), float(r["avg_cost_gbp"])
                if qty < 0 or cost < 0:
                    raise ValueError("quantity and avg_cost_gbp must be >= 0")
                rows.append(HoldingRow(acc, r["ticker"].strip(), qty, cost, (r.get("as_of") or "").strip(),
                                       (r.get("note") or "").strip()))
            except (ValueError, KeyError) as exc:
                raise ConfigError(f"{path} line {n}: {exc}") from exc
    return rows


@dataclass(frozen=True)
class Position:
    ticker: str
    name: str
    kind: str
    quantity: float
    avg_cost_gbp: float
    price_gbp: float | None
    value_gbp: float
    cost_gbp: float
    weight: float
    target_weight: float
    sector: str | None
    ter_pct: float | None
    yield_pct: float | None
    accounts: Mapping[str, float]
    valued_at_cost: bool = False

    @property
    def pnl_gbp(self) -> float:
        return self.value_gbp - self.cost_gbp

    @property
    def pnl_pct(self) -> float | None:
        return (self.value_gbp / self.cost_gbp - 1) * 100 if self.cost_gbp else None

    @property
    def drift_pp(self) -> float:
        """Weight minus target, in percentage points (negative = underweight)."""
        return (self.weight - self.target_weight) * 100


@dataclass(frozen=True)
class Portfolio:
    positions: tuple[Position, ...]
    cash_gbp: float
    total_gbp: float
    invested_gbp: float
    cost_gbp: float
    sector_exposure: Mapping[str, float]       # fractions of total (incl. 'Cash')
    blended_ter_pct: float | None
    yield_pct: float | None                    # of invested value
    effective_n: float                         # 1 / HHI of position weights (diversification)
    gia: tuple[GIAPosition, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def pnl_gbp(self) -> float:
        return self.invested_gbp - self.cost_gbp

    @property
    def pnl_pct(self) -> float | None:
        return (self.invested_gbp / self.cost_gbp - 1) * 100 if self.cost_gbp else None

    @property
    def cash_weight(self) -> float:
        return self.cash_gbp / self.total_gbp if self.total_gbp else 0.0

    def get(self, ticker: str) -> Position | None:
        return next((p for p in self.positions if p.ticker == ticker), None)

    @property
    def values(self) -> dict[str, float]:
        return {p.ticker: p.value_gbp for p in self.positions}

    @property
    def top_sector(self) -> tuple[str, float] | None:
        real = {k: v for k, v in self.sector_exposure.items() if k != "Cash"}
        return max(real.items(), key=lambda kv: kv[1]) if real else None


def sector_split(ticker: str, w: WatchItem | None, snap: AnalystSnapshot | None) -> dict[str, float]:
    """Sector weights (sum 1) for one holding: ETF/trust look-through if configured, else its single sector."""
    if w and w.look_through:
        tot = sum(w.look_through.values())
        return {k: v / tot for k, v in w.look_through.items()} if tot > 0 else {"Unclassified": 1.0}
    sec = (w.sector if w and w.sector else None) or (snap.sector if snap else None)
    return {sec or "Unclassified": 1.0}


def _add(d: dict[str, float], k: str, v: float) -> None:
    d[k] = d.get(k, 0.0) + v


def build_portfolio(holdings: Sequence[HoldingRow], accounts: Mapping[str, AccountCfg], quotes: Mapping[str, Quote],
                    watch: Mapping[str, WatchItem], snaps: Mapping[str, AnalystSnapshot] | None = None) -> Portfolio:
    snaps = snaps or {}
    warnings: list[str] = []
    agg: dict[str, dict] = {}
    cash = 0.0
    gia_rows: list[HoldingRow] = []
    for h in holdings:
        acc = accounts.get(h.account)
        if acc is not None and not acc.is_isa:
            if h.ticker != CASH:
                gia_rows.append(h)
            continue
        if h.ticker == CASH:
            cash += h.quantity * (h.avg_cost_gbp or 1.0)
            continue
        a = agg.setdefault(h.ticker, {"qty": 0.0, "cost": 0.0, "acc": {}})
        a["qty"] += h.quantity
        a["cost"] += h.quantity * h.avg_cost_gbp
        a["acc"][h.account] = a["acc"].get(h.account, 0.0) + h.quantity

    rows: list[tuple] = []
    for t, a in agg.items():
        q = quotes.get(t)
        px = q.price_gbp if q else None
        at_cost = px is None
        if at_cost:
            px = a["cost"] / a["qty"] if a["qty"] else 0.0
            warnings.append(f"{t}: no GBP price — valued at cost")
        rows.append((t, a, px, a["qty"] * px, at_cost))
    invested = sum(r[3] for r in rows)
    total = invested + cash

    positions: list[Position] = []
    sector_x: dict[str, float] = {}
    ter_num = yld_num = 0.0
    ter_known = yld_known = 0.0
    for t, a, px, val, at_cost in rows:
        w, s = watch.get(t), snaps.get(t)
        ter = w.ter_pct if (w and w.ter_pct is not None) else (s.ter_pct if s else None)
        if ter is None and w and w.kind in ("stock",):
            ter = 0.0                                   # direct equities carry no ongoing charge
        yld = s.dividend_yield_pct if s else (w.yield_override_pct if w else None)
        positions.append(Position(
            ticker=t, name=(w.name if w else None) or (s.name if s else t), kind=(w.kind if w else "stock"),
            quantity=a["qty"], avg_cost_gbp=a["cost"] / a["qty"] if a["qty"] else 0.0, price_gbp=px, value_gbp=val,
            cost_gbp=a["cost"], weight=val / total if total else 0.0,
            target_weight=(w.target_weight_pct / 100.0 if w else 0.0), sector=(w.sector if w else None) or (s.sector if s else None),
            ter_pct=ter, yield_pct=yld, accounts=dict(a["acc"]), valued_at_cost=at_cost))
        for sec, f in sector_split(t, w, s).items():
            _add(sector_x, sec, val * f / total if total else 0.0)
        if ter is not None:
            ter_num += val * ter
            ter_known += val
        elif w and w.is_fund:
            warnings.append(f"{t}: OCF unknown — excluded from blended OCF (set ter_pct in config.yaml)")
        if yld is not None:
            yld_num += val * yld
            yld_known += val
    if cash:
        sector_x["Cash"] = cash / total if total else 0.0

    # watch-list names with a target but no holding still appear (weight 0) so the buy plan can fund them
    held = {p.ticker for p in positions}
    for t, w in watch.items():
        if t not in held and w.target_weight_pct > 0:
            positions.append(Position(t, w.name, w.kind, 0.0, 0.0, quotes[t].price_gbp if t in quotes else None, 0.0, 0.0,
                                      0.0, w.target_weight_pct / 100.0, w.sector, w.ter_pct, w.yield_override_pct, {}))
    positions.sort(key=lambda p: p.value_gbp, reverse=True)

    gia: list[GIAPosition] = []
    for h in gia_rows:
        px = quotes[h.ticker].price_gbp if h.ticker in quotes else None
        if px:
            gia.append(GIAPosition(h.ticker, h.quantity, h.avg_cost_gbp, px))
        else:
            warnings.append(f"GIA {h.ticker}: no GBP price — excluded from Bed & ISA")

    hhi = sum(p.weight ** 2 for p in positions)
    return Portfolio(
        positions=tuple(positions), cash_gbp=cash, total_gbp=total, invested_gbp=invested,
        cost_gbp=sum(p.cost_gbp for p in positions),
        sector_exposure=dict(sorted(sector_x.items(), key=lambda kv: -kv[1])),
        blended_ter_pct=(ter_num / ter_known) if ter_known else None,
        yield_pct=(yld_num / yld_known) if yld_known else None,
        effective_n=(1.0 / hhi) if hhi > 0 else 0.0, gia=tuple(gia), warnings=tuple(warnings))


# ============================================================================================
# ISA Impact Simulator
# ============================================================================================
@dataclass(frozen=True)
class SimMetrics:
    total_gbp: float
    ticker_weight: float
    blended_ter_pct: float | None
    yield_pct: float | None
    top_sector: tuple[str, float] | None
    sector_exposure: Mapping[str, float]
    cash_gbp: float


@dataclass(frozen=True)
class SimResult:
    ticker: str
    amount_gbp: float
    units: float | None
    funded_from: str
    before: SimMetrics
    after: SimMetrics
    breaches: tuple[str, ...]
    allowance_after_gbp: float | None


def _metrics(positions: Mapping[str, dict], cash: float, ticker: str, watch: Mapping[str, WatchItem],
             snaps: Mapping[str, AnalystSnapshot]) -> SimMetrics:
    invested = sum(p["value"] for p in positions.values())
    total = invested + cash
    ter_n = ter_d = y_n = y_d = 0.0
    sx: dict[str, float] = {}
    for t, p in positions.items():
        if p["ter"] is not None:
            ter_n += p["value"] * p["ter"]
            ter_d += p["value"]
        if p["yield"] is not None:
            y_n += p["value"] * p["yield"]
            y_d += p["value"]
        for sec, f in sector_split(t, watch.get(t), snaps.get(t)).items():
            _add(sx, sec, p["value"] * f / total if total else 0.0)
    if cash:
        sx["Cash"] = cash / total if total else 0.0
    real = {k: v for k, v in sx.items() if k != "Cash"}
    return SimMetrics(total, (positions.get(ticker, {"value": 0.0})["value"] / total) if total else 0.0,
                      (ter_n / ter_d) if ter_d else None, (y_n / y_d) if y_d else None,
                      max(real.items(), key=lambda kv: kv[1]) if real else None, dict(sorted(sx.items(), key=lambda kv: -kv[1])),
                      cash)


def simulate_buy(pf: Portfolio, ticker: str, amount_gbp: float, price_gbp: float | None,
                 watch: Mapping[str, WatchItem], snaps: Mapping[str, AnalystSnapshot] | None = None, *,
                 funded_from: str = "new", max_position_pct: float = 15.0, max_sector_pct: float = 40.0,
                 cap_applies_to: Sequence[str] = ("stock", "trust"), isa_headroom_gbp: float | None = None) -> SimResult:
    """What does buying ``amount_gbp`` of ``ticker`` do to yield, OCF, position weight and sector concentration?

    ``funded_from='new'`` = fresh subscription (uses ISA allowance, grows the pot); ``'cash'`` = existing ISA cash.
    """
    snaps = snaps or {}
    if amount_gbp < 0:
        raise ValueError("amount must be >= 0")
    base = {p.ticker: {"value": p.value_gbp, "ter": p.ter_pct, "yield": p.yield_pct} for p in pf.positions if p.value_gbp > 0}
    w, s = watch.get(ticker), snaps.get(ticker)
    amt = float(amount_gbp)
    if funded_from == "cash":
        amt = min(amt, pf.cash_gbp)
    before = _metrics(base, pf.cash_gbp, ticker, watch, snaps)
    after_pos = {k: dict(v) for k, v in base.items()}
    ter = (w.ter_pct if (w and w.ter_pct is not None) else (s.ter_pct if s else (0.0 if (w and w.kind == "stock") else None)))
    yld = s.dividend_yield_pct if s else (w.yield_override_pct if w else None)
    cur = after_pos.setdefault(ticker, {"value": 0.0, "ter": ter, "yield": yld})
    cur["value"] += amt
    after = _metrics(after_pos, pf.cash_gbp - (amt if funded_from == "cash" else 0.0), ticker, watch, snaps)

    breaches: list[str] = []
    kind = w.kind if w else "stock"
    if kind in cap_applies_to and after.ticker_weight * 100 > max_position_pct:
        breaches.append(f"position {ticker} {after.ticker_weight * 100:.1f}% > {max_position_pct:.0f}% cap (R-CAP-01)")
    if after.top_sector and after.top_sector[1] * 100 > max_sector_pct:
        breaches.append(f"sector {after.top_sector[0]} {after.top_sector[1] * 100:.1f}% > {max_sector_pct:.0f}% cap (R-CAP-02)")
    allowance_after = None
    if isa_headroom_gbp is not None and funded_from == "new":
        allowance_after = isa_headroom_gbp - amt
        if allowance_after < 0:
            breaches.append(f"exceeds ISA allowance by £{-allowance_after:,.2f}")
    return SimResult(ticker, amt, (amt / price_gbp) if price_gbp else None, funded_from, before, after, tuple(breaches),
                     allowance_after)
