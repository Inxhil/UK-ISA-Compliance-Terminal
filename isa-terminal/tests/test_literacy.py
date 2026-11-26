"""Beginner / Intermediate / Advanced wording, badges, What-This-Means, rule summaries and the unit-audit line."""
import re
from datetime import datetime

import pytest

from engine.analyst_screener import AnalystSnapshot
from engine.config import EXPERIENCE_LEVELS, HandbookRules
from engine.literacy import (allowance_bar, analyst_badge, income_badge, momentum_badge, next_level, normalise_level, plain_text,
                             portfolio_badge, render_signal, rule_summary, strip_rule_ids, unit_audit, valuation_badge, what_it_means)
from engine.market_intel import Quote
from engine.portfolio import HoldingRow, Position
from engine.recommender import generate_signals
from test_recommender import (CFG, LONDON, NOW, PX, ctx, deposit_ledger, snap, underweight_ctx, WATCH)

R = HandbookRules()
JARGON = re.compile(r"\bRSI\b|\bPEG\b|\bSMA\b|R-[A-Z]{3}-\d{2}|\bOCF\b|\bDCA\b|look-through|\bpp\b|\bconviction\b", re.I)
NAMES = {w.ticker: w.name.split(" (")[0] for w in CFG.watchlist}
name_of = lambda t: NAMES.get(t, t)


def all_signal_scenarios():
    """Every kind of signal the recommender can emit, across many situations."""
    out = []
    out += generate_signals(ctx(bed=True, data="DEMO"))
    for kw in ({}, {"rsi14": 24.0}, {"rsi14": 78.0}, {"vs_sma200_pct": -18.0}, {"upside_pct": -9.0}, {"upside_pct": 8.0},
               {"payout_ratio": 0.95, "dividend_cover": 1.05, "dividend_yield_pct": 4.0}):
        out += generate_signals(underweight_ctx(**kw))
    for now in (NOW, datetime(2027, 1, 20, 12, tzinfo=LONDON), datetime(2027, 3, 31, 12, tzinfo=LONDON)):
        out += generate_signals(ctx(now=now, ledger=deposit_ledger(10000, lisa=2500), monthly=0.0))
    out += generate_signals(ctx(ledger=deposit_ledger(21500, lisa=4500)))
    out += generate_signals(ctx(snaps={"VWRP.L": snap("VWRP.L", drawdown_52w_pct=-25.0)}))
    out += generate_signals(ctx(holdings=[HoldingRow("T212_ISA", "VWRP.L", 100, 100.0), HoldingRow("T212_ISA", "CASH", 3000, 1.0)]))
    out += generate_signals(ctx(prices={**PX, "AZN.L": 110.0}, snaps={"NVDA": snap("NVDA", rsi14=80.0)}))
    return out


# ------------------------------------------------------------------------------------------ levels
def test_level_helpers():
    assert EXPERIENCE_LEVELS == ("beginner", "intermediate", "advanced")
    assert [next_level(l) for l in EXPERIENCE_LEVELS] == ["intermediate", "advanced", "beginner"]
    assert normalise_level(None) == "advanced" and normalise_level("  BEGINNER ") == "beginner" and normalise_level("wizard") == "advanced"
    assert next_level("nonsense") == "beginner"                 # unknown is treated as advanced -> cycles to beginner


# ------------------------------------------------------------------------------------------ plain English
@pytest.mark.parametrize("text,expected", [
    ("Exceeds 15% Handbook Cap (R-CAP-01)", "Exceeds 15% Handbook Cap"),
    ("rules: R-CAP-01, R-UPS-01 apply", "rules: apply"),
    ("breach [R-CAP-02] here", "breach here"),
    ("no ids here", "no ids here"),
])
def test_strip_rule_ids(text, expected):
    assert strip_rule_ids(text) == expected


def test_beginner_translates_the_jargon_the_engine_actually_emits():
    cases = {
        "Underweight & >15% Analyst Upside (+22%) & Oversold (RSI 24)": "sold off hard",
        "Underweight but Overbought (RSI 78): Stage Entries": "overheated",
        "25% Below 200-Day SMA": "200-day average price",
        "Blended OCF 0.13%": "fund fee",
        "Technology Look-Through 43.3% Exceeds 40% Sector Cap": "including fund holdings",
        "Dividend Cover 1.39x < 1.5x Minimum": "Dividend Safety",
        "+10.4pp Over 6% Target": "10.4 percentage points",
        "accelerate the DCA": "regular monthly investing",
    }
    for src, want in cases.items():
        out = plain_text(src, "beginner")
        assert want in out and not JARGON.search(out), (src, out)


def test_intermediate_only_removes_rule_ids_and_advanced_is_untouched():
    s = "Exceeds 15% Handbook Cap (R-CAP-01) with RSI 78 and PEG 2"
    assert plain_text(s, "advanced") == s
    mid = plain_text(s, "intermediate")
    assert "R-CAP-01" not in mid and "RSI 78" in mid and "PEG 2" in mid


def test_beginner_wording_never_contains_quant_jargon_for_any_signal_the_engine_can_emit():
    sigs = all_signal_scenarios()
    assert len({s.action for s in sigs}) >= 9 and len(sigs) > 60
    for sg in sigs:
        v = render_signal(sg, "beginner", R, name_of)
        for part in (v.headline, v.meaning, *v.rationale):
            assert not JARGON.search(part), (sg.id, part)
        assert v.rules_line == ""


def test_every_beginner_signal_has_a_what_this_means_line():
    for sg in all_signal_scenarios():
        m = what_it_means(sg, R, name_of)
        assert len(m) > 30 and m.endswith((".", "!")), (sg.id, m)
        assert render_signal(sg, "beginner", R, name_of).meaning == m


def test_what_this_means_uses_the_signals_own_numbers():
    sigs = {s.id: s for s in all_signal_scenarios()}
    allowance = what_it_means(sigs["TAX:ALLOWANCE"], R, name_of)
    assert "£" in allowance and "5 April" in allowance
    lisa = what_it_means(next(s for s in all_signal_scenarios() if s.id == "TAX:LISA"), R, name_of)
    assert "25p" in lisa and "£" in lisa
    trim = what_it_means(sigs["TRIM:NVDA"], R, name_of)
    assert "NVIDIA" in trim and "15%" in trim and "without selling" in trim


def test_intermediate_shows_one_line_rule_summaries_not_ids():
    sg = next(s for s in all_signal_scenarios() if s.id == "TRIM:NVDA")
    v = render_signal(sg, "intermediate", R, name_of)
    assert "R-CAP-01" not in v.headline + " ".join(v.rationale) and v.rules_line == "No single stock or trust above 15% of the ISA."
    assert v.meaning == ""


def test_advanced_keeps_raw_rule_ids_and_original_text():
    sg = next(s for s in all_signal_scenarios() if s.id == "TRIM:NVDA")
    v = render_signal(sg, "advanced", R, name_of)
    assert v.rules_line == "R-CAP-01" and v.headline == sg.headline and v.rationale == sg.rationale and v.meaning == ""
    multi = next(s for s in all_signal_scenarios() if len(s.rules) > 1)
    assert render_signal(multi, "advanced", R, name_of).rules_line == ", ".join(multi.rules)


def test_rule_summaries_follow_the_handbook_numbers_and_cover_every_rule():
    ids = {r for s in all_signal_scenarios() for r in s.rules}
    assert ids and all(rule_summary(i, R) for i in ids), [i for i in ids if not rule_summary(i, R)]
    custom = HandbookRules(position=type(R.position)(max_single_position_pct=10.0))
    assert "10%" in rule_summary("R-CAP-01", custom) and rule_summary("R-NOPE-99", R) == ""


# ------------------------------------------------------------------------------------------ badges
def S(**kw):
    base = dict(ticker="X", name="X", kind="stock", currency="GBP", price=100, price_gbp=100, day_change_pct=0)
    base.update(kw)
    return AnalystSnapshot(**base)


def test_valuation_badge_votes():
    assert valuation_badge(S(vs_5y_avg_pct=5, upside_pct=25, n_analysts=20, peg=1.2), R).label == "Good Value"
    assert valuation_badge(S(vs_5y_avg_pct=60, upside_pct=-8, n_analysts=20, peg=3.5), R).label == "Expensive"
    assert valuation_badge(S(vs_5y_avg_pct=10, upside_pct=8, n_analysts=20, peg=2.0), R).label == "Fair"
    assert valuation_badge(S(vs_5y_avg_pct=-20), R).label == "Good Value"            # a fund with only the 5y-average signal
    assert valuation_badge(S(vs_5y_avg_pct=55), R).label == "Expensive"
    assert valuation_badge(S(upside_pct=40, n_analysts=2), R).label == "Unknown"      # thin coverage is ignored (R-UPS-01)
    assert valuation_badge(None, R).label == "Unknown" and valuation_badge(S(), R).tone == "dim"


def test_momentum_badge():
    assert momentum_badge(S(rsi14=80), R).label == "Overheated" and momentum_badge(S(rsi14=20), R).label == "Beaten Down"
    assert momentum_badge(S(rsi14=50, trend="UPTREND"), R).label == "Steady Climb"
    assert momentum_badge(S(rsi14=50, trend="DOWNTREND"), R).label == "Drifting Lower"
    assert momentum_badge(S(rsi14=50, trend="MIXED"), R).label == "Steady" and momentum_badge(S(), R).label == "Unknown"


def test_analyst_and_income_badges():
    assert analyst_badge(S(rating_mean=1.4, n_analysts=30), R).label == "Positive"
    assert analyst_badge(S(rating_mean=3.0, n_analysts=30), R).label == "Mixed"
    assert analyst_badge(S(rating_mean=4.1, n_analysts=30), R).label == "Cautious"
    assert analyst_badge(S(rating_mean=1.4, n_analysts=2), R).label == "Few analysts" and analyst_badge(S(), R).label == "No coverage"
    assert [income_badge(S(dividend_yield_pct=y)).label for y in (5, 2.5, 0.5, 0)] == ["High income", "Some income", "Low income", "Growth, no income"]
    assert income_badge(S()).label == "—"


def test_portfolio_badge_uses_handbook_thresholds():
    c = ctx()
    p = c.portfolio
    assert portfolio_badge(p.get("NVDA"), "stock", R).label == "Too big"
    assert portfolio_badge(p.get("AZN.L"), "stock", R).label == "Room to add"
    assert portfolio_badge(p.get("VWRP.L"), "etf", R).label == "On plan"
    assert portfolio_badge(None, "stock", R).label == "Watching"
    pos = Position("X", "X", "stock", 0, 0, None, 0, 0, 0, 0.05, None, None, None, {})
    assert portfolio_badge(pos, "stock", R).label == "Not held yet"


# ------------------------------------------------------------------------------------------ visuals & audit
def test_allowance_bar():
    assert allowance_bar(12500, 20000, 20) == ("█" * 12, "░" * 8, 62.5)
    assert allowance_bar(25000, 20000, 10) == ("█" * 10, "", 100.0)              # over-subscribed is clamped for the visual only
    assert allowance_bar(0, 20000, 10) == ("", "░" * 10, 0.0) and allowance_bar(5, 0, 10)[2] == 0.0


def Q(**kw):
    base = dict(ticker="AZN.L", price_gbp=136.28, prev_close_gbp=138.0, change_pct=-1.2, price_native=136.28, prev_native=138.0,
                currency="GBP", raw_currency="GBp", fx_rate=None, asof=None, source="live", is_non_security=False, note=None, provider="YF")
    base.update(kw)
    return Quote(**base)


def test_unit_audit_lines_show_exactly_how_a_price_was_normalised():
    assert unit_audit(Q()) == "unit audit: YF · 13,628.00 GBp (pence) ÷ 100 = £136.28"
    assert unit_audit(Q(raw_currency="GBX", provider="TD")) == "unit audit: TD · 13,628.00 GBX (pence) ÷ 100 = £136.28"
    assert "quoted in GBP — no conversion" in unit_audit(Q(raw_currency="GBP", price_native=138.3, price_gbp=138.3))
    usd = unit_audit(Q(ticker="MSFT", currency="USD", raw_currency="USD", price_native=508.29, price_gbp=386.59, fx_rate=1.3149, provider="FH"))
    assert usd == "unit audit: FH · USD 508.29 ÷ 1.3149 GBPUSD = £386.59"
    assert "FX" in (unit_audit(Q(currency="USD", raw_currency="USD", price_gbp=None, fx_rate=None, note="no USD/GBP FX rate — GBP price unavailable")) or "")
    assert unit_audit(Q(is_non_security=True)) is None and unit_audit(None) is None and unit_audit(Q(price_native=None)) is None
