"""Live / near-live market data with caching, graceful degradation and unit normalisation.

All network I/O in the project goes through this module. Principles (see CLAUDE.md):

* ``GBp`` -> ``GBP`` (÷100) for pence-quoted LSE tickers, decided by the *reported* currency, never the ``.L`` suffix;
  USD (and any other) prices are converted with live ``GBP{CCY}=X`` rates (``gbp = foreign / rate``);
* indices / FX pairs are points & rates, never converted;
* per-field TTL cache on disk, **stale-on-error**, a circuit breaker, and an explicit DEMO fallback that can never
  be mistaken for live data (``source == 'demo'``);
* yfinance's threaded downloader is avoided (it can raise ``database is locked`` on its SQLite tz cache) and any
  ticker missing from a batch is retried individually.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from engine.cache import DiskCache
from engine.currency import fx_symbol, normalise_price, price_to_gbp, split_currency
from engine.demo_data import DemoProvider
from engine.models import DataUnavailable, RawQuote, unit_jump          # re-exported: ``from engine.market_intel import RawQuote``
from engine import news as newsmod

log = logging.getLogger("isa.market_intel")

BOE_URL = ("https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
           "?csv.x=yes&Datefrom={frm}&Dateto=now&SeriesCodes=IUDMNPY&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N")
UA = "Mozilla/5.0 (compatible; isa-command-centre/1.0; personal-use)"
NON_SECURITY_TYPES = {"INDEX", "CURRENCY", "FUTURE", "CRYPTOCURRENCY"}
DEFAULT_TTL = {"quote": 90, "fx": 90, "history": 21600, "info": 43200, "news": 900, "gilt": 3600, "meta": 30 * 86400}
INFO_KEYS = (
    "currency", "financialCurrency", "quoteType", "sector", "industry", "longName", "shortName", "recommendationKey",
    "recommendationMean", "numberOfAnalystOpinions", "targetMeanPrice", "targetHighPrice", "targetLowPrice",
    "targetMedianPrice", "currentPrice", "regularMarketPrice", "forwardPE", "trailingPE", "trailingPegRatio",
    "pegRatio", "dividendYield", "trailingAnnualDividendYield", "dividendRate", "payoutRatio", "fiftyTwoWeekLow",
    "fiftyTwoWeekHigh", "marketCap", "beta", "annualReportExpenseRatio", "netExpenseRatio", "yield", "trailingEps",
    "forwardEps", "earningsGrowth", "revenueGrowth", "profitMargins", "returnOnEquity", "debtToEquity", "country",
    "totalAssets", "category", "fundFamily",
)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# =============================================================================================
# Data classes
# =============================================================================================
@dataclass(frozen=True)
class Quote:
    ticker: str
    price_gbp: float | None            # None for indices/FX pairs, or when currency/FX is unknown (never guessed)
    prev_close_gbp: float | None
    change_pct: float | None           # unit-free; computed in native units
    price_native: float | None         # major units (pounds / dollars / index points / FX rate)
    prev_native: float | None
    currency: str | None               # major currency code, e.g. 'GBP', 'USD'
    raw_currency: str | None           # as reported by the vendor, e.g. 'GBp'
    fx_rate: float | None              # units of `currency` per 1 GBP used for conversion (None for GBP)
    asof: datetime | None
    source: str                        # 'live' | 'cache' | 'stale' | 'demo'
    is_non_security: bool = False
    note: str | None = None
    provider: str | None = None        # TD | FH | YF | ST (which feed supplied this price)


@dataclass(frozen=True)
class TapeItem:
    label: str
    value: float | None
    change: float | None               # % for index/fx, basis points for yields
    change_unit: str                   # '%' | 'bp'
    kind: str                          # index | fx | yield
    source: str
    decimals: int = 2


@dataclass(frozen=True)
class Fetched:
    value: Any
    source: str                        # live | cache | stale | demo | none
    age_s: float = 0.0


@dataclass(frozen=True)
class HistoryResult:
    df: pd.DataFrame                   # OHLC + Dividends in MAJOR currency units (pence already converted)
    currency: str | None               # major currency of the series
    source: str
    is_non_security: bool = False
    provider: str | None = None


SOURCE_ORDER = ("TD", "FH", "YF", "ST", "BoE")


@dataclass(frozen=True)
class NewsFeed:
    items: list
    source: str                        # live | stale | demo | none


@dataclass
class DataStatus:
    label: str = "OFFLINE"             # LIVE | STALE | DEMO | OFFLINE
    last_live: datetime | None = None
    errors: list[str] = field(default_factory=list)
    breaker_open: bool = False
    mode: str = "auto"
    sources: tuple[str, ...] = ()      # providers that supplied data this cycle, e.g. ('TD', 'FH', 'YF')

    @property
    def badge(self) -> str:
        """``LIVE · TD + FH + YF`` — the label shown in the status bar."""
        return self.label + (" · " + " + ".join(self.sources) if self.sources else "")


# =============================================================================================
# Pure parsers (unit-tested without the network)
# =============================================================================================
def split_download(df: pd.DataFrame | None, tickers: Sequence[str]) -> dict[str, pd.Series]:
    """Per-ticker Close series from ``yf.download(..., group_by='ticker')`` (multi- or single-level columns)."""
    out: dict[str, pd.Series] = {}
    if df is None or len(df) == 0:
        return out
    if isinstance(df.columns, pd.MultiIndex):
        lvl0 = set(df.columns.get_level_values(0))
        lvl1 = set(df.columns.get_level_values(1))
        for t in tickers:
            try:
                if t in lvl0:
                    s = df[t]["Close"]
                elif t in lvl1:                      # group_by='column' layout
                    s = df["Close"][t]
                else:
                    continue
                s = s.dropna()
                if len(s):
                    out[t] = s
            except KeyError:
                continue
    elif "Close" in df.columns and len(tickers) >= 1:
        s = df["Close"].dropna()
        if len(s):
            out[tickers[0]] = s
    return out


def parse_news(items: Sequence[Mapping] | None, limit: int = 8) -> list[dict]:
    """Normalise yfinance news (legacy flat shape *and* the newer ``{'content': {...}}`` shape)."""
    out: list[dict] = []
    for it in items or []:
        if not isinstance(it, Mapping):
            continue
        c = it.get("content") if isinstance(it.get("content"), Mapping) else it
        title = c.get("title") or it.get("title")
        if not title:
            continue
        prov = c.get("provider")
        publisher = (prov.get("displayName") if isinstance(prov, Mapping) else None) or c.get("publisher") or ""
        url = ""
        for k in ("canonicalUrl", "clickThroughUrl"):
            if isinstance(c.get(k), Mapping) and c[k].get("url"):
                url = c[k]["url"]
                break
        url = url or c.get("link") or ""
        pub = c.get("pubDate") or c.get("displayTime")
        if pub is None and c.get("providerPublishTime"):
            pub = datetime.fromtimestamp(int(c["providerPublishTime"]), tz=timezone.utc).isoformat()
        out.append({"title": str(title).strip(), "publisher": str(publisher), "link": str(url), "published": pub,
                    "ts": newsmod.parse_ts(pub), "summary": str(c.get("summary") or c.get("description") or ""), "related": [],
                    "scope": "company", "category": ""})
        if len(out) >= limit:
            break
    return out


def parse_boe_csv(text: str) -> list[tuple[date, float]]:
    """Bank of England IADB CSV (``DATE,IUDMNPY`` then ``10 Nov 2026,5.1906``) -> [(date, value)] ascending."""
    rows: list[tuple[date, float]] = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            d, m, y = parts[0].split()
            rows.append((date(int(y), _MONTHS.index(m[:3].title()) + 1, int(d)), float(parts[1])))
        except (ValueError, IndexError):
            continue                     # header / blank / footer lines
    rows.sort(key=lambda r: r[0])
    return rows


def _boe_date(d: date) -> str:
    return f"{d.day:02d}/{_MONTHS[d.month - 1]}/{d.year}"      # locale-independent month names


# =============================================================================================
# Live provider (yfinance) + Bank of England gilt yield
# =============================================================================================
def fetch_gilt() -> tuple[float, float | None]:
    """UK 10Y nominal par yield (BoE IADB series IUDMNPY): ``(latest, previous)``. Keyless; one polite retry."""
    import requests
    frm = _boe_date(date.today() - timedelta(days=25))
    rows: list[tuple[date, float]] = []
    for attempt in range(2):                    # the BoE occasionally answers a burst with an HTML error page
        r = requests.get(BOE_URL.format(frm=frm), headers={"User-Agent": UA}, timeout=12)
        r.raise_for_status()
        rows = parse_boe_csv(r.text)
        if rows:
            break
        if attempt == 0:
            time.sleep(2.0)
    if not rows:
        raise DataUnavailable("BoE returned no gilt rows (HTML error page — transient throttling?)")
    return rows[-1][1], (rows[-2][1] if len(rows) > 1 else None)


class LiveProvider:
    """yfinance. The v0.1 provider, kept as the verified fallback (and the only provider when no API keys are set)."""
    name = "live"

    def __init__(self, cache_dir: Path | None = None):
        import yfinance as yf                      # imported lazily so tests / demo never need the network stack
        self.yf = yf
        self._lock = threading.RLock()             # one yfinance call at a time (SQLite tz cache is not thread-safe)
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        logging.getLogger("yfinance").propagate = False
        if cache_dir is not None and hasattr(yf, "set_tz_cache_location"):
            p = Path(cache_dir) / "yf"
            p.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(Exception):
                yf.set_tz_cache_location(str(p))

    # -- helpers -----------------------------------------------------------------------------
    def _single_close(self, t: str) -> pd.Series | None:
        try:
            h = self.yf.Ticker(t).history(period="5d", interval="1d", auto_adjust=False)
            s = h["Close"].dropna() if h is not None and len(h) else None
            return s if s is not None and len(s) else None
        except Exception as exc:                   # noqa: BLE001 - vendor raises anything
            log.debug("single close failed %s: %s", t, exc)
            return None

    # -- provider interface --------------------------------------------------------------------
    def closes(self, tickers: Sequence[str]) -> dict[str, RawQuote]:
        out: dict[str, RawQuote] = {}
        with self._lock:
            try:
                df = self.yf.download(list(tickers), period="5d", interval="1d", group_by="ticker",
                                      auto_adjust=False, progress=False, threads=False)
            except Exception as exc:               # noqa: BLE001
                log.debug("batch download failed: %s", exc)
                df = None
            series = split_download(df, tickers)
            for t in tickers:
                s = series.get(t)
                if s is None or len(s) < 1:
                    s = self._single_close(t)       # retry tickers dropped from the batch ('database is locked' etc.)
                if s is None or len(s) < 1:
                    continue
                idx = s.index[-1]
                asof = idx.to_pydatetime().replace(tzinfo=None) if hasattr(idx, "to_pydatetime") else None
                out[t] = RawQuote(float(s.iloc[-1]), float(s.iloc[-2]) if len(s) > 1 else None, asof, None, "YF", None)
        if not out:
            raise DataUnavailable("no quotes returned (network blocked or rate-limited?)")
        return out

    def metadata(self, ticker: str) -> dict:
        with self._lock:
            md = self.yf.Ticker(ticker).history_metadata or {}
        if not md.get("currency"):
            raise DataUnavailable(f"{ticker}: no currency in chart metadata")
        return {k: md.get(k) for k in ("currency", "instrumentType", "longName", "exchangeTimezoneName")}

    def history(self, ticker: str, period: str = "5y") -> pd.DataFrame:
        with self._lock:
            h = self.yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False, actions=True)
        if h is None or len(h) == 0:
            raise DataUnavailable(f"{ticker}: empty history")
        h = h.copy()
        h.index = pd.DatetimeIndex(h.index).tz_localize(None).normalize() if h.index.tz is not None \
            else pd.DatetimeIndex(h.index).normalize()
        if "Dividends" not in h.columns:
            h["Dividends"] = 0.0
        h = h[["Open", "High", "Low", "Close", "Volume", "Dividends"]].dropna(subset=["Close"])
        h = h[~h.index.duplicated(keep="last")]
        h.attrs["provider"] = "YF"
        return h

    def info(self, ticker: str) -> dict:
        with self._lock:
            raw = self.yf.Ticker(ticker).info or {}
        return {k: raw[k] for k in INFO_KEYS if k in raw and raw[k] is not None}

    def analyst(self, ticker: str) -> dict:
        res: dict[str, Any] = {"targets": {}, "recs": []}
        with self._lock:
            t = self.yf.Ticker(ticker)
            try:
                tg = t.analyst_price_targets
                res["targets"] = {k: float(v) for k, v in (tg or {}).items() if v is not None}
            except Exception as exc:               # noqa: BLE001
                log.debug("price targets failed %s: %s", ticker, exc)
            try:
                rc = t.recommendations
                if rc is not None and len(rc):
                    res["recs"] = rc.head(4).to_dict("records")
            except Exception as exc:               # noqa: BLE001
                log.debug("recommendations failed %s: %s", ticker, exc)
        return res

    def news(self, ticker: str) -> list[dict]:
        items: list[dict] = []
        with self._lock:
            try:
                items = parse_news(self.yf.Ticker(ticker).news)
            except Exception as exc:               # noqa: BLE001
                log.debug("yfinance news failed %s: %s", ticker, exc)
        if not items:
            raise DataUnavailable(f"{ticker}: no news")
        return items[:10]

    def gilt(self) -> tuple[float, float | None] | None:
        return fetch_gilt()


# =============================================================================================
# MarketIntel facade
# =============================================================================================
class MarketIntel:
    """Cached, normalised market data. Thread-safe; call from worker threads, never the UI thread."""

    BREAKER_THRESHOLD = 3
    BREAKER_COOLOFF_S = 120

    def __init__(self, cache_dir: str | Path, *, ttl: Mapping[str, float] | None = None, mode: str = "auto",
                 price_units: Mapping[str, str] | None = None, live: Any | None = None, demo: Any | None = None,
                 finnhub_key: str | None = None, td_key: str | None = None, clock: Callable[[], float] = time.time):
        if mode not in ("auto", "live", "demo"):
            raise ValueError("mode must be auto | live | demo")
        self.cache = DiskCache(cache_dir)
        self.ttl = {**DEFAULT_TTL, **(ttl or {})}
        self.mode = mode
        self.price_units = dict(price_units or {})
        self._clock = clock
        self._live = live
        self._live_factory_dir = Path(cache_dir)
        self._finnhub_key = finnhub_key
        self._td_key = td_key
        self._sources: set[str] = set()
        self._demo = demo or DemoProvider()
        self._fail_streak = 0
        self._breaker_until = 0.0
        self._errors: list[str] = []
        self._cycle: set[str] = set()
        self._demo_active = False          # True only when the whole feed is down (quotes fell back to demo)
        self._last_live: datetime | None = None
        self._lock = threading.RLock()

    # -- plumbing ----------------------------------------------------------------------------
    @property
    def live(self):
        if self._live is None:
            if self._td_key or self._finnhub_key:        # v0.2 provider chain; no keys => plain yfinance (v0.1 behaviour)
                from engine.providers import build_chain
                self._live = build_chain(self._live_factory_dir, td_key=self._td_key, finnhub_key=self._finnhub_key)
            else:
                self._live = LiveProvider(self._live_factory_dir)
        return self._live

    def _err(self, msg: str) -> None:
        with self._lock:
            self._errors = (self._errors + [f"{datetime.now():%H:%M:%S} {msg[:160]}"])[-6:]

    def breaker_open(self) -> bool:
        return self._clock() < self._breaker_until

    def _live_call(self, fn: Callable[[], Any], *, critical: bool, what: str) -> Any:
        """Run a live provider call. Critical failures feed the circuit breaker; soft ones don't."""
        if self.mode == "demo":
            raise DataUnavailable("demo mode")
        if self.breaker_open():
            raise DataUnavailable("circuit breaker open (cooling off after repeated failures)")
        try:
            val = fn()
        except Exception as exc:                   # noqa: BLE001
            self._err(f"{what}: {type(exc).__name__}: {exc}")
            if critical:
                with self._lock:
                    self._fail_streak += 1
                    if self._fail_streak >= self.BREAKER_THRESHOLD:
                        self._breaker_until = self._clock() + self.BREAKER_COOLOFF_S
                        self._fail_streak = 0
            raise
        if critical:
            with self._lock:
                self._fail_streak = 0
                self._last_live = datetime.now()
        return val

    def _cached(self, key: str, ttl: float | Callable[[Any], float], live_fn: Callable[[], Any],
                demo_fn: Callable[[], Any], *, critical: bool = False, demo_ok: bool | None = None,
                encode: Callable = lambda v: v, decode: Callable = lambda v: v) -> Fetched:
        """Fresh cache -> live fetch -> stale cache -> (DEMO, only if the whole feed is down) -> nothing.

        A *soft* endpoint failing while quotes are live must NOT be papered over with fabricated demo values, so
        ``demo_ok`` defaults to "is the feed as a whole in demo mode this cycle".
        """
        if self.mode == "demo":
            self._cycle.add("demo")
            return Fetched(demo_fn(), "demo")
        hit = self.cache.get(key, 0)
        if hit is not None:
            t = ttl(hit.value) if callable(ttl) else ttl
            if hit.age_s <= t:
                self._cycle.add("cache")
                return Fetched(decode(hit.value), "cache", hit.age_s)
        try:
            val = self._live_call(live_fn, critical=critical, what=key.split(":")[0])
            self.cache.set(key, encode(val))
            self._cycle.add("live")
            return Fetched(val, "live")
        except Exception:                          # noqa: BLE001 - already recorded in _live_call
            pass
        if hit is not None:
            self._cycle.add("stale")
            return Fetched(decode(hit.value), "stale", hit.age_s)
        allow_demo = self._demo_active if demo_ok is None else demo_ok
        if self.mode == "auto" and allow_demo:
            try:
                v = demo_fn()
                self._cycle.add("demo")
                return Fetched(v, "demo")
            except Exception:                      # noqa: BLE001
                pass
        return Fetched(None, "none")

    def begin_cycle(self) -> None:
        """Call at the start of each refresh so ``status()`` reflects only that cycle."""
        self._cycle = set()
        self._demo_active = False
        self._sources = set()
        used = getattr(self._live, "used", None)
        if used is not None:
            used.clear()

    def _note_source(self, name: str | None) -> None:
        if name:
            self._sources.add(name)

    def status(self) -> DataStatus:
        c = self._cycle
        label = ("DEMO" if "demo" in c else "STALE" if "stale" in c else "LIVE" if c & {"live", "cache"} else "OFFLINE")
        used = set(self._sources) | set(getattr(self._live, "used", ()) or ())
        sources = () if label == "DEMO" else tuple(x for x in SOURCE_ORDER if x in used)
        errs = list(self._errors) + [f"{x}" for x in (getattr(self._live, "log", None) or [])[-2:]]
        return DataStatus(label, self._last_live, errs[-6:], self.breaker_open(), self.mode, sources)

    # -- metadata ----------------------------------------------------------------------------
    def meta(self, ticker: str, demo_ok: bool | None = None) -> dict:
        """Currency / instrument type. Falls back to demo metadata only for tickers whose *quote* is demo — a live
        price must never be scaled by invented metadata. Unknown => ``{}`` (price is then left unconverted)."""
        f = self._cached(f"meta:{ticker}", self.ttl["meta"], lambda: self.live.metadata(ticker),
                         lambda: self._demo.metadata(ticker), demo_ok=demo_ok)
        return f.value or {}

    def _is_non_security(self, meta: Mapping) -> bool:
        return (meta.get("instrumentType") or "").upper() in NON_SECURITY_TYPES

    # -- quotes ------------------------------------------------------------------------------
    def _raw_closes(self, symbols: Sequence[str]) -> dict[str, tuple[RawQuote, str]]:
        res: dict[str, tuple[RawQuote, str]] = {}
        missing: list[str] = []
        stale: dict[str, RawQuote] = {}
        if self.mode != "demo":
            for s in symbols:
                hit = self.cache.get(f"close:{s}", 0)
                if hit is not None and hit.age_s <= self.ttl["quote"]:
                    res[s] = (RawQuote.from_json(hit.value), "cache")
                    self._note_source(res[s][0].provider)
                else:
                    missing.append(s)
                    if hit is not None:
                        stale[s] = RawQuote.from_json(hit.value)
        else:
            missing = list(symbols)
        if missing:
            got: dict[str, RawQuote] = {}
            try:
                if self.mode == "demo":
                    raise DataUnavailable("demo")
                got = self._live_call(lambda: self.live.closes(missing), critical=True, what="quotes")
                for s, rq in list(got.items()):
                    jump = unit_jump(rq.last, stale[s].last) if s in stale else None
                    if jump:                           # tripwire: a provider flipped pence <-> pounds. Never use or cache it.
                        self._err(f"{s}: {rq.provider or 'provider'} price jumped x{jump:g} vs last good price — rejected (unit mismatch?)")
                        del got[s]
                        continue
                    self.cache.set(f"close:{s}", rq.to_json())
                    res[s] = (rq, "live")
                    self._note_source(rq.provider)
            except Exception:                      # noqa: BLE001
                pass
            for s in (m for m in missing if m not in got):
                if s in stale:
                    res[s] = (stale[s], "stale")
                    self._note_source(stale[s].provider)
        still = [s for s in symbols if s not in res]
        # DEMO only when the *whole* feed is down: never splice a demo rate/price into otherwise-live data
        if still and (self.mode == "demo" or (self.mode == "auto" and not res)):
            for s, rq in self._demo.closes(still).items():
                res[s] = (rq, "demo")
        for _, src in res.values():
            self._cycle.add(src)
            if src == "demo":
                self._demo_active = True
        if self.mode == "demo":
            self._demo_active = True
        return res

    def quotes(self, tickers: Sequence[str], fx_ccys: Sequence[str] = ("USD",)) -> dict[str, Quote]:
        tickers = list(dict.fromkeys(tickers))
        base_fx = {c: fx_symbol(c) for c in fx_ccys if c != "GBP"}
        raw = self._raw_closes(tickers + [s for s in base_fx.values() if s and s not in tickers])
        # metadata (currency) per ticker; demo metadata is only ever used for demo quotes
        metas = {t: ({"currency": raw[t][0].currency, "instrumentType": raw[t][0].kind or "EQUITY"} if raw[t][0].currency
                      else {"currency": None, "instrumentType": raw[t][0].kind} if raw[t][0].kind in NON_SECURITY_TYPES
                      else self.meta(t, demo_ok=(raw[t][1] == "demo")))
                 for t in tickers if t in raw}
        needed = {split_currency(m.get("currency"), self.price_units.get(t))[0] for t, m in metas.items()}
        extra = {c: fx_symbol(c) for c in needed if c and c != "GBP" and c not in base_fx}
        if extra:                                   # a currency we did not anticipate (e.g. EUR-quoted ETF)
            raw.update(self._raw_closes([s for s in extra.values() if s]))
        fx_syms = {**base_fx, **extra}
        fx = {c: raw[s][0].last for c, s in fx_syms.items() if s in raw and raw[s][0].last > 0}
        out: dict[str, Quote] = {}
        for t in tickers:
            if t not in raw:
                continue
            rq, src = raw[t]
            m = metas.get(t, {})
            non_sec = self._is_non_security(m)
            raw_ccy = m.get("currency")
            ovr = self.price_units.get(t)
            major, _ = split_currency(raw_ccy, ovr)
            # indices / FX pairs are points & rates: no currency is needed (Twelve Data's forex quote carries none)
            nat = normalise_price(rq.last, raw_ccy, override=ovr, is_index=non_sec) if (raw_ccy or non_sec) else None
            prv = normalise_price(rq.prev, raw_ccy, override=ovr, is_index=non_sec) if (raw_ccy or non_sec) else None
            gbp = None if non_sec or not raw_ccy else price_to_gbp(rq.last, raw_ccy, fx, override=ovr)
            prv_gbp = None if non_sec or not raw_ccy else price_to_gbp(rq.prev, raw_ccy, fx, override=ovr)
            note = None
            if not raw_ccy and not non_sec:
                note = "currency unknown — price not converted"        # never guess pence vs pounds
            elif not non_sec and gbp is None:
                note = f"no {major}/GBP FX rate — GBP price unavailable"
            out[t] = Quote(t, gbp, prv_gbp, ((rq.last / rq.prev - 1) * 100) if rq.prev else None, nat, prv, major,
                           raw_ccy, None if major == "GBP" else fx.get(major or ""), rq.asof, src, non_sec, note, rq.provider)
        return out

    def fx_rates(self, ccys: Sequence[str] = ("USD",)) -> dict[str, float]:
        syms = {c: fx_symbol(c) for c in ccys if c != "GBP"}
        raw = self._raw_closes([s for s in syms.values() if s])
        return {c: raw[s][0].last for c, s in syms.items() if s in raw}

    # -- history -----------------------------------------------------------------------------
    def history(self, ticker: str, period: str = "5y") -> HistoryResult | None:
        f = self._cached(f"hist:{ticker}:{period}", self.ttl["history"], lambda: self.live.history(ticker, period),
                         lambda: self._demo.history(ticker, period), critical=True)
        if f.value is None or len(f.value) == 0:
            return None
        df = f.value.copy()
        prov = df.attrs.get("provider")
        self._note_source(prov)
        m = self.meta(ticker) if not df.attrs.get("currency") else {"currency": df.attrs["currency"]}
        if (df.attrs.get("type") or "").upper() in NON_SECURITY_TYPES:
            m["instrumentType"] = df.attrs["type"]
        non_sec = self._is_non_security(m)
        ovr = self.price_units.get(ticker)
        major, div = split_currency(m.get("currency"), ovr)
        if not non_sec and div != 1.0:
            for c in ("Open", "High", "Low", "Close", "Dividends"):
                if c in df.columns:
                    df[c] = df[c] / div
        return HistoryResult(df, major, f.source, non_sec, prov)

    # -- fundamentals / analysts / news ----------------------------------------------------------
    def info(self, ticker: str) -> Fetched:
        # an almost-empty .info (rate-limited crumb) is retried after 5 min rather than cached for 12 h
        return self._cached(f"info:{ticker}", lambda v: self.ttl["info"] if len(v or {}) >= 10 else 300,
                            lambda: self.live.info(ticker), lambda: self._demo.info(ticker))

    def analyst(self, ticker: str) -> Fetched:
        return self._cached(f"analyst:{ticker}", self.ttl["info"], lambda: self.live.analyst(ticker),
                            lambda: self._demo.analyst(ticker))

    def news(self, ticker: str) -> Fetched:
        return self._cached(f"news:{ticker}", self.ttl["news"], lambda: self.live.news(ticker),
                            lambda: self._demo.news(ticker))

    def market_news(self) -> Fetched:
        return self._cached("news:macro", self.ttl["news"], lambda: self.live.market_news(),
                            lambda: [{"title": "[DEMO] Synthetic macro headline — no live news in demo mode", "publisher": "Demo Wire",
                                      "link": "", "ts": time.time() - 7200, "summary": "Demo data only.", "related": [],
                                      "scope": "macro", "category": "demo"}])

    def news_feed(self, tickers: Sequence[str], targets: Sequence[newsmod.TagTarget] = (), *, macro: bool = True) -> "NewsFeed":
        """Company news for every ticker + macro headlines, normalised, de-duplicated, newest first."""
        items: list[newsmod.NewsItem] = []
        sources: set[str] = set()
        for t in tickers:
            f = self.news(t)
            sources.add(f.source)
            for d in (f.value or []):
                d = dict(d)
                if not d.get("related"):
                    d["related"] = [t]
                items.append(newsmod.to_item(d, targets))
        if macro:
            f = self.market_news()
            sources.add(f.source)
            items += [newsmod.to_item(d, targets) for d in (f.value or [])]
        merged = newsmod.dedupe(items)
        merged.sort(key=lambda i: -(i.ts or 0))
        return NewsFeed(merged, "demo" if "demo" in sources else "live" if sources & {"live", "cache"} else "stale" if "stale" in sources else "none")

    def search(self, query: str) -> list[dict]:
        """Symbol search (Finnhub) for the on-demand ticker bar. Empty list when unavailable — never raises."""
        q = query.strip()
        if not q or self.mode == "demo" or not hasattr(self.live, "search"):
            return []
        f = self._cached(f"search:{q.lower()}", 3600, lambda: self.live.search(q), lambda: [], demo_ok=False)
        return list(f.value or [])

    # -- tape --------------------------------------------------------------------------------
    def gilt_yield(self) -> TapeItem:
        f = self._cached("gilt:10y", self.ttl["gilt"], lambda: list(self.live.gilt() or ()),
                         lambda: list(self._demo.gilt()))
        v = f.value
        if not v:
            return TapeItem("UK 10Y GILT", None, None, "bp", "yield", "none")
        latest, prev = v[0], (v[1] if len(v) > 1 else None)
        return TapeItem("UK 10Y GILT", float(latest), None if prev is None else (latest - prev) * 100.0, "bp", "yield",
                        f.source, 3)

    def tape(self, spec: Sequence[Mapping[str, str]]) -> list[TapeItem]:
        syms = [s["symbol"] for s in spec]
        quotes = self.quotes(syms) if syms else {}
        items: list[TapeItem] = []
        for s in spec:
            q = quotes.get(s["symbol"])
            if q is None:
                items.append(TapeItem(s["label"], None, None, "%", s.get("kind", "index"), "none"))
                continue
            dec = 4 if s.get("kind") == "fx" else 2
            items.append(TapeItem(s["label"], q.price_native, q.change_pct, "%", s.get("kind", "index"), q.source, dec))
        items.append(self.gilt_yield())
        return items
