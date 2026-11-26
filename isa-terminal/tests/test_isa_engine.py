"""ISA tax-year boundaries, flexible-ISA allowance maths, countdown, no-sell rebalancer, Bed & ISA."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from engine.isa_engine import (LONDON, AccountSpec, GIAPosition, LedgerEntry, LedgerError, TAX_YEAR_RULES,
                               allowance_pace, compute_isa_status, countdown_to_deadline, D, parse_ledger_csv,
                               plan_bed_and_isa, plan_rebalance, rules_for, tax_year, tax_year_for,
                               tax_year_start_for)

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc


# ----------------------------------------------------------------------------- rules / calendar
class TestTaxYearBoundaries:
    @pytest.mark.parametrize("d,start", [
        (date(2026, 4, 5), 2025),      # last day of 2025/26
        (date(2026, 4, 6), 2026),      # first day of 2026/27
        (date(2026, 11, 18), 2026),
        (date(2027, 1, 1), 2026),
        (date(2027, 4, 5), 2026),      # last day of 2026/27 — still allowed
        (date(2027, 4, 6), 2027),      # first day of 2027/28
        (date(2024, 2, 29), 2023),     # leap day
    ])
    def test_date_boundaries(self, d, start):
        assert tax_year_start_for(d) == start

    def test_2026_27_dates_and_label(self):
        ty = tax_year(2026)
        assert (ty.start, ty.end, ty.label) == (date(2026, 4, 6), date(2027, 4, 5), "2026/27")
        assert ty.contains(date(2027, 4, 5)) and not ty.contains(date(2027, 4, 6))

    def test_deadline_is_end_of_5_april_london_bst(self):
        dl = tax_year(2026).deadline
        assert (dl.year, dl.month, dl.day, dl.hour) == (2027, 4, 6, 0)
        assert dl.utcoffset() == timedelta(hours=1)                 # BST, never a hard-coded UTC midnight
        assert dl.astimezone(UTC) == datetime(2027, 4, 5, 23, 0, tzinfo=UTC)

    def test_utc_timestamp_just_before_london_midnight_is_next_tax_year(self):
        # 23:30 UTC on 5 April 2027 == 00:30 BST on 6 April 2027 => 2027/28
        assert tax_year_start_for(datetime(2027, 4, 5, 23, 30, tzinfo=UTC)) == 2027
        assert tax_year_start_for(datetime(2027, 4, 5, 22, 59, tzinfo=UTC)) == 2026
        # In winter (GMT) the same UTC clock time is still 5 April... only matters for April, but check Jan
        assert tax_year_start_for(datetime(2027, 1, 5, 23, 30, tzinfo=UTC)) == 2026

    def test_naive_datetime_is_london_local(self):
        assert tax_year_start_for(datetime(2027, 4, 5, 23, 59)) == 2026
        assert tax_year_start_for(datetime(2027, 4, 6, 0, 0)) == 2027

    def test_tax_year_for_defaults_to_now_without_crash(self):
        assert tax_year_for().start_year >= 2026


class TestRulesTable:
    def test_2026_27_constants(self):
        r = rules_for(2026)
        assert r.annual_limit == 20000 and r.lisa_limit == 4000
        assert r.lisa_bonus_rate == Decimal("0.25") and r.cgt_annual_exempt == 3000 and r.confirmed

    def test_lisa_limit_is_inside_annual_limit(self):
        assert all(r.lisa_limit < r.annual_limit for r in TAX_YEAR_RULES.values())

    def test_unknown_future_year_falls_back_and_is_flagged(self):
        r = rules_for(2035)
        assert r.annual_limit == 20000 and r.confirmed is False and r.start_year == 2035


class TestCountdown:
    def test_exact_one_hour_left_at_23_00_bst(self):
        c = countdown_to_deadline(datetime(2027, 4, 5, 23, 0, tzinfo=LONDON))
        assert (c.days, c.hours, c.minutes, c.expired) == (0, 1, 0, False)

    def test_expired_at_midnight(self):
        c = countdown_to_deadline(datetime(2027, 4, 6, 0, 0, tzinfo=LONDON), tax_year(2026))
        assert c.expired and c.total_seconds == 0 and c.short() == "EXPIRED"

    def test_countdown_uses_real_elapsed_time_across_the_clocks_going_forward(self):
        # Clocks go forward 28 Mar 2027 01:00 GMT -> 02:00 BST. 12:00 on 27 Mar (GMT) to 00:00 6 Apr (BST):
        now = datetime(2027, 3, 27, 12, 0, tzinfo=LONDON)
        real = (datetime(2027, 4, 5, 23, 0, tzinfo=UTC) - datetime(2027, 3, 27, 12, 0, tzinfo=UTC))
        c = countdown_to_deadline(now)
        assert c.total_seconds == int(real.total_seconds())
        # naive wall-clock subtraction would be exactly 1h out
        wall = datetime(2027, 4, 6, 0, 0) - datetime(2027, 3, 27, 12, 0)
        assert c.total_seconds == int(wall.total_seconds()) - 3600

    def test_mid_november_gmt_to_bst_deadline(self):
        # 18 Nov 15:36 GMT -> 23:00 UTC on 5 Apr (= 00:00 BST on 6 Apr): 138 days + 7h24m of real time
        c = countdown_to_deadline(datetime(2026, 11, 18, 15, 36, tzinfo=LONDON))
        assert (c.days, c.hours, c.minutes) == (138, 7, 24)
        assert c.long() == "138d 07h 24m 00s" and c.short() == "138d 07h 24m"

    def test_accepts_utc_input(self):
        a = countdown_to_deadline(datetime(2027, 4, 5, 22, 0, tzinfo=UTC))
        assert a.total_seconds == 3600


# ----------------------------------------------------------------------------- allowance ledger
ACC = {
    "T212": AccountSpec("T212", "SS_ISA", flexible=True),
    "HL": AccountSpec("HL", "SS_ISA", flexible=True),
    "STRICT": AccountSpec("STRICT", "SS_ISA", flexible=False),
    "LISA": AccountSpec("LISA", "LISA", flexible=False),
    "GIA": AccountSpec("GIA", "GIA"),
}
AS_OF = date(2026, 11, 18)


def E(d, acc, typ, amt):
    return LedgerEntry(date.fromisoformat(d), acc, typ, D(amt))


def status(entries, as_of=AS_OF, accounts=ACC):
    return compute_isa_status(entries, accounts, as_of=as_of)


class TestAllowanceBasics:
    def test_empty_ledger_full_allowance(self):
        s = status([])
        assert s.remaining == 20000 and s.headroom == 20000 and s.net_used == 0 and not s.has_breach

    def test_simple_deposits(self):
        s = status([E("2026-05-01", "T212", "deposit", "1000.50"), E("2026-06-01", "HL", "deposit", "2000")])
        assert s.net_used == D("3000.50") and s.remaining == D("16999.50")

    def test_entries_outside_tax_year_ignored(self):
        s = status([E("2026-04-05", "T212", "deposit", "5000"),     # last day of 2025/26
                    E("2026-04-06", "T212", "deposit", "100")])      # first day of 2026/27
        assert s.net_used == 100

    def test_future_dated_entries_not_counted_yet(self):
        s = status([E("2026-12-01", "T212", "deposit", "500")])
        assert s.net_used == 0

    def test_deposit_on_deadline_day_counts(self):
        s = status([E("2027-04-05", "T212", "deposit", "500")], as_of=date(2027, 4, 5))
        assert s.net_used == 500

    def test_gia_ignored_and_unknown_account_is_conservative(self):
        s = status([E("2026-05-01", "GIA", "deposit", "9999"), E("2026-05-01", "MYSTERY", "deposit", "100"),
                    E("2026-05-02", "MYSTERY", "withdrawal", "100")])
        assert s.net_used == 100                      # unknown => non-flexible => withdrawal gives NO room
        assert any("MYSTERY" in w for w in s.warnings)


class TestFlexibleIsa:
    def test_classic_withdraw_and_replace_in_same_year(self):
        # Pay in 20k, take 5k out, put 5k back: allowance used is still only 20k.
        s = status([E("2026-04-07", "T212", "deposit", "20000"), E("2026-07-01", "T212", "withdrawal", "5000"),
                    E("2026-08-01", "T212", "deposit", "5000")])
        assert s.subscriptions == 25000 and s.net_used == 20000 and s.remaining == 0 and not s.has_breach

    def test_withdrawal_creates_room_to_resubscribe(self):
        s = status([E("2026-04-07", "T212", "deposit", "20000"), E("2026-07-01", "T212", "withdrawal", "5000")])
        assert s.net_used == 15000 and s.remaining == 5000 and s.headroom == 5000

    def test_withdrawal_of_old_money_gives_replacement_credit_only_for_that_account(self):
        # £4k pulled from pre-existing T212 money, nothing subscribed there this year; £10k paid into HL.
        s = status([E("2026-06-01", "T212", "withdrawal", "4000"), E("2026-06-02", "HL", "deposit", "10000")])
        assert s.net_used == 10000 and s.remaining == 10000
        assert s.replacement_credit == 4000 and s.headroom == 14000
        assert s.max_new_subscription("T212") == 14000 and s.max_new_subscription("HL") == 10000
        assert s.max_new_subscription() == 10000

    def test_withdrawal_from_non_flexible_account_gives_nothing(self):
        s = status([E("2026-04-07", "STRICT", "deposit", "10000"), E("2026-07-01", "STRICT", "withdrawal", "4000")])
        assert s.net_used == 10000 and s.remaining == 10000 and s.non_flexible_withdrawals == 4000

    def test_previous_tax_year_withdrawal_cannot_be_replaced_now(self):
        s = status([E("2026-03-20", "T212", "withdrawal", "2000"), E("2026-04-07", "T212", "deposit", "2000")])
        assert s.net_used == 2000 and s.eligible_withdrawals == 0

    def test_withdrawal_cannot_raise_headroom_above_the_limit_by_itself_for_other_accounts(self):
        s = status([E("2026-04-07", "HL", "deposit", "20000"), E("2026-05-01", "T212", "withdrawal", "3000")])
        assert s.remaining == 0 and s.replacement_credit == 3000 and s.max_new_subscription("HL") == 0

    def test_over_subscription_is_flagged_not_capped(self):
        s = status([E("2026-04-07", "STRICT", "deposit", "21500")])
        assert s.breach == 1500 and s.has_breach and s.remaining == 0


class TestLisa:
    def test_lisa_counts_inside_annual_limit(self):
        s = status([E("2026-04-07", "LISA", "deposit", "4000"), E("2026-04-08", "T212", "deposit", "16000")])
        assert s.net_used == 20000 and s.remaining == 0 and s.lisa_remaining == 0 and not s.has_breach

    def test_bonus_is_never_a_subscription(self):
        s = status([E("2026-05-01", "LISA", "deposit", "1000"), E("2026-05-20", "LISA", "bonus", "250")])
        assert s.net_used == 1000 and s.ignored_bonus == 250

    def test_unused_lisa_room_forfeits_bonus(self):
        s = status([E("2026-05-01", "LISA", "deposit", "2000")])
        assert s.lisa_remaining == 2000 and s.lisa_bonus_forgone == D("500.00")

    def test_lisa_sublimit_breach(self):
        s = status([E("2026-05-01", "LISA", "deposit", "4500")])
        assert s.lisa_breach == 500 and s.has_breach

    def test_lisa_withdrawal_is_never_flexible(self):
        acc = {"LISA": AccountSpec("LISA", "LISA", flexible=True)}   # even if mis-flagged
        s = status([E("2026-05-01", "LISA", "deposit", "3000"), E("2026-06-01", "LISA", "withdrawal", "1000")], accounts=acc)
        assert s.net_used == 3000 and s.eligible_withdrawals == 0

    def test_max_new_lisa_subscription_is_capped_by_lisa_room(self):
        s = status([E("2026-05-01", "LISA", "deposit", "3500")])
        assert s.max_new_subscription("LISA") == 500


def test_transfers_are_not_subscriptions():
    s = status([E("2026-05-01", "T212", "transfer_in", "50000"), E("2026-05-02", "T212", "transfer_out", "1000")])
    assert s.net_used == 0 and s.ignored_transfers == 51000


def test_previous_tax_year_status():
    s = compute_isa_status([E("2026-03-01", "T212", "deposit", "800")], ACC, as_of=date(2026, 3, 30))
    assert s.tax_year.label == "2025/26" and s.net_used == 800


class TestSampleCsv:
    def test_parse_and_headline_numbers(self):
        from engine.config import load_config
        cfg = load_config(ROOT / "config.yaml")
        accounts = {a.id: AccountSpec(a.id, a.kind, a.flexible, a.broker) for a in cfg.accounts.values()}
        s = compute_isa_status(parse_ledger_csv(cfg.contributions_csv), accounts, as_of=date(2026, 11, 18))
        # 3000 HL + 7x1000 + 800 replacement T212 + 2000 VG + 8x250 LISA = 14,800 gross;
        # withdrawals 800 (T212, replaced) and 1500 (HL, not replaced) are same-year flexible => used 12,500
        assert s.subscriptions == D("14800.00") and s.eligible_withdrawals == D("2300.00")
        assert s.net_used == D("12500.00") and s.remaining == D("7500.00") and s.headroom == D("7500.00")
        assert s.lisa_subscriptions == 2000 and s.lisa_bonus_forgone == D("500.00")
        assert s.ignored_bonus == D("375.00") and not s.has_breach
        # the £12,000 transfer-in is dated Feb 2026 (previous tax year): outside this year's window entirely
        assert s.ignored_transfers == 0

    def test_transfer_in_is_ignored_in_its_own_tax_year_too(self):
        from engine.config import load_config
        cfg = load_config(ROOT / "config.yaml")
        accounts = {a.id: AccountSpec(a.id, a.kind, a.flexible, a.broker) for a in cfg.accounts.values()}
        s = compute_isa_status(parse_ledger_csv(cfg.contributions_csv), accounts, as_of=date(2026, 4, 5))
        assert s.tax_year.label == "2025/26" and s.ignored_transfers == 12000
        # 2025/26: 5000 HL + 12x750 T212 + 2000 VG + 4000 LISA = 20,000 gross; the £2,000 March HL withdrawal is
        # flexible + same year => 18,000 used, and £2,000 of unreplaced room expires on 5 April 2026
        assert s.subscriptions == 20000 and s.net_used == 18000 and s.remaining == 2000

    def test_bad_csv_fails_loudly(self, tmp_path):
        p = tmp_path / "c.csv"
        p.write_text("date,account,type,amount_gbp\n2026-05-01,T212,deposit,-5\n")
        with pytest.raises(LedgerError, match="line 2"):
            parse_ledger_csv(p)
        p.write_text("date,account,kind,amount\n")
        with pytest.raises(LedgerError, match="need columns"):
            parse_ledger_csv(p)
        p.write_text("date,account,type,amount_gbp\n2026-05-01,T212,gift,5\n")
        with pytest.raises(LedgerError, match="unknown type"):
            parse_ledger_csv(p)


def test_allowance_pace_and_cutoff():
    s = status([E("2026-05-01", "T212", "deposit", "12500")])
    now = datetime(2026, 11, 18, 12, 0, tzinfo=LONDON)
    p = allowance_pace(s, now, practical_cutoff_days=3)
    assert p.practical_cutoff == date(2027, 4, 2)
    assert 136 < p.days_left < 139
    assert float(p.required_per_month) == pytest.approx(7500 / p.months_left, abs=0.5)
    done = allowance_pace(status([E("2026-05-01", "T212", "deposit", "20000")]), now)
    assert done.required_per_month == 0


# ----------------------------------------------------------------------------- rebalancer
class TestRebalance:
    VALUES = {"A": 6000.0, "B": 3000.0, "C": 1000.0}
    TARGETS = {"A": 0.5, "B": 0.3, "C": 0.2}

    def test_sums_exactly_and_never_sells(self):
        plan = plan_rebalance(self.VALUES, self.TARGETS, "1000.00")
        assert sum(o.amount for o in plan.orders) == D("1000.00") == plan.deployed and plan.undeployed == 0
        assert all(o.amount > 0 for o in plan.orders)

    def test_perfect_rebalance_when_cash_is_large_enough(self):
        # total 10k + 5k => targets 7.5k / 4.5k / 3k => buys 1.5k / 1.5k / 2k
        plan = plan_rebalance(self.VALUES, self.TARGETS, 5000)
        got = {o.ticker: o.amount for o in plan.orders}
        assert got == {"A": D("1500.00"), "B": D("1500.00"), "C": D("2000.00")}
        assert plan.post_drift < 1e-9 < plan.pre_drift

    def test_small_cash_goes_to_most_underweight_first(self):
        plan = plan_rebalance(self.VALUES, self.TARGETS, 300)
        assert [o.ticker for o in plan.orders] == ["C"] and plan.orders[0].amount == D("300.00")

    def test_overweight_name_gets_nothing(self):
        v = {"A": 9000.0, "B": 500.0, "C": 500.0}
        plan = plan_rebalance(v, self.TARGETS, 1000)
        assert "A" not in {o.ticker for o in plan.orders}

    def test_water_filling_equalises_value_over_target(self):
        plan = plan_rebalance(self.VALUES, self.TARGETS, 1500)
        post = {t: self.VALUES[t] + float(next((o.amount for o in plan.orders if o.ticker == t), 0)) for t in self.VALUES}
        funded = [t for t in post if t in {o.ticker for o in plan.orders}]
        ratios = [post[t] / self.TARGETS[t] for t in funded]
        assert max(ratios) - min(ratios) < 0.1

    def test_exclude_reallocates_to_the_rest(self):
        plan = plan_rebalance(self.VALUES, self.TARGETS, 1000, exclude=["C"])
        assert "C" not in {o.ticker for o in plan.orders} and plan.deployed == D("1000.00")

    def test_position_cap_is_respected_and_cash_spills_over(self):
        v = {"A": 1400.0, "B": 8000.0, "C": 600.0}       # A at 14%, cap 15% of 10k+1k
        plan = plan_rebalance(v, {"A": 0.5, "B": 0.25, "C": 0.25}, 1000, max_weight=0.15, reserve_cash=0)
        for o in plan.orders:
            assert o.post_weight <= 0.15 + 1e-6
        assert plan.deployed + plan.undeployed == D("1000.00")

    def test_per_ticker_caps_exempt_unlisted_names(self):
        v = {"ETF": 5000.0, "STK": 1000.0}
        plan = plan_rebalance(v, {"ETF": 0.5, "STK": 0.5}, 4000, max_weights={"STK": 0.15})
        got = {o.ticker: o.amount for o in plan.orders}
        # uncapped, the water-fill would put all 4,000 into STK (it is 5k behind); the 15% cap (1,500 of 10,000) limits it
        assert got["STK"] == D("500.00") and got["ETF"] == D("3500.00")
        assert plan_rebalance(v, {"ETF": 0.5, "STK": 0.5}, 4000).orders[0].ticker == "STK"

    def test_cash_reserve_counts_in_denominator(self):
        plan = plan_rebalance({"A": 1000.0}, {"A": 1.0}, 100, reserve_cash=900.0)
        assert plan.orders[0].pre_weight == pytest.approx(1000 / 1900)           # before the new cash
        assert plan.orders[0].post_weight == pytest.approx(1100 / 2000)          # after: (1000+100)/(1900+100)

    def test_min_order_drops_small_lines(self):
        plan = plan_rebalance(self.VALUES, self.TARGETS, 1000, min_order=250, prices=None)
        assert all(o.amount >= 250 for o in plan.orders) and plan.deployed == D("1000.00")

    def test_whole_shares(self):
        prices = {"A": 100.0, "B": 250.0, "C": 40.0}
        plan = plan_rebalance(self.VALUES, self.TARGETS, 1000, prices=prices, fractional=False)
        assert all(o.units == int(o.units) for o in plan.orders)
        for o in plan.orders:
            assert float(o.amount) == pytest.approx(o.units * prices[o.ticker])
        assert plan.deployed <= D("1000.00") and plan.undeployed < D("40.01")

    def test_new_position_with_zero_value_is_funded(self):
        plan = plan_rebalance({"A": 1000.0}, {"A": 0.5, "NEW": 0.5}, 500)
        assert {o.ticker for o in plan.orders} == {"NEW"}

    def test_no_cash_or_no_targets(self):
        assert plan_rebalance(self.VALUES, self.TARGETS, 0).orders == ()
        assert plan_rebalance(self.VALUES, self.TARGETS, 100, exclude=list(self.TARGETS)).undeployed == D("100.00")

    def test_penny_exact_with_awkward_numbers(self):
        v = {"A": 1234.56, "B": 789.01, "C": 3.33}
        plan = plan_rebalance(v, {"A": 0.3333, "B": 0.3333, "C": 0.3334}, "333.33")
        assert sum(o.amount for o in plan.orders) == D("333.33")


# ----------------------------------------------------------------------------- Bed & ISA
class TestBedAndIsa:
    def test_everything_fits(self):
        p = plan_bed_and_isa([GIAPosition("X", 10, 100.0, 150.0)], 3000, 20000)
        assert p.lines[0].units == 10 and p.total_gain == pytest.approx(500) and p.limited_by == "all_positions_moved"

    def test_cgt_allowance_is_the_binding_limit(self):
        p = plan_bed_and_isa([GIAPosition("X", 100, 100.0, 150.0)], 1000, 50000)      # £50/unit gain
        assert p.lines[0].units == pytest.approx(20) and p.total_gain == pytest.approx(1000)
        assert p.limited_by == "cgt_allowance" and p.cgt_allowance_left == pytest.approx(0)

    def test_isa_headroom_is_the_binding_limit(self):
        p = plan_bed_and_isa([GIAPosition("X", 100, 100.0, 110.0)], 3000, 550)
        assert p.lines[0].units == pytest.approx(5) and p.limited_by == "isa_headroom"

    def test_lowest_gain_per_pound_first_and_losses_first(self):
        pos = [GIAPosition("BIGGAIN", 10, 50.0, 150.0), GIAPosition("LOSS", 10, 120.0, 100.0),
               GIAPosition("SMALLGAIN", 10, 95.0, 100.0)]
        p = plan_bed_and_isa(pos, 3000, 10000)
        assert [l.ticker for l in p.lines] == ["LOSS", "SMALLGAIN", "BIGGAIN"]
        assert p.lines[0].realised_gain == pytest.approx(-200)

    def test_zero_allowance_realised_loss_unlocks_an_equal_gain(self):
        # £0 CGT room. Selling L realises -£200, which lets £200 of G's gain (£50/unit => 4 units) be realised.
        p = plan_bed_and_isa([GIAPosition("G", 10, 50.0, 100.0), GIAPosition("L", 10, 120.0, 100.0)], 0, 10000)
        by = {l.ticker: l for l in p.lines}
        assert by["L"].units == 10 and by["L"].realised_gain == pytest.approx(-200)
        assert by["G"].units == pytest.approx(4)
        assert p.total_gain == pytest.approx(0, abs=1e-9)

    def test_zero_allowance_and_no_losses_does_nothing(self):
        p = plan_bed_and_isa([GIAPosition("G", 10, 50.0, 100.0)], 0, 10000)
        assert p.lines == ()

    def test_whole_shares_only(self):
        p = plan_bed_and_isa([GIAPosition("X", 100, 100.0, 133.0)], 1000, 50000, fractional=False)
        assert p.lines[0].units == 30 and p.total_gain == pytest.approx(990)

    def test_nothing_to_do(self):
        p = plan_bed_and_isa([], 3000, 1000)
        assert p.lines == () and p.limited_by == "nothing_to_do"
