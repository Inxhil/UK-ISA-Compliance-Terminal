"""Market-data providers: Twelve Data, Finnhub, Stooq — plus the :class:`ProviderChain` that routes between them.

Network I/O lives here and in ``market_intel.py`` only. Everything that *interprets* a response is a pure function
(``parse_td_*``, ``parse_fh_*``, ``finnhub_info``, ``parse_stooq_csv``) and is unit-tested against recorded fixtures.

Routing (verified against the real APIs on a free-tier key, Nov 2026):

=================  ==========================================  =================================================
what               first choice                                fallbacks
=================  ==========================================  =================================================
US quotes          Finnhub ``/quote`` (real-time, 60/min)      Twelve Data batch ``/quote`` -> yfinance -> Stooq
US daily history   Twelve Data ``/time_series`` (1 credit)     yfinance -> Stooq
LSE (``.L``) data  Twelve Data (**paid plans only**)           yfinance -> Stooq   (free key: refused, remembered 24h)
FX (GBPUSD=X …)    Twelve Data forex ``/quote``                yfinance
analyst consensus  Finnhub ``/stock/recommendation`` (US)      yfinance
price targets      (Finnhub: premium)                          yfinance
US fundamentals    Finnhub ``/stock/profile2`` + ``/metric``   yfinance
news               Finnhub ``/company-news`` + ``/news``       yfinance ``Ticker.news``
=================  ==========================================  =================================================

Credentials are sent in HTTP **headers** (never in URLs) and are never logged. Currency handling is *not* done here:
providers report the raw vendor currency and ``engine.currency`` normalises it (``GBX``/``GBp`` pence -> GBP pounds).
"""
from __future__ import annotations

import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from engine import news as newsmod
from engine.cache import DiskCache
from engine.models import DataUnavailable, RawQuote
from engine.ratelimit import CreditBudget

UA = "Mozilla/5.0 (compatible; isa-command-centre/0.2; personal-use)"
HttpGet = Callable[[str, Mapping[str, Any] | None, Mapping[str, str] | None, float], tuple[int, Any]]


class PlanLimit(DataUnavailable):
    """The provider refuses this symbol/endpoint on the current plan (not a transient error)."""


def default_http(url: str, params: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None,
                 timeout: float = 15.0) -> tuple[int, Any]:
    import requests
    r = requests.get(url, params=dict(params or {}), headers=dict(headers or {}), timeout=timeout)
    try:
        body = r.json()
    except ValueError:
        body = {"_raw": r.text[:300]}
    return r.status_code, body


# =============================================================================================
# Symbol mapping (pure)
# =============================================================================================
def is_non_security(t: str) -> bool:
    return t.startswith("^") or t.endswith("=X") or t.endswith("=F")


def is_lse(t: str) -> bool:
    return t.upper().endswith(".L")


def is_us_ticker(t: str) -> bool:
    """Plain US listing (``MSFT``, ``BRK-B``). Anything with an exchange suffix (``.L`` …), index or FX pair is not."""
    return bool(t) and not is_non_security(t) and "." not in t


def normalise_user_ticker(raw: str) -> str:
    """Accept what people type: ``barc.l`` · ``LON:BARC`` · ``BARC:LSE`` · ``brk.b`` · `` aapl `` -> Yahoo-style symbol."""
    s = raw.strip().upper().replace(" ", "")
    m = re.fullmatch(r"(?:LON|LSE):([A-Z0-9.\-]+)", s) or re.fullmatch(r"([A-Z0-9.\-]+):(?:LON|LSE)", s)
    if m:
        return m.group(1).removesuffix(".L") + ".L"
    if re.fullmatch(r"[A-Z]+\.[AB]", s):            # BRK.B -> BRK-B (US class shares); UK '.L' is handled above/untouched
        return s.replace(".", "-")
    return s


def to_twelvedata_symbol(t: str) -> str | None:
    m = re.fullmatch(r"([A-Z]{3})([A-Z]{3})=X", t)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    if t.startswith("^") or t.endswith("=F") or t.endswith("=X"):
        return None
    if is_lse(t):
        return t[:-2].replace("-", ".") + ":LSE"
    if "." in t:
        return None
    return t.replace("-", ".")


def to_finnhub_symbol(t: str) -> str | None:
    return t.replace("-", ".") if is_us_ticker(t) else None


def to_stooq_symbol(t: str) -> str | None:
    if is_non_security(t):
        return None
    if is_lse(t):
        return t[:-2].lower() + ".uk"
    if "." in t:
        return None
    return t.lower() + ".us"


# =============================================================================================
# Twelve Data — pure parsers
# =============================================================================================
def _f(x: Any) -> float | None:
    try:
        v = float(x)
        return None if v != v else v
    except (TypeError, ValueError):
        return None


def td_error(j: Any) -> tuple[int, str] | None:
    """``(code, message)`` if this is a top-level Twelve Data error body, else ``None``."""
    if isinstance(j, Mapping) and j.get("status") == "error":
        try:
            code = int(j.get("code", 0))
        except (TypeError, ValueError):
            code = 0
        return code, str(j.get("message", ""))
    return None


def is_plan_error(code: int, msg: str) -> bool:
    m = msg.lower()
    return code in (403, 404) and ("plan" in m or "upgrad" in m)


def parse_td_quote(d: Any) -> RawQuote | None:
    if not isinstance(d, Mapping) or d.get("status") == "error":
        return None
    last, prev = _f(d.get("close")), _f(d.get("previous_close"))
    if last is None or last <= 0:
        return None
    ts = d.get("timestamp")
    asof = datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None) if ts else None
    is_fx = d.get("exchange") == "Forex" or "/" in str(d.get("symbol", ""))
    return RawQuote(last, prev, asof, None if is_fx else (d.get("currency") or None), "TD", "CURRENCY" if is_fx else None)


def parse_td_batch(j: Any, requested: Mapping[str, str]) -> tuple[dict[str, RawQuote], dict[str, tuple[int, str]]]:
    """Twelve Data ``/quote`` body for 1..N symbols -> ``({ticker: RawQuote}, {ticker: (code, message)})``.

    One symbol returns a flat object; several return ``{symbol: object}`` where a failed symbol is its own error object.
    ``requested`` maps the TD symbol sent -> our ticker.
    """
    quotes: dict[str, RawQuote] = {}
    errors: dict[str, tuple[int, str]] = {}
    if not isinstance(j, Mapping):
        return quotes, {t: (0, "bad response") for t in requested.values()}
    top = td_error(j)
    if top and len(requested) == 1:
        return quotes, {next(iter(requested.values())): top}
    body: Mapping[str, Any] = {next(iter(requested)): j} if len(requested) == 1 and "symbol" in j else j
    for sym, tkr in requested.items():
        entry = body.get(sym) or body.get(sym.split(":")[0])
        if entry is None:
            errors[tkr] = (0, "symbol missing from response")
            continue
        err = td_error(entry)
        if err:
            errors[tkr] = err
            continue
        q = parse_td_quote(entry)
        if q is None:
            errors[tkr] = (0, "unparseable quote")
        else:
            quotes[tkr] = q
    return quotes, errors


def parse_td_series(j: Any) -> pd.DataFrame:
    """``/time_series`` -> ascending daily OHLCV frame with ``attrs['currency'|'provider'|'type']``.

    Deliberately has **no ``Dividends`` column** (Twelve Data doesn't provide one): the yield logic treats that as
    "unknown" and falls back to the fundamentals feed rather than reading it as a 0% payer.
    """
    err = td_error(j)
    if err:
        raise (PlanLimit if is_plan_error(*err) else DataUnavailable)(f"Twelve Data {err[0]}: {err[1][:90]}")
    rows = (j or {}).get("values") if isinstance(j, Mapping) else None
    if not rows:
        raise DataUnavailable("Twelve Data returned no candles")
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df["datetime"]).dt.normalize()
    out = pd.DataFrame(index=df.index)
    for src, dst in (("open", "Open"), ("high", "High"), ("low", "Low"), ("close", "Close"), ("volume", "Volume")):
        out[dst] = pd.to_numeric(df[src], errors="coerce") if src in df.columns else 0.0
    out = out.dropna(subset=["Close"]).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    meta = j.get("meta", {}) if isinstance(j, Mapping) else {}
    out.attrs.update({"currency": meta.get("currency") or None, "provider": "TD", "type": meta.get("type")})
    return out


# =============================================================================================
# Finnhub — pure parsers
# =============================================================================================
def parse_fh_quote(j: Any) -> RawQuote | None:
    """Finnhub ``/quote``: unknown/non-US symbols come back as all zeros — treat that as 'no data', not as £0."""
    if not isinstance(j, Mapping):
        return None
    c, pc = _f(j.get("c")), _f(j.get("pc"))
    if not c or c <= 0:
        return None
    t = j.get("t")
    asof = datetime.fromtimestamp(int(t), tz=timezone.utc).replace(tzinfo=None) if t else None
    return RawQuote(c, pc if pc and pc > 0 else None, asof, "USD", "FH", None)      # Finnhub's free feed is US-listed => USD


def parse_fh_reco(j: Any) -> list[dict]:
    """``/stock/recommendation`` -> newest-first list of ``{period, strongBuy, buy, hold, sell, strongSell}``."""
    if not isinstance(j, list):
        return []
    rows = [r for r in j if isinstance(r, Mapping)]
    rows.sort(key=lambda r: str(r.get("period", "")), reverse=True)
    return [{"period": str(r.get("period", "")), **{k: int(r.get(k) or 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}}
            for r in rows[:4]]


_SECTOR = {"Technology": "Technology", "Pharmaceuticals": "Healthcare", "Biotechnology": "Healthcare", "Health Care": "Healthcare",
           "Life Sciences Tools & Services": "Healthcare", "Banking": "Financial Services", "Financial Services": "Financial Services",
           "Insurance": "Financial Services", "Media": "Communication Services", "Telecommunication": "Communication Services",
           "Communications": "Communication Services", "Retail": "Consumer Cyclical", "Automobiles": "Consumer Cyclical",
           "Hotels, Restaurants & Leisure": "Consumer Cyclical", "Textiles, Apparel & Luxury Goods": "Consumer Cyclical",
           "Consumer products": "Consumer Defensive", "Food Products": "Consumer Defensive", "Beverages": "Consumer Defensive",
           "Energy": "Energy", "Utilities": "Utilities", "Real Estate": "Real Estate", "Machinery": "Industrials",
           "Aerospace & Defense": "Industrials", "Industrial Conglomerates": "Industrials", "Transportation": "Industrials",
           "Chemicals": "Basic Materials", "Metals & Mining": "Basic Materials"}


def finnhub_info(profile: Mapping | None, metric: Mapping | None) -> dict:
    """Finnhub profile + metric -> the yfinance-style ``info`` keys the screener already understands.

    Units (verified live): ``currentDividendYieldTTM`` and ``payoutRatioTTM`` are **percent**; ``marketCapitalization``
    is in **millions**. ``dividendYield`` is emitted in percent, matching Yahoo's current convention (CLAUDE.md §2.7).
    """
    out: dict[str, Any] = {}
    p, m = profile or {}, (metric or {}).get("metric", metric or {}) if isinstance(metric, Mapping) else {}
    if p.get("currency"):
        out["currency"] = p["currency"]
    if p.get("name"):
        out["longName"] = out["shortName"] = p["name"]
    if p.get("finnhubIndustry"):
        out["sector"] = _SECTOR.get(p["finnhubIndustry"], p["finnhubIndustry"])
    mc = _f(p.get("marketCapitalization")) or _f(m.get("marketCapitalization"))
    if mc:
        out["marketCap"] = mc * 1e6
    for src, dst, scale in (("currentDividendYieldTTM", "dividendYield", 1.0), ("payoutRatioTTM", "payoutRatio", 0.01),
                            ("forwardPE", "forwardPE", 1.0), ("peTTM", "trailingPE", 1.0), ("pegTTM", "trailingPegRatio", 1.0),
                            ("beta", "beta", 1.0), ("52WeekHigh", "fiftyTwoWeekHigh", 1.0), ("52WeekLow", "fiftyTwoWeekLow", 1.0)):
        v = _f(m.get(src))
        if v is not None:
            out[dst] = v * scale
    return out


# =============================================================================================
# Stooq — pure parser
# =============================================================================================
def parse_stooq_csv(text: str) -> pd.DataFrame:
    """Stooq daily CSV (``Date,Open,High,Low,Close,Volume``) -> ascending frame. Stooq states no currency."""
    t = (text or "").strip()
    if not t.lower().startswith("date,"):
        raise DataUnavailable("Stooq returned no data")
    df = pd.read_csv(StringIO(t))
    df.index = pd.to_datetime(df["Date"]).dt.normalize()
    out = pd.DataFrame({c: pd.to_numeric(df[c], errors="coerce") if c in df.columns else 0.0
                        for c in ("Open", "High", "Low", "Close", "Volume")}, index=df.index)
    out = out.dropna(subset=["Close"]).sort_index()
    if out.empty:
        raise DataUnavailable("Stooq returned no rows")
    out.attrs.update({"currency": None, "provider": "ST"})
    return out


# =============================================================================================
# Clients
# =============================================================================================
class TwelveDataClient:
    name = "TD"
    BASE = "https://api.twelvedata.com"

    def __init__(self, key: str, budget: CreditBudget, http: HttpGet = default_http, cache: DiskCache | None = None,
                 clock: Callable[[], float] = time.time):
        self._key, self.budget, self.http, self.cache, self._clock = key, budget, http, cache, clock
        self._disabled = False
        self._lse_blocked_until = 0.0
        self._lse_ok_until = 0.0
        self._lock = threading.RLock()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"apikey {self._key}", "User-Agent": UA}          # header, never the URL

    # -- plan awareness: a free key refuses every LSE symbol; discover once, remember 24h ---------
    def lse_blocked(self) -> bool:
        if self._clock() < self._lse_blocked_until:
            return True
        if self.cache is not None:
            hit = self.cache.get("td:lse_blocked", float("inf"))
            until = float((hit.value or {}).get("until", 0)) if hit is not None and isinstance(hit.value, dict) else 0.0
            if until > self._clock():                  # expiry is stored in the flag and compared on the client's own clock
                self._lse_blocked_until = until
                return True
        return False

    def _block_lse(self) -> None:
        self._lse_blocked_until = self._clock() + 86400
        if self.cache is not None:
            self.cache.set("td:lse_blocked", {"reason": "plan", "until": self._lse_blocked_until})

    def lse_confirmed(self) -> bool:
        """True once a real LSE quote has been served (paid plan) — only then are LSE symbols sent in full batches."""
        if self._clock() < self._lse_ok_until:
            return True
        if self.cache is not None:
            hit = self.cache.get("td:lse_ok", float("inf"))
            until = float((hit.value or {}).get("until", 0)) if hit is not None and isinstance(hit.value, dict) else 0.0
            if until > self._clock():
                self._lse_ok_until = until
                return True
        return False

    def _confirm_lse(self) -> None:
        self._lse_ok_until = self._clock() + 86400
        if self.cache is not None:
            self.cache.set("td:lse_ok", {"until": self._lse_ok_until})

    def supports(self, ticker: str) -> bool:
        if self._disabled:
            return False
        sym = to_twelvedata_symbol(ticker)
        return sym is not None and not (sym.endswith(":LSE") and self.lse_blocked())

    def _call(self, path: str, params: Mapping[str, Any], cost: int) -> Any:
        if self._disabled:
            raise DataUnavailable("Twelve Data disabled (key rejected)")
        if not self.budget.try_acquire(cost):
            raise DataUnavailable("Twelve Data credit budget spent (8/min free tier)")
        try:
            _, j = self.http(f"{self.BASE}/{path}", params, self._headers(), 20.0)
        except Exception as exc:                                  # noqa: BLE001 - never leak URL/headers in the message
            self.budget.refund(cost)
            raise DataUnavailable(f"Twelve Data network error: {type(exc).__name__}") from None
        err = td_error(j)
        if err and not (path == "quote" and cost > 1):             # batch errors are reported per symbol
            code, msg = err
            if code == 429:
                self.budget.penalise()
                raise DataUnavailable("Twelve Data rate limit reached")
            if code in (401, 400) and "api" in msg.lower() and "key" in msg.lower():
                self._disabled = True
                raise DataUnavailable("Twelve Data key rejected")
            raise (PlanLimit if is_plan_error(code, msg) else DataUnavailable)(f"Twelve Data {code}: {msg[:90]}")
        return j

    def quotes(self, tickers: Sequence[str]) -> tuple[dict[str, RawQuote], dict[str, str]]:
        """One batched ``/quote`` call for as many supported tickers as the credit budget allows (priority = order)."""
        sym_map: dict[str, str] = {}
        probing = False
        for t in tickers:
            if self.supports(t):
                sym = to_twelvedata_symbol(t)
                if sym.endswith(":LSE") and not self.lse_confirmed():   # type: ignore[union-attr]
                    if probing:
                        continue                                       # plan unknown: spend ONE credit probing, not one per ticker
                    probing = True
                sym_map.setdefault(sym, t)                             # type: ignore[arg-type]
        n = min(len(sym_map), self.budget.available())
        if n <= 0:
            return {}, {t: "no credits" for t in sym_map.values()}
        chosen = dict(list(sym_map.items())[:n])
        try:
            j = self._call("quote", {"symbol": ",".join(chosen)}, cost=len(chosen))
        except PlanLimit as exc:
            if any(s.endswith(":LSE") for s in chosen):
                self._block_lse()
            return {}, {t: str(exc) for t in chosen.values()}
        except DataUnavailable as exc:
            return {}, {t: str(exc) for t in chosen.values()}
        quotes, errs = parse_td_batch(j, chosen)
        for tkr, (code, msg) in errs.items():
            if is_plan_error(code, msg) and is_lse(tkr):
                self._block_lse()
        if any(is_lse(t) for t in quotes):
            self._confirm_lse()
        return quotes, {t: f"{c}: {m[:80]}" for t, (c, m) in errs.items()}

    def history(self, ticker: str, outputsize: int = 1300) -> pd.DataFrame:
        sym = to_twelvedata_symbol(ticker)
        if sym is None or not self.supports(ticker):
            raise DataUnavailable(f"Twelve Data does not serve {ticker} on this plan")
        try:
            j = self._call("time_series", {"symbol": sym, "interval": "1day", "outputsize": outputsize}, cost=1)
            return parse_td_series(j)
        except PlanLimit:
            if sym.endswith(":LSE"):
                self._block_lse()
            raise


class FinnhubClient:
    name = "FH"
    BASE = "https://finnhub.io/api/v1"

    def __init__(self, key: str, budget: CreditBudget | None = None, http: HttpGet = default_http):
        self._key, self.http = key, http
        self.budget = budget or CreditBudget(50)
        self._disabled = False
        self._denied: set[str] = set()

    def supports(self, ticker: str) -> bool:
        return not self._disabled and to_finnhub_symbol(ticker) is not None and ticker not in self._denied

    def _get(self, path: str, **params: Any) -> Any:
        if self._disabled:
            raise DataUnavailable("Finnhub disabled (key rejected)")
        if not self.budget.try_acquire(1):
            raise DataUnavailable("Finnhub call budget spent")
        try:
            status, j = self.http(f"{self.BASE}/{path}", params, {"X-Finnhub-Token": self._key, "User-Agent": UA}, 15.0)
        except Exception as exc:                                  # noqa: BLE001
            self.budget.refund(1)
            raise DataUnavailable(f"Finnhub network error: {type(exc).__name__}") from None
        if status == 429:
            self.budget.penalise(30.0)
            raise DataUnavailable("Finnhub rate limit reached")
        if status == 401:
            self._disabled = True
            raise DataUnavailable("Finnhub key rejected")
        if status == 403:
            raise PlanLimit("Finnhub: not available on the free plan")
        if status >= 400:
            raise DataUnavailable(f"Finnhub HTTP {status}")
        return j

    def _symbol(self, ticker: str) -> str:
        s = to_finnhub_symbol(ticker)
        if s is None:
            raise DataUnavailable(f"Finnhub free tier covers US symbols only ({ticker})")
        return s

    def quote(self, ticker: str) -> RawQuote:
        try:
            q = parse_fh_quote(self._get("quote", symbol=self._symbol(ticker)))
        except PlanLimit:
            self._denied.add(ticker)
            raise
        if q is None:
            raise DataUnavailable(f"Finnhub has no quote for {ticker}")
        return q

    def recommendations(self, ticker: str) -> list[dict]:
        return parse_fh_reco(self._get("stock/recommendation", symbol=self._symbol(ticker)))

    def profile(self, ticker: str) -> dict:
        j = self._get("stock/profile2", symbol=self._symbol(ticker))
        return dict(j) if isinstance(j, Mapping) else {}

    def metric(self, ticker: str) -> dict:
        j = self._get("stock/metric", symbol=self._symbol(ticker), metric="all")
        return dict(j) if isinstance(j, Mapping) else {}

    def company_news(self, ticker: str, days: int = 7) -> list[dict]:
        today = date.today()
        j = self._get("company-news", symbol=self._symbol(ticker), **{"from": str(today - timedelta(days=days)), "to": str(today)})
        return [x for x in j if isinstance(x, Mapping)] if isinstance(j, list) else []

    def market_news(self, category: str = "general") -> list[dict]:
        j = self._get("news", category=category)
        return [x for x in j if isinstance(x, Mapping)] if isinstance(j, list) else []

    def search(self, query: str) -> list[dict]:
        j = self._get("search", q=query)
        res = j.get("result", []) if isinstance(j, Mapping) else []
        return [{"symbol": r.get("symbol", ""), "description": r.get("description", ""), "type": r.get("type", "")}
                for r in res if isinstance(r, Mapping) and r.get("symbol")][:15]


class StooqClient:
    """Keyless last-resort source for daily history. States no currency: callers must not guess one."""
    name = "ST"
    URL = "https://stooq.com/q/d/l/"

    def __init__(self, http: HttpGet = default_http):
        self.http = http

    def history(self, ticker: str) -> pd.DataFrame:
        sym = to_stooq_symbol(ticker)
        if sym is None:
            raise DataUnavailable(f"Stooq has no mapping for {ticker}")
        try:
            status, body = self.http(self.URL, {"s": sym, "i": "d"}, {"User-Agent": UA}, 15.0)
        except Exception as exc:                                  # noqa: BLE001
            raise DataUnavailable(f"Stooq network error: {type(exc).__name__}") from None
        text = body.get("_raw", "") if isinstance(body, Mapping) else str(body)
        return parse_stooq_csv(text)

    def closes(self, tickers: Sequence[str]) -> dict[str, RawQuote]:
        out: dict[str, RawQuote] = {}
        for t in tickers:
            try:
                s = self.history(t)["Close"]
            except DataUnavailable:
                continue
            out[t] = RawQuote(float(s.iloc[-1]), float(s.iloc[-2]) if len(s) > 1 else None, s.index[-1].to_pydatetime(), None, "ST", None)
        return out


# =============================================================================================
# The chain
# =============================================================================================
class ProviderChain:
    """Same interface as the plain yfinance provider (``closes/metadata/history/info/analyst/news/gilt``), routing per the
    table at the top of this module. Any client may be ``None`` (no key / not installed): the chain simply skips it, so with
    no keys at all it behaves exactly like v0.1 (yfinance only)."""

    name = "chain"

    def __init__(self, *, td: TwelveDataClient | None = None, fh: FinnhubClient | None = None, yf: Any | None = None,
                 stooq: StooqClient | None = None):
        self.td, self.fh, self.yf, self.stooq = td, fh, yf, stooq
        self.used: set[str] = set()
        self.log: list[str] = []
        self._lock = threading.RLock()

    # -- bookkeeping --------------------------------------------------------------------------
    def _use(self, name: str) -> None:
        with self._lock:
            self.used.add(name)

    def _note(self, msg: str) -> None:
        with self._lock:
            self.log = (self.log + [msg[:140]])[-8:]

    @staticmethod
    def _tag(q: RawQuote, name: str) -> RawQuote:
        return q if q.provider else RawQuote(q.last, q.prev, q.asof, q.currency, name, q.kind)

    # -- quotes -------------------------------------------------------------------------------------
    def closes(self, tickers: Sequence[str]) -> dict[str, RawQuote]:
        todo = list(dict.fromkeys(tickers))
        out: dict[str, RawQuote] = {}
        if self.td:
            fx = [t for t in todo if t.endswith("=X")]
            if fx:
                got, errs = self.td.quotes(fx)
                out.update(got)
                for t, e in errs.items():
                    self._note(f"TD {t}: {e}")
        if self.fh:
            for t in [t for t in todo if t not in out and self.fh.supports(t)]:
                try:
                    out[t] = self.fh.quote(t)
                except DataUnavailable as exc:
                    self._note(f"FH {t}: {exc}")
        if self.td:
            ask = [t for t in todo if t not in out and self.td.supports(t) and not t.endswith("=X")]
            if ask:
                got, errs = self.td.quotes(ask)
                out.update(got)
                for t, e in errs.items():
                    self._note(f"TD {t}: {e}")
        rest = [t for t in todo if t not in out]
        if rest and self.yf:
            try:
                for t, q in self.yf.closes(rest).items():
                    out[t] = self._tag(q, "YF")
            except DataUnavailable as exc:
                self._note(f"YF: {exc}")
        rest = [t for t in todo if t not in out]
        if rest and self.stooq:
            out.update(self.stooq.closes(rest))
        for q in out.values():
            if q.provider:
                self._use(q.provider)
        if not out:
            raise DataUnavailable("no provider returned a quote")
        return out

    def metadata(self, ticker: str) -> dict:
        if self.yf:
            try:
                return self.yf.metadata(ticker)
            except Exception as exc:                              # noqa: BLE001
                self._note(f"YF meta {ticker}: {exc}")
        if self.fh and self.fh.supports(ticker):
            p = self.fh.profile(ticker)
            if p.get("currency"):
                return {"currency": p["currency"], "instrumentType": "EQUITY", "longName": p.get("name")}
        raise DataUnavailable(f"{ticker}: no currency metadata from any provider")

    # -- history -------------------------------------------------------------------------------------
    def history(self, ticker: str, period: str = "5y") -> pd.DataFrame:
        if self.td and self.td.supports(ticker) and not is_non_security(ticker):
            try:
                df = self.td.history(ticker)
                self._use("TD")
                return df
            except DataUnavailable as exc:
                self._note(f"TD history {ticker}: {exc}")
        if self.yf:
            try:
                df = self.yf.history(ticker, period)
                df.attrs.setdefault("provider", "YF")
                self._use("YF")
                return df
            except DataUnavailable as exc:
                self._note(f"YF history {ticker}: {exc}")
        if self.stooq:
            df = self.stooq.history(ticker)
            self._use("ST")
            return df
        raise DataUnavailable(f"{ticker}: no history from any provider")

    # -- fundamentals / analysts ----------------------------------------------------------------------
    def info(self, ticker: str) -> dict:
        info: dict[str, Any] = {}
        if self.fh and self.fh.supports(ticker):
            try:
                info = finnhub_info(self.fh.profile(ticker), self.fh.metric(ticker))
                if info:
                    self._use("FH")
            except DataUnavailable as exc:
                self._note(f"FH info {ticker}: {exc}")
        if self.yf and not ({"dividendYield", "forwardPE"} & set(info)):
            try:
                y = self.yf.info(ticker)
                info = {**y, **info} if y else info
                if y:
                    self._use("YF")
            except Exception as exc:                              # noqa: BLE001
                self._note(f"YF info {ticker}: {exc}")
        return info

    def analyst(self, ticker: str) -> dict:
        res: dict[str, Any] = {"targets": {}, "recs": []}
        if self.fh and self.fh.supports(ticker):
            try:
                res["recs"] = self.fh.recommendations(ticker)
                if res["recs"]:
                    self._use("FH")
            except DataUnavailable as exc:
                self._note(f"FH recs {ticker}: {exc}")
        if self.yf:
            try:
                y = self.yf.analyst(ticker)                       # price targets: Finnhub's endpoint is premium
                res["targets"] = y.get("targets") or {}
                if not res["recs"]:
                    res["recs"] = y.get("recs") or []
                if res["targets"] or y.get("recs"):
                    self._use("YF")
            except Exception as exc:                              # noqa: BLE001
                self._note(f"YF analyst {ticker}: {exc}")
        return res

    # -- news ---------------------------------------------------------------------------------------------
    def news(self, ticker: str) -> list[dict]:
        items: list[dict] = []
        if self.fh and self.fh.supports(ticker):
            try:
                raw = sorted(self.fh.company_news(ticker), key=lambda r: -(r.get("datetime") or 0))[:15]
                items = [d for d in (newsmod.from_finnhub(r, queried=ticker) for r in raw) if d]
                if items:
                    self._use("FH")
            except DataUnavailable as exc:
                self._note(f"FH news {ticker}: {exc}")
        if len(items) < 3 and self.yf:
            try:
                items += self.yf.news(ticker)
                self._use("YF")
            except Exception as exc:                              # noqa: BLE001
                self._note(f"YF news {ticker}: {exc}")
        if not items:
            raise DataUnavailable(f"{ticker}: no news")
        return items[:15]

    def market_news(self, categories: Sequence[str] = ("general", "forex")) -> list[dict]:
        if not self.fh:
            raise DataUnavailable("market news needs a Finnhub key")
        out: list[dict] = []
        for cat in categories:
            try:
                raw = sorted(self.fh.market_news(cat), key=lambda r: -(r.get("datetime") or 0))[:40]
                out += [d for d in (newsmod.from_finnhub(r, scope="macro") for r in raw) if d]
            except DataUnavailable as exc:
                self._note(f"FH {cat} news: {exc}")
        if not out:
            raise DataUnavailable("no market news")
        self._use("FH")
        return out

    def search(self, query: str) -> list[dict]:
        if not (self.fh and query.strip()):
            return []
        return self.fh.search(query.strip())

    def gilt(self):
        if self.yf is not None:
            return self.yf.gilt()
        from engine.market_intel import fetch_gilt
        return fetch_gilt()


def build_chain(cache_dir, *, td_key: str | None, finnhub_key: str | None, http: HttpGet = default_http,
                with_yfinance: bool = True, with_stooq: bool = True, yf_factory: Callable[[], Any] | None = None) -> ProviderChain:
    """Assemble the chain from whatever credentials exist. No keys => yfinance only (v0.1 behaviour)."""
    from pathlib import Path
    cache_dir = Path(cache_dir)
    cache = DiskCache(cache_dir)
    td = TwelveDataClient(td_key, CreditBudget(8, daily=780, reserve=1, path=cache_dir / "td_budget.json"), http, cache) if td_key else None
    fh = FinnhubClient(finnhub_key, CreditBudget(50, path=cache_dir / "fh_budget.json"), http) if finnhub_key else None
    yf = None
    if with_yfinance:
        try:
            yf = (yf_factory or _default_yf)(cache_dir)
        except ImportError:                                       # yfinance not installed: the keyed providers still work
            yf = None
    return ProviderChain(td=td, fh=fh, yf=yf, stooq=StooqClient(http) if with_stooq else None)


def _default_yf(cache_dir):
    from engine.market_intel import LiveProvider
    return LiveProvider(cache_dir)
