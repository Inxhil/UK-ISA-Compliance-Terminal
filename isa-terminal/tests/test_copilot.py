import shutil
from datetime import datetime
from pathlib import Path

import pytest

from engine.copilot import CoPilot, explain, llm_prompt, portfolio_health, tax_text, weekly_memo
from engine.desk import Desk
from engine.isa_engine import LONDON

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    d = tmp_path_factory.mktemp("cp")
    for f in ("config.yaml", "handbook.md"):
        shutil.copy(ROOT / f, d / f)
    shutil.copytree(ROOT / "data", d / "data", ignore=shutil.ignore_patterns("samples"))
    desk = Desk(d / "config.yaml", mode="demo", now_fn=lambda: datetime(2026, 11, 18, 15, 36, tzinfo=LONDON))
    st = desk.refresh()
    return desk, st, CoPilot(desk), d


def test_health_is_bounded_and_explains_drivers(env):
    _, st, _, _ = env
    h = portfolio_health(st)
    assert 0 <= h.score <= 100 and h.grade in "ABCDE" and len(h.components) == 7
    comps = {c.name: c for c in h.components}
    assert comps["Concentration"].score < 100                    # NVDA + Technology breach in the sample
    assert comps["Data quality"].score == 0                      # demo mode
    assert comps["Cost"].score > 80 and 0 < comps["Allowance pace"].score < 100


def test_health_perfect_pace_when_allowance_used(env):
    desk, st, _, _ = env
    import copy
    st2 = copy.copy(st)
    from decimal import Decimal
    from dataclasses import replace
    st2.isa = replace(st.isa, remaining=Decimal(0), headroom=Decimal(0), replacement_credit=Decimal(0))
    assert {c.name: c for c in portfolio_health(st2).components}["Allowance pace"].score == 100


def test_memo_has_every_section_and_the_disclaimer(env):
    desk, st, _, _ = env
    m = weekly_memo(st, desk)
    for h in ("# Weekly Investment Committee Memo", "## 1. Portfolio health", "## 2. Snapshot", "## 3. Signals that need a decision",
              "## 4. Tax & allowance", "## 5. This month's buy plan", "## 6. Decisions", "## 7. Data notes"):
        assert h in m
    assert "Not FCA-regulated financial advice" in m and "DEMO DATA" in m
    assert "R-CAP-01" in m and "NVDA" in m and "£7,500.00" in m


def test_tax_text_numbers(env):
    t = tax_text(env[1])
    assert "£12,500.00 (62.5%)" in t and "Remaining £7,500.00" in t and "138d 07h" in t
    assert "£500 bonus forgone" in t and "LISA bonus £375.00" in t and "transfers £0" in t


def test_explain_cites_rules_and_data(env):
    e = explain(env[1], "nvda")
    assert "R-CAP-01" in e and "Handbook Cap" in e and "Conviction score" in e and "RSI" in e
    assert "No data for" in explain(env[1], "ZZZZ")
    assert explain(env[1], "vwrp").startswith("**VWRP.L**")          # bare symbol resolves to the .L ticker


@pytest.mark.parametrize("q,expect", [
    ("help", "Co-Pilot commands"), ("how is my portfolio health?", "Portfolio health"), ("tax", "ISA 2026/27"),
    ("what should I buy with £750", "Buy plan for £750.00"), ("buy", "Buy plan for £1,000.00"),
    ("why is NVDA flagged", "R-CAP-01"), ("explain azn", "AstraZeneca"), ("risk", "look-through"),
    ("signals", "Top 8 signals"), ("sim VUAG.L 2000", "ISA Impact Simulator — buy £2,000.00 of VUAG.L"),
    ("what if I buy 5k of NVDA", "ISA Impact Simulator — buy £5,000.00 of NVDA"), ("gibberish zzz", "didn't match"),
])
def test_router(env, q, expect):
    assert expect in env[2].respond(q)


def test_simulator_reports_cap_breach(env):
    out = env[2].respond("sim NVDA 5000")
    assert "R-CAP-01" in out and "yield" in out.lower()


def test_memo_and_prompt_are_saved_and_nothing_is_sent(env):
    desk, st, cp, d = env
    out = cp.respond("draft the weekly memo")
    assert (d / "reports" / "IC_memo_2026-11-18.md").exists() and "Saved to" in out
    out = cp.respond("export a prompt for chatgpt")
    p = d / "reports" / "llm_prompt_2026-11-18.md"
    assert p.exists() and "nothing was sent anywhere" in out
    txt = p.read_text()
    assert "NOT regulated financial advice" in txt and "handbook-rules" not in txt.split("```yaml")[0] and "max_single_position_pct" in txt
    assert "R-CAP-01" in txt


def test_no_state_yet(env):
    desk = env[0]
    fresh = CoPilot(Desk(env[3] / "config.yaml", mode="demo"))
    assert "No data loaded yet" in fresh.respond("health")


def test_buy_amount_parsing_k_suffix_and_commas(env):
    assert "Buy plan for £2,500.00" in env[2].respond("invest 2.5k")
    assert "Buy plan for £2,500.00" in env[2].respond("invest £2,500")
