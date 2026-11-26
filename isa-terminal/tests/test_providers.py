"""Twelve Data / Finnhub / Stooq parsing, credit budgeting, plan-awareness and chain routing.

The fixtures in tests/fixtures/ are REAL responses recorded from the live APIs (Nov 2026, free-tier keys), trimmed. The
client classes are exercised for real against a fake HTTP layer that serves them — no network, no keys.
"""
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from engine.cache import DiskCache
from engine.market_intel import MarketIntel
from engine.models import DataUnavailable, RawQuote, unit_jump
from engine.providers import (FinnhubClient, PlanLimit, ProviderChain, StooqClient, TwelveDataClient, build_chain,
                              finnhub_info, is_plan_error, is_us_ticker, normalise_user_ticker, parse_fh_quote,
                              parse_fh_reco, parse_stooq_csv, parse_td_batch, parse_td_quote, parse_td_series,
                              td_error, to_finnhub_symbol, to_stooq_symbol, to_twelvedata_symbol)
from engine.ratelimit import CreditBudget

FIX = Path(__file__).parent / "fixtures"
KEY = "TESTKEY-not-a-real-key"


def fx(name):
    return json.loads((FIX / f"{name}.json").read_text())


# =============================================================================================== symbols
@pytest.mark.parametrize("t,td,fh,st", [
    ("AZN.L", "AZN:LSE", None, "azn.uk"), ("VWRP.L", "VWRP:LSE", None, "vwrp.uk"), ("MSFT", "MSFT", "MSFT", "msft.us"),
    ("BRK-B", "BRK.B", "BRK.B", "brk-b.us"), ("GBPUSD=X", "GBP/USD", None, None), ("^FTSE", None, None, None),
    ("GBPEUR=X", "GBP/EUR", None, None)])
def test_symbol_mapping(t, td, fh, st):
    assert (to_twelvedata_symbol(t), to_finnhub_symbol(t), to_stooq_symbol(t)) == (td, fh, st)


@pytest.mark.parametrize("raw,norm", [("barc.l", "BARC.L"), (" aapl ", "AAPL"), ("LON:BARC", "BARC.L"), ("BARC:LSE", "BARC.L"),
                                      ("brk.b", "BRK-B"), ("BRK-B", "BRK-B"), ("vod.l", "VOD.L"), ("LSE:AZN.L", "AZN.L")])
def test_user_typed_tickers_are_normalised(raw, norm):
    assert normalise_user_ticker(raw) == norm


def test_us_ticker_detection():
    assert is_us_ticker("MSFT") and is_us_ticker("BRK-B")
    assert not any(is_us_ticker(t) for t in ("AZN.L", "^GSPC", "GBPUSD=X", ""))


# =============================================================================================== Twelve Data parsing
def test_td_quote_msft_real_payload():
    q = parse_td_quote(fx("td_quote_msft"))
    assert (q.last, q.prev, q.currency, q.provider) == (508.29001, 506.23999, "USD", "TD")
    assert q.asof == datetime(2026, 11, 18, 14, 30) and q.kind is None


def test_td_forex_quote_is_a_rate_with_no_currency():
    q = parse_td_quote(fx("td_quote_gbpusd"))
    assert q.last == pytest.approx(1.31485) and q.currency is None and q.kind == "CURRENCY"    # USD per GBP


def test_td_batch_and_per_symbol_errors():
    quotes, errs = parse_td_batch(fx("td_quote_batch"), {"NVDA": "NVDA", "AAPL": "AAPL"})
    assert set(quotes) == {"NVDA", "AAPL"} and not errs and quotes["AAPL"].last == pytest.approx(352.45001)
    quotes, errs = parse_td_batch(fx("td_quote_mixed"), {"AZN:LSE": "AZN.L", "AAPL": "AAPL"})
    assert set(quotes) == {"AAPL"} and errs["AZN.L"][0] == 403 and is_plan_error(*errs["AZN.L"])


def test_td_single_symbol_returns_a_flat_object_and_flat_errors():
    q, e = parse_td_batch(fx("td_quote_brkb"), {"BRK.B": "BRK-B"})
    assert q["BRK-B"].last == pytest.approx(574.62) and not e
    q, e = parse_td_batch(fx("td_quote_azn_lse"), {"AZN:LSE": "AZN.L"})
    assert not q and e["AZN.L"][0] == 404 and "Grow or Venture" in e["AZN.L"][1] and is_plan_error(*e["AZN.L"])


def test_td_error_helpers():
    assert td_error({"code": 429, "message": "x", "status": "error"}) == (429, "x") and td_error({"close": "1"}) is None
    assert not is_plan_error(429, "out of credits") and is_plan_error(403, "not available with your plan")


def test_td_series_is_ascending_numeric_and_has_no_dividend_column():
    df = parse_td_series(fx("td_ts_msft"))
    assert list(df.index) == sorted(df.index) and df.index[-1] == pd.Timestamp("2026-11-18")
    assert df["Close"].iloc[-1] == pytest.approx(508.29001) and df["Volume"].iloc[-1] == 21349348
    assert df.attrs["currency"] == "USD" and df.attrs["provider"] == "TD"
    assert "Dividends" not in df.columns                        # unknown, not 0%: yield logic must not read this as a non-payer


def test_td_series_errors_are_typed():
    with pytest.raises(PlanLimit):
        parse_td_series(fx("td_quote_azn_lse"))
    with pytest.raises(DataUnavailable, match="no candles"):
        parse_td_series({"meta": {}, "values": []})


# =============================================================================================== Finnhub parsing
def test_fh_quote_us_is_usd_and_unknown_symbols_are_not_zero_priced():
    q = parse_fh_quote(fx("fh_quote_msft"))
    assert (q.last, q.prev, q.currency, q.provider) == (508.29, 506.24, "USD", "FH")
    assert parse_fh_quote({"c": 0, "d": None, "dp": None, "h": 0, "l": 0, "o": 0, "pc": 0, "t": 0}) is None
    assert parse_fh_quote(fx("fh_quote_azn")) is None


def test_fh_recommendations_newest_first():
    r = parse_fh_reco(fx("fh_reco_msft"))
    assert r[0]["period"] == "2026-11-01" and len(r) == 4 and r[0]["strongBuy"] == 22 and r[0]["buy"] == 40
    assert [x["period"] for x in r] == sorted((x["period"] for x in r), reverse=True)
    assert parse_fh_reco({"error": "x"}) == [] and parse_fh_reco(None) == []


def test_fh_info_units_percent_and_millions():
    info = finnhub_info(fx("fh_profile_msft"), fx("fh_metric_msft"))
    assert info["currency"] == "USD" and info["longName"] == "Microsoft Corporation" and info["sector"] == "Technology"
    assert info["dividendYield"] == pytest.approx(0.7191)                    # PERCENT, as Yahoo reports it
    assert info["payoutRatio"] == pytest.approx(0.1895)                      # 18.95% -> fraction
    assert info["marketCap"] == pytest.approx(3.7594e12, rel=1e-3)           # millions -> units
    assert info["forwardPE"] == pytest.approx(24.59, abs=0.01) and info["trailingPegRatio"] == pytest.approx(1.53, abs=0.01)
    assert finnhub_info(None, None) == {} and finnhub_info({}, {"metric": {}}) == {}


def test_stooq_parser():
    df = parse_stooq_csv("Date,Open,High,Low,Close,Volume\n2026-11-17,13700,13900,13650,13802,100\n2026-11-18,13800,13850,13500,13628,200\n")
    assert df["Close"].iloc[-1] == 13628 and df.attrs["currency"] is None and df.attrs["provider"] == "ST"
    for bad in ("No data", "", "<html>blocked</html>"):
        with pytest.raises(DataUnavailable):
            parse_stooq_csv(bad)


# =============================================================================================== credit budget
class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_budget_allows_exactly_n_per_minute_then_recovers():
    c = Clock()
    b = CreditBudget(8, clock=c, reserve=1)
    assert b.available() == 7 and b.try_acquire(7) and not b.try_acquire(1) and b.available() == 0
    assert 59 < b.seconds_until(1) <= 60
    c.t += 61
    assert b.available() == 7


def test_budget_daily_cap_refund_and_penalty():
    c = Clock()
    b = CreditBudget(100, daily=10, clock=c)
    assert b.try_acquire(10) and not b.try_acquire(1)
    b.refund(4)
    assert b.available() == 4
    b.penalise(62)
    assert b.available() == 0 and b.seconds_until() == pytest.approx(62)
    c.t += 63
    assert b.available() == 4


def test_budget_survives_a_restart(tmp_path):
    c = Clock()
    p = tmp_path / "b.json"
    CreditBudget(8, path=p, clock=c).try_acquire(6)
    assert CreditBudget(8, path=p, clock=c).available() == 2          # a second process / restart sees the spend
    p.write_text("{corrupt")
    assert CreditBudget(8, path=p, clock=c).available() == 8          # corrupt state => safe default, no crash


# =============================================================================================== fake HTTP serving the fixtures
class FixtureHttp:
    """Behaves like the real services for the symbols we recorded; everything else is a plan error / 403."""

    def __init__(self):
        self.calls: list[tuple[str, dict, dict]] = []
        self.fail_with: Exception | None = None
        self.td_override = None
        self.lse_plan = False                      # True = emulate a paid plan that serves LSE (prices in GBX pence)

    def __call__(self, url, params=None, headers=None, timeout=15.0):
        params, headers = dict(params or {}), dict(headers or {})
        self.calls.append((url, params, headers))
        if self.fail_with:
            raise self.fail_with
        host, path = url.split("//")[1].split("/", 1)
        if host == "api.twelvedata.com":
            return self._td(path, params)
        if host == "finnhub.io":
            return self._fh(path.removeprefix("api/v1/"), params)
        if host == "stooq.com":
            return 200, {"_raw": "Date,Open,High,Low,Close,Volume\n2026-11-17,1,2,0.5,1.5,10\n2026-11-18,1.5,2,1,1.8,10\n"}
        return 404, {}

    def _td(self, path, p):
        if self.td_override:
            return 200, self.td_override
        if path == "quote":
            syms = p["symbol"].split(",")
            per = {"MSFT": "td_quote_msft", "NVDA": None, "AAPL": None, "BRK.B": "td_quote_brkb", "GBP/USD": "td_quote_gbpusd"}
            batch = fx("td_quote_batch")
            entries = {}
            for s in syms:
                if s.endswith(":LSE") and self.lse_plan:
                    entries[s] = {"symbol": s.split(":")[0], "exchange": "LSE", "mic_code": "XLON", "currency": "GBX", "close": "13628.00",
                                  "previous_close": "13802.00", "timestamp": 1795012200}
                elif s.endswith(":LSE"):
                    entries[s] = {"code": 403, "message": f"**symbol** {s.split(':')[0]} is not available with your plan.", "status": "error"}
                elif s in batch:
                    entries[s] = batch[s]
                elif per.get(s):
                    entries[s] = fx(per[s])
                else:
                    entries[s] = {"code": 400, "message": "symbol not found", "status": "error"}
            if len(syms) == 1:
                e = entries[syms[0]]
                if e.get("status") == "error" and syms[0].endswith(":LSE") and not self.lse_plan:
                    return 200, {"code": 404, "message": "This symbol is available starting with the Grow or Venture plan.", "status": "error"}
                return 200, e
            return 200, entries
        if path == "time_series":
            if p["symbol"].endswith(":LSE"):
                return 200, {"code": 404, "message": "This symbol is available starting with the Grow or Venture plan.", "status": "error"}
            return 200, fx("td_ts_msft")
        return 404, {}

    def _fh(self, path, p):
        sym = p.get("symbol", "")
        if sym.endswith(".L"):
            return 403, {"error": "You don't have access to this resource."}
        table = {"quote": "fh_quote_msft", "stock/recommendation": "fh_reco_msft", "stock/profile2": "fh_profile_msft",
                 "stock/metric": "fh_metric_msft", "company-news": "fh_cnews_msft", "search": "fh_search"}
        if path == "news":
            return 200, fx("fh_news_general" if p.get("category") == "general" else "fh_news_forex")
        if path in ("stock/price-target", "forex/rates", "stock/candle"):
            return 403, {"error": "You don't have access to this resource."}
        return 200, fx(table[path]) if path in table else (404, {})


@pytest.fixture
def http():
    return FixtureHttp()


def td_client(http, tmp_path, clock=None):
    return TwelveDataClient(KEY, CreditBudget(8, reserve=1, clock=clock or Clock(), path=tmp_path / "td.json"), http,
                            DiskCache(tmp_path), clock or Clock())


def fh_client(http, clock=None):
    return FinnhubClient(KEY, CreditBudget(50, clock=clock or Clock()), http)


# =============================================================================================== Twelve Data client
def test_td_key_is_sent_as_a_header_never_in_the_url(http, tmp_path):
    td_client(http, tmp_path).quotes(["MSFT"])
    url, params, headers = http.calls[0]
    assert headers["Authorization"] == f"apikey {KEY}" and KEY not in url and KEY not in json.dumps(params)


def test_td_batches_us_quotes_in_one_call(http, tmp_path):
    q, errs = td_client(http, tmp_path).quotes(["NVDA", "AAPL", "BRK-B"])
    assert len(http.calls) == 1 and set(q) == {"NVDA", "AAPL", "BRK-B"} and not errs
    assert http.calls[0][1]["symbol"] == "NVDA,AAPL,BRK.B"


def test_td_stays_inside_the_free_tier_budget_and_prioritises_the_head_of_the_list(http, tmp_path):
    c = td_client(http, tmp_path)
    many = ["MSFT", "NVDA", "AAPL", "BRK-B", "GOOGL", "TSLA", "AMZN", "META", "NFLX", "AMD"]       # 10 symbols, budget = 7
    c.quotes(many)
    assert len(http.calls[0][1]["symbol"].split(",")) == 7 and http.calls[0][1]["symbol"].startswith("MSFT,NVDA,AAPL")
    assert c.budget.available() == 0
    q, errs = c.quotes(["MSFT"])                                  # budget spent: no network call, no exception
    assert q == {} and len(http.calls) == 1


def test_td_free_plan_lse_refusal_is_discovered_once_and_remembered_for_24h(http, tmp_path):
    c = td_client(http, tmp_path)
    assert c.supports("AZN.L")
    q, errs = c.quotes(["AZN.L"])
    assert q == {} and "Grow or Venture" in errs["AZN.L"]
    assert not c.supports("AZN.L") and not c.supports("VWRP.L") and c.supports("MSFT")        # every .L is now skipped
    n = len(http.calls)
    c.quotes(["AZN.L", "SMT.L"])
    with pytest.raises(DataUnavailable):
        c.history("SMT.L")
    assert len(http.calls) == n                                   # no credits wasted rediscovering it
    c2 = td_client(http, tmp_path)                                # a new process reads the persisted flag
    assert not c2.supports("AZN.L")
    late = Clock(1_000_000.0 + 86400 + 5)
    assert td_client(http, tmp_path, clock=late).supports("AZN.L")                               # expires after 24h (plan may change)


def test_td_mixed_batch_returns_the_good_symbols_and_blocks_lse(http, tmp_path):
    c = td_client(http, tmp_path)
    q, errs = c.quotes(["AZN.L", "AAPL"])
    assert set(q) == {"AAPL"} and "AZN.L" in errs and not c.supports("SMT.L")


def test_td_rate_limit_response_pauses_the_client_without_raising_to_the_caller(http, tmp_path):
    http.td_override = {"code": 429, "message": "You have run out of API credits for the current minute.", "status": "error"}
    c = td_client(http, tmp_path)
    q, errs = c.quotes(["MSFT"])
    assert q == {} and "rate limit" in errs["MSFT"] and c.budget.available() == 0
    n = len(http.calls)
    c.quotes(["MSFT"])
    assert len(http.calls) == n


def test_td_network_error_refunds_credits_and_never_leaks_the_key(http, tmp_path):
    http.fail_with = ConnectionError(f"boom https://api.twelvedata.com/quote?apikey={KEY}")
    c = td_client(http, tmp_path)
    before = c.budget.available()
    q, errs = c.quotes(["MSFT", "NVDA"])
    assert q == {} and c.budget.available() == before
    assert KEY not in json.dumps(errs) and "ConnectionError" in json.dumps(errs)
    with pytest.raises(DataUnavailable) as e:
        c.history("MSFT")
    assert KEY not in str(e.value)


def test_td_history_for_us_and_fx_quote(http, tmp_path):
    c = td_client(http, tmp_path)
    df = c.history("MSFT")
    assert df.attrs["provider"] == "TD" and len(df) == 4
    q, _ = c.quotes(["GBPUSD=X"])
    assert q["GBPUSD=X"].last == pytest.approx(1.31485) and q["GBPUSD=X"].kind == "CURRENCY"


def test_td_invalid_key_disables_the_client(http, tmp_path):
    http.td_override = {"code": 401, "message": "**apikey** parameter is incorrect or not specified.", "status": "error"}
    c = td_client(http, tmp_path)
    c.quotes(["MSFT"])
    assert not c.supports("MSFT")


# =============================================================================================== Finnhub client
def test_fh_client_header_auth_and_us_only(http):
    c = fh_client(http)
    q = c.quote("MSFT")
    url, params, headers = http.calls[0]
    assert q.provider == "FH" and headers["X-Finnhub-Token"] == KEY and KEY not in url and KEY not in json.dumps(params)
    assert params["symbol"] == "MSFT"
    assert not c.supports("AZN.L") and not c.supports("^GSPC") and c.supports("BRK-B")
    with pytest.raises(DataUnavailable, match="US symbols only"):
        c.quote("AZN.L")
    assert len(http.calls) == 1                                   # the UK symbol never reached the network


def test_fh_403_is_a_plan_limit_and_is_remembered(http):
    c = fh_client(http)
    with pytest.raises(PlanLimit):
        c.recommendations.__self__._get("stock/price-target", symbol="MSFT")
    assert c.recommendations("MSFT")[0]["strongBuy"] == 22 and c.profile("MSFT")["currency"] == "USD"


def test_fh_company_news_market_news_and_search(http):
    c = fh_client(http)
    news = c.company_news("MSFT")
    assert news and "headline" in news[0]
    assert http.calls[-1][1]["symbol"] == "MSFT" and "from" in http.calls[-1][1] and "to" in http.calls[-1][1]
    assert len(c.market_news("general")) == 12 and c.market_news("forex")
    res = c.search("BARC")
    assert res[0] == {"symbol": "BARC.L", "description": "BARCLAYS PLC", "type": "Common Stock"}


def test_fh_rate_limit_and_bad_key(http):
    c = FinnhubClient(KEY, CreditBudget(2, clock=Clock()), http)
    c.quote("MSFT"); c.quote("MSFT")
    with pytest.raises(DataUnavailable, match="budget"):
        c.quote("MSFT")
    def resp(status):
        return lambda *a, **k: (status, {})
    for status, msg in ((429, "rate limit"), (401, "key rejected"), (500, "HTTP 500")):
        cc = FinnhubClient(KEY, CreditBudget(50, clock=Clock()), resp(status))
        with pytest.raises(DataUnavailable, match=msg):
            cc.quote("MSFT")


def test_stooq_client_is_keyless_and_reports_no_currency(http):
    c = StooqClient(http)
    q = c.closes(["AZN.L", "^FTSE"])
    assert set(q) == {"AZN.L"} and q["AZN.L"].currency is None and q["AZN.L"].provider == "ST" and q["AZN.L"].last == 1.8
    assert http.calls[0][1]["s"] == "azn.uk" and not http.calls[0][2].get("Authorization")


# =============================================================================================== the chain
class FakeYF:
    """Stand-in for the yfinance provider with realistic raw units (pence for AZN.L/SMT.L)."""
    name = "yf"

    def __init__(self):
        self.calls = []
        self.q = {"AZN.L": (13628.0, 13802.0), "SMT.L": (1450.5, 1434.0), "VWRP.L": (138.3, 137.86), "MSFT": (500.0, 499.0),
                  "GBPUSD=X": (1.3154, 1.3185), "^FTSE": (10585.5, 10689.9)}

    def closes(self, ts):
        self.calls.append(("closes", tuple(ts)))
        return {t: RawQuote(*self.q[t], None, None, "YF", None) for t in ts if t in self.q}

    def metadata(self, t):
        return {"currency": {"AZN.L": "GBp", "SMT.L": "GBp", "VWRP.L": "GBP"}.get(t, "USD"), "instrumentType": "EQUITY"}

    def history(self, t, period="5y"):
        self.calls.append(("history", t))
        idx = pd.bdate_range(end="2026-11-18", periods=30)
        df = pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 100.0, "Volume": 1.0, "Dividends": 0.0}, index=idx)
        df.attrs["provider"] = "YF"
        return df

    def info(self, t):
        self.calls.append(("info", t))
        return {"forwardPE": 20.0, "sector": "Healthcare", "currency": "GBp"}

    def analyst(self, t):
        self.calls.append(("analyst", t))
        return {"targets": {"mean": 150.0, "high": 170.0, "low": 120.0}, "recs": [{"period": "0m", "buy": 9, "strongBuy": 1, "hold": 2, "sell": 0, "strongSell": 0}]}

    def news(self, t):
        self.calls.append(("news", t))
        return [{"title": f"{t} yahoo story", "publisher": "Yahoo", "link": "https://y/1", "published": None, "ts": 1.0, "summary": "s",
                 "related": [], "scope": "company", "category": ""}]

    def gilt(self):
        return (5.3, 5.2)


def make_chain(http, tmp_path, *, td=True, fh=True, yf=True, stooq=False):
    return ProviderChain(td=td_client(http, tmp_path) if td else None, fh=fh_client(http) if fh else None,
                         yf=FakeYF() if yf else None, stooq=StooqClient(http) if stooq else None)


def test_chain_routes_us_to_finnhub_lse_to_yfinance_and_fx_to_twelvedata(http, tmp_path):
    ch = make_chain(http, tmp_path)
    q = ch.closes(["AZN.L", "VWRP.L", "MSFT", "GBPUSD=X", "^FTSE"])
    assert {t: v.provider for t, v in q.items()} == {"AZN.L": "YF", "VWRP.L": "YF", "MSFT": "FH", "GBPUSD=X": "TD", "^FTSE": "YF"}
    assert q["MSFT"].currency == "USD" and q["AZN.L"].currency is None            # YF currency comes from metadata (unchanged path)
    assert ch.used == {"TD", "FH", "YF"}
    assert ch.td is not None and not ch.td.supports("AZN.L")                       # the free plan refused the LSE probe... or never asked


def test_chain_falls_back_to_yfinance_when_finnhub_and_twelvedata_fail(http, tmp_path):
    http.fail_with = ConnectionError("offline")
    ch = make_chain(http, tmp_path)
    q = ch.closes(["MSFT", "AZN.L"])
    assert {v.provider for v in q.values()} == {"YF"} and set(q) == {"MSFT", "AZN.L"}


def test_chain_without_any_keys_is_exactly_the_v01_yfinance_path(http, tmp_path):
    ch = make_chain(http, tmp_path, td=False, fh=False)
    assert {v.provider for v in ch.closes(["MSFT", "AZN.L"]).values()} == {"YF"} and not http.calls


def test_chain_with_everything_down_raises(tmp_path):
    ch = ProviderChain()
    with pytest.raises(DataUnavailable):
        ch.closes(["MSFT"])


def test_chain_history_prefers_twelvedata_for_us_and_yfinance_for_lse(http, tmp_path):
    ch = make_chain(http, tmp_path)
    us, uk = ch.history("MSFT"), ch.history("AZN.L")
    assert us.attrs["provider"] == "TD" and uk.attrs["provider"] == "YF"
    assert ch.used >= {"TD", "YF"}


def test_chain_info_is_finnhub_for_us_and_skips_yfinance_when_complete(http, tmp_path):
    ch = make_chain(http, tmp_path)
    info = ch.info("MSFT")
    assert info["dividendYield"] == pytest.approx(0.7191) and ("info", "MSFT") not in ch.yf.calls
    uk = ch.info("AZN.L")                                         # UK: Finnhub is skipped, yfinance serves it
    assert uk["forwardPE"] == 20.0 and ("info", "AZN.L") in ch.yf.calls


def test_chain_analyst_merges_finnhub_consensus_with_yfinance_targets(http, tmp_path):
    ch = make_chain(http, tmp_path)
    a = ch.analyst("MSFT")
    assert a["recs"][0]["strongBuy"] == 22 and a["targets"]["mean"] == 150.0          # recs: Finnhub; targets: yfinance (FH premium)
    assert ch.analyst("AZN.L")["recs"][0]["buy"] == 9                                # UK: yfinance only


def test_chain_news_finnhub_for_us_yfinance_for_uk_and_market_news(http, tmp_path):
    ch = make_chain(http, tmp_path)
    us = ch.news("MSFT")
    assert us[0]["related"][0] == "MSFT" and us[0]["ts"] >= us[-1]["ts"] and any(n["publisher"] for n in us)
    assert ch.news("AZN.L")[0]["publisher"] == "Yahoo"
    macro = ch.market_news()
    assert macro and all(m["scope"] == "macro" for m in macro)
    with pytest.raises(DataUnavailable):
        make_chain(http, tmp_path, fh=False).market_news()


def test_build_chain_assembles_from_whatever_keys_exist(tmp_path):
    only_fh = build_chain(tmp_path, td_key=None, finnhub_key=KEY, with_yfinance=False, with_stooq=False)
    assert only_fh.td is None and only_fh.fh is not None and only_fh.yf is None
    both = build_chain(tmp_path, td_key=KEY, finnhub_key=KEY, with_yfinance=False)
    assert both.td.budget.per_minute == 8 and both.td.budget.daily == 780 and both.fh.budget.per_minute == 50 and both.stooq


# =============================================================================================== MarketIntel + GBX safeguards
class OneQuoteChain:
    """A live provider returning exactly the RawQuote we hand it (to test normalisation of each vendor's currency label)."""
    used: set = set()
    log: list = []

    def __init__(self, quote_by_ticker, fx=1.3154):
        self.q = dict(quote_by_ticker)
        self.q["GBPUSD=X"] = RawQuote(fx, fx, None, None, "TD", "CURRENCY")

    def closes(self, ts):
        return {t: self.q[t] for t in ts if t in self.q}

    def metadata(self, t):
        raise DataUnavailable("no metadata")

    def history(self, t, period="5y"):
        raise DataUnavailable("none")

    def info(self, t): return {}
    def analyst(self, t): return {"targets": {}, "recs": []}
    def news(self, t): raise DataUnavailable("none")
    def gilt(self): raise DataUnavailable("none")


@pytest.mark.parametrize("label,raw,expected_gbp", [
    ("GBX", 13628.0, 136.28),         # Twelve Data's pence code
    ("GBp", 13628.0, 136.28),         # Yahoo's pence code
    ("GBP", 138.30, 138.30),          # pounds, untouched
])
def test_each_vendors_pence_code_normalises_to_pounds(tmp_path, label, raw, expected_gbp):
    mi = MarketIntel(tmp_path, live=OneQuoteChain({"AZN.L": RawQuote(raw, raw * 0.99, None, label, "TD")}), mode="live")
    q = mi.quotes(["AZN.L"])["AZN.L"]
    assert q.price_gbp == pytest.approx(expected_gbp) and q.provider == "TD" and q.raw_currency == label


def test_unknown_currency_label_is_never_guessed(tmp_path):
    mi = MarketIntel(tmp_path, live=OneQuoteChain({"AZN.L": RawQuote(13628.0, 13600.0, None, "GBx", "TD")}), mode="live")
    q = mi.quotes(["AZN.L"])["AZN.L"]
    assert q.price_gbp is None and "FX rate" in q.note                    # 'GBx' is not a known code: refuse rather than guess


def test_us_quote_from_finnhub_converts_with_twelvedata_fx(tmp_path):
    mi = MarketIntel(tmp_path, live=OneQuoteChain({"MSFT": RawQuote(508.29, 506.24, None, "USD", "FH")}, fx=1.31485), mode="live")
    q = mi.quotes(["MSFT"])["MSFT"]
    assert q.price_gbp == pytest.approx(508.29 / 1.31485) and q.fx_rate == pytest.approx(1.31485)
    assert q.change_pct == pytest.approx((508.29 / 506.24 - 1) * 100)


def test_unit_jump_helper():
    assert unit_jump(13628, 136.28) == 100.0 and unit_jump(136.28, 13628) == 0.01 and unit_jump(138.2, 137.9) is None
    assert unit_jump(None, 5) is None and unit_jump(5, 0) is None and unit_jump(200, 100) is None       # a genuine 2x is not flagged


def _age_cache(path, seconds):
    import time
    for f in path.glob("close*.json"):
        b = json.loads(f.read_text())
        b["ts"] = time.time() - seconds
        f.write_text(json.dumps(b))


def test_unit_jump_tripwire_rejects_a_provider_that_flips_pence_to_pounds(tmp_path):
    chain = OneQuoteChain({"AZN.L": RawQuote(13628.0, 13802.0, None, "GBX", "TD")})
    mi = MarketIntel(tmp_path, live=chain, mode="live", ttl={"quote": 90})
    assert mi.quotes(["AZN.L"])["AZN.L"].price_gbp == pytest.approx(136.28)
    _age_cache(tmp_path, 600)                                             # the cached price is now stale -> provider is asked again
    chain.q["AZN.L"] = RawQuote(136.28, 138.0, None, "GBX", "TD")        # ...and mislabels pounds as GBX (100x too small once /100)
    mi.begin_cycle()
    q = mi.quotes(["AZN.L"])["AZN.L"]
    assert q.price_gbp == pytest.approx(136.28) and q.source == "stale"  # the bad value was NOT used (it would have been £1.36)
    assert any("jumped" in e for e in mi.status().errors)
    # a genuine move is accepted
    chain.q["AZN.L"] = RawQuote(13900.0, 13628.0, None, "GBX", "TD")
    _age_cache(tmp_path, 600)
    mi.begin_cycle()
    assert mi.quotes(["AZN.L"])["AZN.L"].price_gbp == pytest.approx(139.0)


def test_status_badge_lists_active_sources(http, tmp_path):
    ch = make_chain(http, tmp_path)
    mi = MarketIntel(tmp_path, live=ch, mode="live")
    mi.begin_cycle()
    mi.quotes(["MSFT", "VWRP.L"])
    st = mi.status()
    assert st.label == "LIVE" and st.sources == ("TD", "FH", "YF") and st.badge == "LIVE · TD + FH + YF"
    mi.begin_cycle()
    assert mi.status().sources == ()                                      # per-cycle, not sticky
    assert MarketIntel(tmp_path, live=FakeYF(), mode="demo").status().badge == "OFFLINE"


def test_history_currency_comes_from_the_series_and_survives_the_cache(http, tmp_path):
    ch = make_chain(http, tmp_path)
    mi = MarketIntel(tmp_path, live=ch, mode="live")
    h = mi.history("MSFT")
    assert h.currency == "USD" and h.provider == "TD" and h.df["Close"].iloc[-1] == pytest.approx(508.29001)
    h2 = MarketIntel(tmp_path, live=make_chain(FixtureHttp(), tmp_path), mode="live").history("MSFT")     # served from disk cache
    assert h2.source == "cache" and h2.provider == "TD" and h2.currency == "USD"


def test_news_feed_merges_company_and_macro_and_dedupes(http, tmp_path):
    ch = make_chain(http, tmp_path)
    mi = MarketIntel(tmp_path, live=ch, mode="live")
    feed = mi.news_feed(["MSFT", "AZN.L"])
    assert feed.source == "live" and feed.items
    assert any(i.scope == "macro" for i in feed.items) and any("MSFT" in i.tickers for i in feed.items)
    assert any(i.tickers == ("AZN.L",) and i.source == "Yahoo" for i in feed.items)
    ts = [i.ts or 0 for i in feed.items]
    assert ts == sorted(ts, reverse=True)
    assert len({i.title.lower() for i in feed.items}) == len(feed.items)


def test_search_goes_through_finnhub_and_never_raises(http, tmp_path):
    mi = MarketIntel(tmp_path, live=make_chain(http, tmp_path), mode="live")
    assert mi.search("BARC")[0]["symbol"] == "BARC.L"
    assert MarketIntel(tmp_path, live=FakeYF(), mode="live").search("BARC") == []            # no search capability => []
    assert MarketIntel(tmp_path, live=make_chain(http, tmp_path), mode="demo").search("BARC") == []
    assert mi.search("   ") == []


# =============================================================================================== LSE probing (credit economy)
def test_td_probes_lse_with_one_credit_not_one_per_ticker(http, tmp_path):
    c = td_client(http, tmp_path)
    c.quotes(["AZN.L", "SMT.L", "VWRP.L", "VUAG.L", "MSFT"])
    syms = http.calls[0][1]["symbol"].split(",")
    assert [x for x in syms if x.endswith(":LSE")] == ["AZN:LSE"] and "MSFT" in syms     # one LSE probe + the US symbol
    assert c.budget.available() == 7 - 2 and not c.supports("SMT.L")                      # plan refused it -> blocked after ONE credit


def test_paid_plan_confirms_lse_then_sends_full_batches_and_gbx_is_normalised(http, tmp_path):
    http.lse_plan = True
    c = td_client(http, tmp_path)
    q, errs = c.quotes(["AZN.L", "SMT.L"])
    assert list(q) == ["AZN.L"] and q["AZN.L"].currency == "GBX" and q["AZN.L"].last == 13628.0 and c.lse_confirmed()
    q2, _ = c.quotes(["SMT.L", "VWRP.L"])                       # confirmed: no more single-symbol probing
    assert set(q2) == {"SMT.L", "VWRP.L"} and http.calls[-1][1]["symbol"] == "SMT:LSE,VWRP:LSE"
    assert td_client(http, tmp_path).lse_confirmed()            # persisted across restarts
    mi = MarketIntel(tmp_path / "mi", live=ProviderChain(td=c, yf=None), mode="live")
    assert mi.quotes(["AZN.L"])["AZN.L"].price_gbp == pytest.approx(136.28)   # 13628 GBX -> £136.28, end to end


# =============================================================================================== regression: FX/index quotes without a currency
def test_fx_and_index_quotes_with_no_currency_still_have_a_price(tmp_path):
    """Found live: Twelve Data's forex quote has no currency, and the tape showed 'GBP/USD n/a'."""
    chain = OneQuoteChain({"^FTSE": RawQuote(10585.5, 10689.9, None, None, "YF", "INDEX")}, fx=1.31485)
    mi = MarketIntel(tmp_path, live=chain, mode="live")
    q = mi.quotes(["GBPUSD=X", "^FTSE"])
    fxq, idx = q["GBPUSD=X"], q["^FTSE"]
    assert fxq.price_native == pytest.approx(1.31485) and fxq.is_non_security and fxq.price_gbp is None and fxq.note is None
    assert fxq.change_pct == pytest.approx(0.0, abs=1e-9) and fxq.provider == "TD"
    assert idx.price_native == pytest.approx(10585.5) and idx.is_non_security and idx.change_pct == pytest.approx((10585.5 / 10689.9 - 1) * 100)


def test_tape_shows_twelvedata_fx_and_yahoo_indices(http, tmp_path):
    ch = make_chain(http, tmp_path)
    mi = MarketIntel(tmp_path, live=ch, mode="live")
    items = {t.label: t for t in mi.tape([{"symbol": "^FTSE", "label": "FTSE 100", "kind": "index"},
                                          {"symbol": "GBPUSD=X", "label": "GBP/USD", "kind": "fx"}])}
    assert items["GBP/USD"].value == pytest.approx(1.31485) and items["GBP/USD"].decimals == 4 and items["GBP/USD"].change is not None
    assert items["FTSE 100"].value == pytest.approx(10585.5)
