"""Rule-based + quantitative "Terminal Signals".

Cross-references the live portfolio, analyst consensus, technicals and the ISA tax position against the thresholds in
``handbook.md`` §0. Every signal:

* has a headline of the form ``ACTION - detail`` (e.g. ``ACCUMULATE - Underweight & >15% Analyst Upside``),
* carries a rationale (the *why*, with the numbers) and the handbook rule IDs that fired (``R-CAP-01`` …),
* has a severity and a numeric priority so the Command Desk can sort it.

Signals are *flags produced by rules*, not personal advice. Missing data never produces a signal by omission of
caution: an unknown value simply doesn't trigger the rule that needs it (and says so in the rationale where relevant).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Sequence

from engine.analyst_screener import AnalystSnapshot
from engine.config import AppConfig, HandbookRules, WatchItem
from engine.isa_engine import BedISAPlan, Countdown, ISAStatus, Pace
from engine.portfolio import Portfolio, Position

SEVERITIES = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")
BASE_PRIORITY = {"CRITICAL": 90, "HIGH": 75, "MEDIUM": 55, "LOW": 30, "INFO": 10}


def money(x: float, dp: int = 0) -> str:
    return f"{'-' if x < 0 else ''}£{abs(x):,.{dp}f}"


def fnum(x: float | None, spec: str = ",.2f", suffix: str = "") -> str:
    """Null-safe number formatting for rationale text: missing data renders as '—', never crashes."""
    return "—" if x is None else f"{x:{spec}}{suffix}"


def _bump(sev: str, n: int, ceiling: str = "HIGH") -> str:
    """Move a severity up/down, never above ``ceiling`` (CRITICAL is reserved for hard breaches / imminent deadlines)."""
    top = SEVERITIES.index(ceiling)
    return SEVERITIES[max(0, min(top, SEVERITIES.index(sev) + n))]


@dataclass(frozen=True)
class Signal:
    id: str
    action: str
    detail: str
    severity: str
    priority: float
    ticker: str | None = None
    rationale: tuple[str, ...] = ()
    rules: tuple[str, ...] = ()
    suggested_gbp: float | None = None
    side: str | None = None                      # 'BUY' | 'SELL' | None
    metrics: Mapping[str, object] = field(default_factory=dict)

    @property
    def headline(self) -> str:
        """``ACTION - detail`` (ticker-free, as in the spec's examples; the UI shows the ticker in its own column)."""
        return f"{self.action} - {self.detail}"

    @property
    def label(self) -> str:
        """Headline with the ticker, for plain-text contexts (memos, logs)."""
        return f"{self.action} {self.ticker} - {self.detail}" if self.ticker else self.headline

    @property
    def blocks_buying(self) -> bool:
        return self.action in ("TRIM WARNING", "STOP ADDING") and self.ticker is not None


def _sig(id_: str, action: str, detail: str, sev: str, ticker: str | None = None, *, rationale: Sequence[str] = (),
         rules: Sequence[str] = (), gbp: float | None = None, side: str | None = None, adj: float = 0.0,
         **metrics: object) -> Signal:
    return Signal(id_, action, detail, sev, BASE_PRIORITY[sev] + adj, ticker, tuple(rationale), tuple(rules), gbp, side,
                  metrics)


@dataclass(frozen=True)
class SignalContext:
    now: datetime
    isa: ISAStatus
    countdown: Countdown
    pace: Pace
    portfolio: Portfolio
    snaps: Mapping[str, AnalystSnapshot]
    rules: HandbookRules
    config: AppConfig
    monthly_cash: float
    bed_isa: BedISAPlan | None = None
    data_label: str = "LIVE"                     # LIVE | STALE | DEMO | OFFLINE


# =============================================================================================
# Conviction score
# =============================================================================================
@dataclass(frozen=True)
class ScoreCard:
    total: float | None
    components: Mapping[str, float]
    label: str


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def screen_failures(snap: AnalystSnapshot | None, watch: WatchItem | None,
                    rules: HandbookRules) -> list[tuple[str, str, str]]:
    """Handbook §4 screens. Returns ``[(rule_id, short_headline, explanation)]``; unknown data never fails a screen."""
    out: list[tuple[str, str, str]] = []
    if snap is None:
        return out
    kind = watch.kind if watch else snap.kind
    if kind in ("etf", "trust") and snap.ter_pct is not None:
        limit = rules.costs.max_ocf_pct_passive if kind == "etf" else rules.costs.max_ocf_pct_active
        if snap.ter_pct > limit:
            out.append(("R-OCF-01", f"OCF {snap.ter_pct:.2f}% > {limit:.2f}% {'Passive' if kind == 'etf' else 'Active'} Limit",
                        f"OCF {snap.ter_pct:.2f}% exceeds the {'passive' if kind == 'etf' else 'active'} limit of {limit:.2f}%"))
    if kind == "stock":
        q = rules.quality
        if (snap.dividend_yield_pct or 0) > q.min_dividend_yield_for_cover_test_pct and snap.dividend_cover is not None \
                and snap.dividend_cover < q.min_dividend_cover:
            out.append(("R-DIV-01", f"Dividend Cover {snap.dividend_cover:.2f}x < {q.min_dividend_cover:.1f}x Minimum",
                        f"dividend cover {snap.dividend_cover:.2f}x is below the {q.min_dividend_cover:.1f}x minimum "
                        f"(yield {snap.dividend_yield_pct:.2f}%, payout {fnum(None if snap.payout_ratio is None else snap.payout_ratio * 100, '.0f', '%')})"))
        peg_bad = snap.peg is not None and snap.peg > q.max_peg
        pe_bad = snap.forward_pe is not None and snap.forward_pe > q.max_forward_pe
        known = [x for x in (snap.peg, snap.forward_pe) if x is not None]
        if known and ((peg_bad and pe_bad) or (len(known) == 1 and (peg_bad or pe_bad))):
            out.append(("R-VAL-01", "Fails PEG / Forward P/E Valuation Screen",
                        f"valuation fails: PEG {snap.peg if snap.peg is not None else '—'} (max {q.max_peg}) / "
                        f"fwd P/E {f'{snap.forward_pe:.1f}' if snap.forward_pe is not None else '—'} (max {q.max_forward_pe:.0f})"))
    return out


def score_security(snap: AnalystSnapshot | None, pos: Position | None, rules: HandbookRules,
                   watch: WatchItem | None = None) -> ScoreCard:
    """0-100 composite from the factors in handbook ``scoring`` weights; missing factors drop out and weights renormalise."""
    comps: dict[str, float] = {}
    if snap is not None:
        v: list[float] = []
        if snap.upside_pct is not None:
            v.append(_clamp((snap.upside_pct + 20) / 60 * 100))
        if snap.peg is not None and snap.peg > 0:
            v.append(_clamp((3.0 - snap.peg) / 2.5 * 100))
        if snap.vs_5y_avg_pct is not None:
            v.append(_clamp((50 - snap.vs_5y_avg_pct) / 80 * 100))
        if v:
            comps["valuation"] = sum(v) / len(v)
        if snap.rating_mean is not None:
            raw = _clamp((5 - snap.rating_mean) / 4 * 100)
            conf = min(1.0, (snap.n_analysts or 0) / max(rules.analyst.min_analysts, 1))
            comps["consensus"] = 50 + (raw - 50) * conf
        m: list[tuple[float, float]] = []
        if snap.rsi14 is not None:
            m.append((_clamp((rules.technicals.rsi_overbought - snap.rsi14) / 40 * 100), 0.6))
        if snap.trend != "N/A":
            m.append(({"UPTREND": 70.0, "MIXED": 50.0, "DOWNTREND": 35.0}[snap.trend], 0.4))
        if m:
            comps["momentum"] = sum(a * w for a, w in m) / sum(w for _, w in m)
        if (snap.kind in ("etf", "trust") and snap.ter_pct is not None) or snap.kind == "stock":
            q = 100.0 - 50.0 * len([f for f in screen_failures(snap, watch, rules) if f[0] == "R-OCF-01"]) \
                      - 40.0 * len([f for f in screen_failures(snap, watch, rules) if f[0] in ("R-DIV-01", "R-VAL-01")])
            comps["quality"] = _clamp(q)
    if pos is not None and pos.target_weight > 0:
        comps["allocation"] = _clamp(50 - pos.drift_pp * 10)
    weights = {k: getattr(rules.scoring, k) for k in comps}
    if len(comps) < 2:        # one factor alone (e.g. only 'quality' = 100) is not a score: report N/A, keep the factors
        return ScoreCard(None, {k: round(v, 1) for k, v in comps.items()}, "N/A")
    total = sum(comps[k] * weights[k] for k in comps) / sum(weights.values())
    label = "STRONG" if total >= 70 else "FAVOURABLE" if total >= 55 else "NEUTRAL" if total >= 40 else "WEAK"
    return ScoreCard(round(total, 1), {k: round(v, 1) for k, v in comps.items()}, label)


# =============================================================================================
# Signal generators
# =============================================================================================
def _tax_signals(c: SignalContext) -> list[Signal]:
    isa, cd, pace, r = c.isa, c.countdown, c.pace, c.rules.tax
    out: list[Signal] = []
    label = isa.tax_year.label
    if isa.breach > 0:
        out.append(_sig("TAX:BREACH", "TAX ALERT", f"Over-Subscribed by {money(float(isa.breach), 2)}", "CRITICAL",
                        rationale=[f"Net subscriptions exceed the {money(float(isa.limit))} {label} limit by {money(float(isa.breach), 2)}.",
                                   "Contact the provider(s) promptly: excess subscriptions can be removed and may be reported to HMRC.",
                                   "Check the ledger for deposits mis-tagged to the wrong tax year or a missing flexible-withdrawal flag."],
                        rules=["R-TAX-01"], adj=9))
    if isa.lisa_breach > 0:
        out.append(_sig("TAX:LISA-BREACH", "TAX ALERT", f"LISA Over £{float(isa.rules.lisa_limit):,.0f} Sub-Limit by {money(float(isa.lisa_breach), 2)}",
                        "CRITICAL", rationale=["The LISA sub-limit counts inside the overall allowance; the excess earns no bonus and must be removed."],
                        rules=["R-TAX-02"], adj=8))
    head = float(isa.headroom)
    if head > 0 and not cd.expired:
        days = cd.total_seconds / 86400
        sev = "LOW"
        for d, s in zip(r.alert_days, ("MEDIUM", "HIGH", "CRITICAL")):
            if days <= d:
                sev = s
        planned = c.monthly_cash
        projected = planned * pace.months_left
        on_pace = projected >= head - 0.5
        if on_pace and sev == "LOW":
            sev = "INFO"
        req = float(pace.required_per_month)
        why = [f"{money(head, 2)} of the {money(float(isa.limit))} {label} allowance is unused ({isa.pct_used:.1f}% used). "
               "It is use-it-or-lose-it: it cannot be carried into the next tax year.",
               f"Deadline: end of 5 April {isa.tax_year.end.year} (London) — {cd.long()} left. Practical funding cut-off "
               f"{pace.practical_cutoff:%a %d %b %Y} ({max(pace.days_to_cutoff, 0)} days; brokers need cleared funds).",
               f"Run-rate to use it all: {money(req)}/month vs planned {money(planned)}/month — "
               + ("on pace." if on_pace else f"projected shortfall {money(head - projected)} by the deadline.")]
        if isa.replacement_credit > 0:
            why.append(f"Includes {money(float(isa.replacement_credit), 2)} of flexible-ISA replacement credit "
                       "(re-subscribable only into the account the cash left; also expires on 5 April).")
        why.append("Do not force a trade to use cash — the subscription is what counts; cash can sit in the ISA (handbook §7).")
        out.append(_sig("TAX:ALLOWANCE", "TAX ALERT", f"{money(head)} Allowance Expiring in {int(days)} days", sev,
                        rationale=why, rules=["R-TAX-01"], gbp=req, side="BUY", adj=-days / 365,
                        headroom=head, days_left=round(days, 1), required_per_month=req))
    lisa_cfg = any(a.kind == "LISA" for a in c.config.accounts.values())
    if lisa_cfg and isa.lisa_remaining > 0 and not cd.expired:
        days = cd.total_seconds / 86400
        sev = "LOW"
        for d, s in zip(r.alert_days, ("MEDIUM", "HIGH", "CRITICAL")):
            if days <= d:
                sev = s
        bonus = float(isa.lisa_bonus_forgone)
        out.append(_sig("TAX:LISA", "TAX ALERT", f"{money(float(isa.lisa_remaining))} LISA Room Unused = {money(bonus)} Bonus at Risk",
                        sev, rationale=[f"LISA sub-limit {money(float(isa.rules.lisa_limit))}/yr; {money(float(isa.lisa_subscriptions))} paid in so far.",
                                        f"Each unused £1 forfeits £{float(isa.rules.lisa_bonus_rate):.2f} of government bonus: {money(bonus)} at stake.",
                                        "Only worthwhile if the money is not needed before 60 (or a first-home purchase) — handbook §2/§7.",
                                        "LISA subscriptions also use the unified allowance: topping up reduces room elsewhere."],
                        rules=["R-TAX-02"], gbp=float(isa.lisa_remaining), side="BUY", adj=-days / 400,
                        lisa_remaining=float(isa.lisa_remaining), bonus=bonus))
    return out


def _bed_signal(c: SignalContext) -> list[Signal]:
    p = c.bed_isa
    if not p or not p.lines or p.total_proceeds <= 0:
        return []
    r = c.rules.tax
    if abs(p.total_gain) < r.bed_and_isa_min_gain_gbp and p.total_gain >= 0:
        return []
    days = c.countdown.total_seconds / 86400
    sev = "HIGH" if days <= 30 else "MEDIUM"
    why = [f"Remaining CGT exempt amount {money(p.cgt_allowance_start)} → {money(max(p.cgt_allowance_left, 0))} after these sales; "
           f"ISA headroom {money(p.isa_headroom_start)} → {money(p.isa_headroom_left)}.",
           f"Limited by: {p.limited_by.replace('_', ' ')}."]
    for l in p.lines:
        why.append(f"Sell {l.units:,.2f} {l.ticker} (≈{money(l.proceeds)}, {l.fraction_of_position * 100:.0f}% of the GIA holding): "
                   f"realised {'gain' if l.realised_gain >= 0 else 'loss'} {money(abs(l.realised_gain))}; re-buy inside the ISA.")
        pos = c.portfolio.get(l.ticker)
        w = c.config.watch(l.ticker)
        if pos and w and w.kind in c.rules.position.cap_applies_to and \
                (pos.value_gbp + l.proceeds) / (c.portfolio.total_gbp + l.proceeds) * 100 > c.rules.position.max_single_position_pct:
            why.append(f"⚠ Re-buying {l.ticker} in the ISA would push it over the {c.rules.position.max_single_position_pct:.0f}% cap (R-CAP-01) — "
                       "consider buying a different name inside the ISA.")
    used = p.isa_headroom_start - p.isa_headroom_left
    if used > 0:
        why.append(f"Trade-off: this subscribes {money(used)} of your {money(p.isa_headroom_start)} ISA headroom, leaving "
                   f"{money(p.isa_headroom_left)} for new cash this tax year. Weigh it against your monthly plan (handbook §8).")
    why.extend(p.warnings)
    return [_sig("BED:ISA", "BED & ISA", f"Realise {money(p.total_gain)} Gain in {money(p.cgt_allowance_start)} CGT Allowance, "
                 f"Shelter {money(p.total_proceeds)}", sev, rationale=why, rules=["R-BED-01", "R-TAX-01"], gbp=p.total_proceeds,
                 side="SELL", adj=-days / 500, gain=p.total_gain, proceeds=p.total_proceeds)]


def _sector_signals(c: SignalContext) -> list[Signal]:
    cap = c.rules.position.max_sector_pct
    out: list[Signal] = []
    for sec, f in c.portfolio.sector_exposure.items():
        if sec in ("Cash", "Other", "Unclassified") or f * 100 <= cap:
            continue
        sev = "HIGH" if f * 100 > cap + 3 else "MEDIUM"
        direct = [(p.ticker, p.value_gbp / c.portfolio.total_gbp) for p in c.portfolio.positions
                  if p.sector == sec and p.value_gbp > 0]
        why = [f"{sec} look-through exposure is {f * 100:.1f}% of the ISA vs the {cap:.0f}% sector cap (includes index-fund constituents).",
               "Direct holdings in the sector: " + (", ".join(f"{t} {w * 100:.1f}%" for t, w in direct) or "none") + "."]
        over = [p for p in c.portfolio.positions if p.sector == sec and p.drift_pp > 0]
        if over:
            why.append("Dilute or trim the over-target direct names first: " + ", ".join(f"{p.ticker} (+{p.drift_pp:.1f}pp)" for p in over) + ".")
        why.append("Direct new contributions to non-" + sec + " names until below the cap.")
        out.append(_sig(f"SECTOR:{sec}", "TRIM WARNING", f"{sec} Look-Through {f * 100:.1f}% Exceeds {cap:.0f}% Sector Cap", sev,
                        rationale=why, rules=["R-CAP-02"], side="SELL", adj=(f * 100 - cap), sector=sec, exposure_pct=f * 100))
    return out


def _ticker_signals(c: SignalContext, pos: Position) -> list[Signal]:
    t, rules = pos.ticker, c.rules
    snap = c.snaps.get(t)
    w = c.config.watch(t)
    kind = w.kind if w else pos.kind
    held = pos.value_gbp > 0
    watch_only = (not held) and pos.target_weight == 0
    out: list[Signal] = []
    total = c.portfolio.total_gbp
    cap = rules.position.max_single_position_pct
    t_ = rules.technicals
    drift = pos.drift_pp
    tgt_pct = pos.target_weight * 100
    score = score_security(snap, pos, rules, w)
    sc = {"score": score.total}
    rsi = snap.rsi14 if snap else None
    below200 = (snap.vs_sma200_pct if snap else None)
    oversold = rsi is not None and rsi < t_.rsi_oversold
    overbought = rsi is not None and rsi > t_.rsi_overbought
    caution_trend = below200 is not None and below200 <= t_.caution_below_sma200_pct
    has_rsi_signal = False

    # --- R-CAP-01 / R-REB-01 : concentration --------------------------------------------------
    sec_cap = rules.position.max_sector_pct
    sector_over = (kind == "stock" and pos.sector is not None and
                   c.portfolio.sector_exposure.get(pos.sector, 0.0) * 100 > sec_cap)
    over_cap = held and kind in rules.position.cap_applies_to and pos.weight * 100 > cap
    if over_cap:
        excess = (pos.weight * 100 - cap) / 100 * total
        sev = "CRITICAL" if pos.weight * 100 > cap + 5 else "HIGH"
        need = pos.value_gbp / (cap / 100) - total          # extra cash needed to dilute below the cap
        months = need / c.monthly_cash if c.monthly_cash > 0 else None
        why = [f"{t} is {pos.weight * 100:.1f}% of the ISA vs the {cap:.0f}% single-position cap (target {tgt_pct:.0f}%): "
               f"{money(excess)} over the cap.",
               (f"Dilution route: {money(need)} of new money into other holdings brings it back under the cap"
                + (f" — about {months:.1f} months at {money(c.monthly_cash)}/month." if months else ".")),
               "Dilution is preferred (handbook §1.5): selling inside an ISA is CGT-free but does not free allowance, and costs spread.",
               ("Sell only if dilution would take more than 3 months." if months is None or months > 3
                else "Dilution looks quick enough — no sale needed.")]
        if oversold:
            why.append(f"RSI {rsi:.0f} is oversold: avoid selling into weakness — dilute rather than trim.")
        elif overbought:
            why.append(f"RSI {rsi:.0f} is overbought (> {t_.rsi_overbought:.0f}): no case for adding; if trimming, strength beats weakness (R-RSI-01).")
        out.append(_sig(f"TRIM:{t}", "TRIM WARNING", f"Exceeds {cap:.0f}% Handbook Cap ({pos.weight * 100:.1f}%)", sev, t,
                        rationale=why, rules=["R-CAP-01"], gbp=excess, side="SELL", adj=(pos.weight * 100 - cap), weight_pct=pos.weight * 100,
                        **sc))
        has_rsi_signal = oversold or overbought
    elif held and drift >= rules.position.overweight_trigger_pp:
        out.append(_sig(f"STOP:{t}", "STOP ADDING", f"+{drift:.1f}pp Over {tgt_pct:.0f}% Target", "LOW", t,
                        rationale=[f"{t} is {pos.weight * 100:.1f}% vs a {tgt_pct:.0f}% target (trigger: +{rules.position.overweight_trigger_pp:.0f}pp).",
                                   "Excluded from this month's buy plan; let contributions to other names dilute it."],
                        rules=["R-REB-01"], side=None, weight_pct=pos.weight * 100, **sc))

    # --- screens (R-OCF-01 / R-DIV-01 / R-VAL-01) -----------------------------------------------
    for rid, short, why in screen_failures(snap, w, rules):
        out.append(_sig(f"SCREEN:{t}:{rid}", "SCREEN FAIL", short + (" (watch-only)" if watch_only else ""),
                        "LOW" if watch_only else "MEDIUM", t,
                        rationale=[f"{t}: {why}.", "A failed screen blocks new buys under handbook §4 until reviewed."
                                   + (" (Watch-only name: informational.)" if watch_only else "")], rules=[rid], **sc))

    # --- R-UPS-02 : price above consensus target -------------------------------------------------
    if held and snap and snap.upside_pct is not None and (snap.n_analysts or 0) >= rules.analyst.min_analysts \
            and snap.upside_pct <= rules.analyst.review_below_target_pct:
        out.append(_sig(f"REVIEW:{t}", "REVIEW", f"Trades {abs(snap.upside_pct):.0f}% Above Mean Analyst Target", "MEDIUM", t,
                        rationale=[f"Mean target {fnum(snap.target_mean)} vs price {fnum(snap.price)} ({snap.upside_pct:+.1f}%) from {snap.n_analysts} analysts; "
                                   f"rule fires at {rules.analyst.review_below_target_pct:+.0f}% or worse.",
                                   "Consensus says it is priced in. Don't add; revisit the thesis."], rules=["R-UPS-02"], **sc))

    # --- accumulate family (R-REB-01 / R-UPS-01 / R-RSI-01 / R-TRD-01) --------------------------------------
    under = -drift >= rules.position.underweight_trigger_pp and pos.target_weight > 0 and not over_cap
    blocked = any(s.action in ("SCREEN FAIL", "REVIEW") and s.severity != "LOW" for s in out)
    if under and not blocked:
        deficit = (pos.target_weight - pos.weight) * total
        why = [f"{t} is {pos.weight * 100:.1f}% vs a {tgt_pct:.0f}% target ({drift:+.1f}pp; trigger {rules.position.underweight_trigger_pp:.0f}pp). "
               f"Closing the gap = {money(deficit)}."]
        rid = ["R-REB-01"]
        cov = snap is not None and snap.upside_pct is not None and (snap.n_analysts or 0) >= rules.analyst.min_analysts
        if snap is None:
            sev, det = "LOW", f"Underweight {drift:+.1f}pp (no market data)"
        elif cov and snap.upside_pct >= rules.analyst.min_upside_pct:
            sev, det = "HIGH", f"Underweight & >{rules.analyst.min_upside_pct:.0f}% Analyst Upside ({snap.upside_pct:+.0f}%)"
            rng = (f"; range {snap.upside_low_pct:+.0f}% to {snap.upside_high_pct:+.0f}%"
                   if snap.upside_low_pct is not None and snap.upside_high_pct is not None else "")
            why.append(f"Mean target {fnum(snap.target_mean)} vs {fnum(snap.price)}: {snap.upside_pct:+.1f}% upside "
                       f"({snap.n_analysts} analysts, '{snap.rating_label}'{rng}).")
            rid.append("R-UPS-01")
        elif cov:
            sev, det = "LOW", f"Underweight, Upside {snap.upside_pct:+.0f}% < {rules.analyst.min_upside_pct:.0f}% Threshold"
            why.append(f"Analyst upside {snap.upside_pct:+.1f}% is below the {rules.analyst.min_upside_pct:.0f}% conviction bar — DCA only, don't lump-sum.")
            rid.append("R-UPS-01")
        elif kind in ("etf", "trust"):
            sev, det = "MEDIUM", f"Underweight vs Core Target ({drift:+.1f}pp)"
            why.append("Fund: no analyst price targets exist — the allocation rule alone drives this signal.")
        else:
            sev, det = "LOW", f"Underweight {drift:+.1f}pp (thin analyst coverage)"
            why.append(f"Fewer than {rules.analyst.min_analysts} analysts — consensus ignored per R-UPS-01.")
        if oversold:
            sev, det = _bump(sev, 1), det + f" & Oversold (RSI {rsi:.0f})"
            why.append(f"RSI(14) {rsi:.1f} < {t_.rsi_oversold:.0f}: oversold — accelerate the DCA (R-RSI-01).")
            rid.append("R-RSI-01")
            has_rsi_signal = True
        if overbought:
            sev = _bump(sev, -1)
            det = f"Underweight but Overbought (RSI {rsi:.0f}): Stage Entries"
            why.append(f"RSI(14) {rsi:.1f} > {t_.rsi_overbought:.0f}: overbought — split the buy across several weeks rather than chasing (R-RSI-01).")
            rid.append("R-RSI-01")
            has_rsi_signal = True
        if caution_trend:
            sev = _bump(sev, -1)
            why.append(f"Price is {abs(below200):.0f}% below its 200-day SMA (< {t_.caution_below_sma200_pct:.0f}%): scale in, don't catch a falling knife (R-TRD-01).")
            rid.append("R-TRD-01")
        action = "HOLD OFF" if overbought else "ACCUMULATE"
        extra: dict[str, object] = {}
        if sector_over:
            action, sev = "HOLD OFF", "LOW"
            det = f"Underweight but {pos.sector} Over {sec_cap:.0f}% Sector Cap"
            why.append(f"{pos.sector} look-through exposure is {c.portfolio.sector_exposure[pos.sector] * 100:.1f}% (cap {sec_cap:.0f}%): "
                       f"adding a direct {pos.sector} stock makes it worse. Fund non-{pos.sector} names first (R-CAP-02).")
            rid.append("R-CAP-02")
            extra["blocks_buy"] = True
        out.append(_sig(f"ACC:{t}", action, det, sev, t, rationale=why, rules=rid,
                        gbp=deficit, side="BUY", adj=(score.total or 50) / 10, weight_pct=pos.weight * 100, **extra, **sc))
    elif held and not over_cap and not blocked and oversold and abs(drift) < rules.position.underweight_trigger_pp:
        out.append(_sig(f"RSI:{t}", "DEPLOY", f"Oversold (RSI {rsi:.0f}): Accelerate DCA", "LOW", t,
                        rationale=[f"RSI(14) {rsi:.1f} < {t_.rsi_oversold:.0f}; weight is on target ({pos.weight * 100:.1f}% vs {tgt_pct:.0f}%).",
                                   "Handbook R-RSI-01: oversold readings on a name I want to own are a reason to buy this month's tranche, not skip it."],
                        rules=["R-RSI-01"], side="BUY", **sc))
        has_rsi_signal = True

    # --- trend / valuation cautions (R-TRD-01 / R-VAL-02), held only ---------------------------------------
    if held and caution_trend and not (under and not blocked):
        out.append(_sig(f"TRD:{t}", "CAUTION", f"{abs(below200):.0f}% Below 200-Day SMA", "LOW", t,
                        rationale=[f"Price {fnum(snap.price)} vs 200-day SMA {fnum(snap.sma200)}; trend {snap.trend}.",
                                   "Not a sell signal for a long-horizon holding — it only argues against lump-sum adds (R-TRD-01)."],
                        rules=["R-TRD-01"], **sc))
    if held and snap and snap.valuation_label == "RICH" and snap.vs_5y_avg_pct is not None:
        out.append(_sig(f"VAL:{t}", "CAUTION", f"Rich: {snap.vs_5y_avg_pct:+.0f}% vs 5Y Average Price", "LOW", t,
                        rationale=[f"Price is {snap.vs_5y_avg_pct:+.1f}% above its own 5-year average (rich threshold +{t_.rich_vs_5y_avg_pct:.0f}%).",
                                   "This is a price-based proxy, not a P/E comparison — treat as context, not a sell trigger (R-VAL-02)."],
                        rules=["R-VAL-02"], **sc))

    # --- per-ticker custom alerts (config.yaml) ------------------------------------------------------------
    al = {**c.config.default_alerts, **(w.alerts if w else {})}
    px = snap.price_gbp if snap and snap.price_gbp is not None else pos.price_gbp
    if w and w.alerts:
        if "price_below" in al and px is not None and px < al["price_below"]:
            out.append(_sig(f"ALERT:{t}:pb", "PRICE ALERT", f"{money(px, 2)} Below {money(al['price_below'], 2)} Floor", "MEDIUM", t,
                            rationale=[f"Custom alert in config.yaml: price_below {al['price_below']:,.2f} (GBP-equivalent)."], rules=[], **sc))
        if "price_above" in al and px is not None and px > al["price_above"]:
            out.append(_sig(f"ALERT:{t}:pa", "PRICE ALERT", f"{money(px, 2)} Above {money(al['price_above'], 2)} Ceiling", "MEDIUM", t,
                            rationale=[f"Custom alert in config.yaml: price_above {al['price_above']:,.2f} (GBP-equivalent)."], rules=[], **sc))
    if w and "rsi_above" in w.alerts and rsi is not None and rsi > w.alerts["rsi_above"] and not has_rsi_signal:
        out.append(_sig(f"ALERT:{t}:ra", "RSI ALERT", f"RSI {rsi:.0f} > {w.alerts['rsi_above']:.0f}", "LOW", t,
                        rationale=["Custom RSI alert in config.yaml."], rules=[], **sc))
    if w and "rsi_below" in w.alerts and rsi is not None and rsi < w.alerts["rsi_below"] and not has_rsi_signal:
        out.append(_sig(f"ALERT:{t}:rb", "RSI ALERT", f"RSI {rsi:.0f} < {w.alerts['rsi_below']:.0f}", "LOW", t,
                        rationale=["Custom RSI alert in config.yaml."], rules=[], **sc))
    return out


def _portfolio_signals(c: SignalContext) -> list[Signal]:
    out: list[Signal] = []
    pf, r = c.portfolio, c.rules
    # R-DD-01 drawdown deployment
    ref = c.snaps.get(r.drawdown.reference_ticker)
    if ref and ref.drawdown_52w_pct is not None and pf.cash_gbp > 0:
        dd = -ref.drawdown_52w_pct
        hit = [t for t in r.drawdown.tiers if dd >= t.drop_pct]
        if hit:
            cum = sum(t.deploy_pct_of_reserve for t in hit)
            tier = len(hit)
            amt = pf.cash_gbp * cum / 100
            out.append(_sig("DD:RESERVE", "DEPLOY RESERVE", f"{r.drawdown.reference_ticker} −{dd:.1f}% From 52W High: Tier {tier} "
                            f"(Deploy {cum:.0f}% of Cash)", ("MEDIUM", "HIGH", "CRITICAL")[min(tier, 3) - 1],
                            rationale=[f"{r.drawdown.reference_ticker} is {dd:.1f}% below its trailing 52-week high; tier {tier} threshold −{hit[-1].drop_pct:.0f}%.",
                                       f"Cumulative reserve to deploy: {cum:.0f}% of {money(pf.cash_gbp)} = {money(amt)}, into core underweights first (handbook §9).",
                                       "Tranche over ≥ 2 weeks unless RSI(14) < 30. Do not sell to raise cash.",
                                       f"Sanity check: is this within my −35% tolerance? (§2)"],
                            rules=["R-DD-01"], gbp=amt, side="BUY", drawdown_pct=dd))
    # R-CSH-01 cash drag
    if pf.cash_weight * 100 > r.cash.max_cash_drag_pct:
        out.append(_sig("CASH:DRAG", "DEPLOY", f"Cash Drag {pf.cash_weight * 100:.1f}% > {r.cash.max_cash_drag_pct:.0f}% Limit", "MEDIUM",
                        rationale=[f"Cash is {money(pf.cash_gbp)} ({pf.cash_weight * 100:.1f}% of the ISA) vs a {r.cash.reserve_target_pct:.0f}% target.",
                                   "Use this month's Buy Plan to put the excess to work."], rules=["R-CSH-01"], gbp=pf.cash_gbp - pf.total_gbp * r.cash.reserve_target_pct / 100,
                        side="BUY"))
    # data quality
    if c.data_label == "DEMO":
        out.append(_sig("DATA:DEMO", "DATA WARNING", "Running on DEMO data — signals are NOT actionable", "HIGH",
                        rationale=["Prices, analyst figures and news are synthetic (network unavailable or --demo).",
                                   "Your holdings/contributions are real; the market numbers are not. Do not trade on this screen."], adj=12))
    elif c.data_label == "STALE":
        out.append(_sig("DATA:STALE", "DATA WARNING", "Using stale cached market data (live feed failing)", "MEDIUM",
                        rationale=["The last refresh could not reach the data provider; showing cached values."], adj=4))
    for i, wmsg in enumerate(pf.warnings[:3]):
        out.append(_sig(f"DATA:W{i}", "DATA WARNING", wmsg, "LOW", rationale=[wmsg]))
    return out


def generate_signals(c: SignalContext) -> list[Signal]:
    """All signals, most important first."""
    sigs: list[Signal] = []
    sigs += _tax_signals(c)
    sigs += _bed_signal(c)
    sigs += _sector_signals(c)
    sigs += _portfolio_signals(c)
    for pos in c.portfolio.positions:
        sigs += _ticker_signals(c, pos)
    # watch-only names with no position row
    held = {p.ticker for p in c.portfolio.positions}
    for w in c.config.watchlist:
        if w.ticker not in held and w.target_weight_pct == 0 and w.ticker in c.snaps:
            ghost = Position(w.ticker, w.name, w.kind, 0, 0, c.snaps[w.ticker].price_gbp, 0, 0, 0, 0, w.sector, w.ter_pct, None, {})
            sigs += _ticker_signals(c, ghost)
    sigs.sort(key=lambda s: (-s.priority, s.id))
    return sigs


def blocked_tickers(signals: Sequence[Signal]) -> set[str]:
    """Tickers that must not receive new money in the buy plan (TRIM / STOP ADDING / failed screen / REVIEW)."""
    out = set()
    for s in signals:
        if s.ticker and (s.action in ("TRIM WARNING", "STOP ADDING", "REVIEW") or s.metrics.get("blocks_buy")
                         or (s.action == "SCREEN FAIL" and s.severity != "LOW")):
            out.add(s.ticker)
    return out
