"""Analyst-consensus, fundamentals and technical-momentum screener.

Builds one :class:`AnalystSnapshot` per ticker from ``MarketIntel``. Design notes (from live probing of Yahoo):

* **Units.** Everything price-like is in the security's *major* currency (pounds, not pence). Analyst targets arrive in
  the vendor's raw unit and are divided by the same pence-divisor as the price, then *reconciled* against the live
  price (a ~100x ratio means a mixed-up unit and is corrected, with a note).
* **Ratios are unit-free** and computed in native currency (upside %, RSI, SMA state, vs-5Y-average).
* **Yield** = trailing-12-month dividends ÷ price from the *same* history call (same unit). Yahoo's ``dividendYield``
  is in percent and ``trailingAnnualDividendYield`` is wrong by 100x for some pence-quoted tickers, so both are
  fallbacks only. Accumulating funds use ``yield_override_pct`` from config (flagged as an estimate).
* **Missing != zero.** Absent data stays ``None`` and renders as ``—``.
* Yahoo throttles: ``.info`` can come back nearly empty, so each field has an independent fallback chain.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

import pandas as pd

from engine.config import HandbookRules, WatchItem
from engine.currency import reconcile_unit, split_currency
from engine.market_intel import MarketIntel, Quote
from engine.technicals import cross_state, drawdown_from_high, last_value, pct_vs_mean, rsi_wilder, sma, trend_label

RATING_KEYS = {"strong_buy": "Strong Buy", "buy": "Buy", "hold": "Hold", "underperform": "Sell", "sell": "Strong Sell",
               "strong_sell": "Strong Sell"}


def consensus_label(mean: float | None) -> str:
    """Yahoo scale: 1 = Strong Buy ... 5 = Strong Sell."""
    if mean is None:
        return "—"
    return ("Strong Buy" if mean <= 1.5 else "Buy" if mean <= 2.5 else "Hold" if mean <= 3.5
            else "Sell" if mean <= 4.5 else "Strong Sell")


def mean_from_counts(rec: Mapping[str, float] | None) -> tuple[float | None, int]:
    if not rec:
        return None, 0
    w = {"strongBuy": 1, "buy": 2, "hold": 3, "sell": 4, "strongSell": 5}
    n = sum(float(rec.get(k, 0) or 0) for k in w)
    if n <= 0:
        return None, 0
    return sum(w[k] * float(rec.get(k, 0) or 0) for k in w) / n, int(n)


@dataclass(frozen=True)
class AnalystSnapshot:
    ticker: str
    name: str
    kind: str
    currency: str | None
    price: float | None                    # native major units
    price_gbp: float | None
    day_change_pct: float | None
    # consensus
    rating_label: str = "—"
    rating_mean: float | None = None
    n_analysts: int | None = None
    rec_counts: Mapping[str, int] | None = None
    target_mean: float | None = None
    target_median: float | None = None
    target_high: float | None = None
    target_low: float | None = None
    upside_pct: float | None = None
    upside_high_pct: float | None = None
    upside_low_pct: float | None = None
    # fundamentals
    forward_pe: float | None = None
    trailing_pe: float | None = None
    peg: float | None = None
    dividend_yield_pct: float | None = None
    yield_is_estimate: bool = False
    payout_ratio: float | None = None
    dividend_cover: float | None = None
    market_cap: float | None = None
    beta: float | None = None
    sector: str | None = None
    ter_pct: float | None = None
    # range & technicals
    low_52w: float | None = None
    high_52w: float | None = None
    range_pos: float | None = None         # 0 = at 52w low, 1 = at 52w high
    drawdown_52w_pct: float | None = None
    sma50: float | None = None
    sma200: float | None = None
    rsi14: float | None = None
    trend: str = "N/A"
    cross: str | None = None
    vs_sma200_pct: float | None = None
    avg_5y: float | None = None
    vs_5y_avg_pct: float | None = None
    valuation_label: str = "—"
    notes: tuple[str, ...] = ()
    source: str = "none"

    @property
    def has_analyst_coverage(self) -> bool:
        return self.target_mean is not None or self.rating_mean is not None

    @property
    def is_dividend_payer(self) -> bool:
        return (self.dividend_yield_pct or 0) > 0


def valuation_label(vs_avg_pct: float | None, rules: HandbookRules) -> str:
    if vs_avg_pct is None:
        return "—"
    if vs_avg_pct >= rules.technicals.rich_vs_5y_avg_pct:
        return "RICH"
    if vs_avg_pct <= rules.technicals.cheap_vs_5y_avg_pct:
        return "CHEAP"
    return "FAIR"


def trailing_yield_pct(df: pd.DataFrame, price: float | None) -> float | None:
    """Trailing-12-month dividends ÷ price, both in the same (major) unit. ``None`` if history is too short."""
    if df is None or price is None or price <= 0 or "Dividends" not in df.columns or len(df) < 200:
        return None
    cutoff = df.index[-1] - pd.Timedelta(days=365)
    paid = float(df.loc[df.index > cutoff, "Dividends"].sum())
    return paid / price * 100.0


def _f(x) -> float | None:
    try:
        v = float(x)
        return None if v != v else v
    except (TypeError, ValueError):
        return None


class AnalystScreener:
    def __init__(self, mi: MarketIntel, rules: HandbookRules, watch: Mapping[str, WatchItem] | None = None):
        self.mi, self.rules, self.watch = mi, rules, dict(watch or {})

    def snapshot(self, ticker: str, quote: Quote | None = None) -> AnalystSnapshot:
        w = self.watch.get(ticker)
        notes: list[str] = []
        hist = self.mi.history(ticker)
        info_f = self.mi.info(ticker)
        info: Mapping = info_f.value or {}
        an_f = self.mi.analyst(ticker)
        analyst: Mapping = an_f.value or {}
        src = "demo" if "demo" in (info_f.source, an_f.source, hist.source if hist else "") else \
              "stale" if "stale" in (info_f.source, an_f.source, hist.source if hist else "") else \
              ("live" if hist else "none")

        # ---- price context -------------------------------------------------------------------
        close = hist.df["Close"] if hist is not None else pd.Series(dtype=float)
        ccy = (quote.currency if quote else None) or (hist.currency if hist else None)
        price = quote.price_native if quote and quote.price_native is not None else last_value(close)
        raw_ccy = quote.raw_currency if quote else None
        _, divisor = split_currency(raw_ccy, (w.price_unit if w else None)) if raw_ccy else (None, 1.0)
        if not info:
            notes.append("fundamentals/analyst fields unavailable (vendor throttled?)")

        # ---- technicals (native units) -----------------------------------------------------------
        sma50 = last_value(sma(close, 50)) if len(close) else None
        sma200 = last_value(sma(close, 200)) if len(close) else None
        rsi = last_value(rsi_wilder(close, 14)) if len(close) else None
        last = price if price is not None else last_value(close)
        yr = close.iloc[-252:] if len(close) else close
        lo52 = float(yr.min()) if len(yr) else None
        hi52 = float(yr.max()) if len(yr) else None
        if last is not None and lo52 is not None and hi52 is not None:   # live print may sit outside cached closes
            lo52, hi52 = min(lo52, last), max(hi52, last)
        rng_pos = ((last - lo52) / (hi52 - lo52)) if (last is not None and lo52 is not None and hi52 and hi52 > lo52) else None
        avg5 = float(close.mean()) if len(close) >= 250 else None
        vs5 = pct_vs_mean(close) if len(close) >= 250 and last is not None else None
        if vs5 is not None and last is not None and avg5:
            vs5 = (last / avg5 - 1.0) * 100.0           # use the LIVE price against the 5y mean
        if len(close) and len(close) < 250:
            notes.append(f"only {len(close)} daily bars — 5y/200d stats limited")

        # ---- consensus ---------------------------------------------------------------------------------
        recs = analyst.get("recs") or []
        rec0 = recs[0] if recs else None
        counts = ({k: int(rec0.get(k, 0) or 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}
                  if rec0 else None)
        c_mean, c_n = mean_from_counts(counts)
        r_mean = _f(info.get("recommendationMean")) or analyst.get("rec_mean") or c_mean
        n_an = int(info.get("numberOfAnalystOpinions") or analyst.get("n") or c_n or 0) or None
        label = consensus_label(r_mean) if r_mean is not None else RATING_KEYS.get(str(info.get("recommendationKey")), "—")

        # ---- price targets (vendor raw unit -> major -> reconcile against live price) -----------------------
        tg = dict(analyst.get("targets") or {})
        raw_t = {
            "mean": _f(tg.get("mean")) or _f(info.get("targetMeanPrice")),
            "median": _f(tg.get("median")) or _f(info.get("targetMedianPrice")),
            "high": _f(tg.get("high")) or _f(info.get("targetHighPrice")),
            "low": _f(tg.get("low")) or _f(info.get("targetLowPrice")),
        }
        tgt: dict[str, float | None] = {}
        for k, v in raw_t.items():
            if v is None:
                tgt[k] = None
                continue
            v_major = v / divisor if raw_ccy else v
            fixed, note = reconcile_unit(v_major, price)
            if note:
                notes.append(f"{k} target {note}")
            tgt[k] = fixed

        def up(t: float | None) -> float | None:
            return (t / price - 1.0) * 100.0 if (t is not None and price) else None

        # ---- fundamentals ------------------------------------------------------------------------------------
        y = trailing_yield_pct(hist.df, last) if hist else None
        y_est = False
        if w and w.yield_override_pct is not None and (w.distribution == "acc" or y in (None, 0.0)):
            y, y_est = w.yield_override_pct, True      # accumulating fund: Yahoo shows ~0% cash yield
        if y is None:
            ty = _f(info.get("trailingAnnualDividendYield"))
            dy = _f(info.get("dividendYield"))          # Yahoo now reports PERCENT (0.77 == 0.77%)
            y = ty * 100.0 if ty is not None else dy
            if y is not None:
                notes.append("yield from vendor field (history too short)")
        payout = _f(info.get("payoutRatio"))
        cover = (1.0 / payout) if (payout and payout > 0) else None
        ter = w.ter_pct if (w and w.ter_pct is not None) else (
            (_f(info.get("annualReportExpenseRatio")) or _f(info.get("netExpenseRatio")) or 0) * 100 or None)
        kind = w.kind if w else ("etf" if str(info.get("quoteType")) == "ETF" else "stock")

        return AnalystSnapshot(
            ticker=ticker, name=(w.name if w else None) or info.get("longName") or ticker, kind=kind, currency=ccy,
            price=price, price_gbp=quote.price_gbp if quote else None,
            day_change_pct=quote.change_pct if quote else None,
            rating_label=label, rating_mean=r_mean, n_analysts=n_an, rec_counts=counts,
            target_mean=tgt["mean"], target_median=tgt["median"], target_high=tgt["high"], target_low=tgt["low"],
            upside_pct=up(tgt["mean"]), upside_high_pct=up(tgt["high"]), upside_low_pct=up(tgt["low"]),
            forward_pe=_f(info.get("forwardPE")), trailing_pe=_f(info.get("trailingPE")),
            peg=_f(info.get("trailingPegRatio")) or _f(info.get("pegRatio")),
            dividend_yield_pct=y, yield_is_estimate=y_est, payout_ratio=payout, dividend_cover=cover,
            market_cap=_f(info.get("marketCap")), beta=_f(info.get("beta")),
            sector=(w.sector if w and w.sector else None) or info.get("sector"), ter_pct=ter,
            low_52w=lo52, high_52w=hi52, range_pos=rng_pos,
            drawdown_52w_pct=((last / hi52 - 1) * 100 if (last is not None and hi52) else None),
            sma50=sma50, sma200=sma200, rsi14=rsi, trend=trend_label(last, sma50, sma200),
            cross=cross_state(sma50, sma200),
            vs_sma200_pct=((last / sma200 - 1) * 100 if (last is not None and sma200) else None),
            avg_5y=avg5, vs_5y_avg_pct=vs5, valuation_label=valuation_label(vs5, self.rules),
            notes=tuple(notes), source=src)

    def screen_all(self, tickers: Sequence[str], quotes: Mapping[str, Quote],
                   progress: Callable[[int, int, str], None] | None = None) -> dict[str, AnalystSnapshot]:
        out: dict[str, AnalystSnapshot] = {}
        for i, t in enumerate(tickers, 1):
            if progress:
                progress(i, len(tickers), t)
            try:
                out[t] = self.snapshot(t, quotes.get(t))
            except Exception as exc:                # noqa: BLE001 - one bad ticker must not blank the whole grid
                q = quotes.get(t)
                out[t] = AnalystSnapshot(t, t, "stock", q.currency if q else None, q.price_native if q else None,
                                         q.price_gbp if q else None, q.change_pct if q else None,
                                         notes=(f"screen failed: {type(exc).__name__}: {exc}",))
        return out
