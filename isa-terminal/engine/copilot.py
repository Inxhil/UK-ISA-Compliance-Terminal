"""Local, deterministic "AI Co-Pilot": portfolio-health summary, weekly Investment-Committee memo, Q&A over the desk state,
and an LLM-ready prompt exporter.

No model and no network are used — every sentence is generated from the same numbers the Command Desk shows, so it can
never contradict the signals. If you *want* a language model's help, ``prompt`` writes a context-packed prompt you can
paste into any assistant (nothing is sent anywhere by this program).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

from engine import DISCLAIMER
from engine.desk import Desk, DeskState
from engine.recommender import Signal, fnum, money

HELP = """\
**Co-Pilot commands** (type naturally — keywords are matched)
- `health` — portfolio health score and what drives it
- `memo` — draft the weekly Investment Committee memo (also saved to `reports/`)
- `tax` — ISA allowance, LISA, flexible-withdrawal credit, deadline pace
- `buy 750` — exact £ buy plan for new cash, no selling (blank = monthly cash)
- `explain NVDA` — why a ticker is (or isn't) flagged, with the handbook rules
- `sim VUAG.L 2000` — ISA Impact Simulator: yield / OCF / sector effect of buying £2,000
- `signals` — top actions;  `risk` — concentration & sector;  `prompt` — export an LLM-ready prompt
"""


@dataclass(frozen=True)
class HealthComponent:
    name: str
    score: float
    weight: float
    note: str


@dataclass(frozen=True)
class Health:
    score: float
    grade: str
    components: tuple[HealthComponent, ...]

    @property
    def drivers(self) -> list[HealthComponent]:
        return sorted(self.components, key=lambda c: (c.score - 100) * c.weight)[:2]


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def portfolio_health(st: DeskState) -> Health:
    pf, r, isa = st.portfolio, st.rules, st.isa
    comps: list[HealthComponent] = []

    cap_breaches = [s for s in st.signals if s.action == "TRIM WARNING"]
    conc = _clamp(100 - 25 * len([s for s in cap_breaches if not s.id.startswith("SECTOR")]) - 15 * len([s for s in cap_breaches if s.id.startswith("SECTOR")]))
    comps.append(HealthComponent("Concentration", conc, 25, f"{len(cap_breaches)} cap breach(es)" if cap_breaches else "all caps respected"))

    comps.append(HealthComponent("Diversification", _clamp((pf.effective_n - 2) / 4 * 100), 10, f"effective N = {pf.effective_n:.1f} positions"))

    ocf = pf.blended_ter_pct
    if ocf is not None:
        comps.append(HealthComponent("Cost", _clamp(100 - max(0.0, (ocf - 0.10) / 0.30 * 100)), 15, f"blended OCF {ocf:.2f}%"))

    drift = st.plan.pre_drift * 100 if st.plan else 0.0
    comps.append(HealthComponent("Allocation drift", _clamp(100 - drift * 5), 20, f"{drift:.1f}% of the pot is away from target"))

    head = float(isa.headroom)
    if isa.has_breach:
        pace_score, note = 0.0, "ALLOWANCE BREACH"
    elif head <= 0:
        pace_score, note = 100.0, "allowance fully used"
    else:
        projected = st.monthly_cash * st.pace.months_left
        pace_score = _clamp(projected / head * 100)
        note = f"{money(head)} unused; plan funds {pace_score:.0f}% of it by 5 April"
    comps.append(HealthComponent("Allowance pace", pace_score, 20, note))

    cash_gap = abs(pf.cash_weight * 100 - r.cash.reserve_target_pct)
    comps.append(HealthComponent("Cash sleeve", _clamp(100 - 15 * cash_gap), 5, f"{pf.cash_weight * 100:.1f}% vs {r.cash.reserve_target_pct:.0f}% target"))

    dq = {"LIVE": 100.0, "STALE": 50.0, "DEMO": 0.0, "OFFLINE": 0.0}.get(st.data_label, 0.0)
    comps.append(HealthComponent("Data quality", dq, 5, st.data_label))

    total = sum(c.score * c.weight for c in comps) / sum(c.weight for c in comps)
    grade = "A" if total >= 85 else "B" if total >= 70 else "C" if total >= 55 else "D" if total >= 40 else "E"
    return Health(round(total, 1), grade, tuple(comps))


# ---------------------------------------------------------------------------------------------------- text blocks
def _pos_table(st: DeskState) -> str:
    rows = ["| Ticker | Value | Weight | Target | Drift | Consensus | Upside | RSI | Score |", "|---|---:|---:|---:|---:|---|---:|---:|---:|"]
    for p in st.portfolio.positions:
        s = st.snaps.get(p.ticker)
        sc = st.scores.get(p.ticker)
        rows.append(f"| {p.ticker} | {money(p.value_gbp)} | {p.weight * 100:.1f}% | {p.target_weight * 100:.0f}% | {p.drift_pp:+.1f}pp | "
                    f"{s.rating_label if s else '—'} | {fnum(s.upside_pct if s else None, '+.0f', '%')} | {fnum(s.rsi14 if s else None, '.0f')} | "
                    f"{fnum(sc.total if sc else None, '.0f')} |")
    rows.append(f"| Cash | {money(st.portfolio.cash_gbp)} | {st.portfolio.cash_weight * 100:.1f}% | {st.rules.cash.reserve_target_pct:.0f}% | | | | | |")
    return "\n".join(rows)


def tax_text(st: DeskState) -> str:
    isa, cd, pace = st.isa, st.countdown, st.pace
    L = [f"**ISA {isa.tax_year.label}** — limit {money(float(isa.limit))} (unified), LISA sub-limit {money(float(isa.rules.lisa_limit))} inside it.",
         f"- Subscribed {money(float(isa.subscriptions), 2)}; flexible withdrawals this year {money(float(isa.eligible_withdrawals), 2)}; "
         f"**net used {money(float(isa.net_used), 2)} ({isa.pct_used:.1f}%)**.",
         f"- **Remaining {money(float(isa.remaining), 2)}**" + (f" + {money(float(isa.replacement_credit), 2)} replacement credit (same-account only)" if isa.replacement_credit > 0 else "") + ".",
         f"- Deadline: end of 5 April {isa.tax_year.end.year}, London — **{cd.long()}**. Practical cut-off {pace.practical_cutoff:%d %b %Y}.",
         f"- To use it all: **{money(float(pace.required_per_month))}/month** vs plan {money(st.monthly_cash)}/month."]
    if isa.lisa_remaining > 0:
        L.append(f"- LISA: {money(float(isa.lisa_subscriptions))} in, {money(float(isa.lisa_remaining))} unused → {money(float(isa.lisa_bonus_forgone))} bonus forgone if left.")
    if isa.ignored_transfers or isa.ignored_bonus or isa.non_flexible_withdrawals:
        L.append(f"- Ignored for the allowance: transfers {money(float(isa.ignored_transfers))}, LISA bonus {money(float(isa.ignored_bonus), 2)}"
                 + (f"; non-flexible withdrawals {money(float(isa.non_flexible_withdrawals))} (no replacement room)" if isa.non_flexible_withdrawals else "") + ".")
    if isa.has_breach:
        L.append(f"- **BREACH**: over by {money(float(isa.breach), 2)} (LISA over by {money(float(isa.lisa_breach), 2)}). Contact the provider.")
    if st.bed_isa and st.bed_isa.lines:
        b = st.bed_isa
        L.append(f"- Bed & ISA: could realise {money(b.total_gain)} within the {money(b.cgt_allowance_start)} CGT allowance left, moving {money(b.total_proceeds)} into the ISA.")
    L.extend(f"- ⚠ {w}" for w in isa.warnings)
    return "\n".join(L)


def buy_text(desk: Desk, st: DeskState, cash: float) -> str:
    plan = desk.buy_plan(st, cash)
    L = [f"**Buy plan for {money(cash, 2)} new cash — no selling**"]
    if not plan.orders:
        L.append("No eligible names (all blocked or at target). " + " ".join(plan.warnings))
        return "\n".join(L)
    L += ["", "| Ticker | £ | Units | Weight (now → after) | Target | Why |", "|---|---:|---:|---|---:|---|"]
    for o in plan.orders:
        L.append(f"| {o.ticker} | {o.amount:,.2f} | {fnum(o.units, ',.3f')} | {o.pre_weight * 100:.1f}% → {o.post_weight * 100:.1f}% | "
                 f"{o.target_weight * 100:.0f}% | {o.reason or 'closest to target'} |")
    L.append(f"\nDeployed {money(float(plan.deployed), 2)}; undeployed {money(float(plan.undeployed), 2)}. "
             f"Drift from target {plan.pre_drift * 100:.1f}% → {plan.post_drift * 100:.1f}%.")
    from engine.recommender import blocked_tickers
    b = sorted(blocked_tickers(st.signals))
    if b:
        L.append(f"Excluded by handbook rules: {', '.join(b)}.")
    L.extend(f"⚠ {w}" for w in plan.warnings)
    return "\n".join(L)


def explain(st: DeskState, ticker: str) -> str:
    t = next((w.ticker for w in st.config.watchlist if w.ticker.upper() in (ticker.upper(), ticker.upper() + ".L")), None) or ticker.upper()
    s, p, sc = st.snaps.get(t), st.portfolio.get(t), st.scores.get(t)
    if s is None and p is None:
        return f"No data for `{ticker}`. Watchlist: {', '.join(st.config.tickers)}."
    L = [f"**{t}** — {s.name if s else t}"]
    if p and p.value_gbp:
        L.append(f"- Position {money(p.value_gbp)} ({p.weight * 100:.1f}% of ISA; target {p.target_weight * 100:.0f}%, {p.drift_pp:+.1f}pp); "
                 f"P&L {money(p.pnl_gbp)} ({fnum(p.pnl_pct, '+.1f', '%')}).")
    if s:
        L.append(f"- Price {fnum(s.price_gbp, ',.2f')} GBP ({fnum(s.day_change_pct, '+.2f', '%')} today); RSI {fnum(s.rsi14, '.0f')}; trend {s.trend}; "
                 f"SMA50/200 {fnum(s.sma50)}/{fnum(s.sma200)} ({s.currency}).")
        L.append(f"- Consensus: {s.rating_label}" + (f" ({s.n_analysts} analysts), mean target {fnum(s.target_mean)} = {fnum(s.upside_pct, '+.1f', '%')}" if s.target_mean else " — no analyst targets") + ".")
        L.append(f"- Valuation: fwd P/E {fnum(s.forward_pe, '.1f')}, PEG {fnum(s.peg)}, yield {fnum(s.dividend_yield_pct, '.2f', '%')}"
                 f"{' (est.)' if s.yield_is_estimate else ''}, vs 5Y avg price {fnum(s.vs_5y_avg_pct, '+.0f', '%')} → {s.valuation_label}.")
    if sc and sc.total is not None:
        L.append(f"- Conviction score **{sc.total:.0f}/100 ({sc.label})**: " + ", ".join(f"{k} {v:.0f}" for k, v in sc.components.items()) + ".")
    sigs = st.signals_for(t)
    if not sigs:
        L.append("- **No rule is flagging this name** (nothing outside the handbook thresholds).")
    for g in sigs:
        L.append(f"\n**{g.headline}** [{g.severity}] — rules: {', '.join(g.rules) or 'custom alert'}")
        L.extend(f"  - {x}" for x in g.rationale)
    if s and s.notes:
        L.append("\n_Data notes:_ " + "; ".join(s.notes))
    return "\n".join(L)


def signals_text(st: DeskState, n: int = 6) -> str:
    L = [f"**Top {n} signals** (of {len(st.signals)})"]
    for s in st.signals[:n]:
        L.append(f"- `{s.severity}` **{s.action}**{' ' + s.ticker if s.ticker else ''} — {s.detail}  _[{', '.join(s.rules) or 'custom'}]_")
    return "\n".join(L)


def risk_text(st: DeskState) -> str:
    pf, r = st.portfolio, st.rules
    L = ["**Concentration & sector (look-through)**"]
    for p in pf.positions[:6]:
        capped = next((w.kind for w in st.config.watchlist if w.ticker == p.ticker), "stock") in r.position.cap_applies_to
        L.append(f"- {p.ticker}: {p.weight * 100:.1f}%" + (f" (cap {r.position.max_single_position_pct:.0f}%)" if capped else " (core ETF — exempt from the single-name cap)"))
    L.append("- Sectors: " + ", ".join(f"{k} {v * 100:.1f}%" for k, v in list(pf.sector_exposure.items())[:6]) + f" (cap {r.position.max_sector_pct:.0f}%)")
    L.append(f"- Effective number of positions {pf.effective_n:.1f}; blended OCF {fnum(pf.blended_ter_pct, '.2f', '%')}; yield {fnum(pf.yield_pct, '.2f', '%')}.")
    return "\n".join(L)


def health_text(st: DeskState) -> str:
    h = portfolio_health(st)
    L = [f"**Portfolio health: {h.score:.0f}/100 — grade {h.grade}**", "",
         "| Factor | Score | Weight | Note |", "|---|---:|---:|---|"]
    L += [f"| {c.name} | {c.score:.0f} | {c.weight:.0f} | {c.note} |" for c in h.components]
    L.append("")
    L.append("Biggest drags: " + "; ".join(f"**{c.name}** ({c.note})" for c in h.drivers if c.score < 100) + ".")
    pf = st.portfolio
    L.append(f"\nValue {money(pf.total_gbp)} (invested {money(pf.invested_gbp)} {fnum(pf.pnl_pct, '+.1f', '%')} vs cost; cash {money(pf.cash_gbp)}). "
             f"Data: **{st.data_label}**.")
    return "\n".join(L)


def weekly_memo(st: DeskState, desk: Desk | None = None) -> str:
    now = st.now
    h = portfolio_health(st)
    pf = st.portfolio
    L = [f"# Weekly Investment Committee Memo — w/c {now:%d %b %Y}",
         f"*Generated {now:%Y-%m-%d %H:%M} (Europe/London) · Data: **{st.data_label}** · {DISCLAIMER}*", ""]
    if st.data_label == "DEMO":
        L += ["> ⚠ **DEMO DATA** — market figures below are synthetic. Do not act on this memo.", ""]
    L += ["## 1. Portfolio health", health_text(st), "",
          "## 2. Snapshot", f"Total {money(pf.total_gbp)}; invested {money(pf.invested_gbp)} ({fnum(pf.pnl_pct, '+.1f', '%')} vs cost); "
          f"yield {fnum(pf.yield_pct, '.2f', '%')}; blended OCF {fnum(pf.blended_ter_pct, '.2f', '%')}; top sector "
          f"{pf.top_sector[0] + ' ' + format(pf.top_sector[1] * 100, '.1f') + '%' if pf.top_sector else '—'}.", "", _pos_table(st), "",
          "## 3. Signals that need a decision"]
    top = [s for s in st.signals if s.severity in ("CRITICAL", "HIGH", "MEDIUM")][:7] or st.signals[:3]
    for s in top:
        L.append(f"- **{s.severity} · {s.action}**{' ' + s.ticker if s.ticker else ''} — {s.detail}. _Why:_ {s.rationale[0] if s.rationale else ''} "
                 f"_Rules: {', '.join(s.rules) or 'custom'}_")
    L += ["", "## 4. Tax & allowance", tax_text(st), ""]
    if desk is not None:
        L += ["## 5. This month's buy plan", buy_text(desk, st, st.monthly_cash), ""]
    elif st.plan is not None:
        L += ["## 5. This month's buy plan", f"{len(st.plan.orders)} order(s), {money(float(st.plan.deployed), 2)} deployed.", ""]
    L += ["## 6. Decisions (fill in, then copy to handbook §10 review log)", "",
          "| Decision | Reason | Rule |", "|---|---|---|", "| | | |", "",
          "## 7. Data notes"]
    notes = [f"{t}: {n}" for t, s in st.snaps.items() for n in s.notes]
    L += [f"- {n}" for n in notes[:8]] or ["- No data-quality issues recorded."]
    L += [f"- Feed errors: {e}" for e in st.status.errors[-3:]]
    return "\n".join(L)


def llm_prompt(st: DeskState, question: str | None = None) -> str:
    rules_block = ""
    m = re.search(r"```yaml handbook-rules\n(.*?)\n```", st.handbook_text, re.DOTALL)
    if m:
        rules_block = m.group(1)
    return "\n".join([
        "You are helping me review my personal UK Stocks & Shares ISA against my own written handbook.",
        "Rules of engagement: this is a modelling exercise, NOT regulated financial advice. Use only the data below; say when data is missing;",
        "cite the handbook rule ID behind each point; challenge my assumptions; do not invent prices, targets or tax rules.",
        "",
        f"## Context (generated {st.now:%Y-%m-%d %H:%M} Europe/London; market data: {st.data_label})",
        tax_text(st), "", "## Positions", _pos_table(st), "",
        "## Signals fired by my rule engine", signals_text(st, 10), "",
        "## My handbook rules (machine-readable block)", "```yaml", rules_block, "```", "",
        "## Task", question or "1) Summarise portfolio health in 5 bullets. 2) Which 3 decisions matter most this month and why? "
        "3) What am I most likely to be wrong about? 4) Draft a one-page Investment Committee memo.",
    ])


def save_text(st: DeskState, name: str, text: str) -> Path:
    d = st.config.reports_dir
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}_{st.now:%Y-%m-%d}.md"
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------------------------------- query router
class CoPilot:
    def __init__(self, desk: Desk):
        self.desk = desk

    def _ticker_in(self, st: DeskState, q: str) -> str | None:
        up = q.upper()
        for w in sorted(st.config.watchlist, key=lambda w: -len(w.ticker)):
            base = w.ticker.upper().removesuffix(".L")
            if re.search(rf"(?<![A-Z0-9]){re.escape(w.ticker.upper())}(?![A-Z0-9])", up) or re.search(rf"(?<![A-Z0-9]){re.escape(base)}(?![A-Z0-9])", up):
                return w.ticker
        return None

    def respond(self, query: str, st: DeskState | None = None) -> str:
        st = st or self.desk.last
        if st is None:
            return "No data loaded yet — wait for the first refresh (or press `r`)."
        q = query.strip()
        low = q.lower()
        if not q or re.search(r"^(help|\?|commands)", low):
            return HELP
        tkr = self._ticker_in(st, q)
        amt = re.search(r"£?\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?\b", low.replace(tkr.lower(), "") if tkr else low)
        amount = None
        if amt:
            amount = float(amt.group(1).replace(",", "")) * (1000 if amt.group(2) else 1)
        if re.search(r"\bsim|impact|what if\b", low) and tkr:
            r = self.desk.simulate(st, tkr, amount if amount is not None else st.monthly_cash)
            b, a = r.before, r.after
            L = [f"**ISA Impact Simulator — buy {money(r.amount_gbp, 2)} of {tkr}** ({'new subscription' if r.funded_from == 'new' else 'from ISA cash'})",
                 f"- Position weight {b.ticker_weight * 100:.1f}% → **{a.ticker_weight * 100:.1f}%**" + (f" ({r.units:,.3f} units)" if r.units else ""),
                 f"- Portfolio yield {fnum(b.yield_pct, '.2f', '%')} → **{fnum(a.yield_pct, '.2f', '%')}**",
                 f"- Blended OCF {fnum(b.blended_ter_pct, '.3f', '%')} → **{fnum(a.blended_ter_pct, '.3f', '%')}**",
                 f"- Top sector {b.top_sector[0] if b.top_sector else '—'} {fnum(b.top_sector[1] * 100 if b.top_sector else None, '.1f', '%')} → "
                 f"**{a.top_sector[0] if a.top_sector else '—'} {fnum(a.top_sector[1] * 100 if a.top_sector else None, '.1f', '%')}**"]
            if r.allowance_after_gbp is not None:
                L.append(f"- ISA headroom after: {money(r.allowance_after_gbp, 2)}")
            L += [f"- ⚠ {x}" for x in r.breaches] or ["- No handbook cap would be breached."]
            return "\n".join(L)
        if re.search(r"explain|why|tell me about|what about", low) and tkr:
            return explain(st, tkr)
        if re.search(r"\bprompt|\bllm\b|chatgpt|claude|gpt", low):
            p = save_text(st, "llm_prompt", llm_prompt(st, q if len(q) > 40 else None))
            return f"LLM-ready prompt written to `{p}` (nothing was sent anywhere). Paste it into the assistant of your choice."
        if re.search(r"memo|committee|weekly|\bic\b", low):
            text = weekly_memo(st, self.desk)
            p = save_text(st, "IC_memo", text)
            return text + f"\n\n---\nSaved to `{p}`."
        if re.search(r"health|summar|status|how am i|overview", low):
            return health_text(st)
        if re.search(r"tax|allowance|isa left|deadline|lisa|flexible|bed", low):
            return tax_text(st)
        if re.search(r"risk|concentrat|sector|cap\b", low):
            return risk_text(st)
        if re.search(r"signal|action|todo|priorit", low):
            return signals_text(st, 8)
        if re.search(r"buy|invest|deploy|plan|contribut|cash|rebalanc", low) or (amount and not tkr):
            return buy_text(self.desk, st, amount if amount is not None else st.monthly_cash)
        if tkr:
            return explain(st, tkr)
        return "I didn't match that to a command.\n\n" + HELP
