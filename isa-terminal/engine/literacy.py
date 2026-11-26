"""Experience levels: Beginner / Intermediate / Advanced (pure functions, no I/O).

* **Advanced** — the full v0.1 Bloomberg density: raw rule IDs, technicals, ``GBp`` unit audit notes, the simulator.
* **Intermediate** — standard fundamentals (yield, forward P/E, analyst rating & upside, sector weights), one-line rule
  summaries instead of raw IDs, and the monthly buy planner.
* **Beginner** — no quant jargon (RSI, PEG, SMA, rule IDs …): plain-English badges, a "What This Means" line under every
  signal, and a visual £20,000 allowance bar.

Nothing here changes a number: it only decides *how* an already-computed fact is worded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from engine.analyst_screener import AnalystSnapshot
from engine.config import EXPERIENCE_LEVELS, HandbookRules
from engine.market_intel import Quote
from engine.portfolio import Position
from engine.recommender import Signal, money

LEVEL_LABEL = {"beginner": "Beginner", "intermediate": "Intermediate", "advanced": "Advanced"}


# Beginner-mode labels for the Command Desk table (same information, no desk jargon)
SEV_PLAIN = {"CRITICAL": "URGENT", "HIGH": "IMPORTANT", "MEDIUM": "WORTH A LOOK", "LOW": "FYI", "INFO": "FYI"}
ACTION_PLAIN = {"ACCUMULATE": "Consider adding", "HOLD OFF": "Wait a little", "TRIM WARNING": "Too big", "STOP ADDING": "Pause adding",
                "TAX ALERT": "Tax deadline", "BED & ISA": "Tax saving", "SCREEN FAIL": "Check failed", "REVIEW": "Re-check",
                "DEPLOY": "Put cash to work", "DEPLOY RESERVE": "Market fall plan", "CAUTION": "Heads-up", "PRICE ALERT": "Price alert",
                "RSI ALERT": "Momentum alert", "DATA WARNING": "Data warning"}


def normalise_level(level: str | None) -> str:
    l = (level or "advanced").strip().lower()
    return l if l in EXPERIENCE_LEVELS else "advanced"


def next_level(level: str) -> str:
    order = EXPERIENCE_LEVELS
    return order[(order.index(normalise_level(level)) + 1) % len(order)]


def is_beginner(level: str) -> bool:
    return normalise_level(level) == "beginner"


def is_advanced(level: str) -> bool:
    return normalise_level(level) == "advanced"


# =============================================================================================== badges
@dataclass(frozen=True)
class Badge:
    label: str
    tone: str            # good | ok | warn | bad | cool | dim


def valuation_badge(s: AnalystSnapshot | None, rules: HandbookRules) -> Badge:
    """Good Value / Fair / Expensive — a vote across price-vs-own-average, analyst upside and growth-adjusted P/E."""
    if s is None:
        return Badge("Unknown", "dim")
    t, a, q = rules.technicals, rules.analyst, rules.quality
    pts = n = 0
    if s.vs_5y_avg_pct is not None:
        n += 1
        pts += 1 if s.vs_5y_avg_pct <= t.cheap_vs_5y_avg_pct else -1 if s.vs_5y_avg_pct >= t.rich_vs_5y_avg_pct else 0
    if s.upside_pct is not None and (s.n_analysts or 0) >= a.min_analysts:
        n += 1
        pts += 1 if s.upside_pct >= a.min_upside_pct else -1 if s.upside_pct <= a.review_below_target_pct else 0
    if s.peg is not None and s.peg > 0:
        n += 1
        pts += 1 if s.peg <= 1.5 else -1 if s.peg > q.max_peg else 0
    if n == 0:
        return Badge("Unknown", "dim")
    if pts >= 2 or (n == 1 and pts == 1):
        return Badge("Good Value", "good")
    if pts <= -1:
        return Badge("Expensive", "bad")
    return Badge("Fair", "ok")


def momentum_badge(s: AnalystSnapshot | None, rules: HandbookRules) -> Badge:
    if s is None or s.rsi14 is None:
        return Badge("Unknown", "dim")
    if s.rsi14 > rules.technicals.rsi_overbought:
        return Badge("Overheated", "warn")
    if s.rsi14 < rules.technicals.rsi_oversold:
        return Badge("Beaten Down", "cool")
    return Badge({"UPTREND": "Steady Climb", "DOWNTREND": "Drifting Lower"}.get(s.trend, "Steady"), "ok")


def analyst_badge(s: AnalystSnapshot | None, rules: HandbookRules) -> Badge:
    if s is None or s.rating_mean is None:
        return Badge("No coverage", "dim")
    if (s.n_analysts or 0) < rules.analyst.min_analysts:
        return Badge("Few analysts", "dim")
    return Badge("Positive" if s.rating_mean <= 2.2 else "Mixed" if s.rating_mean <= 3.2 else "Cautious",
                 "good" if s.rating_mean <= 2.2 else "ok" if s.rating_mean <= 3.2 else "bad")


def income_badge(s: AnalystSnapshot | None) -> Badge:
    y = s.dividend_yield_pct if s else None
    if y is None:
        return Badge("—", "dim")
    return Badge("High income" if y >= 4 else "Some income" if y >= 2 else "Low income" if y > 0 else "Growth, no income",
                 "good" if y >= 4 else "ok" if y >= 2 else "dim")


def portfolio_badge(pos: Position | None, kind: str, rules: HandbookRules) -> Badge:
    p = rules.position
    if pos is None or pos.value_gbp <= 0:
        return Badge("Not held yet" if pos is not None and pos.target_weight > 0 else "Watching", "dim")
    if kind in p.cap_applies_to and pos.weight * 100 > p.max_single_position_pct:
        return Badge("Too big", "bad")
    if pos.drift_pp >= p.overweight_trigger_pp:
        return Badge("Above plan", "warn")
    if pos.drift_pp <= -p.underweight_trigger_pp:
        return Badge("Room to add", "good")
    return Badge("On plan", "ok")


# =============================================================================================== plain English
_RULE_ID = re.compile(r"\bR-[A-Z]{3}-\d{2}\b")
_RULE_PAREN = re.compile(r"\s*[\(\[]\s*(?:R-[A-Z]{3}-\d{2}(?:[,/ ]+(?:and\s+)?)?)+[\)\]]")
_BEGINNER: list[tuple[re.Pattern, str]] = [(re.compile(p, re.I), r) for p, r in [
    (r"\(RSI\s*\d+(?:\.\d+)?\)", ""),
    (r"RSI\(?14?\)?\s*[\d.]+\s*[<>]=?\s*\d+\s*:?\s*", "the price recently moved a lot: "),
    (r"RSI\(?14?\)?\s*is\s*", "the price is "),
    (r"RSI\(?14?\)?\s*\d+(?:\.\d+)?\s*", "recent momentum "),
    (r"RSI\s*(alert)?", "momentum"),
    (r"\boversold\b", "sold off hard"), (r"\boverbought\b", "overheated"),
    (r"200-day SMA", "200-day average price"), (r"\bSMA(?:\s*50/200|\s*200|\s*50)?\b", "average price"),
    (r"\bPEG\b(?:\s*/\s*forward P/E)?", "growth-adjusted price"), (r"\bforward P/E\b", "expected price-to-earnings"),
    (r"\bOCF\b", "fund fee"), (r"look-through", "including fund holdings"),
    (r"\bDCA\b", "regular monthly investing"), (r"\btranche(s)?\b", "instalment\\1"),
    (r"(\d+(?:\.\d+)?)\s*pp\b", r"\1 percentage points"), (r"Dividend Cover", "Dividend Safety"), (r"dividend cover", "dividend safety"),
    (r"\bconviction\b", "confidence"), (r"\bconsensus\b", "analyst view"), (r"Section 104(?: average cost)?", "average cost"),
    (r"\bdrawdown\b", "market fall"), (r"\bflex(?:ible)?-credit\b", "flexible-ISA credit"),
]]


_RULE_RUN = re.compile(r"(?:R-[A-Z]{3}-\d{2}(?:\s*(?:,|/|and|&)\s*)?)+\s*")


def strip_rule_ids(text: str) -> str:
    out = _RULE_PAREN.sub("", text)
    out = _RULE_RUN.sub("", out)                       # bare ids, incl. comma-separated runs like "R-CAP-01, R-UPS-01"
    out = re.sub(r"([:;(])\s*,", r"\1", out)
    return re.sub(r"\s{2,}", " ", re.sub(r"\s+([,.;:])", r"\1", out)).strip()


def plain_text(text: str, level: str) -> str:
    """Re-word one line for the level. Advanced: untouched. Intermediate: rule IDs removed. Beginner: jargon translated too."""
    lvl = normalise_level(level)
    if lvl == "advanced":
        return text
    out = strip_rule_ids(text)
    if lvl == "beginner":
        for pat, rep in _BEGINNER:
            out = pat.sub(rep, out)
        out = re.sub(r"\s{2,}", " ", out).strip()
    return out


def rule_summary(rule_id: str, r: HandbookRules) -> str:
    p, c, q, a, t, tx, cs, dd = r.position, r.costs, r.quality, r.analyst, r.technicals, r.tax, r.cash, r.drawdown
    return {
        "R-CAP-01": f"No single stock or trust above {p.max_single_position_pct:.0f}% of the ISA.",
        "R-CAP-02": f"No industry above {p.max_sector_pct:.0f}% once fund holdings are counted.",
        "R-REB-01": f"Add when {p.underweight_trigger_pp:.0f}+ points under target; stop adding at {p.overweight_trigger_pp:.0f}+ over.",
        "R-OCF-01": f"Fund fees: at most {c.max_ocf_pct_passive:.2f}% for index funds, {c.max_ocf_pct_active:.2f}% for active.",
        "R-DIV-01": f"Dividends should be covered {q.min_dividend_cover:.1f}x by profits.",
        "R-VAL-01": f"Growth-adjusted P/E at most {q.max_peg:g}, or forward P/E at most {q.max_forward_pe:.0f}.",
        "R-UPS-01": f"A confident buy needs {a.min_upside_pct:.0f}%+ analyst upside from {a.min_analysts}+ analysts.",
        "R-UPS-02": f"Re-check when the price is {abs(a.review_below_target_pct):.0f}%+ above the analyst target.",
        "R-RSI-01": f"Momentum extremes: sold-off below {t.rsi_oversold:.0f}, overheated above {t.rsi_overbought:.0f}.",
        "R-TRD-01": f"Be careful adding when {abs(t.caution_below_sma200_pct):.0f}%+ below the long-term average price.",
        "R-VAL-02": f"Price vs its own 5-year average: rich at +{t.rich_vs_5y_avg_pct:.0f}%, cheap at {t.cheap_vs_5y_avg_pct:.0f}%.",
        "R-TAX-01": "Use the £20,000 ISA allowance before 5 April; unused room is lost.",
        "R-TAX-02": "Lifetime ISA: £4,000 a year inside the £20,000, with a 25% government bonus.",
        "R-BED-01": "Move taxable holdings into the ISA, realising gains within the £3,000 CGT allowance.",
        "R-CSH-01": f"Hold about {cs.reserve_target_pct:.0f}% cash; put anything above {cs.max_cash_drag_pct:.0f}% to work.",
        "R-DD-01": "Invest the cash reserve in tiers as markets fall " + "/".join(f"{x.drop_pct:.0f}%" for x in dd.tiers) + ".",
    }.get(rule_id, "")


def what_it_means(sig: Signal, rules: HandbookRules, name_of: Callable[[str], str] = lambda t: t) -> str:
    """One or two plain sentences under a signal (Beginner mode)."""
    a, nm, m = sig.action, (name_of(sig.ticker) if sig.ticker else ""), sig.metrics
    cap = rules.position.max_single_position_pct
    d = sig.detail.lower()
    if a == "TAX ALERT":
        if sig.id == "TAX:BREACH" or sig.id == "TAX:LISA-BREACH":
            return "You have paid in more than the yearly limit allows. Contact your ISA provider soon so they can put it right."
        if sig.id == "TAX:LISA":
            return (f"The government adds 25p for every £1 you put in a Lifetime ISA. You have {money(m.get('lisa_remaining', 0))} of room left this year, "
                    f"so up to {money(m.get('bonus', 0))} of free bonus is still unclaimed.")
        return (f"You can still put {money(m.get('headroom', 0))} into ISAs this tax year. Whatever is unused disappears on 5 April, "
                "so a regular monthly payment is an easy way to use it.")
    if a == "BED & ISA":
        return ("You hold investments in a taxable account that could move into your ISA. Doing it now uses this year's tax-free profit allowance, "
                "and from then on their growth and dividends are tax-free.")
    if a == "TRIM WARNING":
        if sig.ticker is None:
            return "Too much of your money leans on one industry once you count what your funds hold. That makes your ISA riskier than it looks."
        return (f"{nm} has grown bigger than the limit you set yourself ({cap:.0f}% of your ISA). The gentle fix is to send new money elsewhere "
                "so it shrinks as a share, without selling.")
    if a == "STOP ADDING":
        return f"{nm} is already above the share you planned for it, so there is no need to add more for now."
    if a == "ACCUMULATE":
        grow = " and analysts think it has room to grow" if "upside" in d and "<" not in d else ""
        return f"{nm} is a smaller slice of your ISA than you planned{grow}. Adding a little this month moves you closer to your plan."
    if a == "HOLD OFF":
        if "sector cap" in d:
            return f"{nm} is below plan, but your ISA already leans heavily on its industry. Better to add elsewhere first."
        return f"{nm} has risen fast lately. Buying in smaller steps over several weeks is calmer than buying all at once."
    if a == "SCREEN FAIL":
        return f"{nm} fails one of your own checks, so the planner will not add to it until you have looked at why."
    if a == "REVIEW":
        return f"Analysts think {nm} has already risen past where they expect it to go. Worth re-checking why you own it."
    if a == "DEPLOY":
        if sig.id == "CASH:DRAG":
            return "You are holding more spare cash than planned. The monthly plan shows where it could go to work."
        return f"{nm}'s price has dropped sharply. For something you still want to own for years, a dip can be a good moment to add your usual amount."
    if a == "DEPLOY RESERVE":
        return "Markets have fallen far enough to trigger the plan you wrote for this. It suggests investing part of your cash reserve in stages."
    if a == "CAUTION":
        return f"A heads-up about {nm}, not an instruction. It only argues against buying a large amount all at once."
    if a in ("PRICE ALERT", "RSI ALERT"):
        return f"A level you asked to be told about has been crossed for {nm}."
    if a == "DATA WARNING":
        return "The numbers on screen are not live right now. Please don't act on them."
    return ""


@dataclass(frozen=True)
class SignalView:
    headline: str
    rationale: tuple[str, ...]
    rules_line: str
    meaning: str


def render_signal(sig: Signal, level: str, rules: HandbookRules, name_of: Callable[[str], str] = lambda t: t) -> SignalView:
    lvl = normalise_level(level)
    head = plain_text(sig.headline, lvl)
    why = tuple(plain_text(x, lvl) for x in sig.rationale)
    if lvl == "advanced":
        return SignalView(head, why, ", ".join(sig.rules) or "—", "")
    if lvl == "intermediate":
        sums = [s for s in (rule_summary(r, rules) for r in sig.rules) if s]
        return SignalView(head, why, " ".join(sums), "")
    return SignalView(head, why, "", what_it_means(sig, rules, name_of))


# =============================================================================================== small visuals / audit
def allowance_bar(used: float, limit: float, width: int = 30) -> tuple[str, str, float]:
    """(filled, empty, pct) for the Beginner £20,000 allowance bar."""
    pct = max(0.0, min(100.0, used / limit * 100)) if limit else 0.0
    n = round(pct / 100 * width)
    return "█" * n, "░" * (width - n), pct


def unit_audit(q: Quote | None) -> str | None:
    """Advanced-mode audit line showing exactly how a price was normalised (the GBp/GBX -> GBP rule)."""
    if q is None or q.price_native is None or q.is_non_security:
        return None
    src = f"{q.provider or '?'} · " if q.provider else ""
    rc = q.raw_currency or "?"
    if rc in ("GBp", "GBX"):
        return f"unit audit: {src}{q.price_native * 100:,.2f} {rc} (pence) ÷ 100 = £{q.price_native:,.2f}"
    if q.currency == "GBP":
        return f"unit audit: {src}quoted in GBP — no conversion (£{q.price_native:,.2f})"
    if q.fx_rate and q.price_gbp is not None:
        return f"unit audit: {src}{q.currency} {q.price_native:,.2f} ÷ {q.fx_rate:.4f} GBP{q.currency} = £{q.price_gbp:,.2f}"
    return f"unit audit: {src}{rc} {q.price_native:,.2f} — {q.note or 'no GBP conversion available'}"
