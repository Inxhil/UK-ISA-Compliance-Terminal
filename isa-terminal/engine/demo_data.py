"""Synthetic DEMO market data — used only when the network is unavailable or ``--demo`` is passed.

**This is not market data.** Prices are a seeded random walk anchored on reference levels; analyst figures are
invented. Everything produced here is tagged ``source='demo'`` and the UI shows a permanent DEMO banner. News
headlines are prefixed ``[DEMO]``.  Deterministic: same ticker => same series (hash-seeded, not ``hash()``).
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

# ticker: (raw price, raw currency code, instrumentType, daily vol, annual drift, long name, sector, quoteType-ish extras)
_REF: dict[str, dict] = {
    "VWRP.L": dict(px=138.30, ccy="GBP", typ="ETF", vol=0.0085, drift=0.07, name="Vanguard FTSE All-World UCITS ETF"),
    "VUAG.L": dict(px=107.96, ccy="GBP", typ="ETF", vol=0.0105, drift=0.09, name="Vanguard S&P 500 UCITS ETF (Acc)"),
    "SMT.L": dict(px=1450.5, ccy="GBp", typ="EQUITY", vol=0.017, drift=0.08, name="Scottish Mortgage Investment Trust",
                  sector="Financial Services"),
    "AZN.L": dict(px=13628.0, ccy="GBp", typ="EQUITY", vol=0.013, drift=0.07, name="AstraZeneca PLC", sector="Healthcare",
                  div_pct=1.9, upside=0.12, rec=2.0, n=22, pe=17.5, peg=1.34, payout=0.58),
    "MSFT": dict(px=501.69, ccy="USD", typ="EQUITY", vol=0.015, drift=0.12, name="Microsoft Corporation",
                 sector="Technology", div_pct=0.77, upside=0.268, rec=1.4, n=57, pe=20.7, peg=1.50, payout=0.24),
    "NVDA": dict(px=152.19, ccy="USD", typ="EQUITY", vol=0.027, drift=0.20, name="NVIDIA Corporation",
                 sector="Technology", div_pct=0.03, upside=0.34, rec=1.3, n=60, pe=26.0, peg=1.1, payout=0.01),
    "ISF.L": dict(px=1030.0, ccy="GBp", typ="ETF", vol=0.0085, drift=0.05, name="iShares Core FTSE 100 UCITS ETF (Dist)",
                  div_pct=3.4),
    "ULVR.L": dict(px=4400.0, ccy="GBp", typ="EQUITY", vol=0.012, drift=0.04, name="Unilever PLC",
                   sector="Consumer Defensive", div_pct=3.2, upside=0.06, rec=2.6, n=18, pe=19.0, peg=2.3, payout=0.72),
    "SWDA.L": dict(px=10603.0, ccy="GBp", typ="ETF", vol=0.009, drift=0.08, name="iShares Core MSCI World UCITS ETF USD (Acc)"),
    "VERG.L": dict(px=50.44, ccy="GBP", typ="ETF", vol=0.009, drift=0.06, name="Vanguard FTSE Developed Europe ex UK UCITS ETF"),
    "VFEG.L": dict(px=60.32, ccy="GBP", typ="ETF", vol=0.010, drift=0.05, name="Vanguard FTSE Emerging Markets UCITS ETF"),
    "VHYL.L": dict(px=68.605, ccy="GBP", typ="ETF", vol=0.008, drift=0.06, name="Vanguard FTSE All-World High Dividend Yield UCITS ETF",
                   div_pct=3.4),
    "SHEL.L": dict(px=3595.5, ccy="GBp", typ="EQUITY", vol=0.014, drift=0.05, name="Shell plc", sector="Energy", div_pct=3.9,
                   upside=0.15, rec=2.0, n=20, pe=11.5, peg=1.2, payout=0.55),
    "HSBA.L": dict(px=1292.2, ccy="GBp", typ="EQUITY", vol=0.014, drift=0.09, name="HSBC Holdings plc", sector="Financial Services",
                   div_pct=5.1, upside=0.08, rec=2.3, n=20, pe=9.5, peg=1.4, payout=0.5),
    "REL.L": dict(px=2964.0, ccy="GBp", typ="EQUITY", vol=0.013, drift=0.04, name="RELX PLC", sector="Industrials", div_pct=1.9,
                  upside=0.18, rec=2.1, n=18, pe=25.0, peg=2.4, payout=0.5),
    "FGT.L": dict(px=869.0, ccy="GBp", typ="EQUITY", vol=0.011, drift=0.04, name="Finsbury Growth & Income Ord", div_pct=2.0),
    "JGGI.L": dict(px=586.5, ccy="GBp", typ="EQUITY", vol=0.011, drift=0.07, name="JPMorgan Global Growth & Income Ord", div_pct=3.0),
    "AAPL": dict(px=352.45, ccy="USD", typ="EQUITY", vol=0.016, drift=0.10, name="Apple Inc.", sector="Technology", div_pct=0.5,
                 upside=0.08, rec=1.9, n=50, pe=30.0, peg=2.5, payout=0.15),
    "GOOGL": dict(px=282.44, ccy="USD", typ="EQUITY", vol=0.017, drift=0.14, name="Alphabet Inc.", sector="Communication Services",
                  div_pct=0.4, upside=0.12, rec=1.6, n=55, pe=22.0, peg=1.5, payout=0.08),
    "BRK-B": dict(px=574.62, ccy="USD", typ="EQUITY", vol=0.010, drift=0.09, name="Berkshire Hathaway Inc.", sector="Financial Services",
                  upside=0.06, rec=2.2, n=12, pe=24.0, peg=2.0, payout=0.0),
    # not in the default watchlist: lets the on-demand ticker search be demonstrated/tested offline
    "BARC.L": dict(px=245.0, ccy="GBp", typ="EQUITY", vol=0.018, drift=0.08, name="Barclays PLC", sector="Financial Services", div_pct=2.9,
                   upside=0.10, rec=2.2, n=18, pe=8.5, peg=0.9, payout=0.25),
    "TSLA": dict(px=320.0, ccy="USD", typ="EQUITY", vol=0.032, drift=0.15, name="Tesla, Inc.", sector="Consumer Cyclical",
                 upside=0.05, rec=2.6, n=45, pe=90.0, peg=4.0, payout=0.0),
    "^FTSE": dict(px=10585.5, ccy="GBP", typ="INDEX", vol=0.0075, drift=0.05, name="FTSE 100"),
    "^GSPC": dict(px=7308.9, ccy="USD", typ="INDEX", vol=0.0095, drift=0.09, name="S&P 500"),
    "GBPUSD=X": dict(px=1.3154, ccy="USD", typ="CURRENCY", vol=0.0045, drift=0.0, name="GBP/USD"),
    "GBPEUR=X": dict(px=1.15, ccy="EUR", typ="CURRENCY", vol=0.004, drift=0.0, name="GBP/EUR"),
}
DEMO_GILT = (5.31, 5.28)   # latest, previous (illustrative)


def _seed(ticker: str) -> int:
    return int(hashlib.sha256(ticker.encode()).hexdigest()[:8], 16)


def _ref(ticker: str) -> dict:
    return _REF.get(ticker, dict(px=100.0, ccy="GBP", typ="EQUITY", vol=0.015, drift=0.06, name=ticker))


def _bdays(end: date, n: int) -> pd.DatetimeIndex:
    return pd.bdate_range(end=pd.Timestamp(end), periods=n)


class DemoProvider:
    """Same interface as the live provider; every method is local and instantaneous."""

    name = "demo"

    def __init__(self, today: date | None = None):
        self.today = today or date.today()
        self._hist: dict[str, pd.DataFrame] = {}

    # -- quotes --------------------------------------------------------------------------------
    def closes(self, tickers):
        from engine.market_intel import RawQuote
        out = {}
        for t in tickers:
            h = self.history(t, "5y")
            c = h["Close"].dropna()
            out[t] = RawQuote(float(c.iloc[-1]), float(c.iloc[-2]), datetime.combine(c.index[-1].date(), datetime.min.time()))
        return out

    def metadata(self, ticker):
        r = _ref(ticker)
        return {"currency": r["ccy"], "instrumentType": r["typ"], "longName": r["name"]}

    def history(self, ticker, period="5y"):
        if ticker in self._hist:
            return self._hist[ticker]
        r = _ref(ticker)
        n = 1305
        rng = np.random.default_rng(_seed(ticker))
        mu = r["drift"] / 252
        rets = rng.normal(mu - 0.5 * r["vol"] ** 2, r["vol"], n)
        path = np.exp(np.cumsum(rets))
        close = path / path[-1] * r["px"]                      # anchor the LAST close on the reference level
        idx = _bdays(self.today, n)
        op = np.concatenate([[close[0]], close[:-1]]) * (1 + rng.normal(0, r["vol"] * 0.25, n))
        hi = np.maximum(op, close) * (1 + np.abs(rng.normal(0, r["vol"] * 0.35, n)))
        lo = np.minimum(op, close) * (1 - np.abs(rng.normal(0, r["vol"] * 0.35, n)))
        vol = rng.integers(2_000_000, 9_000_000, n).astype(float)
        df = pd.DataFrame({"Open": op, "High": hi, "Low": lo, "Close": close, "Volume": vol}, index=idx)
        df["Dividends"] = 0.0
        if r.get("div_pct"):
            per = r["px"] * r["div_pct"] / 100 / 4          # quarterly, constant — illustrative
            for k in range(n - 1, 0, -63):
                df.iloc[k, df.columns.get_loc("Dividends")] = per * close[k] / r["px"]
        self._hist[ticker] = df
        return df

    # -- fundamentals / analysts -----------------------------------------------------------------
    def info(self, ticker):
        r = _ref(ticker)
        d = {"currency": r["ccy"], "quoteType": "ETF" if r["typ"] == "ETF" else "EQUITY",
             "longName": r["name"], "shortName": r["name"], "regularMarketPrice": r["px"]}
        if r.get("sector"):
            d["sector"] = r["sector"]
        if "pe" in r:
            d.update(forwardPE=r["pe"], trailingPE=r["pe"] * 1.2, trailingPegRatio=r["peg"], payoutRatio=r["payout"],
                     dividendYield=r.get("div_pct", 0.0), beta=1.1, marketCap=1e11)
        h = self.history(ticker)["Close"]
        d["fiftyTwoWeekHigh"] = float(h.iloc[-252:].max())
        d["fiftyTwoWeekLow"] = float(h.iloc[-252:].min())
        return d

    def analyst(self, ticker):
        r = _ref(ticker)
        if "upside" not in r:
            return {"targets": {}, "recs": []}
        px = r["px"]                                         # raw units, i.e. pence for GBp tickers — like Yahoo
        mean = px * (1 + r["upside"])
        n = r["n"]
        sb = int(n * max(0.05, (3.0 - r["rec"]) / 3.0 * 0.5))
        b = int(n * 0.4)
        sell = max(0, int(n * (r["rec"] - 2.0) * 0.15))
        hold = max(0, n - sb - b - sell)
        return {"targets": {"current": px, "high": mean * 1.35, "low": mean * 0.72, "mean": mean, "median": mean * 0.99},
                "recs": [{"period": "0m", "strongBuy": sb, "buy": b, "hold": hold, "sell": sell, "strongSell": 0}],
                "rec_mean": r["rec"], "n": n}

    def news(self, ticker):
        now = datetime.combine(self.today, datetime.min.time())
        return [
            {"title": f"[DEMO] Analysts update their view on {ticker}", "publisher": "Demo Wire",
             "link": "", "published": (now - timedelta(hours=3)).isoformat()},
            {"title": f"[DEMO] {ticker}: what the latest results mean for long-term holders", "publisher": "Demo Wire",
             "link": "", "published": (now - timedelta(days=1)).isoformat()},
            {"title": "[DEMO] Synthetic headline — no live news in demo mode", "publisher": "Demo Wire",
             "link": "", "published": (now - timedelta(days=2)).isoformat()},
        ]

    def gilt(self):
        return DEMO_GILT
