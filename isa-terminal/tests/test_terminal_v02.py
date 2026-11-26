"""v0.2 UI: experience levels, sleeve pills, News & Catalysts, on-demand search + save, timeframes, income, audit line."""
import re
import time

import pytest

pytest.importorskip("textual")
import terminal  # noqa: E402
from engine import news as N  # noqa: E402
from engine.config import load_config  # noqa: E402
from engine.desk import Desk  # noqa: E402
from engine.market_intel import DataStatus  # noqa: E402
from textual.widgets import Button, DataTable, Input, RichLog, Select, TabbedContent  # noqa: E402
from test_terminal import plain, project, run  # noqa: E402,F401

JARGON = re.compile(r"\bRSI\b|\bPEG\b|\bSMA\b|R-[A-Z]{3}-\d{2}|\bOCF\b|\bDCA\b|look-through|\bpp\b", re.I)


def table_text(tbl: DataTable) -> str:
    return "\n".join(" | ".join(plain(c) for c in tbl.get_row_at(i)) for i in range(tbl.row_count))


def labels(tbl: DataTable) -> list[str]:
    return [str(c.label) for c in tbl.columns.values()]


async def to_pane(pilot, key, secs=0.6):
    await pilot.press(key)
    await pilot.pause(secs)


# =============================================================================================== levels
def test_default_is_advanced_and_matches_v01_density(project):
    async def sc(app, pilot):
        assert app.level == "advanced"
        assert labels(app.query_one("#signals", DataTable)) == ["PRI", "SEV", "ACTION", "TICKER", "SIGNAL"]
        assert "RSI14" in labels(app.query_one("#watchlist", DataTable)) and len(labels(app.query_one("#watchlist", DataTable))) == 16
        assert app.query_one("#ledger").display and app.query_one("#sim").display and app.query_one("#plan").display
        assert not app.query_one("#allow-bar").display
        app._show_signal(next(s for s in app.state.signals if s.id == "TRIM:NVDA"))
        assert "RULES  R-CAP-01" in plain(app.query_one("#detail-body"))
    run(project, sc)


def test_l_cycles_levels_in_order_and_persists_to_config(project):
    async def sc(app, pilot):
        seen = []
        for _ in range(3):
            await pilot.press("l"); await pilot.pause(0.5)
            seen.append(app.level)
            assert load_config(project / "config.yaml").experience_level == app.level
            assert f"◐ {app.level.title()}" in plain(app.query_one(terminal.StatusBar))
        assert seen == ["beginner", "intermediate", "advanced"]
    run(project, sc)


def test_level_survives_a_restart(project):
    async def sc(app, pilot):
        await pilot.press("l"); await pilot.press("l"); await pilot.pause(0.5)
        assert app.level == "intermediate"
    run(project, sc)
    again = terminal.ISATerminal(Desk(project / "config.yaml", mode="demo"))
    assert again.level == "intermediate"


def test_clicking_the_status_bar_badge_cycles_the_level(project):
    async def sc(app, pilot):
        bar = app.query_one(terminal.StatusBar)
        badge = bar.level_badge()
        assert badge.spans or badge.style                                   # carries the @click meta
        x = plain(bar).splitlines()[1].index("◐")
        await pilot.click(terminal.StatusBar, offset=(x + 3, 1)); await pilot.pause(0.5)
        assert app.level == "beginner"
    run(project, sc)


def test_beginner_desk_is_plain_english_with_a_visual_allowance_bar(project):
    async def sc(app, pilot):
        await pilot.press("l"); await pilot.pause(0.6)
        assert app.level == "beginner"
        sig = app.query_one("#signals", DataTable)
        assert labels(sig) == ["HOW IMPORTANT", "WHAT TO DO", "HOLDING", "WHAT'S HAPPENING"]
        text = table_text(sig)
        assert not JARGON.search(text), JARGON.search(text)
        assert "IMPORTANT" in text and "Too big" in text and "Consider adding" in text and "Tax deadline" in text
        assert not app.query_one("#ledger").display and not app.query_one("#sim").display and not app.query_one("#plan").display
        bar = plain(app.query_one("#allow-bar"))
        assert app.query_one("#allow-bar").display and "YOUR ISA ALLOWANCE" in bar and "£12,500 of £20,000 used (62%)" in bar
        assert "£7,500 still available" in bar and "days until the 5 April deadline" in bar
        strip = plain(app.query_one("#strip"))
        assert "of your £20,000 ISA allowance is still available" in strip and "OCF" not in strip and "EFF.N" not in strip
        note = plain(app.query_one("#plan-note"))
        assert "This month's idea for your £1,000" in note and "No selling needed" in note
    run(project, sc)


def test_beginner_detail_shows_what_this_means_and_no_jargon_for_every_signal(project):
    async def sc(app, pilot):
        await pilot.press("l"); await pilot.pause(0.6)
        for g in app.state.signals:
            app._show_signal(g)
            body = plain(app.query_one("#detail-body"))
            assert "WHAT THIS MEANS" in body and not JARGON.search(body), (g.id, JARGON.search(body))
        app._show_signal(next(s for s in app.state.signals if s.id == "TRIM:NVDA"))
        body = plain(app.query_one("#detail-body"))
        assert "Too big" in body and "NVIDIA" in body and "without selling" in body and "RULES" not in body
    run(project, sc)


def test_intermediate_shows_rule_summaries_not_ids_and_the_buy_planner(project):
    async def sc(app, pilot):
        await pilot.press("l"); await pilot.press("l"); await pilot.pause(0.6)
        assert app.level == "intermediate"
        sig = app.query_one("#signals", DataTable)
        assert labels(sig) == ["SEV", "ACTION", "TICKER", "SIGNAL"]
        assert not re.search(r"R-[A-Z]{3}-\d{2}", table_text(sig))
        app._show_signal(next(s for s in app.state.signals if s.id == "TRIM:NVDA"))
        body = plain(app.query_one("#detail-body"))
        assert "RULE  No single stock or trust above 15% of the ISA." in body and "R-CAP-01" not in body and "WHAT THIS MEANS" not in body
        assert app.query_one("#plan").display and not app.query_one("#sim").display and not app.query_one("#ledger").display
        assert app.query_one("#plan").row_count >= 1
    run(project, sc)


def test_watchlist_columns_per_level(project):
    async def sc(app, pilot):
        await to_pane(pilot, "2")
        wl = app.query_one("#watchlist", DataTable)
        assert labels(wl)[:4] == ["TICKER", "NAME", "PRICE £", "DAY %"] and "RSI14" in labels(wl) and "SCORE" in labels(wl)
        await pilot.press("l"); await pilot.pause(0.6)                                           # beginner
        assert labels(wl) == ["TICKER", "NAME", "PRICE £", "DAY %", "ANALYSTS SAY", "VALUATION", "MOMENTUM", "INCOME", "YOUR ISA", "TIP"]
        txt = table_text(wl)
        assert re.search(r"Good Value|Fair|Expensive", txt) and re.search(r"Steady|Overheated|Beaten Down|Drifting", txt)
        assert "Too big" in txt and "Room to add" in txt and not JARGON.search(txt)
        await pilot.press("l"); await pilot.pause(0.6)                                           # intermediate
        assert labels(wl) == ["TICKER", "NAME", "PRICE £", "DAY %", "CONSENSUS", "UPSIDE", "FWD P/E", "YIELD", "SECTOR", "WT/TGT %", "TOP SIGNAL"]
        assert "RSI14" not in labels(wl)
    run(project, sc)


def test_tearsheet_fundamentals_follow_the_level(project):
    async def sc(app, pilot):
        await to_pane(pilot, "3", 1.0)
        app.query_one("#ts-ticker", Select).value = "VWRP.L"; await pilot.pause(1.2)
        f = plain(app.query_one("#fund"))
        assert "FUNDAMENTALS & TECHNICALS" in f and "PEG" in f and "RSI (14)" in f and "SMA 200" in f
        assert app.query_one("#sim").display
        await pilot.press("l"); await pilot.pause(1.0)                                           # beginner
        f = plain(app.query_one("#fund"))
        assert "AT A GLANCE" in f and "Value for money" in f and "Recent momentum" in f and not JARGON.search(f)
        assert not app.query_one("#sim").display
        await pilot.press("l"); await pilot.pause(1.0)                                           # intermediate
        f = plain(app.query_one("#fund"))
        assert "FWD P/E" in f and "SECTOR WEIGHTS" in f and "Technology" in f and "PEG" not in f and "RSI" not in f
    run(project, sc)


def test_beginner_copilot_answers_without_jargon_but_advanced_keeps_rule_ids(project):
    async def sc(app, pilot):
        await to_pane(pilot, "4", 0.4)
        log = app.query_one("#chat", RichLog)
        app._cp_say("explain NVDA"); await pilot.pause(0.4)
        adv = "\n".join(l.text for l in log.lines)
        assert "R-CAP-01" in adv
        log.clear()
        await pilot.press("l"); await pilot.pause(0.4)
        app._cp_say("explain NVDA"); await pilot.pause(0.4)
        beg = "\n".join(l.text for l in log.lines)
        assert beg.strip() and not JARGON.search(beg), JARGON.search(beg)
    run(project, sc)


# =============================================================================================== data sources in the status bar
def test_status_bar_lists_the_active_data_sources(project):
    async def sc(app, pilot):
        st = app.state
        st.status = DataStatus("LIVE", None, [], False, "auto", ("TD", "FH"))
        app.query_one(terminal.StatusBar).tick()
        assert "[LIVE · TD + FH]" in plain(app.query_one(terminal.StatusBar))
        st.status = DataStatus("LIVE", None, [], False, "auto", ("TD", "FH", "YF"))
        app.query_one(terminal.StatusBar).tick()
        assert "[LIVE · TD + FH + YF]" in plain(app.query_one(terminal.StatusBar))
        st.status = DataStatus("STALE", None, [], False, "auto", ("YF",))
        app.query_one(terminal.StatusBar).tick()
        assert "[STALE · YF]" in plain(app.query_one(terminal.StatusBar))
        assert "[DEMO]" not in plain(app.query_one(terminal.StatusBar))
    run(project, sc)


# =============================================================================================== sleeve pills
def test_sleeve_pills_filter_the_watchlist_and_show_counts(project):
    async def sc(app, pilot):
        await to_pane(pilot, "2")
        wl = app.query_one("#watchlist", DataTable)
        assert wl.row_count == 20
        assert [str(app.query_one(f"#pill-{k}", Button).label) for k in ("all", "core_etf", "uk_div", "trust", "global_stock")] == \
            ["All (20)", "Core ETFs (5)", "UK & Div (7)", "Trusts (3)", "Global Stocks (5)"]
        for pill, n, members in (("trust", 3, {"SMT.L", "FGT.L", "JGGI.L"}), ("core_etf", 5, {"VWRP.L", "VUAG.L", "SWDA.L", "VERG.L", "VFEG.L"}),
                                 ("uk_div", 7, {"ISF.L", "AZN.L", "SHEL.L"}), ("global_stock", 5, {"MSFT", "NVDA", "AAPL", "GOOGL", "BRK-B"})):
            await pilot.click(f"#pill-{pill}"); await pilot.pause(0.4)
            keys = {k.value for k in wl.rows}
            assert wl.row_count == n and members <= keys and app.query_one(f"#pill-{pill}", Button).has_class("on")
            assert not app.query_one("#pill-all", Button).has_class("on")
        await pilot.click("#pill-all"); await pilot.pause(0.4)
        assert wl.row_count == 20
    run(project, sc)


def test_f_cycles_the_sleeve_filter_in_pane_2(project):
    async def sc(app, pilot):
        await to_pane(pilot, "2")
        wl = app.query_one("#watchlist", DataTable)
        counts = []
        for _ in range(5):
            await pilot.press("f"); await pilot.pause(0.3)
            counts.append(wl.row_count)
        assert counts == [5, 7, 3, 5, 20]                                # core -> uk -> trusts -> global -> all
    run(project, sc)


# =============================================================================================== News & Catalysts
def story(title, ts, tickers=(), scope="company", url="https://example.com/a", summary="A short summary. Second sentence.", uk=False):
    return N.NewsItem(title, "Reuters", url, summary, ts, tuple(tickers), scope, uk)


def test_pane_5_exists_and_loads_news_on_open(project):
    async def sc(app, pilot):
        tabs = app.query_one(TabbedContent)
        from textual.widgets import Tab
        assert "5 NEWS · CATALYSTS" in [str(t.label) for t in app.query(Tab)]
        await to_pane(pilot, "5", 2.0)
        assert tabs.active == "newsdesk" and app.focused.id == "news-table"
        assert app.query_one("#news-table", DataTable).row_count > 5 and "stories" in plain(app.query_one("#news-meta"))
        assert labels(app.query_one("#news-table", DataTable)) == ["WHEN", "SOURCE", "TICKER", "HEADLINE"]
        assert [str(app.query_one(f"#nf-{k}", Button).label) for k in N.FILTER_ORDER] == ["All Portfolio", "Selected Ticker", "Macro / UK Market"]
    run(project, sc)


def test_clicking_the_pane_5_tab_works_too(project):
    async def sc(app, pilot):
        await pilot.click("Tab#--content-tab-newsdesk"); await pilot.pause(1.5)
        assert app.query_one(TabbedContent).active == "newsdesk"
    run(project, sc)


def test_news_filters(project):
    async def sc(app, pilot):
        await to_pane(pilot, "5", 2.0)
        n_all = len(app._news_view)
        await pilot.click("#nf-macro"); await pilot.pause(0.4)
        assert app._news_mode == "macro" and app._news_view and all(i.scope == "macro" for i in app._news_view)
        assert app.query_one("#nf-macro", Button).has_class("on") and not app.query_one("#nf-all", Button).has_class("on")
        app._ts_ticker = "MSFT"
        await pilot.click("#nf-ticker"); await pilot.pause(0.4)
        assert app._news_mode == "ticker" and app._news_view and all("MSFT" in i.tickers for i in app._news_view)
        assert "MSFT" in plain(app.query_one("#news-meta"))
        await pilot.click("#nf-all"); await pilot.pause(0.4)
        assert len(app._news_view) == n_all
        await pilot.press("f"); await pilot.pause(0.3)                    # `f` cycles all -> ticker
        assert app._news_mode == "ticker"
        await pilot.press("f"); await pilot.press("f"); await pilot.pause(0.3)
        assert app._news_mode == "all"
    run(project, sc)


def test_news_rows_show_time_source_ticker_badge_and_the_detail_panel(project):
    async def sc(app, pilot):
        await to_pane(pilot, "5", 1.5)
        now = time.time()
        app._news_items = [story("MSFT cloud beats", now - 3 * 3600, ["MSFT"]),
                           story("BoE holds rates", now - 600, scope="macro", uk=True, summary="Short."),
                           story("No summary here", now - 86400 * 2, ["AZN.L"], summary="")]
        app._render_news()
        tbl = app.query_one("#news-table", DataTable)
        txt = table_text(tbl)
        assert "3h ago" in txt and "2d ago" in txt and "Reuters" in txt and "MSFT" in txt and "MSFT cloud beats" in txt
        assert "BoE holds rates" not in txt                                    # 'All Portfolio' excludes macro that names no holding
        app._news_mode = "macro"; app._render_news()
        macro = table_text(tbl)
        assert "10m ago" in macro and "BoE holds rates" in macro and " UK " in macro and "MSFT cloud beats" not in macro
        app._news_mode = "all"; app._render_news()
        app._show_news(app._news_view[0])
        d = plain(app.query_one("#news-detail"))
        assert "Reuters" in d and "read the full article" in d and "https://example.com/a" in d
        app._show_news(app._news_view[-1])
        assert "No summary supplied" in plain(app.query_one("#news-detail"))
    run(project, sc)


def test_o_and_enter_open_articles_in_the_browser_but_unsafe_links_never_open(project):
    async def sc(app, pilot):
        opened = []
        app.open_url = lambda url, **k: opened.append(url)
        await to_pane(pilot, "5", 1.5)
        now = time.time()
        app._news_items = [story("Good", now, ["MSFT"], url="https://example.com/good"),
                           story("Evil", now - 100, ["MSFT"], url="javascript:alert(1)"),
                           story("File", now - 200, ["MSFT"], url="file:///etc/passwd")]
        app._render_news(); await pilot.pause(0.3)
        app.query_one("#news-table").focus()
        await pilot.press("o"); await pilot.pause(0.2)
        assert opened == ["https://example.com/good"]
        await pilot.press("down", "o"); await pilot.pause(0.2)
        await pilot.press("down", "o"); await pilot.pause(0.2)
        assert opened == ["https://example.com/good"]                 # javascript: and file: were refused
        await pilot.press("up", "up", "enter"); await pilot.pause(0.3)
        assert opened == ["https://example.com/good"] * 2             # Enter on a row also opens it
        await pilot.click("#news-open"); await pilot.pause(0.2)
        assert len(opened) == 3
    run(project, sc)


def test_tearsheet_headline_links_open_via_click_action(project):
    async def sc(app, pilot):
        opened = []
        app.open_url = lambda url, **k: opened.append(url)
        app._sheet_urls = ["https://example.com/x", "javascript:bad()"]
        app.action_open_link("sheet", 0); app.action_open_link("sheet", 1); app.action_open_link("sheet", 9)
        assert opened == ["https://example.com/x"]
    run(project, sc)


# =============================================================================================== on-demand search + save
async def search(app, pilot, text, secs=2.0):
    await to_pane(pilot, "3", 0.8)
    box = app.query_one("#ts-search", Input)
    box.focus(); box.value = text
    await pilot.press("enter"); await pilot.pause(secs)


def test_search_opens_an_unlisted_lse_ticker_and_the_save_button_adds_it_to_config(project):
    async def sc(app, pilot):
        assert not app.query_one("#ts-save").display
        await search(app, pilot, "barc.l")
        assert "BARC.L" in app._adhoc and app._ts_ticker == "BARC.L"
        head = plain(app.query_one("#ts-head"))
        assert "Barclays" in head and "not in your watchlist" in head and "£2.45" in head
        assert "unit audit" in head and "GBp (pence) ÷ 100" in head
        assert app.query_one("#ts-save").display
        assert "ANALYST" in plain(app.query_one("#analyst")) and "ANNUAL TAX-FREE" in plain(app.query_one("#income"))
        assert "ISA total" in plain(app.query_one("#sim-out"))                        # the simulator works for unlisted tickers
        before = (project / "config.yaml").read_text()
        await pilot.click("#ts-save"); await pilot.pause(1.5)
        cfg = load_config(project / "config.yaml")
        w = cfg.watch("BARC.L")
        assert w is not None and w.target_weight_pct == 0 and w.sleeve == "uk_div" and w.kind == "stock" and w.sector == "Financial Services"
        assert len(cfg.watchlist) == 21 and abs(sum(x.target_weight_pct for x in cfg.watchlist) + cfg.cash_target_pct - 100) < 1e-9
        assert not app.query_one("#ts-save").display
        assert app.query_one("#watchlist", DataTable).row_count == 21                       # refreshed into pane 2
        assert (project / "config.yaml").read_text().startswith(before[:300])               # comments/layout untouched
        assert (project / "config.yaml.bak").read_text() == before
    run(project, sc)


def test_search_accepts_a_bare_us_ticker_in_any_case_and_shows_native_usd(project):
    async def sc(app, pilot):
        await search(app, pilot, " tsla ")
        assert app._ts_ticker == "TSLA"
        head = plain(app.query_one("#ts-head"))
        assert "Tesla" in head and "USD" in head and "not in your watchlist" in head
        assert app.query_one("#ts-save").display
    run(project, sc)


def test_search_of_a_listed_ticker_just_selects_it_and_unknown_tickers_fail_softly(project):
    async def sc(app, pilot):
        await search(app, pilot, "azn.l", 1.2)
        assert app._ts_ticker == "AZN.L" and "AZN.L" not in app._adhoc and not app.query_one("#ts-save").display
        await search(app, pilot, "zzzz", 2.0)
        assert "ZZZZ" not in app._adhoc and app._ts_ticker == "AZN.L" and app.is_running             # no crash, selection unchanged
        await search(app, pilot, "   ", 0.5)
        assert app.is_running
    run(project, sc)


def test_saving_the_same_ticker_twice_is_refused_cleanly(project):
    async def sc(app, pilot):
        await search(app, pilot, "barc.l")
        await pilot.click("#ts-save"); await pilot.pause(1.5)
        a = app._adhoc["BARC.L"]
        from engine.config_edit import append_watch_item
        with pytest.raises(Exception, match="already"):
            append_watch_item(project / "config.yaml", a.suggestion)
        assert len(load_config(project / "config.yaml").watchlist) == 21
    run(project, sc)


# =============================================================================================== timeframes, income, audit
def test_timeframe_pills_and_t_key(project):
    async def sc(app, pilot):
        await to_pane(pilot, "3", 1.0)
        assert app._ts_range == "1Y" and app.query_one("#tf-1Y", Button).has_class("on")
        for k in ("1M", "6M", "5Y", "1Y"):
            await pilot.click(f"#tf-{k}"); await pilot.pause(0.6)
            assert app._ts_range == k and app.query_one(f"#tf-{k}", Button).has_class("on")
            assert sum(app.query_one(f"#tf-{x}", Button).has_class("on") for x in terminal.RANGES) == 1
        await pilot.press("t"); await pilot.pause(0.4)
        assert app._ts_range == "5Y"                                  # 1Y -> 5Y
        await pilot.press("t"); await pilot.pause(0.4)
        assert app._ts_range == "1M"                                  # wraps
    run(project, sc)


def test_dividend_income_panel_for_a_payer_and_an_accumulating_fund(project):
    async def sc(app, pilot):
        await to_pane(pilot, "3", 1.0)
        sel = app.query_one("#ts-ticker", Select)
        sel.value = "AZN.L"; await pilot.pause(1.2)
        inc = plain(app.query_one("#income"))
        assert "ANNUAL TAX-FREE ISA DIVIDEND INCOME" in inc and "/yr per £1,000" in inc and "your £" in inc and "Whole ISA:" in inc
        assert "Tax-free inside an ISA" in inc and "not a promise" in inc
        sel.value = "VWRP.L"; await pilot.pause(1.2)
        inc = plain(app.query_one("#income"))
        assert "Accumulating fund: no cash is paid out" in inc and "reinvested inside accumulating funds" in inc
        await pilot.press("l"); await pilot.pause(1.0)
        sel.value = "AZN.L"; await pilot.pause(1.2)
        inc = plain(app.query_one("#income"))
        assert "£1,000 in this has paid about" in inc and "tax-free in your ISA" in inc
    run(project, sc)


def test_unit_audit_line_is_advanced_only(project):
    async def sc(app, pilot):
        await to_pane(pilot, "3", 1.0)
        app.query_one("#ts-ticker", Select).value = "AZN.L"; await pilot.pause(1.2)
        assert "unit audit: 13,628.00 GBp (pence) ÷ 100 = £136.28" in plain(app.query_one("#ts-head"))
        app.query_one("#ts-ticker", Select).value = "MSFT"; await pilot.pause(1.2)
        assert re.search(r"unit audit: USD 501\.69 ÷ 1\.3154 GBPUSD = £381\.40", plain(app.query_one("#ts-head")))
        await pilot.press("l"); await pilot.pause(1.0)
        assert "unit audit" not in plain(app.query_one("#ts-head"))
    run(project, sc)


# =============================================================================================== pure view functions
def test_safe_url():
    assert terminal.safe_url("https://a.com/x?y=1") and terminal.safe_url("http://a.com")
    for bad in ("javascript:alert(1)", "file:///etc/passwd", "ftp://a.com", "//a.com", "", "not a url", "data:text/html,hi"):
        assert terminal.safe_url(bad) is None


def test_link_text_carries_both_a_hyperlink_and_a_click_action():
    t = terminal.link_text("read", "https://a.com/x", "app.open_link('news', 0)")
    st = t.style
    assert st.link == "https://a.com/x" and st.meta["@click"] == "app.open_link('news', 0)" and "underline" in str(st)


def test_news_compact_and_detail_views():
    now = 1_795_040_000.0
    items = [story("Alpha headline", now - 120), story("Bad link headline", now - 3600, url="javascript:x"), story("C", now - 90000)]
    out = terminal.news_compact(items, now)
    s = out.plain
    assert "2m ago" in s and "1h ago" in s and "1d ago" in s and "Alpha headline" in s and "click a headline" in s
    linked = [sp for sp in out.spans if getattr(sp.style, "link", None)]
    assert len(linked) == 2 and all(sp.style.meta["@click"].startswith("app.open_link('sheet'") for sp in linked)      # javascript: not linked
    assert "no headlines available" in terminal.news_compact([], now).plain
    d = terminal.news_detail(story("T", now - 60, ["MSFT", "NVDA"], uk=True), now).plain
    assert "1m ago" in d and " UK " in d and " MSFT " in d and " NVDA " in d and "A short summary. Second sentence." in d
    assert "select a story" in terminal.news_detail(None).plain
    assert "no link available" in terminal.news_detail(story("T", now, url="javascript:x")).plain


def test_income_and_analyst_views_by_level():
    from engine.analyst_screener import AnalystSnapshot
    from engine.config import WatchItem
    from engine.income import IncomeForecast
    s = AnalystSnapshot("X", "X", "stock", "GBP", 100.0, 100.0, 0.0, rating_label="Buy", rating_mean=1.8, n_analysts=20,
                        rec_counts={"strongBuy": 5, "buy": 12, "hold": 3, "sell": 0, "strongSell": 0}, target_mean=125.0,
                        target_low=100.0, target_high=150.0, upside_pct=25.0, upside_low_pct=0.0, upside_high_pct=50.0, dividend_yield_pct=3.0)
    beg = terminal.analyst_view("beginner", s).plain
    assert "17 of 20 analysts say buy" in beg and "£125.00 vs £100.00 today (+25%)" in beg and "mean" not in beg.lower().split("analysts")[0]
    assert "1=Strong Buy" in terminal.analyst_view("advanced", s).plain and "1=Strong Buy" not in terminal.analyst_view("intermediate", s).plain
    assert "no analyst price targets" in terminal.analyst_view("advanced", None).plain
    fc = IncomeForecast((), 450.0, 0.0, ())
    w = WatchItem("X", "X", "stock")
    adv = terminal.income_view("advanced", "X", s, w, 5000.0, fc).plain
    assert "£30/yr per £1,000" in adv and "your £5,000 holding ≈ £150/yr" in adv and "£450/yr cash dividends" in adv
    assert "£1,000 in this has paid about £30" in terminal.income_view("beginner", "X", s, w, 0.0, fc).plain
    unk = AnalystSnapshot("X", "X", "stock", "GBP", 1, 1, 0)
    assert "yield unknown" in terminal.income_view("advanced", "X", unk, w, 0.0, None).plain
