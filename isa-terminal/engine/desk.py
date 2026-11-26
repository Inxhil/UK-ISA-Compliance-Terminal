"""Orchestrator: load files -> fetch market data -> portfolio -> ISA status -> signals. One call = one consistent state.

Config, handbook and CSVs are re-read on every refresh, so editing ``handbook.md`` and pressing ``r`` changes the signals.
Network I/O is confined to ``MarketIntel``; call :meth:`Desk.refresh` from a worker thread in the UI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

from engine.analyst_screener import AnalystScreener, AnalystSnapshot
from engine.config import AppConfig, HandbookRules, ROOT, load_config, load_env, load_handbook_rules
from engine.isa_engine import (AccountSpec, BedISAPlan, Countdown, ISAStatus, Pace, RebalancePlan, allowance_pace,
                               compute_isa_status, countdown_to_deadline, D, now_london, parse_ledger_csv,
                               plan_bed_and_isa, plan_rebalance)
from engine.config_edit import suggest_watch_item
from engine.market_intel import DataStatus, MarketIntel, NewsFeed, Quote, TapeItem
from engine.models import DataUnavailable
from engine.news import TagTarget
from engine.portfolio import Portfolio, SimResult, build_portfolio, load_holdings_csv, simulate_buy
from engine.recommender import ScoreCard, Signal, SignalContext, blocked_tickers, generate_signals, score_security

Progress = Callable[[str, int, int], None]


@dataclass
class Adhoc:
    """A ticker looked up on demand (not in the watchlist): everything the tear-sheet needs, plus a ready config entry."""
    ticker: str
    snap: AnalystSnapshot
    quote: Quote | None
    suggestion: dict


@dataclass
class DeskState:
    now: datetime
    config: AppConfig
    rules: HandbookRules
    isa: ISAStatus
    countdown: Countdown
    pace: Pace
    quotes: dict[str, Quote]
    snaps: dict[str, AnalystSnapshot]
    portfolio: Portfolio
    bed_isa: BedISAPlan | None
    signals: list[Signal]
    scores: dict[str, ScoreCard]
    plan: RebalancePlan | None
    tape: list[TapeItem]
    status: DataStatus
    monthly_cash: float
    handbook_text: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def data_label(self) -> str:
        return self.status.label

    def signals_for(self, ticker: str) -> list[Signal]:
        return [s for s in self.signals if s.ticker == ticker]


class Desk:
    def __init__(self, config_path: str | Path | None = None, mode: str = "auto",
                 now_fn: Callable[[], datetime] = now_london, mi: MarketIntel | None = None):
        self.config_path = Path(config_path) if config_path else ROOT / "config.yaml"
        self.mode = mode
        self.now_fn = now_fn
        cfg = load_config(self.config_path)
        load_env(cfg.root / ".env")
        import os
        self.mi = mi or MarketIntel(cfg.cache_dir, ttl=cfg.cache_ttl, mode=mode,
                                    price_units={w.ticker: w.price_unit for w in cfg.watchlist},
                                    finnhub_key=os.environ.get("FINNHUB_API_KEY") or None,
                                    td_key=os.environ.get("TWELVEDATA_API_KEY") or None)
        self.last: DeskState | None = None

    # ------------------------------------------------------------------------------------------
    def refresh(self, monthly_cash: float | None = None, progress: Progress | None = None) -> DeskState:
        errors: list[str] = []
        cfg = load_config(self.config_path)
        rules = load_handbook_rules(cfg.handbook_path)
        self.mi.price_units = {w.ticker: w.price_unit for w in cfg.watchlist}
        now = self.now_fn()
        cash_in = cfg.monthly_contribution_gbp if monthly_cash is None else monthly_cash

        accounts = {a.id: AccountSpec(a.id, a.kind, a.flexible, a.broker) for a in cfg.accounts.values()}
        ledger = parse_ledger_csv(cfg.contributions_csv)
        isa = compute_isa_status(ledger, accounts, as_of=now)
        cd = countdown_to_deadline(now, isa.tax_year)
        pace = allowance_pace(isa, now, rules.tax.practical_cutoff_days)

        holdings = load_holdings_csv(cfg.holdings_csv, cfg.accounts)
        held = {h.ticker for h in holdings if h.ticker != "CASH"}
        # held positions first: they get priority on the scarce rate-limited credits (Twelve Data free tier = 8/min)
        screen = list(dict.fromkeys([t for t in cfg.tickers if t in held] + cfg.tickers + sorted(held - set(cfg.tickers))))
        ref = rules.drawdown.reference_ticker
        if ref not in screen:
            screen.append(ref)

        self.mi.begin_cycle()
        if progress:
            progress("quotes", 0, len(screen))
        quotes = self.mi.quotes(screen)
        watch = {w.ticker: w for w in cfg.watchlist}
        screener = AnalystScreener(self.mi, rules, watch)
        snaps = screener.screen_all(screen, quotes, progress=(lambda i, n, t: progress(f"screen {t}", i, n)) if progress else None)
        if progress:
            progress("tape", len(screen), len(screen))
        tape = self.mi.tape(list(cfg.tape))
        status = self.mi.status()

        pf = build_portfolio(holdings, cfg.accounts, quotes, watch, snaps)
        bed = None
        if pf.gia:
            aea = max(0.0, float(isa.rules.cgt_annual_exempt) - cfg.cgt_realised_ytd)
            bed = plan_bed_and_isa(pf.gia, aea, float(isa.headroom), fractional=cfg.fractional_shares,
                                   min_gain_gbp=rules.tax.bed_and_isa_min_gain_gbp)
        ctx = SignalContext(now, isa, cd, pace, pf, snaps, rules, cfg, cash_in, bed, status.label)
        signals = generate_signals(ctx)
        scores = {p.ticker: score_security(snaps.get(p.ticker), p, rules, watch.get(p.ticker)) for p in pf.positions}
        for t in cfg.tickers:
            scores.setdefault(t, score_security(snaps.get(t), None, rules, watch.get(t)))
        state = DeskState(now, cfg, rules, isa, cd, pace, quotes, snaps, pf, bed, signals, scores, None, tape, status,
                          cash_in, cfg.handbook_path.read_text(encoding="utf-8") if cfg.handbook_path.exists() else "", errors)
        state.plan = self.buy_plan(state, cash_in)
        self.last = state
        return state

    # ------------------------------------------------------------------------------------------
    def buy_plan(self, st: DeskState, cash: float) -> RebalancePlan:
        """This month's no-sell buy list: targets from config, caps/blocks from the handbook + live signals."""
        cfg, rules, pf = st.config, st.rules, st.portfolio
        warn: list[str] = []
        amount = D(cash)
        head = st.isa.headroom
        if amount > head:
            warn.append(f"Cash £{float(amount):,.2f} exceeds ISA allowance headroom £{float(head):,.2f} — clamped (R-TAX-01)")
            amount = head
        values = {p.ticker: p.value_gbp for p in pf.positions}
        kinds = {w.ticker: w.kind for w in cfg.watchlist}
        cap = rules.position.max_single_position_pct / 100
        caps = {t: cap for t, k in kinds.items() if k in rules.position.cap_applies_to}
        reasons: dict[str, str] = {}
        for s in st.signals:
            if s.ticker and s.action in ("ACCUMULATE", "HOLD OFF", "DEPLOY") and s.ticker not in reasons:
                reasons[s.ticker] = s.headline
        prices = {t: q.price_gbp for t, q in st.quotes.items() if q.price_gbp}
        plan = plan_rebalance(values, cfg.targets, amount, prices=prices, fractional=cfg.fractional_shares,
                              min_order=cfg.min_order_gbp, max_weights=caps, reserve_cash=pf.cash_gbp,
                              exclude=sorted(blocked_tickers(st.signals)), reasons=reasons)
        if warn:
            from dataclasses import replace
            plan = replace(plan, warnings=tuple(warn) + plan.warnings)
        return plan

    def simulate(self, st: DeskState, ticker: str, amount: float, funded_from: str = "new",
                 adhoc: "Adhoc | None" = None) -> SimResult:
        w = {x.ticker: x for x in st.config.watchlist}
        q = st.quotes.get(ticker) or (adhoc.quote if adhoc else None)
        snaps = {**st.snaps, **({ticker: adhoc.snap} if adhoc and ticker not in st.snaps else {})}
        return simulate_buy(st.portfolio, ticker, amount, q.price_gbp if q else None, w, snaps, funded_from=funded_from,
                            max_position_pct=st.rules.position.max_single_position_pct,
                            max_sector_pct=st.rules.position.max_sector_pct, cap_applies_to=st.rules.position.cap_applies_to,
                            isa_headroom_gbp=float(st.isa.headroom))

    # ------------------------------------------------------------------------------------------
    def adhoc(self, ticker: str) -> Adhoc:
        """Look up any LSE or US ticker on demand. Raises :class:`DataUnavailable` if no provider has a price for it."""
        from engine.demo_data import _REF
        cfg = load_config(self.config_path)
        rules = load_handbook_rules(cfg.handbook_path)
        if self.mode == "demo" and ticker not in _REF:
            raise DataUnavailable(f"{ticker}: demo mode only knows the built-in tickers")
        q = self.mi.quotes([ticker]).get(ticker)
        if q is None or q.price_native is None:
            raise DataUnavailable(f"no provider has a price for {ticker}")
        snap = AnalystScreener(self.mi, rules, {}).snapshot(ticker, q)
        info = self.mi.info(ticker).value or {}
        item = suggest_watch_item(ticker, name=snap.name if snap.name != ticker else info.get("longName"),
                                  quote_type=info.get("quoteType"), sector=snap.sector, currency=q.currency, ter_pct=snap.ter_pct)
        return Adhoc(ticker, snap, q, item)

    # ------------------------------------------------------------------------------------------
    def news_tickers(self, st: DeskState) -> list[str]:
        """Held positions first (they matter most), then the rest of the universe."""
        held = [p.ticker for p in st.portfolio.positions if p.value_gbp > 0]
        return held + [t for t in st.config.tickers if t not in held]

    def tag_targets(self, st: DeskState) -> list[TagTarget]:
        return [TagTarget(w.ticker, w.name, w.kind, w.aliases) for w in st.config.watchlist]

    def news_feed(self, st: DeskState, tickers: list[str] | None = None, *, macro: bool = True) -> NewsFeed:
        return self.mi.news_feed(tickers if tickers is not None else self.news_tickers(st), self.tag_targets(st), macro=macro)
