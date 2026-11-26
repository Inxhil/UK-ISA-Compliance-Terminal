"""market_intel with a fake provider: unit normalisation, FX direction, cache/stale/demo policy, parsers."""
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from engine.market_intel import (DataUnavailable, MarketIntel, RawQuote, parse_boe_csv, parse_news, split_download,
                                 _boe_date)
from datetime import date


class FakeProvider:
    """Mimics Yahoo as observed live on 2026-11-18."""
    name = "fake"
    META = {"VWRP.L": ("GBP", "ETF"), "AZN.L": ("GBp", "EQUITY"), "SMT.L": ("GBp", "EQUITY"), "MSFT": ("USD", "EQUITY"),
            "^FTSE": ("GBP", "INDEX"), "GBPUSD=X": ("USD", "CURRENCY"), "WEIRD.L": ("GBp", "ETF")}
    CLOSE = {"VWRP.L": (138.30, 137.86), "AZN.L": (13628.0, 13802.0), "SMT.L": (1450.55, 1434.0),
             "MSFT": (501.69, 506.24), "^FTSE": (10585.5, 10689.9), "GBPUSD=X": (1.3154, 1.3185), "WEIRD.L": (120.0, 119.0)}

    def __init__(self):
        self.calls = {"closes": 0, "metadata": 0, "history": 0, "info": 0}
        self.fail = False
        self.fail_meta = set()

    def closes(self, tickers):
        self.calls["closes"] += 1
        if self.fail:
            raise DataUnavailable("network down")
        return {t: RawQuote(*self.CLOSE[t], datetime(2026, 11, 18)) for t in tickers if t in self.CLOSE}

    def metadata(self, t):
        self.calls["metadata"] += 1
        if t in self.fail_meta or self.fail:
            raise DataUnavailable("meta fail")
        c, ty = self.META[t]
        return {"currency": c, "instrumentType": ty}

    def history(self, t, period="5y"):
        self.calls["history"] += 1
        if self.fail:
            raise DataUnavailable("down")
        idx = pd.bdate_range(end="2026-11-18", periods=300)
        px = np.linspace(self.CLOSE[t][0] * 0.8, self.CLOSE[t][0], 300)
        return pd.DataFrame({"Open": px, "High": px * 1.01, "Low": px * 0.99, "Close": px, "Volume": 1e6,
                             "Dividends": 0.0}, index=idx)

    def info(self, t):
        self.calls["info"] += 1
        if self.fail:
            raise DataUnavailable("down")
        return {"currency": "GBp"}

    def analyst(self, t):
        return {"targets": {}, "recs": []}

    def news(self, t):
        raise DataUnavailable("no news")

    def gilt(self):
        if self.fail:
            raise DataUnavailable("down")
        return (5.19, 5.1)


@pytest.fixture
def mi(tmp_path):
    fake = FakeProvider()
    return MarketIntel(tmp_path, live=fake, mode="auto", price_units={"WEIRD.L": "GBP"}), fake


class TestQuoteNormalisation:
    def test_pence_pounds_usd_and_index(self, mi):
        m, _ = mi
        q = m.quotes(["VWRP.L", "AZN.L", "SMT.L", "MSFT", "^FTSE"])
        assert q["VWRP.L"].price_gbp == pytest.approx(138.30)                      # GBP: untouched
        assert q["AZN.L"].price_gbp == pytest.approx(136.28)                       # GBp: /100
        assert q["SMT.L"].price_gbp == pytest.approx(14.5055)
        assert q["MSFT"].price_gbp == pytest.approx(501.69 / 1.3154)               # USD: / (USD per GBP)
        assert q["MSFT"].price_native == pytest.approx(501.69) and q["MSFT"].fx_rate == pytest.approx(1.3154)
        assert q["^FTSE"].price_gbp is None and q["^FTSE"].price_native == pytest.approx(10585.5)
        assert q["^FTSE"].is_non_security

    def test_change_pct_is_unit_free(self, mi):
        m, _ = mi
        q = m.quotes(["AZN.L", "MSFT"])
        assert q["AZN.L"].change_pct == pytest.approx((13628 / 13802 - 1) * 100)
        assert q["MSFT"].change_pct == pytest.approx((501.69 / 506.24 - 1) * 100)
        assert q["AZN.L"].prev_close_gbp == pytest.approx(138.02)

    def test_config_price_unit_override_beats_vendor_label(self, mi):
        m, _ = mi
        assert m.quotes(["WEIRD.L"])["WEIRD.L"].price_gbp == pytest.approx(120.0)  # labelled GBp, forced to GBP

    def test_fx_pair_quote_is_a_rate_not_money(self, mi):
        m, _ = mi
        q = m.quotes(["GBPUSD=X"])["GBPUSD=X"]
        assert q.price_native == pytest.approx(1.3154) and q.price_gbp is None

    def test_fx_rates_helper(self, mi):
        assert mi[0].fx_rates(["USD", "GBP"]) == {"USD": pytest.approx(1.3154)}

    def test_unknown_currency_is_never_guessed(self, mi):
        m, fake = mi
        fake.fail_meta = {"AZN.L"}
        q = m.quotes(["AZN.L"])["AZN.L"]
        assert q.price_gbp is None and q.price_native is None and "currency unknown" in q.note
        assert q.source != "demo"                       # live price must not be scaled with invented metadata

    def test_missing_fx_rate_gives_none_not_a_guess(self, tmp_path):
        fake = FakeProvider()
        fake.CLOSE = {k: v for k, v in fake.CLOSE.items() if k != "GBPUSD=X"}
        q = MarketIntel(tmp_path, live=fake).quotes(["MSFT"])["MSFT"]
        assert q.price_gbp is None and "FX" in q.note and q.price_native == pytest.approx(501.69)


class TestHistory:
    def test_history_converted_to_major_units(self, mi):
        m, _ = mi
        h = m.history("AZN.L")
        assert h.currency == "GBP" and h.df["Close"].iloc[-1] == pytest.approx(136.28)
        assert h.df["High"].iloc[-1] == pytest.approx(136.28 * 1.01)
        usd = m.history("MSFT")
        assert usd.currency == "USD" and usd.df["Close"].iloc[-1] == pytest.approx(501.69)   # native, not FX-mangled
        idx = m.history("^FTSE")
        assert idx.is_non_security and idx.df["Close"].iloc[-1] == pytest.approx(10585.5)


class TestCachePolicy:
    def test_second_call_is_served_from_cache(self, mi):
        m, fake = mi
        m.quotes(["AZN.L"])
        n = fake.calls["closes"]
        m.begin_cycle()
        q = m.quotes(["AZN.L"])["AZN.L"]
        assert fake.calls["closes"] == n and q.source == "cache" and m.status().label == "LIVE"

    def test_stale_on_error(self, tmp_path):
        fake = FakeProvider()
        t = [1000.0]
        m = MarketIntel(tmp_path, live=fake, mode="auto", clock=lambda: t[0], ttl={"quote": 90})
        m.quotes(["AZN.L"])                              # populate cache
        import os, time
        for f in tmp_path.glob("close*.json"):
            os.utime(f, None)
        fake.fail = True
        # age the cache entries past the TTL by rewriting timestamps
        import json
        for f in tmp_path.glob("*.json"):
            b = json.loads(f.read_text()); b["ts"] = time.time() - 600; f.write_text(json.dumps(b))
        m.begin_cycle()
        q = m.quotes(["AZN.L"])["AZN.L"]
        assert q.source == "stale" and q.price_gbp == pytest.approx(136.28)
        assert m.status().label == "STALE" and m.status().errors

    def test_total_outage_falls_back_to_demo_and_is_labelled(self, tmp_path):
        fake = FakeProvider()
        fake.fail = True
        m = MarketIntel(tmp_path, live=fake, mode="auto")
        q = m.quotes(["AZN.L", "MSFT"])
        assert q["AZN.L"].source == "demo" and q["MSFT"].source == "demo" and m.status().label == "DEMO"
        assert q["AZN.L"].price_gbp == pytest.approx(136.28)

    def test_live_mode_never_serves_demo(self, tmp_path):
        fake = FakeProvider()
        fake.fail = True
        m = MarketIntel(tmp_path, live=fake, mode="live")
        assert m.quotes(["AZN.L"]) == {} and m.status().label == "OFFLINE"

    def test_soft_failure_while_quotes_are_live_is_not_papered_over_with_demo(self, mi):
        m, fake = mi
        m.begin_cycle()
        m.quotes(["AZN.L"])
        f = m.news("AZN.L")                              # provider raises; quotes were live => no fabricated news
        assert f.value is None and f.source == "none"

    def test_demo_mode_never_touches_the_network(self, tmp_path):
        fake = FakeProvider()
        m = MarketIntel(tmp_path, live=fake, mode="demo")
        m.quotes(["AZN.L", "MSFT"])
        m.history("AZN.L"); m.info("AZN.L"); m.analyst("MSFT"); m.news("MSFT"); m.tape([])
        assert sum(fake.calls.values()) == 0

    def test_circuit_breaker_opens_after_repeated_failures_and_recovers(self, tmp_path):
        fake = FakeProvider(); fake.fail = True
        t = [0.0]
        m = MarketIntel(tmp_path, live=fake, mode="auto", clock=lambda: t[0])
        for _ in range(3):
            m.quotes(["AZN.L"])
        assert m.breaker_open()
        n = fake.calls["closes"]
        m.quotes(["AZN.L"])
        assert fake.calls["closes"] == n                 # not hammering a dead endpoint
        t[0] += MarketIntel.BREAKER_COOLOFF_S + 1
        fake.fail = False
        m.begin_cycle()
        assert m.quotes(["AZN.L"])["AZN.L"].source == "live" and not m.breaker_open()

    def test_partial_info_is_retried_soon_not_cached_for_12h(self, tmp_path):
        import json, time
        fake = FakeProvider()
        m = MarketIntel(tmp_path, live=fake)
        m.info("AZN.L")                                   # fake returns 1 key => "partial"
        for f in tmp_path.glob("info*.json"):
            b = json.loads(f.read_text()); b["ts"] = time.time() - 400; f.write_text(json.dumps(b))
        m.info("AZN.L")
        assert fake.calls["info"] == 2                    # >300s old partial entry refetched

    def test_tape_includes_gilt(self, mi):
        m, _ = mi
        items = m.tape([{"symbol": "^FTSE", "label": "FTSE 100", "kind": "index"},
                        {"symbol": "GBPUSD=X", "label": "GBP/USD", "kind": "fx"}])
        assert [i.label for i in items] == ["FTSE 100", "GBP/USD", "UK 10Y GILT"]
        g = items[-1]
        assert g.value == pytest.approx(5.19) and g.change == pytest.approx(9.0) and g.change_unit == "bp"
        assert items[1].decimals == 4

    def test_bad_mode(self, tmp_path):
        with pytest.raises(ValueError):
            MarketIntel(tmp_path, mode="turbo")


class TestParsers:
    def test_boe_csv(self):
        txt = "DATE,IUDMNPY\r\n10 Nov 2026,5.1906\r\n11 Nov 2026,5.2044\r\n\r\n"
        assert parse_boe_csv(txt) == [(date(2026, 11, 10), 5.1906), (date(2026, 11, 11), 5.2044)]

    def test_boe_html_error_page_yields_nothing(self):
        assert parse_boe_csv("<!DOCTYPE html><html><body>Error, the page you requested...</body></html>") == []

    def test_boe_date_is_locale_independent(self):
        assert _boe_date(date(2026, 3, 4)) == "04/Mar/2026"

    def test_news_new_and_legacy_shapes(self):
        new = {"id": "1", "content": {"title": "AZN wins approval", "pubDate": "2026-11-17T10:00:00Z",
                                      "provider": {"displayName": "Reuters"}, "canonicalUrl": {"url": "https://x/1"}}}
        old = {"title": "Old shape", "publisher": "FT", "link": "https://x/2", "providerPublishTime": 1_760_000_000}
        out = parse_news([new, old, {"nothing": 1}, "junk", None])
        base = ("title", "publisher", "link", "published")
        assert {k: out[0][k] for k in base} == {"title": "AZN wins approval", "publisher": "Reuters", "link": "https://x/1",
                                                "published": "2026-11-17T10:00:00Z"}
        assert out[0]["scope"] == "company" and out[0]["summary"] == "" and out[0]["ts"] is not None
        assert out[1]["publisher"] == "FT" and out[1]["published"].startswith("2025-")
        assert len(out) == 2 and parse_news(None) == []

    def test_split_download_multiindex_and_flat(self):
        idx = pd.bdate_range("2026-11-10", periods=3)
        cols = pd.MultiIndex.from_product([["AAA", "BBB"], ["Close", "Open"]])
        df = pd.DataFrame(np.arange(12, dtype=float).reshape(3, 4), index=idx, columns=cols)
        df.loc[idx[-1], ("BBB", "Close")] = np.nan
        out = split_download(df, ["AAA", "BBB", "CCC"])
        assert list(out) == ["AAA", "BBB"] and len(out["BBB"]) == 2 and out["AAA"].iloc[-1] == 8.0
        flat = pd.DataFrame({"Close": [1.0, 2.0]}, index=idx[:2])
        assert split_download(flat, ["ZZZ"])["ZZZ"].iloc[-1] == 2.0
        assert split_download(None, ["A"]) == {} and split_download(pd.DataFrame(), ["A"]) == {}


def test_news_matches_yfinance_1_8_graphql_article_shape():
    """Shape copied from yfinance.base.TickerBase._parse_news_asset (verified in source; live host not reachable here)."""
    story = {"id": "u1", "content": {"id": "u1", "contentType": "STORY", "title": "Microsoft cloud growth beats", "description": "d",
             "summary": "s", "pubDate": "2026-11-17T09:30:00Z", "displayTime": "2026-11-17T09:31:00Z", "isHosted": True,
             "previewUrl": None, "thumbnail": None, "provider": {"displayName": "Reuters", "url": "https://reuters.com"},
             "canonicalUrl": {"url": "https://finance.yahoo.com/m/abc"}, "clickThroughUrl": None,
             "finance": {"premiumFinance": {"isPremiumNews": False}, "stockTickers": [{"symbol": "MSFT"}]}}}
    outlink = {"id": "u2", "content": {"title": "Outlink headline", "pubDate": None, "displayTime": "2026-11-16T08:00:00Z",
               "provider": None, "canonicalUrl": None, "clickThroughUrl": {"url": "https://x.example/2"}}}
    out = parse_news([story, outlink])
    assert {k: out[0][k] for k in ("title", "publisher", "link", "published")} == {
        "title": "Microsoft cloud growth beats", "publisher": "Reuters", "link": "https://finance.yahoo.com/m/abc",
        "published": "2026-11-17T09:30:00Z"}
    assert out[0]["summary"] == "s"                                  # yfinance 1.8 carries a summary: keep it
    assert out[1]["link"] == "https://x.example/2" and out[1]["publisher"] == "" and out[1]["published"] == "2026-11-16T08:00:00Z"


class _Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_gilt_retries_once_after_a_transient_html_error_page(monkeypatch):
    import requests
    import engine.market_intel as m
    calls = []
    pages = ["<html>Error page</html>", "DATE,IUDMNPY\r\n10 Nov 2026,5.19\r\n11 Nov 2026,5.21\r\n"]
    monkeypatch.setattr(requests, "get", lambda *a, **k: (calls.append(k["headers"]["User-Agent"]) or _Resp(pages[len(calls) - 1])))
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    prov = m.LiveProvider.__new__(m.LiveProvider)            # no yfinance needed for this call
    assert prov.gilt() == (5.21, 5.19)
    assert len(calls) == 2 and all(u == m.UA for u in calls)  # honest, descriptive User-Agent on both attempts
    assert "isa-command-centre" in m.UA


def test_gilt_gives_up_after_two_html_pages(monkeypatch):
    import requests
    import engine.market_intel as m
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp("<html>Error</html>"))
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    prov = m.LiveProvider.__new__(m.LiveProvider)
    with pytest.raises(m.DataUnavailable, match="no gilt rows"):
        prov.gilt()
