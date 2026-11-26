"""Recommendation rule triggers. Each test sets up one condition and asserts the signal, severity, rule ID and wording."""
import dataclasses
from datetime import date, datetime
from pathlib import Path

import pytest

from engine.analyst_screener import AnalystSnapshot
from engine.config import HandbookRules, load_config, parse_handbook_rules
from engine.isa_engine import (LONDON, AccountSpec, GIAPosition, LedgerEntry, D, allowance_pace, compute_isa_status,
                               countdown_to_deadline, parse_ledger_csv, plan_bed_and_isa)
from engine.market_intel import Quote
from engine.portfolio import HoldingRow, build_portfolio, load_holdings_csv
from engine.recommender import (SignalContext, blocked_tickers, generate_signals, score_security, screen_failures)

ROOT = Path(__file__).resolve().parent.parent
PX = {"VWRP.L": 140.0, "VUAG.L": 100.0, "SMT.L": 15.0, "AZN.L": 140.0, "MSFT": 400.0, "NVDA": 120.0, "ISF.L": 10.0, "ULVR.L": 48.0}
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=LONDON)
CFG = load_config(ROOT / "config.yaml")
WATCH = {w.ticker: w for w in CFG.watchlist}
ACCS = {a.id: AccountSpec(a.id, a.kind, a.flexible, a.broker) for a in CFG.accounts.values()}


def Q(t, px):
    return Quote(t, px, px, 0.0, px, px, "GBP", "GBP", None, None, "live")


def snap(t, **kw):
    w = WATCH.get(t)
    base = dict(ticker=t, name=t, kind=w.kind if w else "stock", currency="GBP", price=PX.get(t, 100.0),
                price_gbp=PX.get(t, 100.0), day_change_pct=0.0, sector=w.sector if w else None, ter_pct=w.ter_pct if w else None)
    base.update(kw)
    return AnalystSnapshot(**base)


def ctx(*, now=NOW, holdings=None, snaps=None, rules=None, ledger=None, monthly=1000.0, data="LIVE", prices=None,
        bed=False, cfg=CFG):
    rules = rules or HandbookRules()
    prices = prices or PX
    rows = holdings if holdings is not None else load_holdings_csv(cfg.holdings_csv, cfg.accounts)
    snaps = snaps if snaps is not None else {}
    pf = build_portfolio(rows, cfg.accounts, {t: Q(t, p) for t, p in prices.items()}, WATCH, snaps)
    led = ledger if ledger is not None else parse_ledger_csv(cfg.contributions_csv)
    isa = compute_isa_status(led, ACCS, as_of=now)
    cd = countdown_to_deadline(now, isa.tax_year)
    pace = allowance_pace(isa, now, rules.tax.practical_cutoff_days)
    bed_plan = plan_bed_and_isa(pf.gia, 2580, float(isa.headroom)) if (bed and pf.gia) else None
    return SignalContext(now, isa, cd, pace, pf, snaps, rules, cfg, monthly, bed_plan, data)


def by(sigs, action=None, ticker=None, id_prefix=None):
    return [s for s in sigs if (action is None or s.action == action) and (ticker is None or s.ticker == ticker)
            and (id_prefix is None or s.id.startswith(id_prefix))]


def one(sigs, **kw):
    r = by(sigs, **kw)
    assert len(r) == 1, f"expected exactly one {kw}, got {[s.headline for s in r]}"
    return r[0]


# ------------------------------------------------------------------------------------------------ concentration
class TestTrimWarning:
    def test_single_stock_over_cap_fires_with_spec_wording(self):
        s = one(generate_signals(ctx()), action="TRIM WARNING", ticker="NVDA")
        assert s.headline.startswith("TRIM WARNING - Exceeds 15% Handbook Cap") and s.rules == ("R-CAP-01",)
        assert s.severity == "HIGH" and s.side == "SELL" and s.suggested_gbp > 0
        assert any("Dilution route" in r for r in s.rationale) and any("does not free allowance" in r for r in s.rationale)

    def test_critical_when_far_over_cap(self):
        rows = [HoldingRow("T212_ISA", "NVDA", 300, 50.0), HoldingRow("T212_ISA", "VWRP.L", 50, 100.0)]
        assert one(generate_signals(ctx(holdings=rows)), action="TRIM WARNING", ticker="NVDA").severity == "CRITICAL"

    def test_under_cap_no_trim_but_stop_adding_when_over_target(self):
        rows = [HoldingRow("T212_ISA", "NVDA", 60, 50.0), HoldingRow("T212_ISA", "VWRP.L", 300, 100.0),
                HoldingRow("T212_ISA", "VUAG.L", 200, 90.0)]           # NVDA ~10.4%: > 6%+3pp target, < 15% cap
        sigs = generate_signals(ctx(holdings=rows))
        assert not by(sigs, "TRIM WARNING", "NVDA")
        s = one(sigs, action="STOP ADDING", ticker="NVDA")
        assert s.severity == "LOW" and "R-REB-01" in s.rules

    def test_core_etf_exempt_unless_configured(self):
        rows = [HoldingRow("T212_ISA", "VWRP.L", 1000, 100.0), HoldingRow("T212_ISA", "AZN.L", 10, 100.0)]
        assert not by(generate_signals(ctx(holdings=rows)), "TRIM WARNING", "VWRP.L")
        strict = parse_handbook_rules("```yaml handbook-rules\nposition:\n  cap_applies_to: [stock, trust, etf]\n```")
        assert by(generate_signals(ctx(holdings=rows, rules=strict)), "TRIM WARNING", "VWRP.L")

    def test_handbook_number_change_changes_the_signal(self):
        loose = parse_handbook_rules("```yaml handbook-rules\nposition:\n  max_single_position_pct: 25\n```")
        assert not by(generate_signals(ctx(rules=loose)), "TRIM WARNING", "NVDA")

    def test_sector_cap_over_with_look_through(self):
        s = one(generate_signals(ctx()), action="TRIM WARNING", id_prefix="SECTOR:")
        assert "Technology" in s.headline and "Sector Cap" in s.headline and s.rules == ("R-CAP-02",) and s.ticker is None
        assert any("NVDA" in r for r in s.rationale)


# ------------------------------------------------------------------------------------------------ accumulate family
def underweight_ctx(**snap_kw):
    """Only AZN is meaningfully under target so assertions are unambiguous."""
    rows = [HoldingRow("T212_ISA", "VWRP.L", 250, 100.0), HoldingRow("T212_ISA", "VUAG.L", 200, 90.0),
            HoldingRow("T212_ISA", "SMT.L", 800, 10.0), HoldingRow("T212_ISA", "AZN.L", 30, 100.0),
            HoldingRow("T212_ISA", "MSFT", 20, 300.0), HoldingRow("T212_ISA", "NVDA", 25, 50.0)]
    base = dict(upside_pct=22.0, n_analysts=20, target_mean=170.8, rating_label="Buy", rating_mean=1.9, rsi14=50.0,
                trend="UPTREND", vs_sma200_pct=3.0, vs_5y_avg_pct=10.0, sector="Healthcare")
    base.update(snap_kw)
    return ctx(holdings=rows, snaps={"AZN.L": snap("AZN.L", **base)})


class TestAccumulate:
    def test_spec_headline_underweight_and_analyst_upside(self):
        c = underweight_ctx()
        pos = c.portfolio.get("AZN.L")
        assert pos.drift_pp < -2
        s = one(generate_signals(c), action="ACCUMULATE", ticker="AZN.L")
        assert s.headline.startswith("ACCUMULATE - Underweight & >15% Analyst Upside") and s.severity == "HIGH"
        assert {"R-REB-01", "R-UPS-01"} <= set(s.rules) and s.side == "BUY" and s.suggested_gbp > 0
        assert any("20 analysts" in r for r in s.rationale)

    def test_modest_upside_is_low_dca_only(self):
        s = one(generate_signals(underweight_ctx(upside_pct=8.0)), action="ACCUMULATE", ticker="AZN.L")
        assert s.severity == "LOW" and "< 15% Threshold" in s.headline and any("DCA only" in r for r in s.rationale)

    def test_thin_coverage_ignores_consensus(self):
        s = one(generate_signals(underweight_ctx(n_analysts=3)), action="ACCUMULATE", ticker="AZN.L")
        assert "thin analyst coverage" in s.headline and s.severity == "LOW"

    def test_etf_underweight_uses_allocation_rule_only(self):
        rows = [HoldingRow("T212_ISA", "VUAG.L", 50, 90.0), HoldingRow("T212_ISA", "VWRP.L", 250, 100.0),
                HoldingRow("T212_ISA", "AZN.L", 60, 100.0), HoldingRow("T212_ISA", "SMT.L", 800, 10.0),
                HoldingRow("T212_ISA", "MSFT", 30, 300.0), HoldingRow("T212_ISA", "NVDA", 40, 50.0)]
        s = one(generate_signals(ctx(holdings=rows, snaps={"VUAG.L": snap("VUAG.L", rsi14=50.0)})), action="ACCUMULATE", ticker="VUAG.L")
        assert "Underweight vs Core Target" in s.headline and s.severity == "MEDIUM" and any("no analyst price targets" in r for r in s.rationale)

    def test_oversold_bumps_severity_but_never_to_critical(self):
        s = one(generate_signals(underweight_ctx(rsi14=24.0)), action="ACCUMULATE", ticker="AZN.L")
        assert "Oversold (RSI 24)" in s.headline and s.severity == "HIGH" and "R-RSI-01" in s.rules
        low = one(generate_signals(underweight_ctx(upside_pct=8.0, rsi14=24.0)), action="ACCUMULATE", ticker="AZN.L")
        assert low.severity == "MEDIUM"

    def test_overbought_becomes_hold_off(self):
        s = one(generate_signals(underweight_ctx(rsi14=78.0)), action="HOLD OFF", ticker="AZN.L")
        assert "Overbought" in s.headline and "Stage Entries" in s.headline and s.severity == "MEDIUM"
        assert not by(generate_signals(underweight_ctx(rsi14=78.0)), "ACCUMULATE", "AZN.L")

    def test_far_below_200dma_downgrades_and_warns(self):
        s = one(generate_signals(underweight_ctx(vs_sma200_pct=-18.0)), action="ACCUMULATE", ticker="AZN.L")
        assert s.severity == "MEDIUM" and "R-TRD-01" in s.rules and any("falling knife" in r for r in s.rationale)

    def test_direct_stock_in_over_cap_sector_is_held_off_not_accumulated(self):
        s = one(generate_signals(ctx(snaps={"MSFT": snap("MSFT", upside_pct=27.0, n_analysts=50)})), ticker="MSFT", id_prefix="ACC:")
        assert s.action == "HOLD OFF" and "Sector Cap" in s.headline and "R-CAP-02" in s.rules and s.metrics["blocks_buy"]

    def test_screen_fail_blocks_accumulate(self):
        s = underweight_ctx(payout_ratio=0.95, dividend_cover=1.05, dividend_yield_pct=4.0)
        sigs = generate_signals(s)
        assert one(sigs, action="SCREEN FAIL", ticker="AZN.L").severity == "MEDIUM"
        assert not by(sigs, "ACCUMULATE", "AZN.L") and "AZN.L" in blocked_tickers(sigs)

    def test_price_above_consensus_target_triggers_review_and_blocks(self):
        sigs = generate_signals(underweight_ctx(upside_pct=-9.0))
        s = one(sigs, action="REVIEW", ticker="AZN.L")
        assert "R-UPS-02" in s.rules and "9%" in s.headline
        assert not by(sigs, "ACCUMULATE", "AZN.L") and "AZN.L" in blocked_tickers(sigs)

    def test_oversold_on_target_holding_gets_deploy_nudge(self):
        rows = [HoldingRow("T212_ISA", "VWRP.L", 250, 100.0), HoldingRow("T212_ISA", "VUAG.L", 200, 90.0),
                HoldingRow("T212_ISA", "SMT.L", 800, 10.0), HoldingRow("T212_ISA", "AZN.L", 90, 100.0),
                HoldingRow("T212_ISA", "MSFT", 30, 300.0), HoldingRow("T212_ISA", "NVDA", 25, 50.0)]
        c = ctx(holdings=rows, snaps={"SMT.L": snap("SMT.L", rsi14=22.0)})
        assert abs(c.portfolio.get("SMT.L").drift_pp) < 2
        assert "Oversold" in one(generate_signals(c), action="DEPLOY", ticker="SMT.L").headline


# ------------------------------------------------------------------------------------------------ screens
class TestScreens:
    def test_ocf_limits_passive_vs_active(self):
        r = HandbookRules()
        etf_bad = snap("VWRP.L", ter_pct=0.30)
        assert screen_failures(etf_bad, WATCH["VWRP.L"], r)[0][0] == "R-OCF-01"
        assert screen_failures(snap("VWRP.L", ter_pct=0.25), WATCH["VWRP.L"], r) == []          # boundary passes
        assert screen_failures(snap("SMT.L", ter_pct=0.31), WATCH["SMT.L"], r) == []            # trust: active limit 0.80
        assert screen_failures(snap("SMT.L", ter_pct=0.95), WATCH["SMT.L"], r)[0][0] == "R-OCF-01"
        assert screen_failures(snap("VWRP.L", ter_pct=None), WATCH["VWRP.L"], r) == []          # unknown never fails

    def test_dividend_cover_only_for_payers(self):
        r = HandbookRules()
        fail = snap("ULVR.L", dividend_yield_pct=4.7, payout_ratio=0.72, dividend_cover=1 / 0.72)
        assert screen_failures(fail, WATCH["ULVR.L"], r)[0][0] == "R-DIV-01"
        assert screen_failures(snap("ULVR.L", dividend_yield_pct=4.7, dividend_cover=1.5), WATCH["ULVR.L"], r) == []  # boundary
        assert screen_failures(snap("NVDA", dividend_yield_pct=0.03, dividend_cover=0.5), WATCH["NVDA"], r) == []     # non-payer
        assert screen_failures(snap("ULVR.L", dividend_yield_pct=4.7, dividend_cover=None), WATCH["ULVR.L"], r) == []

    def test_valuation_needs_both_peg_and_pe_to_fail_when_both_known(self):
        r = HandbookRules()
        assert screen_failures(snap("MSFT", peg=3.5, forward_pe=20.0), WATCH["MSFT"], r) == []
        assert screen_failures(snap("MSFT", peg=3.5, forward_pe=45.0), WATCH["MSFT"], r)[0][0] == "R-VAL-01"
        assert screen_failures(snap("MSFT", peg=3.5, forward_pe=None), WATCH["MSFT"], r)[0][0] == "R-VAL-01"
        assert screen_failures(snap("MSFT", peg=None, forward_pe=None), WATCH["MSFT"], r) == []

    def test_watch_only_name_screen_fail_is_informational(self):
        c = ctx(snaps={"ULVR.L": snap("ULVR.L", dividend_yield_pct=4.7, payout_ratio=0.72, dividend_cover=1.39)})
        s = one(generate_signals(c), action="SCREEN FAIL", ticker="ULVR.L")
        assert s.severity == "LOW" and "(watch-only)" in s.headline and s.headline.startswith("SCREEN FAIL - Dividend Cover 1.39x < 1.5x")
        assert "ULVR.L" not in blocked_tickers([s])


# ------------------------------------------------------------------------------------------------ tax
def deposit_ledger(total, lisa=0, on=date(2026, 5, 1)):
    led = [LedgerEntry(on, "T212_ISA", "deposit", D(total))]
    if lisa:
        led.append(LedgerEntry(on, "HL_LISA", "deposit", D(lisa)))
    return led


class TestTaxAlerts:
    @pytest.mark.parametrize("now,sev", [
        (datetime(2026, 11, 18, 12, tzinfo=LONDON), "LOW"),        # 138d out
        (datetime(2027, 1, 20, 12, tzinfo=LONDON), "MEDIUM"),      # 75d
        (datetime(2027, 3, 10, 12, tzinfo=LONDON), "HIGH"),        # 26d
        (datetime(2027, 3, 31, 12, tzinfo=LONDON), "CRITICAL"),    # 5d
    ])
    def test_escalation_ladder(self, now, sev):
        s = one(generate_signals(ctx(now=now, ledger=deposit_ledger(10000), monthly=0.0)), id_prefix="TAX:ALLOWANCE")
        assert s.severity == sev and s.headline.startswith("TAX ALERT - £10,000 Allowance Expiring") and s.rules == ("R-TAX-01",)

    def test_on_pace_downgrades_distant_alert_to_info(self):
        s = one(generate_signals(ctx(ledger=deposit_ledger(10000), monthly=5000.0)), id_prefix="TAX:ALLOWANCE")
        assert s.severity == "INFO" and any("on pace" in r for r in s.rationale)

    def test_rationale_explains_pace_deadline_and_cutoff(self):
        s = one(generate_signals(ctx(ledger=deposit_ledger(10000), monthly=500.0)), id_prefix="TAX:ALLOWANCE")
        text = " ".join(s.rationale)
        assert "use-it-or-lose-it" in text and "5 April 2027" in text and "Fri 02 Apr 2027" in text and "shortfall" in text

    def test_no_alert_when_allowance_fully_used(self):
        assert not by(generate_signals(ctx(ledger=deposit_ledger(20000))), id_prefix="TAX:ALLOWANCE")

    def test_breach_is_critical_and_top_of_list(self):
        led = [LedgerEntry(date(2026, 5, 1), "VG_ISA", "deposit", D(21500))]
        sigs = generate_signals(ctx(ledger=led))
        s = one(sigs, id_prefix="TAX:BREACH")
        assert s.severity == "CRITICAL" and "£1,500.00" in s.headline and sigs[0].id == "TAX:BREACH"

    def test_lisa_bonus_at_risk_uses_25_percent(self):
        s = one(generate_signals(ctx(ledger=deposit_ledger(1000, lisa=2500))), id_prefix="TAX:LISA")
        assert "£1,500 LISA Room Unused = £375 Bonus at Risk" in s.headline and s.rules == ("R-TAX-02",)

    def test_no_lisa_alert_when_lisa_full(self):
        assert not by(generate_signals(ctx(ledger=deposit_ledger(1000, lisa=4000))), id_prefix="TAX:LISA")

    def test_flexible_credit_is_explained(self):
        led = deposit_ledger(5000) + [LedgerEntry(date(2026, 6, 1), "VG_ISA", "withdrawal", D(3000))]
        s = one(generate_signals(ctx(ledger=led)), id_prefix="TAX:ALLOWANCE")
        assert any("replacement credit" in r for r in s.rationale) and "£18,000" in s.headline


class TestBedAndIsa:
    def test_candidate_signal_with_headroom_tradeoff(self):
        s = one(generate_signals(ctx(bed=True)), action="BED & ISA")
        assert s.rules == ("R-BED-01", "R-TAX-01") and s.side == "SELL" and "CGT Allowance" in s.headline
        assert any("Trade-off" in r for r in s.rationale) and any("Section 104" in r for r in s.rationale)

    def test_absent_without_gia(self):
        rows = [h for h in load_holdings_csv(CFG.holdings_csv, CFG.accounts) if h.account != "HL_GIA"]
        assert not by(generate_signals(ctx(bed=True, holdings=rows)), action="BED & ISA")

    def test_trivial_gain_ignored(self):
        rows = [HoldingRow("HL_GIA", "AZN.L", 10, 139.0), HoldingRow("T212_ISA", "VWRP.L", 100, 100.0)]
        assert not by(generate_signals(ctx(bed=True, holdings=rows)), action="BED & ISA")

    def test_rebuy_over_cap_warning(self):
        rows = [HoldingRow("HL_GIA", "NVDA", 100, 50.0), HoldingRow("T212_ISA", "NVDA", 92, 47.3), HoldingRow("T212_ISA", "VWRP.L", 200, 100.0)]
        s = one(generate_signals(ctx(bed=True, holdings=rows)), action="BED & ISA")
        assert any("push it over" in r for r in s.rationale)


# ------------------------------------------------------------------------------------------------ portfolio-level
class TestPortfolioSignals:
    @pytest.mark.parametrize("dd,tier,cum,sev", [(-12.0, 1, 25, "MEDIUM"), (-25.0, 2, 60, "HIGH"), (-33.0, 3, 100, "CRITICAL")])
    def test_drawdown_tiers_are_cumulative(self, dd, tier, cum, sev):
        s = one(generate_signals(ctx(snaps={"VWRP.L": snap("VWRP.L", drawdown_52w_pct=dd)})), action="DEPLOY RESERVE")
        assert f"Tier {tier}" in s.headline and f"Deploy {cum}% of Cash" in s.headline and s.severity == sev and s.rules == ("R-DD-01",)
        assert s.suggested_gbp == pytest.approx(1900 * cum / 100)

    def test_shallow_drawdown_no_signal(self):
        assert not by(generate_signals(ctx(snaps={"VWRP.L": snap("VWRP.L", drawdown_52w_pct=-6.0)})), action="DEPLOY RESERVE")

    def test_cash_drag(self):
        rows = [HoldingRow("T212_ISA", "VWRP.L", 100, 100.0), HoldingRow("T212_ISA", "CASH", 3000, 1.0)]
        s = one(generate_signals(ctx(holdings=rows)), id_prefix="CASH:DRAG")
        assert "Cash Drag" in s.headline and s.rules == ("R-CSH-01",)

    def test_demo_data_warning_is_prominent(self):
        sigs = generate_signals(ctx(data="DEMO"))
        s = one(sigs, action="DATA WARNING", id_prefix="DATA:DEMO")
        assert s.severity == "HIGH" and "NOT actionable" in s.headline and sigs.index(s) <= 2
        assert not by(generate_signals(ctx(data="LIVE")), id_prefix="DATA:DEMO")

    def test_custom_price_and_rsi_alerts_from_config(self):
        rows = [HoldingRow("T212_ISA", "NVDA", 60, 50.0), HoldingRow("T212_ISA", "VWRP.L", 300, 100.0),
                HoldingRow("T212_ISA", "VUAG.L", 200, 90.0), HoldingRow("T212_ISA", "AZN.L", 40, 100.0)]
        sigs = generate_signals(ctx(holdings=rows, prices={**PX, "AZN.L": 110.0}, snaps={"NVDA": snap("NVDA", rsi14=80.0)}))
        assert "£110.00 Below £115.00 Floor" in one(sigs, action="PRICE ALERT", ticker="AZN.L").headline
        assert "RSI 80 > 75" in one(sigs, action="RSI ALERT", ticker="NVDA").headline

    def test_rsi_alert_is_not_duplicated_when_another_signal_already_explains_rsi(self):
        sigs = generate_signals(ctx(snaps={"NVDA": snap("NVDA", rsi14=80.0)}))          # NVDA over cap: TRIM cites the RSI
        assert not by(sigs, "RSI ALERT", "NVDA")
        assert any("overbought" in r for r in one(sigs, action="TRIM WARNING", ticker="NVDA").rationale)

    def test_missing_target_range_does_not_crash_signal_generation(self):
        c = underweight_ctx(upside_low_pct=None, upside_high_pct=None)
        s = one(generate_signals(c), action="ACCUMULATE", ticker="AZN.L")
        assert "range" not in " ".join(s.rationale)

    def test_sorted_most_important_first_and_headlines_are_ticker_free(self):
        sigs = generate_signals(ctx())
        assert [s.priority for s in sigs] == sorted((s.priority for s in sigs), reverse=True)
        nv = one(sigs, action="TRIM WARNING", ticker="NVDA")
        assert "NVDA" not in nv.headline and nv.label.startswith("TRIM WARNING NVDA - ")

    def test_every_ticker_signal_cites_rules_or_is_custom(self):
        for s in generate_signals(ctx()):
            assert s.rationale, s.id
            if s.action in ("TRIM WARNING", "ACCUMULATE", "SCREEN FAIL", "TAX ALERT", "BED & ISA"):
                assert s.rules, s.id


# ------------------------------------------------------------------------------------------------ score
class TestScore:
    def test_missing_components_drop_out_and_weights_renormalise(self):
        r = HandbookRules()
        only_cons = score_security(snap("MSFT", rating_mean=1.0, n_analysts=50), None, r)
        assert set(only_cons.components) == {"consensus", "quality"} or "consensus" in only_cons.components
        assert only_cons.components["consensus"] == 100.0
        assert score_security(None, None, r).total is None and score_security(None, None, r).label == "N/A"

    def test_thin_coverage_shrinks_consensus_toward_neutral(self):
        r = HandbookRules()
        full = score_security(snap("MSFT", rating_mean=1.0, n_analysts=10), None, r).components["consensus"]
        thin = score_security(snap("MSFT", rating_mean=1.0, n_analysts=1), None, r).components["consensus"]
        assert full == 100.0 and thin == pytest.approx(60.0)

    def test_allocation_score_rewards_underweight(self):
        r = HandbookRules()
        c = ctx()
        under, over = c.portfolio.get("MSFT"), c.portfolio.get("NVDA")
        assert score_security(None, under, r).components["allocation"] > 50 > score_security(None, over, r).components["allocation"]
        assert score_security(None, over, r).components["allocation"] == 0

    def test_bands_and_bounds(self):
        r = HandbookRules()
        best = score_security(snap("AZN.L", upside_pct=40, peg=0.5, vs_5y_avg_pct=-30, rating_mean=1.0, n_analysts=30, rsi14=30.0, trend="UPTREND"), None, r)
        worst = score_security(snap("AZN.L", upside_pct=-30, peg=3.5, vs_5y_avg_pct=60, rating_mean=5.0, n_analysts=30, rsi14=75.0, trend="DOWNTREND"), None, r)
        assert 0 <= worst.total < 40 and worst.label == "WEAK" and best.total > 70 and best.label == "STRONG"


# ------------------------------------------------------------------------------------------------ robustness
def test_fuzz_random_missing_fields_never_crash_signal_generation():
    """Live Yahoo data is patchy. Randomly blank optional snapshot fields and make sure nothing raises."""
    import random
    rng = random.Random(1234)
    optional = {"rating_mean": (1.0, 5.0), "n_analysts": (0, 60), "target_mean": (50, 900), "upside_pct": (-30, 70),
                "upside_low_pct": (-40, 10), "upside_high_pct": (10, 120), "forward_pe": (5, 80), "peg": (0.2, 6),
                "dividend_yield_pct": (0, 6), "payout_ratio": (0.1, 1.4), "dividend_cover": (0.6, 6), "rsi14": (10, 90),
                "sma50": (50, 500), "sma200": (50, 500), "vs_sma200_pct": (-40, 40), "vs_5y_avg_pct": (-40, 90),
                "drawdown_52w_pct": (-45, 0), "ter_pct": (0.03, 1.2), "price": (5, 600), "price_gbp": (5, 600)}
    for i in range(300):
        snaps = {}
        for t in list(PX) + ["^none"]:
            kw = {k: (None if rng.random() < 0.4 else rng.uniform(*r)) for k, r in optional.items()}
            kw["n_analysts"] = None if kw["n_analysts"] is None else int(kw["n_analysts"])
            kw["trend"] = rng.choice(["UPTREND", "DOWNTREND", "MIXED", "N/A"])
            kw["valuation_label"] = rng.choice(["RICH", "CHEAP", "FAIR", "—"])
            kw["rating_label"] = "Buy"
            snaps[t] = snap(t, **kw)
        led = deposit_ledger(rng.choice([0, 5000, 19999, 20000, 21000]), lisa=rng.choice([0, 1000, 4000, 4500]))
        now = rng.choice([NOW, datetime(2027, 3, 30, 9, tzinfo=LONDON), datetime(2027, 4, 5, 23, 59, tzinfo=LONDON)])
        sigs = generate_signals(ctx(snaps=snaps, ledger=led, now=now, monthly=rng.choice([0.0, 500.0, 2000.0]),
                                    data=rng.choice(["LIVE", "DEMO", "STALE"]), bed=bool(i % 2)))
        for s in sigs:
            assert s.headline and s.rationale and s.severity in ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")
            assert "None" not in s.headline and "nan" not in s.headline.lower(), s.headline
            assert all("None" not in r and "nan" not in r.lower().split() for r in s.rationale), (s.id, s.rationale)


def test_a_single_factor_is_not_a_score():
    r = HandbookRules()
    only_quality = score_security(snap("BRK-B", kind="stock"), None, r)           # nothing but the 'quality' gate available
    assert only_quality.total is None and only_quality.label == "N/A" and "quality" in only_quality.components
    two = score_security(snap("BRK-B", kind="stock", rsi14=50.0, trend="UPTREND"), None, r)
    assert two.total is not None and two.label != "N/A"
