"""Headless Textual tests (demo data, no network). Uses asyncio.run so no pytest plugin is required."""
import asyncio
import shutil
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("textual")
import terminal  # noqa: E402
from engine import DISCLAIMER  # noqa: E402
from engine.desk import Desk  # noqa: E402
from engine.isa_engine import LONDON  # noqa: E402
from textual.widgets import DataTable, Input, RichLog, Select, TabbedContent  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SIZE = (200, 56)


@pytest.fixture
def project(tmp_path):
    for f in ("config.yaml", "handbook.md"):
        shutil.copy(ROOT / f, tmp_path / f)
    shutil.copytree(ROOT / "data", tmp_path / "data", ignore=shutil.ignore_patterns("samples"))
    return tmp_path


def run(project, scenario, mode="demo", prep=None):
    desk = Desk(project / "config.yaml", mode=mode, now_fn=lambda: datetime(2026, 11, 18, 15, 36, tzinfo=LONDON))
    if prep:
        prep(desk)

    async def go():
        app = terminal.ISATerminal(desk, auto_refresh=False)
        async with app.run_test(size=SIZE) as pilot:
            for _ in range(100):
                await pilot.pause(0.1)
                if app.state is not None or getattr(app.query_one(terminal.StatusBar), "error", ""):
                    break
            await pilot.pause(0.3)
            return await scenario(app, pilot)

    return asyncio.run(go())


def plain(widget) -> str:
    """Visible text of a widget, whatever Rich renderable (Text, Table, Group …) it holds."""
    from rich.console import Console
    if hasattr(widget, "plain") and not hasattr(widget, "content"):            # a bare Rich Text (e.g. a table cell)
        return widget.plain
    content = getattr(widget, "content", None)
    if content is None:
        content = widget.render() if hasattr(widget, "render") else widget
    if hasattr(content, "plain"):
        return content.plain
    con = Console(width=160, record=True, force_terminal=False, color_system=None, file=open("/dev/null", "w"))
    con.print(content)
    return con.export_text()


def test_mounts_loads_state_and_populates_every_pane(project):
    async def sc(app, pilot):
        assert app.state is not None and app.state.data_label == "DEMO"
        assert app.query_one("#signals", DataTable).row_count == len(app.state.signals) > 5
        assert app.query_one("#watchlist", DataTable).row_count == len(app.state.config.watchlist) == 20
        assert app.query_one("#plan", DataTable).row_count >= 1
        assert "ISA LEDGER" in plain(app.query_one("#ledger"))
    run(project, sc)


def test_persistent_disclaimer_is_visible_on_every_pane(project):
    async def sc(app, pilot):
        for key in ("1", "2", "3", "4", "5"):
            await pilot.press(key)
            await pilot.pause(0.2)
            d = app.query_one("#disclaimer")
            assert plain(d).strip() == DISCLAIMER == "Quantitative modelling and personal handbook tool only. Not FCA-regulated financial advice."
            assert d.region.height >= 1 and d.content_size.height >= 1, "disclaimer has no visible content row"
            assert d.region.y + d.region.height <= app.size.height and d.region.y >= app.size.height - 3
    run(project, sc)


def test_status_bar_shows_countdown_allowance_and_data_state(project):
    async def sc(app, pilot):
        s = plain(app.query_one(terminal.StatusBar))
        assert "2026/27" in s and "to 5 Apr 2027 24:00" in s and "ALLOWANCE LEFT £7,500 of £20,000" in s
        assert "d " in s and "h " in s and "62% used" in s and "LISA £2,000 left" in s and "DEMO" in s
        assert s.count("\n") == 1, "status bar must show both rows"
        assert plain(app.query_one("#status")).count("DATA") >= 1
    run(project, sc)


def test_ticker_tape_content_and_demo_banner(project):
    async def sc(app, pilot):
        tape = app.query_one(terminal.TickerTape)
        full = tape._full.plain
        for needle in ("FTSE 100", "S&P 500", "GBP/USD", "UK 10Y GILT", "DEMO DATA", "VWRP.L", "£"):
            assert needle in full
        before = plain(tape)
        for _ in range(5):
            tape.tick()
        assert plain(tape) != before                       # it scrolls
    run(project, sc)


def test_keyboard_navigation_between_panes(project):
    async def sc(app, pilot):
        tabs = app.query_one(TabbedContent)
        assert tabs.active == "desk"
        for key, pane in (("2", "watch"), ("3", "sheet"), ("4", "hb"), ("5", "newsdesk"), ("1", "desk"), ("f3", "sheet"), ("f5", "newsdesk"), ("f1", "desk")):
            await pilot.press(key)
            await pilot.pause(0.1)
            assert tabs.active == pane, (key, tabs.active)
        await pilot.press("right_square_bracket"); await pilot.pause(0.1)
        assert tabs.active == "watch"
        await pilot.press("left_square_bracket", "left_square_bracket"); await pilot.pause(0.1)
        assert tabs.active == "newsdesk"                   # wraps around: desk <- newsdesk (pane 5)
        await pilot.press("slash"); await pilot.pause(0.1)
        assert tabs.active == "sheet" and app.focused.id == "ts-search"          # `/` lands in the search bar
    run(project, sc)


def test_escape_leaves_a_text_box_and_c_enters_the_copilot(project):
    async def sc(app, pilot):
        await pilot.press("c"); await pilot.pause(0.4)
        assert app.query_one(TabbedContent).active == "hb" and app.focused.id == "cp-input"
        await pilot.press("escape"); await pilot.pause(0.1)
        assert app.focused is None or app.focused.id != "cp-input"
        await pilot.press("2"); await pilot.pause(0.1)
        assert app.query_one(TabbedContent).active == "watch"
        await pilot.press("m"); await pilot.pause(0.4)
        assert app.focused.id == "cash"
    run(project, sc)


def test_typing_digits_in_an_input_does_not_switch_panes(project):
    async def sc(app, pilot):
        await pilot.press("4"); await pilot.pause(0.2)
        app.query_one("#cp-input").focus()
        await pilot.press("1", "2", "3", "q"); await pilot.pause(0.1)
        assert app.query_one(TabbedContent).active == "hb"
        assert app.query_one("#cp-input", Input).value == "123q" and app.is_running
    run(project, sc)


def test_signal_selection_updates_the_rationale_panel(project):
    async def sc(app, pilot):
        tbl = app.query_one("#signals", DataTable)
        tbl.focus()
        ids = [s.id for s in app.state.signals]
        trim = ids.index("TRIM:NVDA")
        for _ in range(trim):
            await pilot.press("down")
        await pilot.pause(0.2)
        body = plain(app.query_one("#detail-body"))
        assert "TRIM WARNING" in body and "R-CAP-01" in body and "WHY (handbook-based)" in body and "Dilution route" in body
    run(project, sc)


def test_buy_plan_recalculates_from_the_cash_box(project):
    async def sc(app, pilot):
        app.action_focus_cash(); await pilot.pause(0.3)
        box = app.query_one("#cash", Input)
        box.value = "250"
        await pilot.press("enter"); await pilot.pause(0.3)
        assert "deployed £250.00" in plain(app.query_one("#plan-note"))
        box.value = "9000"
        await pilot.press("enter"); await pilot.pause(0.3)
        assert "clamped" in plain(app.query_one("#plan-note"))            # exceeds the £7,500 allowance headroom
        box.value = "abc"
    run(project, sc)


def test_watchlist_columns_sort_and_enter_opens_tearsheet(project):
    async def sc(app, pilot):
        await pilot.press("2"); await pilot.pause(0.2)
        wl = app.query_one("#watchlist", DataTable)
        assert [str(c.label) for c in wl.columns.values()][:3] == ["TICKER", "NAME", "PRICE £"]
        first = wl.coordinate_to_cell_key(wl.cursor_coordinate).row_key.value
        await pilot.press("o"); await pilot.press("o"); await pilot.press("o"); await pilot.pause(0.2)
        assert "sorted by DAY % ▲" in plain(app.query_one("#wl-note"))
        wl.focus()
        wl.move_cursor(row=wl.get_row_index("MSFT"))
        await pilot.press("enter"); await pilot.pause(1.5)
        assert app.query_one(TabbedContent).active == "sheet" and app._ts_ticker == "MSFT"
        head = plain(app.query_one("#ts-head"))
        assert "MSFT" in head and "Microsoft" in head and "USD" in head           # shows native USD alongside GBP
        assert "ANALYST TARGETS" in plain(app.query_one("#analyst")) and "Strong Buy" in plain(app.query_one("#analyst"))
        assert "FWD P/E" in plain(app.query_one("#fund")) and "NEWS" in plain(app.query_one("#news"))
        assert "ISA total" in plain(app.query_one("#sim-out"))
    run(project, sc)


def test_tearsheet_ticker_and_range_selectors_and_simulator(project):
    async def sc(app, pilot):
        await pilot.press("3"); await pilot.pause(1.0)
        sel = app.query_one("#ts-ticker", Select)
        sel.value = "NVDA"; await pilot.pause(1.2)
        assert "NVDA" in plain(app.query_one("#ts-head"))
        await pilot.click("#tf-1M"); await pilot.pause(1.0)
        assert app._ts_range == "1M"
        amt = app.query_one("#sim-amt", Input)
        amt.value = "5000"; amt.focus(); await pilot.press("enter"); await pilot.pause(0.3)
        out = plain(app.query_one("#sim-out"))
        assert "R-CAP-01" in out and "NVDA weight" in out
    run(project, sc)


def test_copilot_buttons_and_input(project):
    async def sc(app, pilot):
        await pilot.press("4"); await pilot.pause(0.3)
        log = app.query_one("#chat", RichLog)
        n0 = len(log.lines)
        await pilot.click("#cp-health"); await pilot.pause(0.4)
        assert len(log.lines) > n0 + 3
        box = app.query_one("#cp-input", Input)
        box.focus(); box.value = "explain NVDA"; await pilot.press("enter"); await pilot.pause(0.4)
        txt = "\n".join(l.text for l in log.lines)
        assert "Portfolio health" in txt and "R-CAP-01" in txt and box.value == ""
        box.value = "memo"; await pilot.press("enter"); await pilot.pause(0.5)
        assert (project / "reports").exists() and any((project / "reports").glob("IC_memo_*.md"))
    run(project, sc)


def test_handbook_pane_renders_the_markdown(project):
    async def sc(app, pilot):
        await pilot.press("4"); await pilot.pause(0.5)
        assert "handbook-rules" in app.state.handbook_text and app._hb_hash == hash(app.state.handbook_text)
    run(project, sc)


def test_help_modal_opens_and_closes(project):
    async def sc(app, pilot):
        await pilot.press("question_mark"); await pilot.pause(0.3)
        assert app.screen.__class__.__name__ == "HelpScreen"
        await pilot.press("escape"); await pilot.pause(0.3)
        assert app.screen.__class__.__name__ != "HelpScreen"
    run(project, sc)


def test_toggle_auto_and_manual_refresh(project):
    async def sc(app, pilot):
        assert app.auto is False
        await pilot.press("a"); assert app.auto is True
        await pilot.press("a"); assert app.auto is False
        t0 = app.query_one(terminal.StatusBar).last_ok
        await pilot.press("r"); await pilot.pause(1.0)
        assert app.query_one(terminal.StatusBar).last_ok >= t0 and not app._refreshing
    run(project, sc)


def test_refresh_failure_is_shown_not_fatal(project):
    def prep(desk):
        orig = desk.refresh
        calls = {"n": 0}
        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("boom")
            return orig(*a, **k)
        desk.refresh = flaky

    async def sc(app, pilot):
        assert app.state is not None
        await pilot.press("r"); await pilot.pause(0.8)
        assert "refresh failed: RuntimeError: boom" in plain(app.query_one(terminal.StatusBar))
        assert app.is_running and app.state is not None and app.query_one("#signals", DataTable).row_count > 0
    run(project, sc, prep=prep)


def test_bad_csv_on_startup_is_reported_in_ui(project):
    (project / "data" / "holdings.csv").write_text("account,ticker\nHL_ISA,VWRP.L\n")

    async def sc(app, pilot):
        assert app.state is None
        assert "need columns" in plain(app.query_one(terminal.StatusBar))
    run(project, sc)


def test_editing_handbook_and_refreshing_updates_signals_in_the_ui(project):
    async def sc(app, pilot):
        assert any(s.id == "TRIM:NVDA" for s in app.state.signals)
        hb = project / "handbook.md"
        hb.write_text(hb.read_text().replace("max_single_position_pct: 15 ", "max_single_position_pct: 30 "))
        await pilot.press("r"); await pilot.pause(1.0)
        assert not any(s.id == "TRIM:NVDA" for s in app.state.signals)
        assert "TRIM:NVDA" not in [k.value for k in app.query_one("#signals", DataTable).rows]
    run(project, sc)


# ------------------------------------------------------------------------------------------ pure helpers
def _df(n=1400):
    idx = pd.bdate_range(end="2026-11-18", periods=n)
    px = np.linspace(50, 150, n)
    return pd.DataFrame({"Open": px, "High": px * 1.01, "Low": px * 0.99, "Close": px, "Volume": 1.0, "Dividends": 0.0}, index=idx)


@pytest.mark.parametrize("rng,bar,maxn", [("1M", "daily bars", 21), ("6M", "weekly bars", 30), ("1Y", "weekly bars", 54),
                                         ("5Y", "monthly bars", 62)])
def test_prepare_chart_bar_counts_and_labels(rng, bar, maxn):
    cd = terminal.prepare_chart(_df(), rng)
    assert cd.bar == bar and (5 < len(cd.dates) <= maxn)
    assert list(terminal.RANGES) == ["1M", "6M", "1Y", "5Y"]
    assert len(cd.dates) == len(cd.open) == len(cd.high) == len(cd.low) == len(cd.close) == len(cd.sma50) == len(cd.sma200)
    assert all(h >= l for h, l in zip(cd.high, cd.low)) and cd.dates == sorted(cd.dates)


def test_prepare_chart_smas_use_full_history_not_just_the_window():
    cd = terminal.prepare_chart(_df(), "1M")
    assert cd.sma200[0] is not None and cd.sma50[0] is not None       # warm because computed before windowing
    short = terminal.prepare_chart(_df(120), "1M")
    assert short.sma200[-1] is None and short.sma50[-1] is not None   # genuinely unavailable stays None


def test_target_range_bar_and_rating_bars():
    t = terminal.target_range_bar(110, 150, 165, 136.28)
    assert len(t.plain) == 56 and all(c in t.plain for c in "┣┫◆▼")
    assert t.plain.index("▼") < t.plain.index("◆") < t.plain.index("┫") and t.plain.index("┣") < t.plain.index("▼")
    assert terminal.target_range_bar(None, 1, 2, 1).plain.startswith("no analyst")
    below = terminal.target_range_bar(110, 150, 165, 100.0)               # price under the low target
    assert below.plain.index("▼") < below.plain.index("┣")
    bars = terminal.rating_bars({"strongBuy": 12, "buy": 41, "hold": 5, "sell": 0, "strongSell": 0}).plain
    assert "Strong Buy" in bars and "41" in bars and bars.count("█") > 20
    assert "no analyst" in terminal.rating_bars(None).plain


def test_cli_snapshot_prints_the_memo(project, capsys):
    rc = terminal.main(["--demo", "--snapshot", "--config", str(project / "config.yaml")])
    out = capsys.readouterr().out
    assert rc == 0 and "# Weekly Investment Committee Memo" in out and "Not FCA-regulated financial advice" in out


def test_cli_reports_config_errors(tmp_path, capsys):
    (tmp_path / "c.yaml").write_text("cash_target_pct: 5\nwatchlist:\n  - {ticker: A, target_weight_pct: 50}\n")
    assert terminal.main(["--demo", "--snapshot", "--config", str(tmp_path / "c.yaml")]) == 2
    assert "sum to 100" in capsys.readouterr().err
