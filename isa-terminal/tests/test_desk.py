"""End-to-end Desk in DEMO mode (no network): files -> engines -> signals -> buy plan."""
import shutil
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from engine.desk import Desk
from engine.isa_engine import LONDON

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    d = tmp_path_factory.mktemp("proj")
    for f in ("config.yaml", "handbook.md"):
        shutil.copy(ROOT / f, d / f)
    shutil.copytree(ROOT / "data", d / "data", ignore=shutil.ignore_patterns("samples"))
    return d


@pytest.fixture(scope="module")
def state(project):
    desk = Desk(project / "config.yaml", mode="demo", now_fn=lambda: datetime(2026, 11, 18, 15, 36, tzinfo=LONDON))
    return desk, desk.refresh()


def test_demo_state_is_flagged_everywhere(state):
    _, st = state
    assert st.data_label == "DEMO" and all(q.source == "demo" for q in st.quotes.values())
    assert any(s.action == "DATA WARNING" and "NOT actionable" in s.headline for s in st.signals)
    assert all(t.source == "demo" for t in st.tape)


def test_headline_numbers_for_sample_files(state):
    _, st = state
    assert float(st.isa.headroom) == 7500 and st.countdown.days == 138 and st.isa.tax_year.label == "2026/27"
    assert st.portfolio.cash_gbp == 1900 and len(st.portfolio.gia) == 2
    assert st.portfolio.total_gbp == pytest.approx(st.portfolio.invested_gbp + 1900)


def test_buy_plan_is_penny_exact_no_sells_and_respects_blocks_and_caps(state):
    desk, st = state
    plan = st.plan
    assert plan.deployed == Decimal("1000.00") and all(o.amount > 0 for o in plan.orders)
    blocked = {s.ticker for s in st.signals if s.action in ("TRIM WARNING", "STOP ADDING", "REVIEW") and s.ticker}
    blocked |= {s.ticker for s in st.signals if s.metrics.get("blocks_buy")}
    assert blocked and not blocked & {o.ticker for o in plan.orders}
    assert "NVDA" in blocked
    big = desk.buy_plan(st, 5000)
    for o in big.orders:
        w = next(x for x in st.config.watchlist if x.ticker == o.ticker)
        if w.kind in st.rules.position.cap_applies_to:
            assert o.post_weight <= 0.15 + 1e-9


def test_buy_plan_clamps_to_allowance_headroom(state):
    desk, st = state
    plan = desk.buy_plan(st, 9000)
    assert plan.cash_in == Decimal("7500.00") and any("exceeds ISA allowance" in w for w in plan.warnings)


def test_orders_carry_the_reason_from_the_signal(state):
    _, st = state
    assert all(o.reason.startswith(("ACCUMULATE", "HOLD OFF", "DEPLOY")) for o in st.plan.orders if o.reason)
    assert any(o.reason for o in st.plan.orders)


def test_simulator_via_desk(state):
    desk, st = state
    r = desk.simulate(st, "VUAG.L", 1000)
    assert r.after.total_gbp == pytest.approx(r.before.total_gbp + 1000) and r.allowance_after_gbp == pytest.approx(6500)


def test_editing_handbook_changes_signals_on_refresh(project):
    desk = Desk(project / "config.yaml", mode="demo", now_fn=lambda: datetime(2026, 11, 18, 15, 36, tzinfo=LONDON))
    assert any(s.action == "TRIM WARNING" and s.ticker == "NVDA" for s in desk.refresh().signals)
    hb = project / "handbook.md"
    orig = hb.read_text()
    try:
        hb.write_text(orig.replace("max_single_position_pct: 15 ", "max_single_position_pct: 25 "))
        assert not any(s.action == "TRIM WARNING" and s.ticker == "NVDA" for s in desk.refresh().signals)
    finally:
        hb.write_text(orig)


def test_progress_callback(project):
    seen = []
    Desk(project / "config.yaml", mode="demo").refresh(progress=lambda stage, i, n: seen.append(stage))
    assert seen[0] == "quotes" and any(s.startswith("screen ") for s in seen) and seen[-1] == "tape"


def test_every_watchlist_ticker_gets_a_complete_snapshot_in_demo_mode(state):
    """Regression: a demo-data gap (missing key) silently produced a bare snapshot for BRK-B with a bogus score of 100."""
    _, st = state
    assert len(st.snaps) >= 20
    for t in st.config.tickers:
        s = st.snaps[t]
        assert not any("screen failed" in n for n in s.notes), (t, s.notes)
        assert s.price_gbp is not None and s.rsi14 is not None and s.sma200 is not None, t
    assert st.scores["BRK-B"].total is None or st.scores["BRK-B"].total < 100
