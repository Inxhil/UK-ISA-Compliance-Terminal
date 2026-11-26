"""News normalisation, summarising, tagging, UK-relevance and the three News Desk filters."""
import json
from pathlib import Path

import pytest

from engine import news as N
from engine.news import NewsItem, TagTarget

FIX = Path(__file__).parent / "fixtures"
NOW = 1_795_040_000.0
TARGETS = [TagTarget("MSFT", "Microsoft Corporation"), TagTarget("NVDA", "NVIDIA Corporation"), TagTarget("AZN.L", "AstraZeneca PLC"),
           TagTarget("SHEL.L", "Shell plc"), TagTarget("GOOGL", "Alphabet Inc.", aliases=("Google",)),
           TagTarget("VWRP.L", "Vanguard FTSE All-World UCITS ETF (Acc)", "etf"), TagTarget("BRK-B", "Berkshire Hathaway Inc.", aliases=("Berkshire",))]


# ------------------------------------------------------------------------------------------- text
def test_clean_summary_strips_html_and_limits_to_three_sentences():
    html_in = "<p>First sentence. Second one &amp; more! Third? Fourth sentence should go. Fifth.</p>"
    out = N.clean_summary(html_in)
    assert out == "First sentence. Second one & more! Third?" and "<" not in out


def test_clean_summary_truncates_long_text_at_a_word_boundary():
    out = N.clean_summary("word " * 200, max_sentences=3, max_chars=100)
    assert out.endswith("…") and len(out) <= 101 and not out[:-1].endswith(" ")


def test_summary_that_merely_repeats_the_headline_is_dropped():
    head = "Israel launches ground operation in central Gaza, dozens flee - Reuters"
    assert N.clean_summary("Israel launches ground operation in central Gaza, dozens flee  Reuters", head) == ""
    assert N.clean_summary(head, head) == "" and N.clean_summary(None, head) == "" and N.clean_summary("  ", head) == ""
    assert N.clean_summary("Real summary text here. More.", head) == "Real summary text here. More."


def test_strip_html_on_the_real_forex_item():
    raw = json.loads((FIX / "fh_news_forex.json").read_text())[0]["summary"]
    out = N.strip_html(raw)
    assert "<" not in out and "href" not in out and len(out) > 20


@pytest.mark.parametrize("ago,expect", [(5, "just now"), (120, "2m ago"), (3 * 3600 + 5, "3h ago"), (2 * 86400, "2d ago"),
                                        (20 * 86400, "2w ago"), (-30, "just now")])
def test_relative_time(ago, expect):
    assert N.rel_time(NOW - ago, NOW) == expect
    assert N.rel_time(None, NOW) == "—"


def test_parse_ts_variants():
    assert N.parse_ts(1_795_038_151) == 1_795_038_151 and N.parse_ts(1_795_038_151_000) == 1_795_038_151
    assert N.parse_ts("2026-11-17T10:00:00Z") == 1_794_909_600.0 and N.parse_ts("2026-11-17T10:00:00") == 1_794_909_600.0
    assert N.parse_ts(None) is None and N.parse_ts("") is None and N.parse_ts("garbage") is None


# ------------------------------------------------------------------------------------------- tagging & UK relevance
def test_tag_tickers_by_symbol_and_company_name_and_alias():
    assert N.tag_tickers("NVDA surges as Microsoft deepens AI tie-up", TARGETS) == ("MSFT", "NVDA")
    assert N.tag_tickers("Google unveils new chip; Berkshire trims stake", TARGETS) == ("GOOGL", "BRK-B")
    assert N.tag_tickers("AstraZeneca wins approval", TARGETS) == ("AZN.L",)
    assert N.tag_tickers("Shell raises buyback", TARGETS) == ("SHEL.L",)
    assert N.tag_tickers("The weather is nice", TARGETS) == ()


def test_tagging_has_no_substring_false_positives():
    assert N.tag_tickers("Unvdaed shellfish import rules", TARGETS) == ()
    assert N.tag_tickers("Vanguard launches a new fund", TARGETS) == ()           # fund names are not used as tags


@pytest.mark.parametrize("text,uk", [("Bank of England holds rates as gilt yields climb", True), ("FTSE 100 closes higher", True),
                                     ("Sterling slides after UK inflation data", True), ("Chancellor Reeves sets out Budget", True),
                                     ("Ukraine talks stall", False), ("Fed minutes point to a December hike", False),
                                     ("Nvidia interim CEO addresses staff", False)])
def test_uk_relevance(text, uk):
    assert N.is_uk_relevant(text) is uk


def test_finnhub_class_share_symbols_map_to_our_tickers():
    assert N.finnhub_to_ticker("BRK.B") == "BRK-B" and N.finnhub_to_ticker("msft") == "MSFT"


# ------------------------------------------------------------------------------------------- normalising real payloads
def test_company_news_fixture_normalises_cleanly():
    raw = json.loads((FIX / "fh_cnews_msft.json").read_text())
    d = N.from_finnhub(raw[0], queried="MSFT")
    assert d["title"] and d["publisher"] and d["related"][0] == "MSFT" and d["ts"] and d["scope"] == "company"
    item = N.to_item(d, TARGETS)
    assert item.tickers[0] == "MSFT" and item.source == d["publisher"] and item.url.startswith("http") and len(item.summary) <= 361


def test_macro_news_fixture_is_tagged_and_uk_flagged():
    raw = json.loads((FIX / "fh_news_general.json").read_text())
    items = [N.to_item(d, TARGETS) for d in (N.from_finnhub(r, scope="macro") for r in raw) if d]
    assert len(items) == 12 and all(i.scope == "macro" for i in items)
    nv = [i for i in items if "Nvidia" in i.title]
    assert nv and "NVDA" in nv[0].tickers                                           # "Nvidia" resolves via the company name
    assert not any(i.summary and i.summary.lower().startswith(i.title.lower()[:40]) for i in items)


def test_from_finnhub_skips_items_without_a_headline():
    assert N.from_finnhub({"headline": "  ", "url": "x"}) is None and N.from_finnhub({}) is None


# ------------------------------------------------------------------------------------------- dedupe & filters
def item(title, ts, tickers=(), scope="company", uk=False, summary="s"):
    return NewsItem(title, "Src", "https://x/" + title[:3], summary, ts, tuple(tickers), scope, uk)


def test_dedupe_collapses_wire_copies_and_prefers_the_one_with_a_summary_and_keeps_tags():
    a = item("Microsoft beats estimates!", 100, ["MSFT"], summary="")
    b = item("Microsoft Beats Estimates", 120, [], summary="Real summary.")
    c = item("Something else", 90)
    out = N.dedupe([a, b, c])
    assert len(out) == 2
    kept = next(i for i in out if "Microsoft" in i.title)
    assert kept.summary == "Real summary." and kept.tickers == ("MSFT",)


@pytest.fixture
def feed():
    return [item("MSFT cloud", 500, ["MSFT"]), item("AZN trial", 400, ["AZN.L"]), item("NVDA chip", 300, ["NVDA"]),
            item("Fed holds", 450, scope="macro"), item("BoE cuts rates", 100, scope="macro", uk=True),
            item("UK CPI falls", 200, scope="macro", uk=True), item("Macro mentions NVIDIA", 350, ["NVDA"], scope="macro")]


def test_filter_selected_ticker(feed):
    assert [i.title for i in N.filter_items(feed, "ticker", selected="NVDA")] == ["Macro mentions NVIDIA", "NVDA chip"]
    assert N.filter_items(feed, "ticker", selected=None) == [] and N.filter_items(feed, "ticker", selected="TSLA") == []


def test_filter_macro_puts_uk_first_then_newest(feed):
    out = N.filter_items(feed, "macro")
    assert [i.title for i in out] == ["UK CPI falls", "BoE cuts rates", "Fed holds", "Macro mentions NVIDIA"]


def test_filter_all_portfolio_is_company_news_plus_macro_that_names_a_holding(feed):
    out = N.filter_items(feed, "all", universe=["MSFT", "NVDA", "AZN.L"])
    assert [i.title for i in out] == ["MSFT cloud", "AZN trial", "Macro mentions NVIDIA", "NVDA chip"]
    assert "Fed holds" not in [i.title for i in out]


def test_unknown_filter_fails_loudly(feed):
    with pytest.raises(ValueError, match="unknown news filter"):
        N.filter_items(feed, "everything")
    assert set(N.FILTER_ORDER) == set(N.FILTER_LABELS) == {"all", "ticker", "macro"}
    assert N.FILTER_LABELS["all"] == "All Portfolio" and N.FILTER_LABELS["macro"] == "Macro / UK Market"
