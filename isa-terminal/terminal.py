#!/usr/bin/env python3
"""UK Stocks & Shares ISA Command Centre v0.2 — Bloomberg-style terminal (Textual).

    python terminal.py              # live data (falls back to clearly-labelled DEMO if offline)
    python terminal.py --demo       # synthetic demo data, no network
    python terminal.py --snapshot   # print the weekly memo to stdout and exit
    python setup_isa.py             # first-run wizard: experience level, allowance, API keys

Keys: 1-5 / F1-F5 panes · [ ] prev/next · r refresh · a auto · l experience level · f filter · t timeframe · / search
      o sort (pane 2) / open article (pane 5) · m cash · c co-pilot · ? help · q quit
Quantitative modelling and personal handbook tool only. Not FCA-regulated financial advice.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence
from urllib.parse import urlparse

import pandas as pd
from rich.console import Group
from rich.markdown import Markdown as RichMarkdown
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, DataTable, Input, Markdown, RichLog, Select, Static, TabbedContent, TabPane

from engine import DISCLAIMER, __version__
from engine import news as newsmod
from engine.analyst_screener import AnalystSnapshot
from engine.config import EXPERIENCE_LEVELS, SLEEVES, ConfigError, WatchItem, load_config
from engine.config_edit import append_watch_item, set_experience_level
from engine.copilot import CoPilot, portfolio_health, weekly_memo
from engine.desk import Adhoc, Desk, DeskState
from engine.income import TAX_NOTE, dividend_forecast, security_income
from engine.isa_engine import countdown_to_deadline, now_london
from engine.literacy import (ACTION_PLAIN, LEVEL_LABEL, SEV_PLAIN, allowance_bar, analyst_badge, income_badge, momentum_badge,
                             next_level, normalise_level, plain_text, portfolio_badge, render_signal, unit_audit, valuation_badge)
from engine.market_intel import Quote, TapeItem
from engine.providers import normalise_user_ticker
from engine.recommender import Signal, fnum, money
from engine.technicals import sma

try:                                           # charts degrade to a text note if textual-plotext is missing
    from textual_plotext import PlotextPlot
except ImportError:                            # pragma: no cover
    PlotextPlot = None                         # type: ignore[assignment,misc]

# ------------------------------------------------------------------------------------------------ palette
BG, PANEL, BORDER = "#0B0E11", "#0F1318", "#2A313A"
AMBER, CYAN, UP, DOWN = "#FF9900", "#00E5FF", "#00D26A", "#FF4D4D"
TEXT, DIM, WHITE = "#D0D6DE", "#6B7683", "#FFFFFF"
SEV_STYLE = {"CRITICAL": f"bold {WHITE} on #B3261E", "HIGH": f"bold {DOWN}", "MEDIUM": f"bold {AMBER}", "LOW": CYAN, "INFO": DIM}
TONE = {"good": f"bold {UP}", "ok": TEXT, "warn": f"bold {AMBER}", "bad": f"bold {DOWN}", "cool": f"bold {CYAN}", "dim": DIM}
THEME = Theme(name="isa-terminal", primary=AMBER, secondary=CYAN, accent=CYAN, foreground=TEXT, background=BG, surface=BG,
              panel=PANEL, success=UP, warning=AMBER, error=DOWN, dark=True)
PANES = ("desk", "watch", "sheet", "hb", "newsdesk")
SLEEVE_PILLS = (("all", "All"),) + tuple((k, v) for k, v in SLEEVES.items())


def pct(v: float | None, dp: int = 1, signed: bool = True) -> Text:
    if v is None:
        return Text("—", style=DIM, justify="right")
    s = f"{v:+.{dp}f}%" if signed else f"{v:.{dp}f}%"
    return Text(s, style=UP if v > 0 else DOWN if v < 0 else TEXT, justify="right")


def num(v: float | None, spec: str = ",.2f", style: str = TEXT) -> Text:
    return Text("—" if v is None else format(v, spec), style=style if v is not None else DIM, justify="right")


def rsi_text(v: float | None, lo: float = 30, hi: float = 70) -> Text:
    if v is None:
        return Text("—", style=DIM, justify="right")
    return Text(f"{v:.0f}", style=f"bold {CYAN}" if v < lo else f"bold {DOWN}" if v > hi else TEXT, justify="right")


def sev_text(s: str, plain: bool = False) -> Text:
    return Text(f" {SEV_PLAIN.get(s, s) if plain else s:<12}" if plain else f" {s:<8}", style=SEV_STYLE.get(s, TEXT))


def badge_text(b) -> Text:
    return Text(b.label, style=TONE.get(b.tone, TEXT))


def safe_url(url: str) -> str | None:
    """Only http(s) links are ever opened."""
    try:
        u = urlparse(url)
    except ValueError:
        return None
    return url if u.scheme in ("http", "https") and u.netloc else None


def link_text(label: str, url: str, action: str, style: str = CYAN) -> Text:
    """Underlined link: Ctrl/Cmd-click works in terminals that support OSC-8 hyperlinks, and a plain click runs ``action``."""
    return Text(label, style=Style.parse(f"underline {style}") + Style(link=url, meta={"@click": action}))


# ------------------------------------------------------------------------------------------------ chart prep (pure)
RANGES = {"1M": (21, "D"), "6M": (126, "W"), "1Y": (252, "W"), "5Y": (1260, "ME")}


@dataclass(frozen=True)
class ChartData:
    dates: list[str]
    open: list[float]
    high: list[float]
    low: list[float]
    close: list[float]
    sma50: list[float | None]
    sma200: list[float | None]
    bar: str


def prepare_chart(df: pd.DataFrame, range_key: str = "1Y") -> ChartData:
    """OHLC (+SMA50/200 computed on the full daily series) resampled so there are ~<=110 candles."""
    n, rule = RANGES.get(range_key, RANGES["1Y"])
    d = df.dropna(subset=["Close"]).copy()
    d["SMA50"], d["SMA200"] = sma(d["Close"], 50), sma(d["Close"], 200)
    d = d.iloc[-n:]
    if rule != "D":
        d = d.resample(rule).agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "SMA50": "last", "SMA200": "last"}).dropna(subset=["Close"])
    nn = lambda s: [None if pd.isna(x) else float(x) for x in s]
    return ChartData([i.strftime("%Y-%m-%d") for i in d.index], nn(d["Open"]), nn(d["High"]), nn(d["Low"]), nn(d["Close"]),
                     nn(d["SMA50"]), nn(d["SMA200"]), {"D": "daily", "W": "weekly", "ME": "monthly"}[rule] + " bars")


def target_range_bar(low: float | None, mean: float | None, high: float | None, price: float | None, width: int = 56) -> Text:
    """Text gauge: [low ──── ◆mean ── ▼price ──── high]."""
    if None in (low, mean, high, price):
        return Text("no analyst target range available", style=DIM)
    lo, hi = min(low, price) * 0.97, max(high, price) * 1.03
    pos = lambda v: max(0, min(width - 1, int((v - lo) / (hi - lo) * (width - 1))))
    cells = [("─", DIM)] * width
    for i in range(pos(low), pos(high) + 1):
        cells[i] = ("━", "#3A4350")
    cells[pos(low)], cells[pos(high)] = ("┣", AMBER), ("┫", AMBER)
    cells[pos(mean)] = ("◆", f"bold {AMBER}")
    cells[pos(price)] = ("▼", f"bold {CYAN}")
    t = Text()
    for ch, st in cells:
        t.append(ch, style=st)
    return t


def rating_bars(counts: dict[str, int] | None, width: int = 24) -> Text:
    t = Text()
    if not counts:
        return Text("no analyst rating distribution", style=DIM)
    mx = max(counts.values()) or 1
    for key, label, colour in (("strongBuy", "Strong Buy", UP), ("buy", "Buy", "#7BD88F"), ("hold", "Hold", AMBER),
                               ("sell", "Sell", "#FF8A65"), ("strongSell", "Strong Sell", DOWN)):
        n = counts.get(key, 0)
        t.append(f"{label:<11}", style=TEXT)
        t.append("█" * int(n / mx * width) or ("▏" if n else ""), style=colour)
        t.append(f" {n}\n", style=DIM)
    return t


# ------------------------------------------------------------------------------------------------ level-aware views (pure)
# watchlist columns per experience level: key -> (label, width)
WL_ALL: dict[str, tuple[str, int]] = {
    "ticker": ("TICKER", 8), "name": ("NAME", 24), "price": ("PRICE £", 10), "day": ("DAY %", 8), "consensus": ("CONSENSUS", 12),
    "n": ("#AN", 4), "target": ("TARGET £", 10), "upside": ("UPSIDE", 8), "rsi": ("RSI14", 6), "yield": ("YIELD", 7),
    "vs5y": ("vs 5Y AVG", 9), "value": ("VALUE", 7), "trend": ("TREND", 9), "wt": ("WT/TGT %", 11), "score": ("SCORE", 6),
    "top": ("TOP SIGNAL", 16), "fwdpe": ("FWD P/E", 8), "sector": ("SECTOR", 20),
    "analysts": ("ANALYSTS SAY", 13), "valuation": ("VALUATION", 12), "momentum": ("MOMENTUM", 14), "income": ("INCOME", 18),
    "mine": ("YOUR ISA", 13), "tip": ("TIP", 18),
}
WL_LEVEL: dict[str, list[str]] = {
    "advanced": ["ticker", "name", "price", "day", "consensus", "n", "target", "upside", "rsi", "yield", "vs5y", "value", "trend", "wt", "score", "top"],
    "intermediate": ["ticker", "name", "price", "day", "consensus", "upside", "fwdpe", "yield", "sector", "wt", "top"],
    "beginner": ["ticker", "name", "price", "day", "analysts", "valuation", "momentum", "income", "mine", "tip"],
}


def fundamentals_view(level: str, s: AnalystSnapshot | None, w: WatchItem | None, score_label: str, pos_badge, valuation, momentum,
                      analyst, income) -> Group:
    """Right-hand 'fundamentals' panel for each experience level."""
    lvl = normalise_level(level)
    g = Table.grid(padding=(0, 2))
    g.add_column(style=DIM); g.add_column(style=TEXT, justify="right"); g.add_column(style=DIM); g.add_column(style=TEXT, justify="right")
    cur = f"  ({s.currency} native)" if s and s.currency else ""
    if s is None:
        return Group(Text("FUNDAMENTALS\n", style=f"bold {AMBER}"), Text("no data", style=DIM))
    if lvl == "beginner":
        title = "AT A GLANCE\n"
        rows = [("Value for money", badge_text(valuation)), ("Recent momentum", badge_text(momentum)), ("Analysts say", badge_text(analyst)),
                ("Income", badge_text(income)), ("In your ISA", badge_text(pos_badge))]
        for k, v in rows:
            g.add_row(k, v, "", "")
        if s.range_pos is not None:
            g.add_row("Past-year range", f"{s.range_pos * 100:.0f}% of the way up", "", "")
        if s.ter_pct is not None:
            g.add_row("Yearly fund fee", f"{s.ter_pct:.2f}%", "", "")
    elif lvl == "intermediate":
        title = "FUNDAMENTALS" + cur + "\n"
        g.add_row("FWD P/E", fnum(s.forward_pe, ".1f"), "YIELD", fnum(s.dividend_yield_pct, ".2f", "%" + ("*" if s.yield_is_estimate else "")))
        g.add_row("ANALYSTS", s.rating_label, "UPSIDE", fnum(s.upside_pct, "+.1f", "%"))
        g.add_row("OCF/TER", fnum(s.ter_pct, ".2f", "%"), "SECTOR", (s.sector or "—")[:18])
        g.add_row("52W POS", fnum(None if s.range_pos is None else s.range_pos * 100, ".0f", "%"), "vs 5Y AVG", f"{fnum(s.vs_5y_avg_pct, '+.0f', '%')} {s.valuation_label}")
        if w and w.look_through:
            tot = sum(w.look_through.values()) or 1
            top = sorted(w.look_through.items(), key=lambda kv: -kv[1])[:4]
            g.add_row("SECTOR WEIGHTS", "", "", "")
            for (a, va), (b, vb) in zip(top[::2], (top[1::2] + [("", 0)] * 2)[:len(top[::2])]):
                g.add_row(a[:18], f"{va / tot * 100:.0f}%", b[:18], f"{vb / tot * 100:.0f}%" if b else "")
        elif w and w.sector:
            g.add_row("SECTOR WEIGHTS", "", "", "")
            g.add_row(w.sector[:18], "100%", "", "")
    else:
        title = "FUNDAMENTALS & TECHNICALS" + cur + "\n"
        g.add_row("FWD P/E", fnum(s.forward_pe, ".1f"), "TRAIL P/E", fnum(s.trailing_pe, ".1f"))
        g.add_row("PEG", fnum(s.peg, ".2f"), "YIELD", fnum(s.dividend_yield_pct, ".2f", "%" + ("*" if s.yield_is_estimate else "")))
        g.add_row("PAYOUT", fnum(None if s.payout_ratio is None else s.payout_ratio * 100, ".0f", "%"), "DIV COVER", fnum(s.dividend_cover, ".2f", "x"))
        g.add_row("OCF/TER", fnum(s.ter_pct, ".2f", "%"), "BETA", fnum(s.beta, ".2f"))
        g.add_row("52W LOW", fnum(s.low_52w), "52W HIGH", fnum(s.high_52w))
        g.add_row("52W POS", fnum(None if s.range_pos is None else s.range_pos * 100, ".0f", "%"), "FROM HIGH", fnum(s.drawdown_52w_pct, ".1f", "%"))
        g.add_row("SMA 50", fnum(s.sma50), "SMA 200", fnum(s.sma200))
        g.add_row("RSI (14)", fnum(s.rsi14, ".1f"), "TREND", s.trend)
        g.add_row("5Y AVG PX", fnum(s.avg_5y), "vs 5Y AVG", f"{fnum(s.vs_5y_avg_pct, '+.0f', '%')} {s.valuation_label}")
        g.add_row("SCORE", score_label, "SECTOR", (s.sector or "—")[:20])
    return Group(Text(title, style=f"bold {AMBER}"), g)


def analyst_view(level: str, s: AnalystSnapshot | None) -> Text:
    lvl = normalise_level(level)
    a = Text()
    if not (s and s.has_analyst_coverage):
        a.append("ANALYST TARGETS  ", style=f"bold {AMBER}")
        a.append("none — funds and trusts carry no analyst price targets, or the data provider is throttling.", style=DIM)
        return a
    a.append("WHAT ANALYSTS THINK  " if lvl == "beginner" else "ANALYST TARGETS (12m)  ", style=f"bold {AMBER}")
    if lvl == "beginner":
        c = s.rec_counts or {}
        pos, tot = c.get("strongBuy", 0) + c.get("buy", 0), sum(c.values()) if c else 0
        a.append(f"{pos} of {tot} analysts say buy. " if tot else "", style=TEXT)
        if s.upside_pct is not None and s.price_gbp and s.target_mean:
            f = s.price_gbp / (s.price or 1)
            a.append(f"Their average price target is £{s.target_mean * f:,.2f} vs £{s.price_gbp:,.2f} today ({s.upside_pct:+.0f}%).\n", style=TEXT)
        a.append_text(rating_bars(s.rec_counts))
        return a
    a.append(f"{s.n_analysts or '?'} analysts · {s.rating_label}", style=DIM)
    a.append(f" (mean {fnum(s.rating_mean, '.2f')} on 1=Strong Buy…5=Strong Sell)\n" if lvl == "advanced" else "\n", style=DIM)
    if s.price_gbp and s.upside_pct is not None:
        f = s.price_gbp / (s.price or 1)
        lo = (s.target_low * f) if s.target_low is not None else None
        a.append(f"low £{fnum(lo)}  ·  ▼ price £{s.price_gbp:,.2f}  ·  ◆ mean £{fnum(s.target_mean * f if s.target_mean else None)}  ·  high £{fnum(s.target_high * f if s.target_high else None)}\n", style=TEXT)
        a.append_text(target_range_bar(s.target_low, s.target_mean, s.target_high, s.price)); a.append("\n")
        a.append(f"upside: low {fnum(s.upside_low_pct, '+.0f', '%')} · mean {fnum(s.upside_pct, '+.0f', '%')} · high {fnum(s.upside_high_pct, '+.0f', '%')}\n", style=DIM)
    a.append_text(rating_bars(s.rec_counts))
    return a


def income_view(level: str, ticker: str, s: AnalystSnapshot | None, w: WatchItem | None, held_value: float, forecast) -> Text:
    """'Annual tax-free ISA dividend income' panel."""
    lvl = normalise_level(level)
    si = security_income(ticker, s, held_value, w)
    t = Text("ANNUAL TAX-FREE ISA DIVIDEND INCOME" + ("" if lvl == "advanced" else " (estimate)") + "\n", style=f"bold {AMBER}")
    if si.yield_pct is None:
        t.append("This holding: yield unknown — not counted (never assumed to be zero).\n", style=DIM)
    elif not si.pays_cash:
        t.append(f"Accumulating fund: no cash is paid out. The ~{si.yield_pct:.1f}% yield is reinvested inside the fund"
                 + (" (estimate).\n" if si.estimate else ".\n"), style=TEXT)
    elif lvl == "beginner":
        t.append(f"£1,000 in this has paid about £{si.per_1000_gbp:,.0f} in dividends over the last year", style=TEXT)
        t.append(f" — and you'd keep all of it, tax-free in your ISA.\n" if si.per_1000_gbp else ".\n", style=TEXT)
    else:
        t.append(f"yield {si.yield_pct:.2f}% (trailing 12m) → ", style=DIM)
        t.append(f"£{si.per_1000_gbp:,.0f}/yr per £1,000", style=f"bold {WHITE}")
        if held_value > 0:
            t.append(f" · your £{held_value:,.0f} holding ≈ ", style=DIM)
            t.append(f"£{si.held_annual_gbp:,.0f}/yr\n", style=f"bold {UP}")
        else:
            t.append("\n")
    if forecast is not None:
        t.append("Whole ISA: ", style=DIM)
        t.append(f"£{forecast.cash_gbp:,.0f}/yr cash dividends", style=f"bold {WHITE}")
        t.append(f" (≈ £{forecast.cash_per_month:,.0f}/month)", style=DIM)
        if forecast.reinvested_gbp:
            t.append(f" + £{forecast.reinvested_gbp:,.0f} reinvested inside accumulating funds", style=DIM)
        t.append("\n")
        if forecast.unknown and lvl != "beginner":
            t.append(f"excluded (no yield data): {', '.join(forecast.unknown)}\n", style=DIM)
    t.append(TAX_NOTE + " Projection from the last 12 months — not a promise.", style=DIM)
    return t


def news_compact(items: list[newsmod.NewsItem], now: float | None = None, limit: int = 5, action_prefix: str = "app.open_link('sheet', {i})") -> Text:
    n = Text("NEWS  ", style=f"bold {AMBER}")
    n.append("click a headline to open it\n" if items else "", style=DIM)
    if not items:
        n.append("no headlines available (provider blocked or offline).", style=DIM)
    for i, it in enumerate(items[:limit]):
        n.append(f"{newsmod.rel_time(it.ts, now):>8} ", style=DIM)
        n.append(f"{it.source[:18]}\n", style=CYAN)
        url = safe_url(it.url)
        head = it.title[:104]
        n.append("  ")
        n.append_text(link_text(head, url, action_prefix.format(i=i), TEXT) if url else Text(head, style=TEXT))
        n.append("\n")
    return n


def news_detail(it: newsmod.NewsItem | None, now: float | None = None) -> Text:
    t = Text()
    if it is None:
        return Text("select a story — Enter or `o` opens it in your browser", style=DIM)
    t.append(it.title + "\n", style=f"bold {WHITE}")
    t.append(f"{it.source or 'unknown source'}", style=CYAN)
    t.append(f"  ·  {newsmod.rel_time(it.ts, now)}", style=DIM)
    if it.uk:
        t.append("  ·  ", style=DIM); t.append(" UK ", style=f"bold black on {AMBER}")
    for tk in it.tickers[:4]:
        t.append("  "); t.append(f" {tk} ", style=f"bold black on {CYAN}")
    t.append("\n\n")
    t.append((it.summary or "No summary supplied by the source — open the article for the full story.") + "\n\n", style=TEXT if it.summary else DIM)
    url = safe_url(it.url)
    if url:
        t.append("OPEN  ", style=DIM)
        t.append_text(link_text("▸ read the full article in your browser", url, "app.open_link('news', 0)"))
        t.append("\n" + url[:90], style=DIM)
    else:
        t.append("no link available for this story", style=DIM)
    return t


# ------------------------------------------------------------------------------------------------ widgets
class TickerTape(Static):
    """Scrolling one-line tape: indices, FX, gilt yield, then the watchlist."""

    def __init__(self) -> None:
        super().__init__(Text(" loading market data … ", style=DIM), id="tape")
        self._full = Text()
        self._offset = 0

    def set_items(self, items: Sequence[TapeItem], state: DeskState) -> None:
        t = Text()
        if state.data_label == "DEMO":
            t.append(" ◼ DEMO DATA — NOT LIVE ", style=f"bold {WHITE} on #B3261E")
            t.append("   ")
        sep = Text("  │  ", style=BORDER)
        for it in items:
            t.append(f"{it.label} ", style=f"bold {AMBER}")
            if it.value is None:
                t.append("n/a", style=DIM)
            else:
                t.append(f"{it.value:,.{it.decimals}f}" + ("%" if it.kind == "yield" else ""), style=WHITE)
                if it.change is not None:
                    up = it.change >= 0
                    unit = "bp" if it.change_unit == "bp" else "%"
                    t.append(f" {'▲' if up else '▼'}{abs(it.change):.{1 if unit == 'bp' else 2}f}{unit}", style=UP if up else DOWN)
            t.append_text(sep)
        for tk, q in state.quotes.items():
            if tk in state.config.tickers and q.price_gbp is not None and tk in state.portfolio.values:
                t.append(f"{tk} ", style=f"bold {CYAN}")
                t.append(f"£{q.price_gbp:,.2f}", style=WHITE)
                if q.change_pct is not None:
                    t.append(f" {'▲' if q.change_pct >= 0 else '▼'}{abs(q.change_pct):.2f}%", style=UP if q.change_pct >= 0 else DOWN)
                t.append_text(sep)
        self._full = t
        self.tick(advance=False)

    def tick(self, advance: bool = True) -> None:
        w = max(self.size.width, 20)
        n = len(self._full)
        if n == 0:
            return
        if n <= w:
            self.update(self._full)
            return
        if advance:
            self._offset = (self._offset + 2) % n
        doubled = self._full + self._full
        self.update(doubled[self._offset:self._offset + w])


class StatusBar(Static):
    def __init__(self) -> None:
        super().__init__("", id="status")
        self.state: DeskState | None = None
        self.progress = ""
        self.error = ""
        self.auto = True
        self.refresh_s = 60
        self.last_ok: datetime | None = None
        self.level = "advanced"

    def level_badge(self) -> Text:
        """Clickable literacy badge: a click (or `l`) cycles Beginner -> Intermediate -> Advanced."""
        return Text(f" ◐ {LEVEL_LABEL[self.level]} ", style=Style.parse(f"bold black on {AMBER}") + Style(meta={"@click": "app.cycle_level"}))

    def tick(self) -> None:
        st = self.state
        t = Text()
        if st is None:
            t.append(" ISA COMMAND CENTRE ", style=f"bold black on {AMBER}")
            if self.error:                                   # first refresh failed: say why, don't sit on "starting …"
                t.append(f"  ⚠ {self.error}", style=f"bold {DOWN}")
                t.append("\n  fix the file named above, then press r", style=DIM)
            else:
                t.append(f"  {self.progress or 'starting …'}", style=DIM)
            self.update(t)
            return
        isa = st.isa
        cd = countdown_to_deadline(now_london(), isa.tax_year)          # ticks live every second
        used = max(0.0, min(1.0, isa.pct_used / 100))
        bar = "█" * int(used * 20) + "░" * (20 - int(used * 20))
        t.append(" ISA ", style=f"bold black on {AMBER}")
        t.append(f" {isa.tax_year.label} ", style=f"bold {AMBER}")
        t.append("│ ", style=BORDER)
        t.append("⏱ ", style=CYAN)
        t.append(cd.long(), style=f"bold {WHITE if cd.days > 30 else DOWN}")
        t.append(f" to 5 Apr {isa.tax_year.end.year} 24:00 ", style=DIM)
        t.append("│ ", style=BORDER)
        t.append("ALLOWANCE LEFT ", style=DIM)
        t.append(money(float(isa.headroom)), style=f"bold {CYAN}")
        t.append(f" of {money(float(isa.limit))} ", style=DIM)
        t.append(bar, style=AMBER if not isa.has_breach else DOWN)
        t.append(f" {isa.pct_used:.0f}% used ", style=DIM)
        if isa.lisa_remaining > 0:
            t.append(f"│ LISA {money(float(isa.lisa_remaining))} left ", style=DIM)
        if isa.has_breach:
            t.append(" ◼ BREACH ", style=f"bold {WHITE} on #B3261E")
        t.append("\n")
        lbl = st.data_label
        colour = {"LIVE": UP, "STALE": AMBER, "DEMO": DOWN, "OFFLINE": DOWN}.get(lbl, DIM)
        t.append(" DATA ", style=f"bold black on {colour}")
        t.append(f" [{st.status.badge}] ", style=f"bold {colour}")                # e.g. [LIVE · TD + FH + YF]
        if self.last_ok:
            t.append(f"updated {self.last_ok:%H:%M:%S} ", style=DIM)
        t.append(f"│ auto {'every ' + str(self.refresh_s) + 's' if self.auto else 'OFF'} ", style=DIM)
        t.append(f"│ portfolio {money(st.portfolio.total_gbp)} ", style=TEXT)
        t.append("│ ", style=BORDER)
        t.append_text(self.level_badge())
        if self.progress:
            t.append(f" │ ⟳ {self.progress} ", style=CYAN)
        if self.error:
            t.append(f" │ ⚠ {self.error} ", style=f"bold {DOWN}")
        elif st.status.errors and lbl != "LIVE":
            t.append(f" │ {st.status.errors[-1][:60]} ", style=DIM)
        self.update(t)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,question_mark", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        t = Table.grid(padding=(0, 2))
        for k, d in (("1-5 / F1-F5", "switch pane (5 = News & Catalysts)"), ("[  ]", "previous / next pane"), ("Tab / Shift+Tab", "move focus"),
                     ("↑ ↓ Enter", "navigate tables · Enter on a watchlist row opens its tear-sheet"), ("r", "refresh now"),
                     ("a", "toggle auto-refresh"), ("l", "cycle experience level: Beginner → Intermediate → Advanced (or click the badge)"),
                     ("/", "search any LSE or US ticker (pane 3), e.g. BARC.L, TSLA, brk.b"),
                     ("f", "filter: sleeve pills (pane 2) · news filter (pane 5)"), ("t", "cycle chart timeframe 1M · 6M · 1Y · 5Y (pane 3)"),
                     ("o", "sort column (pane 2) · open the selected article (pane 5)"),
                     ("m", "monthly-cash box (pane 1)"), ("c", "Co-Pilot input (pane 4)"), ("Esc", "leave a text box"), ("?", "this help"), ("q", "quit")):
            t.add_row(Text(k, style=f"bold {AMBER}"), Text(d, style=TEXT))
        yield Vertical(Static(Text(f"KEYBOARD  ·  v{__version__}", style=f"bold {CYAN}")), Static(t), Static(Text(f"\n{DISCLAIMER}", style=DIM)), id="help-box")


# ------------------------------------------------------------------------------------------------ application
class ISATerminal(App[None]):
    TITLE = "UK ISA Command Centre"
    CSS = f"""
    Screen {{ background: {BG}; color: {TEXT}; }}
    #tape {{ height: 1; background: #11161C; color: {TEXT}; }}
    #status {{ height: 3; background: {PANEL}; padding: 0; border-bottom: solid {BORDER}; }}
    TabbedContent {{ height: 1fr; }}
    ContentTabs {{ background: {PANEL}; }}
    Tab {{ color: #8B95A1; padding: 0 2; }}
    Tab.-active {{ color: {BG}; background: {AMBER}; text-style: bold; }}
    TabPane {{ padding: 0; background: {BG}; }}
    DataTable {{ background: {BG}; height: 1fr; }}
    DataTable > .datatable--header {{ background: #1B222B; color: {AMBER}; text-style: bold; }}
    DataTable > .datatable--cursor {{ background: #FF9900 22%; color: {WHITE}; }}
    DataTable:focus > .datatable--cursor {{ background: #FF9900 38%; }}
    DataTable > .datatable--hover {{ background: #FFFFFF 6%; }}
    .panel {{ border: round {BORDER}; background: #0D1115; padding: 0 1; }}
    .panel:focus-within {{ border: round {AMBER}; }}
    .title {{ color: {AMBER}; text-style: bold; height: 1; }}
    Input {{ background: {PANEL}; border: tall {BORDER}; height: 3; }}
    Input:focus {{ border: tall {CYAN}; }}
    Select {{ width: 1fr; }}
    SelectCurrent {{ background: {PANEL}; border: tall {BORDER}; }}
    Button {{ min-width: 10; height: 3; margin-right: 1; background: #1B222B; color: {AMBER}; border: tall {BORDER}; }}
    Button:hover {{ background: {AMBER}; color: {BG}; }}
    Button.pill {{ min-width: 7; padding: 0 1; margin-right: 1; background: {PANEL}; color: #8B95A1; border: tall {BORDER}; }}
    Button.pill.on {{ background: {AMBER}; color: {BG}; text-style: bold; border: tall {AMBER}; }}
    Button.save {{ background: {CYAN}; color: {BG}; text-style: bold; border: tall {CYAN}; }}
    #strip {{ height: 4; padding: 0 1; background: {PANEL}; border-bottom: solid {BORDER}; }}
    #allow-bar {{ height: 3; padding: 0 1; background: {PANEL}; border-bottom: solid {BORDER}; display: none; }}
    #desk-body {{ height: 1fr; }}
    #desk-left {{ width: 58%; }}
    #desk-right {{ width: 42%; }}
    #signals {{ height: 1fr; }}
    #detail {{ height: 1fr; overflow-y: auto; }}
    #plan {{ height: 1fr; }}
    #plan-bar {{ height: 3; }}
    #plan-bar Static {{ width: auto; margin: 1 1 0 0; }}
    #plan-bar Input {{ width: 18; }}
    #plan-note {{ height: auto; color: {DIM}; }}
    #ledger {{ height: auto; max-height: 9; }}
    #pills {{ height: 3; padding: 0 1; }}
    #wl-note {{ height: 2; color: {DIM}; padding: 0 1; }}
    #ts-controls {{ height: 3; }}
    #ts-search {{ width: 26; margin-right: 1; }}
    #ts-controls Select {{ width: 34; margin-right: 1; }}
    #ts-head {{ height: 3; padding: 0 1; }}
    #ts-left {{ width: 60%; }}
    #ts-right {{ width: 40%; }}
    #chart {{ height: 1fr; min-height: 12; }}
    #analyst {{ height: auto; max-height: 13; }}
    #fund {{ height: auto; }}
    #income {{ height: auto; }}
    #news {{ height: 1fr; min-height: 6; overflow-y: auto; }}
    #sim {{ height: auto; }}
    #sim-bar {{ height: 3; }}
    #sim-bar Input {{ width: 16; margin-right: 1; }}
    #hb-wrap {{ width: 50%; }}
    #cp-wrap {{ width: 50%; }}
    #chat {{ height: 1fr; background: {BG}; }}
    #cp-buttons {{ height: 3; }}
    #news-controls {{ height: 3; padding: 0 1; }}
    #news-meta {{ width: 1fr; margin: 1 1 0 1; color: {DIM}; }}
    #news-left {{ width: 56%; }}
    #news-right {{ width: 44%; }}
    #news-table {{ height: 1fr; }}
    #news-detail-wrap {{ height: 1fr; overflow-y: auto; }}
    #news-open {{ margin-top: 1; }}
    #keys {{ height: 1; background: #11161C; color: {DIM}; padding: 0 1; }}
    #disclaimer {{ height: 2; background: {BG}; color: {AMBER}; text-style: italic; content-align: center middle; text-align: center; border-top: solid {BORDER}; }}
    HelpScreen {{ align: center middle; background: #000000 70%; }}
    #help-box {{ width: 96; height: auto; padding: 1 2; background: {PANEL}; border: round {AMBER}; }}
    """
    BINDINGS = [
        Binding("1,f1", "goto('desk')", "Desk", show=False), Binding("2,f2", "goto('watch')", "Watchlist", show=False),
        Binding("3,f3", "goto('sheet')", "Tear-sheet", show=False), Binding("4,f4", "goto('hb')", "Handbook", show=False),
        Binding("5,f5", "goto('newsdesk')", "News", show=False),
        Binding("left_square_bracket", "step(-1)", "Prev", show=False), Binding("right_square_bracket", "step(1)", "Next", show=False),
        Binding("r", "refresh", "Refresh"), Binding("a", "toggle_auto", "Auto"), Binding("slash", "search", "Search"),
        Binding("l", "cycle_level", "Level"), Binding("f", "filter", "Filter"), Binding("t", "timeframe", "Timeframe"),
        Binding("o", "sort_or_open", "Sort/Open"), Binding("m", "focus_cash", "Cash", show=False), Binding("c", "focus_cp", "Co-Pilot", show=False),
        Binding("question_mark", "help", "Help"), Binding("q", "quit", "Quit"), Binding("escape", "blur", "Leave box", show=False),
    ]

    def __init__(self, desk: Desk, *, auto_refresh: bool = True) -> None:
        super().__init__()
        self.desk = desk
        self.copilot = CoPilot(desk)
        self.state: DeskState | None = None
        self.auto = auto_refresh
        try:
            self.level = normalise_level(load_config(desk.config_path).experience_level)
        except ConfigError:
            self.level = "advanced"
        self._refreshing = False
        self._sort_col = 0
        self._sort_rev = False
        self._sleeve = "all"
        self._ts_ticker: str | None = None
        self._ts_range = "1Y"
        self._adhoc: dict[str, Adhoc] = {}
        self._sheet_cache: tuple | None = None
        self._sheet_urls: list[str] = []
        self._hb_hash = 0
        self._cash_value = ""
        self._news_items: list[newsmod.NewsItem] = []
        self._news_view: list[newsmod.NewsItem] = []
        self._news_mode = "all"
        self._news_source = "none"
        self._news_loaded_at = 0.0
        self._news_loading = False

    # ---------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        yield TickerTape()
        yield StatusBar()
        with TabbedContent(initial="desk", id="tabs"):
            with TabPane("1 COMMAND DESK", id="desk"):
                yield Static(id="strip")
                yield Static(id="allow-bar")
                with Horizontal(id="desk-body"):
                    with Vertical(id="desk-left", classes="panel"):
                        yield Static(Text("TERMINAL SIGNALS — prioritised, each cites its handbook rule", style=f"bold {AMBER}"), classes="title", id="signals-title")
                        yield DataTable(id="signals", cursor_type="row", zebra_stripes=False)
                    with Vertical(id="desk-right"):
                        with VerticalScroll(id="detail", classes="panel"):
                            yield Static(Text("select a signal", style=DIM), id="detail-body")
                        with Vertical(classes="panel", id="plan-box"):
                            with Horizontal(id="plan-bar"):
                                yield Static(Text("BUY PLAN (no selling)  £", style=f"bold {AMBER}"), classes="title")
                                yield Input(value="", placeholder="new cash £", id="cash", type="number")
                            yield DataTable(id="plan", cursor_type="row")
                            yield Static(id="plan-note")
                        yield Static(id="ledger", classes="panel")
            with TabPane("2 WATCHLIST · ANALYSTS", id="watch"):
                with Horizontal(id="pills"):
                    for key, label in SLEEVE_PILLS:
                        yield Button(label, id=f"pill-{key}", classes="pill on" if key == "all" else "pill")
                yield DataTable(id="watchlist", cursor_type="row")
                yield Static(id="wl-note")
            with TabPane("3 TEAR-SHEET", id="sheet"):
                with Horizontal(id="ts-controls"):
                    yield Input(placeholder="/ search any ticker ⏎", id="ts-search")
                    yield Select([], prompt="Watchlist ticker", id="ts-ticker", allow_blank=True)
                    for k in RANGES:
                        yield Button(k, id=f"tf-{k}", classes="pill on" if k == "1Y" else "pill")
                    yield Button("★ Save to watchlist", id="ts-save", classes="save")
                yield Static(id="ts-head")
                with Horizontal(id="ts-body"):
                    with Vertical(id="ts-left"):
                        if PlotextPlot is not None:
                            yield PlotextPlot(id="chart")
                        else:                                                           # pragma: no cover
                            yield Static("textual-plotext not installed — chart unavailable", id="chart")
                        yield Static(id="analyst", classes="panel")
                        yield Static(id="income", classes="panel")
                    with Vertical(id="ts-right"):
                        yield Static(id="fund", classes="panel")
                        yield Static(id="news", classes="panel")
                        with Vertical(id="sim", classes="panel"):
                            yield Static(Text("ISA IMPACT SIMULATOR", style=f"bold {AMBER}"), classes="title")
                            with Horizontal(id="sim-bar"):
                                yield Input(value="1000", placeholder="£ to buy", id="sim-amt", type="number")
                                yield Select([("New subscription", "new"), ("From ISA cash", "cash")], value="new", id="sim-src", allow_blank=False)
                            yield Static(id="sim-out")
            with TabPane("4 HANDBOOK · CO-PILOT", id="hb"):
                with Horizontal():
                    with VerticalScroll(id="hb-wrap", classes="panel"):
                        yield Markdown("*loading handbook …*", id="hb-md")
                    with Vertical(id="cp-wrap", classes="panel"):
                        yield Static(Text("AI CO-PILOT — local, rule-based (no API) · type 'help'", style=f"bold {AMBER}"), classes="title")
                        yield RichLog(id="chat", wrap=True, markup=False, highlight=False)
                        with Horizontal(id="cp-buttons"):
                            for lbl, cmd in (("Health", "health"), ("Weekly Memo", "memo"), ("Tax", "tax"), ("Buy Plan", "buy"),
                                             ("Signals", "signals"), ("LLM Prompt", "prompt")):
                                yield Button(lbl, id=f"cp-{cmd}", name=cmd)
                        yield Input(placeholder="ask: health · memo · tax · buy 750 · explain NVDA · sim VUAG.L 2000 · prompt", id="cp-input")
            with TabPane("5 NEWS · CATALYSTS", id="newsdesk"):
                with Horizontal(id="news-controls"):
                    for key in newsmod.FILTER_ORDER:
                        yield Button(newsmod.FILTER_LABELS[key], id=f"nf-{key}", classes="pill on" if key == "all" else "pill")
                    yield Static(Text("loading …", style=DIM), id="news-meta")
                with Horizontal(id="news-body"):
                    with Vertical(id="news-left", classes="panel"):
                        yield DataTable(id="news-table", cursor_type="row")
                    with Vertical(id="news-right", classes="panel"):
                        with VerticalScroll(id="news-detail-wrap"):
                            yield Static(id="news-detail")
                        yield Button("Open article in browser  ⏎", id="news-open")
        yield Static(Text(" 1-5 panes · [ ] · r refresh · a auto · l level · f filter · t timeframe · / search · o sort/open · m cash · c co-pilot · ? help · q quit ", style=DIM), id="keys")
        yield Static(Text(DISCLAIMER, style=f"italic {AMBER}"), id="disclaimer")

    # ---------------------------------------------------------------- lifecycle
    def on_mount(self) -> None:
        self.register_theme(THEME)
        self.theme = "isa-terminal"
        bar = self.query_one(StatusBar)
        bar.auto, bar.level = self.auto, self.level
        self.query_one("#ts-save").display = False
        self._setup_signal_columns()
        self._setup_watch_columns()
        plan = self.query_one("#plan", DataTable)
        for label, w in (("TICKER", 8), ("£ BUY", 11), ("UNITS", 9), ("WEIGHT now → after", 20), ("TARGET", 7)):
            plan.add_column(label, width=w)
        news = self.query_one("#news-table", DataTable)
        for label, w in (("WHEN", 9), ("SOURCE", 15), ("TICKER", 9), ("HEADLINE", 80)):
            news.add_column(label, width=w)
        self._apply_level(rerender=False)
        bar.tick()
        self.set_interval(1.0, bar.tick)
        try:
            c = load_config(self.desk.config_path)
            bar.refresh_s = c.refresh_seconds
            self.set_interval(1.0 / max(c.tape_scroll_hz, 1), self.query_one(TickerTape).tick)
            self.set_interval(max(c.refresh_seconds, 10), self._auto_tick)
        except ConfigError as exc:
            self.notify(str(exc), severity="error", timeout=15)
        self.query_one("#chat", RichLog).write(Text("Co-Pilot ready — type 'help'. Local & deterministic: it explains the same numbers the Desk shows.", style=DIM))
        self.query_one("#news-detail", Static).update(news_detail(None))
        self.action_refresh()

    # ---------------------------------------------------------------- experience level
    def _apply_level(self, rerender: bool = True) -> None:
        lvl = self.level
        adv, beg = lvl == "advanced", lvl == "beginner"
        bar = self.query_one(StatusBar)
        bar.level = lvl
        self.query_one("#ledger").display = adv
        self.query_one("#sim").display = adv
        self.query_one("#allow-bar").display = beg
        self.query_one("#plan").display = not beg
        self.query_one("#signals-title", Static).update(Text(
            {"beginner": "WHAT NEEDS YOUR ATTENTION — most important first", "intermediate": "SIGNALS — most important first, with a one-line rule summary",
             "advanced": "TERMINAL SIGNALS — prioritised, each cites its handbook rule"}[lvl], style=f"bold {AMBER}"))
        self._setup_signal_columns()
        self._setup_watch_columns()
        bar.tick()
        if rerender and self.state:
            self._render_all(self.state)

    def action_cycle_level(self) -> None:
        new = next_level(self.level)
        self.level = new
        try:
            set_experience_level(self.desk.config_path, new)          # persisted in config.yaml (ui.experience_level)
            saved = "saved to config.yaml"
        except (ConfigError, OSError) as exc:
            saved = f"not saved: {exc}"
        self._apply_level()
        self.notify(f"Experience level: {LEVEL_LABEL[new]} ({saved})", timeout=3)

    # ---------------------------------------------------------------- columns
    def _setup_signal_columns(self) -> None:
        sig = self.query_one("#signals", DataTable)
        sig.clear(columns=True)
        cols = {"advanced": (("PRI", 4), ("SEV", 9), ("ACTION", 15), ("TICKER", 8), ("SIGNAL", 64)),
                "intermediate": (("SEV", 9), ("ACTION", 15), ("TICKER", 8), ("SIGNAL", 68)),
                "beginner": (("HOW IMPORTANT", 14), ("WHAT TO DO", 17), ("HOLDING", 10), ("WHAT'S HAPPENING", 62))}[self.level]
        for label, w in cols:
            sig.add_column(label, width=w)

    def _wl_cols(self) -> list[str]:
        return WL_LEVEL[self.level]

    def _setup_watch_columns(self) -> None:
        wl = self.query_one("#watchlist", DataTable)
        wl.clear(columns=True)
        for k in self._wl_cols():
            label, w = WL_ALL[k]
            wl.add_column(label, width=w)
        self._sort_col = min(self._sort_col, len(self._wl_cols()) - 1)

    # ---------------------------------------------------------------- refresh
    def action_refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        self._cash_value = self.query_one("#cash", Input).value
        self._do_refresh()

    def _auto_tick(self) -> None:
        if self.auto and self.state is not None:
            self.action_refresh()

    def action_toggle_auto(self) -> None:
        self.auto = not self.auto
        bar = self.query_one(StatusBar)
        bar.auto = self.auto
        bar.tick()
        self.notify(f"Auto-refresh {'ON' if self.auto else 'OFF'}", timeout=2)

    @work(thread=True, exclusive=True, group="refresh")
    def _do_refresh(self) -> None:
        try:
            cash = float(self._cash_value) if self._cash_value.strip() else None
        except ValueError:
            cash = None
        try:
            st = self.desk.refresh(monthly_cash=cash, progress=lambda stage, i, n: self.call_from_thread(self._set_progress, f"{stage} {i}/{n}"))
        except Exception as exc:                                    # noqa: BLE001 - never let a bad file/feed kill the UI
            self.call_from_thread(self._refresh_failed, exc)
            return
        self.call_from_thread(self._apply, st)

    def _set_progress(self, msg: str) -> None:
        bar = self.query_one(StatusBar)
        bar.progress = msg
        bar.tick()

    def _refresh_failed(self, exc: Exception) -> None:
        self._refreshing = False
        bar = self.query_one(StatusBar)
        msg = str(exc).replace(str(self.desk.config_path.parent) + "/", "")      # relative paths: keep the reason visible
        bar.progress, bar.error = "", f"refresh failed: {type(exc).__name__}: {msg}"[:170]
        bar.tick()
        self.notify(f"{type(exc).__name__}: {exc}", severity="error", timeout=12)

    def _apply(self, st: DeskState) -> None:
        self._refreshing = False
        self.state = st
        bar = self.query_one(StatusBar)
        bar.state, bar.progress, bar.error, bar.last_ok = st, "", "", datetime.now()
        bar.tick()
        self.query_one(TickerTape).set_items(st.tape, st)
        self._render_all(st)
        sel = self.query_one("#ts-ticker", Select)
        opts = [(f"{w.ticker:<7}{w.name[:17]}", w.ticker) for w in st.config.watchlist]
        if [o[1] for o in opts] != getattr(self, "_ts_opts", None):
            self._ts_opts = [o[1] for o in opts]
            with sel.prevent(Select.Changed):
                sel.set_options(opts)
                if self._ts_ticker in self._ts_opts:
                    sel.value = self._ts_ticker
        if self._ts_ticker is None and opts:
            self._ts_ticker = opts[0][1]
            with sel.prevent(Select.Changed):
                sel.value = self._ts_ticker
        if self._ts_ticker:
            self._load_tearsheet(self._ts_ticker)
        if self.query_one(TabbedContent).active == "newsdesk":
            self._ensure_news(force=True)

    def _render_all(self, st: DeskState) -> None:
        self._render_desk(st)
        self._render_watchlist(st)
        self._render_handbook(st)
        if self._sheet_cache and self._sheet_cache[0] == self._ts_ticker:
            self._render_tearsheet(*self._sheet_cache)
        self._render_news()

    def _name_of(self, ticker: str) -> str:
        if self.state:
            w = self.state.config.watch(ticker)
            if w:
                return w.name.split(" (")[0]
        a = self._adhoc.get(ticker)
        return (a.snap.name if a else None) or ticker

    # ---------------------------------------------------------------- pane 1
    def _render_desk(self, st: DeskState) -> None:
        pf, isa = st.portfolio, st.isa
        h = portfolio_health(st)
        lvl = self.level
        s = Text()
        s.append(" ISA VALUE ", style=DIM); s.append(f"{money(pf.total_gbp)}  ", style=f"bold {WHITE}")
        s.append("GAIN " if lvl == "beginner" else "P&L ", style=DIM); s.append_text(pct(pf.pnl_pct)); s.append(f" ({money(pf.pnl_gbp)})  ", style=DIM)
        s.append("CASH ", style=DIM); s.append(f"{money(pf.cash_gbp)} ({pf.cash_weight * 100:.1f}%)  ", style=TEXT)
        s.append("HEALTH ", style=DIM)
        s.append(f"{h.score:.0f}/100 [{h.grade}]  ", style=f"bold {UP if h.score >= 70 else AMBER if h.score >= 55 else DOWN}")
        if lvl != "beginner":
            s.append("FEES " if lvl == "intermediate" else "OCF ", style=DIM); s.append(f"{fnum(pf.blended_ter_pct, '.2f', '%')}  ", style=TEXT)
            s.append("YIELD ", style=DIM); s.append(f"{fnum(pf.yield_pct, '.2f', '%')}  ", style=TEXT)
        if pf.top_sector:
            over = pf.top_sector[1] * 100 > st.rules.position.max_sector_pct
            s.append("TOP SECTOR ", style=DIM)
            s.append(f"{pf.top_sector[0]} {pf.top_sector[1] * 100:.1f}%  ", style=DOWN if over else TEXT)
        if lvl == "advanced":
            s.append("EFF.N ", style=DIM); s.append(f"{pf.effective_n:.1f}", style=TEXT)
        s.append("\n")
        s.append(" ALLOWANCE ", style=DIM)
        if lvl == "beginner":
            s.append(f"{money(float(isa.headroom))} of your {money(float(isa.limit))} ISA allowance is still available", style=TEXT)
        else:
            s.append(f"net used {money(float(isa.net_used))} ({isa.pct_used:.1f}%) · remaining {money(float(isa.remaining))}", style=TEXT)
            if isa.replacement_credit > 0:
                s.append(f" + {money(float(isa.replacement_credit))} flex-credit", style=CYAN)
        s.append(f" · {'you need' if lvl == 'beginner' else 'pace needs'} {money(float(st.pace.required_per_month))}/mo vs plan {money(st.monthly_cash)}/mo", style=AMBER)
        hi = sum(1 for g in st.signals if g.severity in ("CRITICAL", "HIGH"))
        s.append(f"\n {len(st.signals)} signals ({hi} important) · data {st.data_label}"
                 + (" · rules from handbook §0" if lvl != "beginner" else ""), style=DIM)
        self.query_one("#strip", Static).update(s)

        # Beginner: a big visual £20,000 allowance bar
        f, e, p = allowance_bar(float(isa.net_used), float(isa.limit), 40)
        bar = Text(" YOUR ISA ALLOWANCE  ", style=f"bold {AMBER}")
        bar.append(f, style=DOWN if isa.has_breach else UP); bar.append(e, style=BORDER)
        bar.append(f"  {money(float(isa.net_used))} of {money(float(isa.limit))} used ({p:.0f}%)", style=f"bold {WHITE}")
        bar.append(f"\n {money(float(isa.headroom))} still available · {st.countdown.days} days until the 5 April deadline", style=DIM)
        self.query_one("#allow-bar", Static).update(bar)

        tbl = self.query_one("#signals", DataTable)
        prev = self._highlighted_signal_id()
        tbl.clear()
        for g in st.signals:
            v = render_signal(g, lvl, st.rules, self._name_of)
            tkr = Text(g.ticker or "—", style=CYAN)
            if lvl == "advanced":
                tbl.add_row(Text(f"{g.priority:.0f}", style=DIM, justify="right"), sev_text(g.severity), Text(g.action, style=f"bold {AMBER}"), tkr,
                            Text(v.headline.split(" - ", 1)[1] if " - " in v.headline else g.detail, style=TEXT), key=g.id)
            elif lvl == "intermediate":
                tbl.add_row(sev_text(g.severity), Text(g.action, style=f"bold {AMBER}"), tkr,
                            Text(v.headline.split(" - ", 1)[1] if " - " in v.headline else g.detail, style=TEXT), key=g.id)
            else:
                tbl.add_row(sev_text(g.severity, plain=True), Text(ACTION_PLAIN.get(g.action, g.action.title()), style=f"bold {AMBER}"), tkr,
                            Text(v.headline.split(" - ", 1)[1] if " - " in v.headline else g.detail, style=TEXT), key=g.id)
        if st.signals:
            idx = next((i for i, g in enumerate(st.signals) if g.id == prev), 0)
            tbl.move_cursor(row=idx)
            self._show_signal(st.signals[idx])
        self._render_plan(st, st.plan)
        self._render_ledger(st)

    def _highlighted_signal_id(self) -> str | None:
        tbl = self.query_one("#signals", DataTable)
        try:
            return tbl.coordinate_to_cell_key(tbl.cursor_coordinate).row_key.value
        except Exception:                                           # noqa: BLE001 - empty table
            return None

    @on(DataTable.RowHighlighted, "#signals")
    def _signal_highlighted(self, ev: DataTable.RowHighlighted) -> None:
        if self.state and ev.row_key is not None:
            g = next((g for g in self.state.signals if g.id == ev.row_key.value), None)
            if g:
                self._show_signal(g)

    def _show_signal(self, g: Signal) -> None:
        st = self.state
        if st is None:
            return
        v = render_signal(g, self.level, st.rules, self._name_of)
        t = Text()
        t.append(f"{ACTION_PLAIN.get(g.action, g.action) if self.level == 'beginner' else g.action}", style=f"bold {AMBER}")
        if g.ticker:
            t.append(f"  {g.ticker}", style=f"bold {CYAN}")
            if self.level == "beginner":
                t.append(f"  {self._name_of(g.ticker)}", style=DIM)
        t.append("  "); t.append_text(sev_text(g.severity, plain=self.level == "beginner")); t.append("\n")
        t.append((v.headline.split(" - ", 1)[1] if " - " in v.headline else g.detail) + "\n\n", style=f"bold {WHITE}")
        if self.level == "beginner" and v.meaning:
            t.append("WHAT THIS MEANS\n", style=f"bold {CYAN}")
            t.append(v.meaning + "\n\n", style=WHITE)
        t.append("WHY (handbook-based)\n" if self.level != "beginner" else "THE DETAIL\n", style=f"bold {CYAN}")
        for line in v.rationale:
            t.append(" • ", style=AMBER); t.append(line + "\n", style=TEXT)
        if self.level == "advanced":
            t.append("\nRULES  ", style=DIM)
            t.append(v.rules_line if g.rules else ("custom alert (config.yaml)" if "ALERT" in g.action and "DATA" not in g.action else "—"), style=AMBER)
        elif self.level == "intermediate" and v.rules_line:
            t.append("\nRULE  ", style=DIM); t.append(v.rules_line, style=AMBER)
        if g.suggested_gbp:
            t.append(f"\n{'SELL/TRIM' if g.side == 'SELL' else 'BUY'} size  " if self.level != "beginner" else
                     f"\n{'Roughly how much to reduce' if g.side == 'SELL' else 'Roughly how much'}  ", style=DIM)
            t.append(money(g.suggested_gbp), style=f"bold {WHITE}")
        sc = g.metrics.get("score")
        if sc is not None and self.level == "advanced":
            t.append("   CONVICTION ", style=DIM); t.append(f"{sc:.0f}/100", style=WHITE)
        self.query_one("#detail-body", Static).update(t)

    def _render_plan(self, st: DeskState, plan) -> None:
        tbl = self.query_one("#plan", DataTable)
        tbl.clear()
        note = Text()
        if plan is None or not plan.orders:
            tbl.add_row(Text("—", style=DIM), Text("nothing eligible this month", style=DIM), "", "", "")
            if self.level == "beginner":
                note.append("Nothing to add this month — your plan looks balanced, or the safety limits are blocking the usual candidates.\n", style=DIM)
        else:
            for o in plan.orders:
                tbl.add_row(Text(o.ticker, style=f"bold {CYAN}"), Text(f"£{o.amount:,.2f}", style=f"bold {WHITE}", justify="right"),
                            num(o.units, ",.3f"), Text(f"{o.pre_weight * 100:5.1f}% → {o.post_weight * 100:5.1f}%", style=TEXT),
                            Text(f"{o.target_weight * 100:.0f}%", style=DIM, justify="right"))
        if plan:
            if self.level == "beginner" and plan.orders:
                note.append(f"This month's idea for your £{float(plan.cash_in):,.0f}: ", style=f"bold {AMBER}")
                note.append("; ".join(f"£{o.amount:,.0f} into {self._name_of(o.ticker)}" for o in plan.orders) + ".\n", style=WHITE)
                note.append("No selling needed — new money just goes where you're furthest below plan.", style=DIM)
            else:
                for o in plan.orders:
                    why = (o.reason.split(" - ", 1)[1] if " - " in o.reason else o.reason) or "closest to target weight"
                    note.append(f"{o.ticker:<7}", style=CYAN); note.append(f"{plain_text(why, self.level)[:60]}\n", style=DIM)
                note.append(f"cash £{plan.cash_in:,.2f} → deployed £{plan.deployed:,.2f}, left £{plan.undeployed:,.2f} · drift {plan.pre_drift * 100:.1f}% → ", style=DIM)
                note.append(f"{plan.post_drift * 100:.1f}%", style=UP)
            for w in plan.warnings:
                note.append(f"\n⚠ {plain_text(w, self.level)}", style=AMBER)
        self.query_one("#plan-note", Static).update(note)
        inp = self.query_one("#cash", Input)
        if not inp.value and not inp.has_focus:
            inp.value = f"{st.monthly_cash:g}"

    @on(Input.Submitted, "#cash")
    def _cash_submitted(self, ev: Input.Submitted) -> None:
        if not self.state:
            return
        try:
            cash = float(ev.value)
        except ValueError:
            self.notify("Enter a number, e.g. 1000", severity="warning")
            return
        self._render_plan(self.state, self.desk.buy_plan(self.state, cash))

    def _render_ledger(self, st: DeskState) -> None:
        isa = st.isa
        t = Text()
        t.append("ISA LEDGER ", style=f"bold {AMBER}"); t.append(f"{isa.tax_year.label}  ", style=DIM)
        t.append(f"subs {money(float(isa.subscriptions), 2)} · flex withdrawals {money(float(isa.eligible_withdrawals), 2)} · net {money(float(isa.net_used), 2)}\n", style=TEXT)
        for u in isa.per_account:
            t.append(f" {u.account:<9}", style=CYAN)
            t.append(f"in {money(float(u.subscriptions)):>8}  out {money(float(u.eligible_withdrawals)):>7}  net {money(float(u.net_used)):>8}", style=TEXT)
            if u.replacement_credit > 0:
                t.append(f"  credit {money(float(u.replacement_credit))}", style=CYAN)
            t.append("  flex" if u.flexible else "", style=DIM)
            t.append("\n")
        if isa.ignored_transfers or isa.ignored_bonus:
            t.append(f" ignored: transfers {money(float(isa.ignored_transfers))}, LISA bonus {money(float(isa.ignored_bonus), 2)}\n", style=DIM)
        if st.bed_isa and st.bed_isa.lines:
            b = st.bed_isa
            t.append(f" Bed & ISA: gain {money(b.total_gain)} within CGT room {money(b.cgt_allowance_start)} → shelter {money(b.total_proceeds)}", style=AMBER)
        self.query_one("#ledger", Static).update(t)

    # ---------------------------------------------------------------- pane 2
    def _wl_rows(self, st: DeskState) -> list[tuple[Any, dict[str, tuple[Any, Text]]]]:
        rows = []
        pos = {p.ticker: p for p in st.portfolio.positions}
        rules = st.rules
        for w in st.config.watchlist:
            if self._sleeve != "all" and w.sleeve != self._sleeve:
                continue
            s, q, sc = st.snaps.get(w.ticker), st.quotes.get(w.ticker), st.scores.get(w.ticker)
            p = pos.get(w.ticker)
            top = next(iter(st.signals_for(w.ticker)), None)
            tgt_gbp = (s.price_gbp * (1 + s.upside_pct / 100)) if (s and s.price_gbp and s.upside_pct is not None) else None
            wt = (f"{p.weight * 100:4.1f}/{w.target_weight_pct:.0f}" if p else f"  0/{w.target_weight_pct:.0f}")
            vc = {"RICH": DOWN, "CHEAP": UP}.get(s.valuation_label if s else "", TEXT)
            vb, mb, ab, ib = valuation_badge(s, rules), momentum_badge(s, rules), analyst_badge(s, rules), income_badge(s)
            pb = portfolio_badge(p, w.kind, rules)
            tip = render_signal(top, self.level, rules, self._name_of) if top else None
            tip_txt = ACTION_PLAIN.get(top.action, top.action.title()) if top else "—"
            cells: dict[str, tuple[Any, Text]] = {
                "ticker": (w.ticker, Text(w.ticker, style=f"bold {CYAN}")), "name": (w.name[:24], Text(w.name[:24], style=TEXT)),
                "price": (s.price_gbp if s else None, num(s.price_gbp if s else None)),
                "day": (q.change_pct if q else None, pct(q.change_pct if q else None, 2)),
                "consensus": (s.rating_mean if s and s.rating_mean else 9, Text(s.rating_label if s else "—", style={"Strong Buy": f"bold {UP}", "Buy": UP, "Hold": AMBER, "Sell": DOWN, "Strong Sell": f"bold {DOWN}"}.get(s.rating_label if s else "", DIM))),
                "n": (s.n_analysts if s else None, num(s.n_analysts if s else None, ",.0f", DIM)), "target": (tgt_gbp, num(tgt_gbp)),
                "upside": (s.upside_pct if s else None, pct(s.upside_pct if s else None, 1)),
                "rsi": (s.rsi14 if s else None, rsi_text(s.rsi14 if s else None, rules.technicals.rsi_oversold, rules.technicals.rsi_overbought)),
                "yield": (s.dividend_yield_pct if s else None, Text(("—" if not s or s.dividend_yield_pct is None else f"{s.dividend_yield_pct:.2f}%" + ("*" if s.yield_is_estimate else "")), style=TEXT, justify="right")),
                "vs5y": (s.vs_5y_avg_pct if s else None, pct(s.vs_5y_avg_pct if s else None, 0)),
                "value": (s.valuation_label if s else "", Text(s.valuation_label if s else "—", style=f"bold {vc}")),
                "trend": (s.trend if s else "", Text(s.trend if s else "—", style={"UPTREND": UP, "DOWNTREND": DOWN, "MIXED": AMBER}.get(s.trend if s else "", DIM))),
                "wt": (p.weight if p else 0, Text(wt, style=TEXT, justify="right")),
                "score": (sc.total if sc and sc.total is not None else None, num(sc.total if sc else None, ".0f", AMBER)),
                "top": (top.severity if top else "", Text(f"{top.action[:16]}" if top else "—", style=SEV_STYLE.get(top.severity, DIM) if top else DIM)),
                "fwdpe": (s.forward_pe if s else None, num(s.forward_pe if s else None, ".1f")),
                "sector": ((s.sector if s and s.sector else w.sector) or "", Text(((s.sector if s and s.sector else w.sector) or ("Fund mix" if w.look_through else "—"))[:20], style=TEXT)),
                "analysts": (s.rating_mean if s and s.rating_mean else 9, badge_text(ab)), "valuation": (vb.label, badge_text(vb)),
                "momentum": (mb.label, badge_text(mb)), "income": (s.dividend_yield_pct if s else None, badge_text(ib)),
                "mine": (pb.label, badge_text(pb)), "tip": (top.severity if top else "", Text(tip_txt[:18], style=SEV_STYLE.get(top.severity, DIM) if top else DIM)),
            }
            rows.append((w.ticker, cells))
        return rows

    def _render_watchlist(self, st: DeskState) -> None:
        tbl = self.query_one("#watchlist", DataTable)
        cols = self._wl_cols()
        rows = self._wl_rows(st)
        sort_key = cols[min(self._sort_col, len(cols) - 1)]
        key = lambda r: (r[1][sort_key][0] is None, r[1][sort_key][0] if r[1][sort_key][0] is not None else 0)
        try:
            rows.sort(key=key, reverse=self._sort_rev)
        except TypeError:
            pass
        sel = None
        try:
            sel = tbl.coordinate_to_cell_key(tbl.cursor_coordinate).row_key.value
        except Exception:                                           # noqa: BLE001
            pass
        tbl.clear()
        for tk, cells in rows:
            tbl.add_row(*[cells[c][1] for c in cols], key=tk)
        if sel is not None:
            try:
                tbl.move_cursor(row=tbl.get_row_index(sel))
            except Exception:                                       # noqa: BLE001
                pass
        counts = {k: sum(1 for w in st.config.watchlist if k == "all" or w.sleeve == k) for k, _ in SLEEVE_PILLS}
        for k, label in SLEEVE_PILLS:
            b = self.query_one(f"#pill-{k}", Button)
            b.label = f"{label} ({counts[k]})"
            b.set_class(k == self._sleeve, "on")
        note = Text(f" {len(rows)} shown · sorted by {WL_ALL[sort_key][0]} {'▼' if self._sort_rev else '▲'} (o)   f = next sleeve   Enter = tear-sheet\n", style=DIM)
        if self.level == "advanced":
            note.append(" vs 5Y AVG = price vs its own 5-year average price (a price-based proxy for valuation, not P/E) · TARGET £ = mean analyst target in GBP · "
                        "* = estimated yield (accumulating fund) · '—' = no data, never zero", style=DIM)
        else:
            note.append(" '—' means no data (never zero) · * = estimated yield for an accumulating fund (income is reinvested inside it)", style=DIM)
        self.query_one("#wl-note", Static).update(note)

    @on(Button.Pressed, "#pills Button")
    def _pill_pressed(self, ev: Button.Pressed) -> None:
        self._sleeve = (ev.button.id or "pill-all").removeprefix("pill-")
        if self.state:
            self._render_watchlist(self.state)

    def action_filter(self) -> None:
        tab = self.query_one(TabbedContent).active
        if tab == "newsdesk":
            self._news_mode = newsmod.FILTER_ORDER[(newsmod.FILTER_ORDER.index(self._news_mode) + 1) % len(newsmod.FILTER_ORDER)]
            self._render_news()
        elif tab == "watch":
            keys = [k for k, _ in SLEEVE_PILLS]
            self._sleeve = keys[(keys.index(self._sleeve) + 1) % len(keys)]
            if self.state:
                self._render_watchlist(self.state)
        else:
            self.notify("Filters live in pane 2 (sleeves) and pane 5 (news)", timeout=2)

    def action_sort_or_open(self) -> None:
        if self.query_one(TabbedContent).active == "newsdesk":
            self.action_open_link("news", 0)
            return
        self._sort_col += 1
        if self._sort_col >= len(self._wl_cols()):
            self._sort_col, self._sort_rev = 0, not self._sort_rev
        if self.state:
            self._render_watchlist(self.state)

    @on(DataTable.RowSelected, "#watchlist")
    def _open_sheet(self, ev: DataTable.RowSelected) -> None:
        if ev.row_key.value:
            self._select_ticker(ev.row_key.value)
            self.action_goto("sheet")

    # ---------------------------------------------------------------- pane 3: selection, search, timeframe
    def _select_ticker(self, ticker: str) -> None:
        self._ts_ticker = ticker
        sel = self.query_one("#ts-ticker", Select)
        with sel.prevent(Select.Changed):
            if ticker in getattr(self, "_ts_opts", []):
                sel.value = ticker
            else:
                sel.clear()
        self.query_one("#ts-save").display = ticker in self._adhoc and not (self.state and self.state.config.watch(ticker))
        self._load_tearsheet(ticker)

    @on(Select.Changed, "#ts-ticker")
    def _ticker_changed(self, ev: Select.Changed) -> None:
        if isinstance(ev.value, str):
            self._select_ticker(ev.value)

    @on(Input.Submitted, "#ts-search")
    def _search_submitted(self, ev: Input.Submitted) -> None:
        raw = ev.value.strip()
        ev.input.value = ""
        if not raw:
            return
        t = normalise_user_ticker(raw)
        if self.state and self.state.config.watch(t):
            self._select_ticker(t)
            return
        self.notify(f"Looking up {t} …", timeout=2)
        self._adhoc_lookup(raw, t)

    @work(thread=True, exclusive=True, group="adhoc")
    def _adhoc_lookup(self, raw: str, t: str) -> None:
        tried: list[str] = []
        result: Adhoc | None = None
        also: list[str] = []
        bare = "." not in t and ":" not in raw and "-" not in t
        matches: list[dict] = []
        if bare or True:
            try:
                matches = self.desk.mi.search(raw)
            except Exception:                                       # noqa: BLE001 - search must never break lookup
                matches = []
        order: list[str] = []
        exact = [m["symbol"] for m in matches if m["symbol"].upper() in (t, t + ".L")]
        for sym in ([t] if not bare else []) + exact + [t] + [m["symbol"] for m in matches[:3]] + ([t + ".L"] if bare else []):
            s = normalise_user_ticker(sym)
            if s not in order:
                order.append(s)
        for sym in order[:5]:
            tried.append(sym)
            try:
                result = self.desk.adhoc(sym)
                break
            except Exception:                                       # noqa: BLE001 - try the next candidate
                continue
        also = [m["symbol"] for m in matches[:5] if m["symbol"] != (result.ticker if result else "")]
        self.call_from_thread(self._adhoc_ready, raw, result, tried, also)

    def _adhoc_ready(self, raw: str, result: Adhoc | None, tried: list[str], also: list[str]) -> None:
        if result is None:
            self.notify(f"No data found for '{raw}' (tried {', '.join(tried) or '—'}). Try the exchange suffix, e.g. BARC.L", severity="warning", timeout=8)
            return
        self._adhoc[result.ticker] = result
        msg = f"Showing {result.ticker} — {result.snap.name}"
        if result.ticker.upper() != raw.strip().upper():
            msg += f" (searched '{raw}')"
        if also:
            msg += f" · also: {', '.join(also[:4])}"
        self.notify(msg, timeout=6)
        self._select_ticker(result.ticker)

    def action_search(self) -> None:
        self.action_goto("sheet")
        self.set_timer(0.1, lambda: self.query_one("#ts-search").focus())

    @on(Button.Pressed, "#ts-save")
    def _save_pressed(self, _ev: Button.Pressed) -> None:
        a = self._adhoc.get(self._ts_ticker or "")
        if a is None:
            return
        try:
            append_watch_item(self.desk.config_path, a.suggestion)
        except (ConfigError, OSError) as exc:
            self.notify(f"Could not save: {exc}", severity="error", timeout=8)
            return
        self.notify(f"★ {a.ticker} saved to config.yaml (watch-only, 0% target). Set a target_weight_pct to include it in the buy plan.", timeout=8)
        self.query_one("#ts-save").display = False
        self.action_refresh()

    @on(Button.Pressed, "#ts-controls .pill")
    def _tf_pressed(self, ev: Button.Pressed) -> None:
        self._set_range((ev.button.id or "tf-1Y").removeprefix("tf-"))

    def _set_range(self, key: str) -> None:
        if key not in RANGES:
            return
        self._ts_range = key
        for k in RANGES:
            self.query_one(f"#tf-{k}", Button).set_class(k == key, "on")
        if self._sheet_cache and self._sheet_cache[0] == self._ts_ticker:
            self._render_tearsheet(*self._sheet_cache)
        elif self._ts_ticker:
            self._load_tearsheet(self._ts_ticker)

    def action_timeframe(self) -> None:
        keys = list(RANGES)
        self._set_range(keys[(keys.index(self._ts_range) + 1) % len(keys)])

    # ---------------------------------------------------------------- pane 3: render
    @work(thread=True, exclusive=True, group="sheet")
    def _load_tearsheet(self, ticker: str) -> None:
        hist = self.desk.mi.history(ticker)
        news = self.desk.mi.news(ticker)
        self.call_from_thread(self._render_tearsheet, ticker, hist, news)

    def _sheet_data(self, ticker: str):
        st = self.state
        a = self._adhoc.get(ticker)
        snap = (st.snaps.get(ticker) if st else None) or (a.snap if a else None)
        quote = (st.quotes.get(ticker) if st else None) or (a.quote if a else None)
        watch = st.config.watch(ticker) if st else None
        return snap, quote, watch

    def _render_tearsheet(self, ticker: str, hist, news) -> None:
        st = self.state
        if st is None or ticker != self._ts_ticker:
            return
        self._sheet_cache = (ticker, hist, news)
        lvl, rules = self.level, st.rules
        s, q, w = self._sheet_data(ticker)
        adhoc = ticker in self._adhoc and w is None
        # header (3 rows): name · price line · unit audit (Advanced)
        h = Text()
        h.append(f" {ticker} ", style=f"bold black on {CYAN}")
        h.append(f" {s.name if s else ticker}", style=f"bold {WHITE}")
        if adhoc:
            h.append("  ◇ not in your watchlist", style=f"bold {AMBER}")
        h.append("\n")
        if s and s.price_gbp is not None:
            h.append(f" £{s.price_gbp:,.2f}", style=f"bold {WHITE}")
            if q and q.currency and q.currency != "GBP" and lvl != "beginner":
                h.append(f"  ({q.currency} {s.price:,.2f} @ {q.fx_rate:.4f})" if q.fx_rate else f"  ({q.currency} {s.price:,.2f})", style=DIM)
            h.append("  "); h.append_text(pct(s.day_change_pct, 2))
            if s.rating_label != "—":
                h.append(f"   {s.rating_label}", style=f"bold {AMBER}")
                h.append(f" · target {fnum(s.target_mean)} ({fnum(s.upside_pct, '+.1f', '%')})", style=TEXT)
        h.append(f"   src {(q.provider if q and q.provider else None) or (s.source if s else '—')}", style=DIM)
        audit = unit_audit(q) if lvl == "advanced" else None
        h.append(f"\n {audit}" if audit else "", style=DIM)
        self.query_one("#ts-head", Static).update(h)
        # chart
        chart = self.query_one("#chart")
        if PlotextPlot is not None and hist is not None and len(hist.df) > 5:
            cd = prepare_chart(hist.df, self._ts_range)
            plt = chart.plt
            plt.clear_figure()
            plt.date_form("Y-m-d")
            try:
                plt.candlestick(cd.dates, {"Open": cd.open, "Close": cd.close, "High": cd.high, "Low": cd.low}, colors=[(0, 210, 106), (255, 77, 77)])
            except Exception:                                       # noqa: BLE001 - older plotext colour handling
                plt.candlestick(cd.dates, {"Open": cd.open, "Close": cd.close, "High": cd.high, "Low": cd.low})
            if lvl != "beginner":
                for name, series, colour in (("SMA50", cd.sma50, (255, 153, 0)), ("SMA200", cd.sma200, (0, 229, 255))):
                    pts = [(d, v) for d, v in zip(cd.dates, series) if v is not None]
                    if len(pts) > 1:
                        plt.plot([p[0] for p in pts], [p[1] for p in pts], color=colour, label=name, marker="braille")
            ccy = hist.currency or ""
            plt.title(f"{ticker}  ·  {self._ts_range}  ·  {cd.bar}  ·  {'index points' if hist.is_non_security else ccy + ' (native)'}"
                      + (f"  ·  {hist.provider}" if hist.provider and lvl == "advanced" else ""))
            plt.grid(True, True)
            chart.refresh()
        elif PlotextPlot is not None:
            chart.plt.clear_figure()
            chart.plt.title(f"{ticker}: no price history available")
            chart.refresh()
        # analyst, fundamentals, income
        self.query_one("#analyst", Static).update(analyst_view(lvl, s))
        sc = st.scores.get(ticker)
        pos = next((p for p in st.portfolio.positions if p.ticker == ticker), None)
        self.query_one("#fund", Static).update(fundamentals_view(
            lvl, s, w, (f"{sc.total:.0f}/100 {sc.label}" if sc and sc.total is not None else "—"), portfolio_badge(pos, w.kind if w else "stock", rules),
            valuation_badge(s, rules), momentum_badge(s, rules), analyst_badge(s, rules), income_badge(s)))
        fc = dividend_forecast(st.portfolio, st.snaps, {x.ticker: x for x in st.config.watchlist})
        self.query_one("#income", Static).update(income_view(lvl, ticker, s, w, pos.value_gbp if pos else 0.0, fc))
        # compact news feed with clickable headlines
        items = [newsmod.to_item({**d, "related": d.get("related") or [ticker]}, self.desk.tag_targets(st)) for d in ((news.value if news is not None else None) or [])]
        items.sort(key=lambda i: -(i.ts or 0))
        self._sheet_urls = [i.url for i in items[:5]]
        self.query_one("#news", Static).update(news_compact(items))
        self.query_one("#ts-save").display = adhoc
        self._render_sim()

    @on(Input.Submitted, "#sim-amt")
    @on(Select.Changed, "#sim-src")
    def _sim_inputs(self, _ev: Any) -> None:
        self._render_sim()

    def _render_sim(self) -> None:
        st, tk = self.state, self._ts_ticker
        if st is None or tk is None or self.level != "advanced":
            return
        try:
            amt = float(self.query_one("#sim-amt", Input).value or 0)
        except ValueError:
            self.query_one("#sim-out", Static).update(Text("enter a number", style=DIM))
            return
        src = self.query_one("#sim-src", Select).value
        r = self.desk.simulate(st, tk, amt, src if isinstance(src, str) else "new", adhoc=self._adhoc.get(tk))
        b, a = r.before, r.after
        row = lambda lbl, x, y: (lbl, x, "→", y)
        g = Table.grid(padding=(0, 1))
        g.add_column(style=DIM); g.add_column(justify="right"); g.add_column(style=AMBER); g.add_column(justify="right", style=f"bold {WHITE}")
        g.add_row(*row(f"{tk} weight", f"{b.ticker_weight * 100:.1f}%", f"{a.ticker_weight * 100:.1f}%"))
        g.add_row(*row("Portfolio yield", fnum(b.yield_pct, ".2f", "%"), fnum(a.yield_pct, ".2f", "%")))
        g.add_row(*row("Blended OCF", fnum(b.blended_ter_pct, ".3f", "%"), fnum(a.blended_ter_pct, ".3f", "%")))
        g.add_row(*row("Top sector", f"{b.top_sector[0][:14]} {b.top_sector[1] * 100:.1f}%" if b.top_sector else "—",
                       f"{a.top_sector[0][:14]} {a.top_sector[1] * 100:.1f}%" if a.top_sector else "—"))
        g.add_row(*row("ISA total", money(b.total_gbp), money(a.total_gbp)))
        if r.allowance_after_gbp is not None:
            g.add_row("Allowance left", "", "→", money(r.allowance_after_gbp))
        extra = Text()
        if r.units:
            extra.append(f"≈ {r.units:,.3f} units\n", style=DIM)
        for br in r.breaches:
            extra.append(f"⚠ {br}\n", style=f"bold {DOWN}")
        if not r.breaches:
            extra.append("✓ no handbook cap breached\n", style=UP)
        self.query_one("#sim-out", Static).update(Group(g, extra))

    # ---------------------------------------------------------------- pane 4
    def _render_handbook(self, st: DeskState) -> None:
        h = hash(st.handbook_text)
        if h != self._hb_hash:
            self._hb_hash = h
            self.query_one("#hb-md", Markdown).update(st.handbook_text)

    def _cp_say(self, query: str) -> None:
        log = self.query_one("#chat", RichLog)
        log.write(Text(f"\n› {query}", style=f"bold {CYAN}"))
        try:
            out = self.copilot.respond(query, self.state)
            if self.level != "advanced":                          # same facts, level-appropriate wording
                out = "\n".join(plain_text(line, self.level) for line in out.splitlines())
            log.write(RichMarkdown(out))
        except Exception as exc:                                    # noqa: BLE001
            log.write(Text(f"co-pilot error: {type(exc).__name__}: {exc}", style=DOWN))

    @on(Input.Submitted, "#cp-input")
    def _cp_submitted(self, ev: Input.Submitted) -> None:
        if ev.value.strip():
            self._cp_say(ev.value.strip())
            ev.input.value = ""

    @on(Button.Pressed, "#cp-buttons Button")
    def _cp_button(self, ev: Button.Pressed) -> None:
        if ev.button.name:
            self._cp_say(ev.button.name)

    # ---------------------------------------------------------------- pane 5: news & catalysts
    @on(TabbedContent.TabActivated)
    def _tab_activated(self, ev: TabbedContent.TabActivated) -> None:
        if ev.pane.id == "newsdesk":
            self._ensure_news()

    def _ensure_news(self, force: bool = False) -> None:
        import time as _t
        if self.state is None or self._news_loading:
            return
        if not force and self._news_items and _t.time() - self._news_loaded_at < 300:
            return
        self._news_loading = True
        self.query_one("#news-meta", Static).update(Text("loading news …", style=DIM))
        self._load_news()

    @work(thread=True, exclusive=True, group="news")
    def _load_news(self) -> None:
        st = self.state
        try:
            tickers = self.desk.news_tickers(st)
            held = [p.ticker for p in st.portfolio.positions if p.value_gbp > 0]
            first = self.desk.news_feed(st, held, macro=True)                      # phase 1: holdings + macro: show something fast
            self.call_from_thread(self._news_loaded, first, False)
            if len(tickers) > len(held):
                full = self.desk.news_feed(st, tickers, macro=True)                # phase 2: the rest of the universe (cached per ticker)
                self.call_from_thread(self._news_loaded, full, True)
                return
        except Exception as exc:                                    # noqa: BLE001
            self.call_from_thread(self._news_failed, exc)
            return
        self.call_from_thread(self._news_loaded, first, True)

    def _news_loaded(self, feed, done: bool) -> None:
        import time as _t
        self._news_items, self._news_source = list(feed.items), feed.source
        self._news_loaded_at = _t.time()
        if done:
            self._news_loading = False
        self._render_news()

    def _news_failed(self, exc: Exception) -> None:
        self._news_loading = False
        self.query_one("#news-meta", Static).update(Text(f"news unavailable: {type(exc).__name__}: {exc}"[:120], style=f"bold {DOWN}"))

    def _render_news(self) -> None:
        st = self.state
        if st is None:
            return
        for k in newsmod.FILTER_ORDER:
            self.query_one(f"#nf-{k}", Button).set_class(k == self._news_mode, "on")
        universe = self.desk.news_tickers(st)
        self._news_view = newsmod.filter_items(self._news_items, self._news_mode, selected=self._ts_ticker, universe=universe)
        tbl = self.query_one("#news-table", DataTable)
        prev = None
        try:
            prev = tbl.coordinate_to_cell_key(tbl.cursor_coordinate).row_key.value
        except Exception:                                           # noqa: BLE001
            pass
        tbl.clear()
        import time as _t
        now = _t.time()
        held = {p.ticker for p in st.portfolio.positions if p.value_gbp > 0}
        for i, it in enumerate(self._news_view):
            badge = Text()
            for tk in it.tickers[:2]:
                badge.append(f" {tk} ", style=f"bold black on {CYAN if tk in held else '#3A4350'}")
                badge.append(" ")
            if it.uk:
                badge.append(" UK ", style=f"bold black on {AMBER}")
            tbl.add_row(Text(newsmod.rel_time(it.ts, now), style=DIM), Text(it.source[:15], style=CYAN), badge or Text("—", style=DIM),
                        Text(it.title[:140], style=TEXT), key=f"n{i}")
        if self._news_view:
            tbl.move_cursor(row=int(prev[1:]) if prev and prev[1:].isdigit() and int(prev[1:]) < len(self._news_view) else 0)
        scope = {"all": "all portfolio & watchlist", "ticker": f"{self._ts_ticker or 'no ticker'}", "macro": "market & UK macro"}[self._news_mode]
        meta = Text(f"{len(self._news_view)} stories · {scope} · source {self._news_source}"
                    + (" · loading more …" if self._news_loading else ""), style=DIM)
        self.query_one("#news-meta", Static).update(meta)
        self._show_news(self._news_view[0] if self._news_view else None)

    def _highlighted_news(self) -> newsmod.NewsItem | None:
        tbl = self.query_one("#news-table", DataTable)
        try:
            k = tbl.coordinate_to_cell_key(tbl.cursor_coordinate).row_key.value
            return self._news_view[int(k[1:])]
        except Exception:                                           # noqa: BLE001
            return None

    def _show_news(self, it: newsmod.NewsItem | None) -> None:
        import time as _t
        self.query_one("#news-detail", Static).update(news_detail(it, _t.time()))

    @on(DataTable.RowHighlighted, "#news-table")
    def _news_highlighted(self, ev: DataTable.RowHighlighted) -> None:
        if ev.row_key is not None and ev.row_key.value and ev.row_key.value[1:].isdigit():
            i = int(ev.row_key.value[1:])
            if i < len(self._news_view):
                self._show_news(self._news_view[i])

    @on(DataTable.RowSelected, "#news-table")
    def _news_selected(self, _ev: DataTable.RowSelected) -> None:
        self.action_open_link("news", 0)

    @on(Button.Pressed, "#news-controls .pill")
    def _news_filter_pressed(self, ev: Button.Pressed) -> None:
        self._news_mode = (ev.button.id or "nf-all").removeprefix("nf-")
        self._render_news()

    @on(Button.Pressed, "#news-open")
    def _news_open_pressed(self, _ev: Button.Pressed) -> None:
        self.action_open_link("news", 0)

    def action_open_link(self, where: str, idx: int = 0) -> None:
        """Open an article in the user's browser. ``where``: 'news' (highlighted row) or 'sheet' (compact feed item ``idx``)."""
        url = None
        if where == "news":
            it = self._highlighted_news()
            url = it.url if it else None
        elif 0 <= idx < len(self._sheet_urls):
            url = self._sheet_urls[idx]
        ok = safe_url(url or "")
        if not ok:
            self.notify("No article link available", severity="warning", timeout=3)
            return
        self.open_url(ok)
        self.notify(f"Opening {urlparse(ok).netloc} in your browser …", timeout=3)

    # ---------------------------------------------------------------- navigation
    def action_goto(self, pane: str) -> None:
        self.query_one(TabbedContent).active = pane
        # land on a non-text widget so the global keys ([ ] / 1-5) keep working; `/` `c` `m` enter the text boxes
        focus = {"desk": "#signals", "watch": "#watchlist", "sheet": "#ts-ticker", "hb": "#hb-wrap", "newsdesk": "#news-table"}[pane]
        self.set_timer(0.05, lambda: self.query_one(focus).focus())

    def action_step(self, d: int) -> None:
        cur = self.query_one(TabbedContent).active
        self.action_goto(PANES[(PANES.index(cur) + d) % len(PANES)])

    def action_focus_cash(self) -> None:
        self.action_goto("desk")
        self.set_timer(0.1, lambda: self.query_one("#cash").focus())

    def action_focus_cp(self) -> None:
        self.action_goto("hb")
        self.set_timer(0.1, lambda: self.query_one("#cp-input").focus())

    def action_blur(self) -> None:
        """Esc leaves a text box so the navigation keys work again."""
        self.set_focus(None)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())


# ------------------------------------------------------------------------------------------------ CLI
def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=f"UK ISA Command Centre v{__version__} — Bloomberg-style terminal. " + DISCLAIMER)
    ap.add_argument("--demo", action="store_true", help="synthetic demo data, no network")
    ap.add_argument("--live", action="store_true", help="never fall back to demo data")
    ap.add_argument("--config", default=None, help="path to config.yaml")
    ap.add_argument("--no-auto", action="store_true", help="start with auto-refresh off")
    ap.add_argument("--snapshot", action="store_true", help="print the weekly memo and exit (no UI)")
    ap.add_argument("--level", choices=EXPERIENCE_LEVELS, help="override ui.experience_level for this run")
    a = ap.parse_args(argv)
    try:
        desk = Desk(a.config, mode="demo" if a.demo else "live" if a.live else "auto")
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if a.snapshot:
        try:
            print(weekly_memo(desk.refresh(), desk))
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0
    app = ISATerminal(desk, auto_refresh=not a.no_auto)
    if a.level:
        app.level = a.level
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
