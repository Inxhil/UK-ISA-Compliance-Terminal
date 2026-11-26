import shutil
from pathlib import Path

import pytest

from engine.config import SLEEVES, ConfigError, load_config
from engine.config_edit import (append_watch_item, set_experience_level, set_mapping_value, set_monthly_contribution,
                                suggest_watch_item)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfgfile(tmp_path):
    p = tmp_path / "config.yaml"
    shutil.copy(ROOT / "config.yaml", p)
    return p


def test_real_config_is_the_20_ticker_universe_with_the_requested_sleeves():
    c = load_config(ROOT / "config.yaml")
    by = {}
    for w in c.watchlist:
        by.setdefault(w.sleeve, []).append(w.ticker)
    assert len(c.watchlist) == 20 and set(by) == set(SLEEVES)
    assert set(by["core_etf"]) == {"VWRP.L", "VUAG.L", "SWDA.L", "VERG.L", "VFEG.L"}
    assert set(by["uk_div"]) == {"ISF.L", "VHYL.L", "AZN.L", "SHEL.L", "HSBA.L", "ULVR.L", "REL.L"}
    assert set(by["trust"]) == {"SMT.L", "FGT.L", "JGGI.L"}
    assert set(by["global_stock"]) == {"MSFT", "NVDA", "AAPL", "GOOGL", "BRK-B"}
    assert abs(sum(w.target_weight_pct for w in c.watchlist) + c.cash_target_pct - 100) < 1e-9
    assert [t for t, v in c.targets.items()] == ["VWRP.L", "VUAG.L", "SMT.L", "AZN.L", "MSFT", "NVDA"] or set(c.targets) == {"VWRP.L", "VUAG.L", "SMT.L", "AZN.L", "MSFT", "NVDA"}
    assert c.watch("GOOGL").aliases == ("Google",) and c.experience_level == "advanced"


def test_bad_sleeve_and_bad_level_fail_loudly(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("cash_target_pct: 0\nwatchlist:\n  - {ticker: A, target_weight_pct: 100, sleeve: moonshots}\n")
    with pytest.raises(ConfigError, match="sleeve"):
        load_config(p)
    p.write_text("ui:\n  experience_level: wizard\n")
    with pytest.raises(ConfigError, match="experience_level"):
        load_config(p)


def test_set_experience_level_preserves_comments_and_everything_else(cfgfile):
    before = cfgfile.read_text()
    set_experience_level(cfgfile, "Beginner")
    after = cfgfile.read_text()
    assert load_config(cfgfile).experience_level == "beginner"
    assert "experience_level: beginner" in after and "# beginner | intermediate | advanced" in after   # trailing comment kept
    diff = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    assert len(diff) == 1 and "experience_level" in diff[0][0]
    assert cfgfile.with_suffix(".yaml.bak").read_text() == before
    for lvl in ("intermediate", "advanced"):
        set_experience_level(cfgfile, lvl)
        assert load_config(cfgfile).experience_level == lvl
    with pytest.raises(ConfigError):
        set_experience_level(cfgfile, "expert")


def test_set_value_creates_missing_section_and_key(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("# top comment\nprofile:\n  monthly_contribution_gbp: 1000\n")
    set_experience_level(p, "intermediate")                       # no ui: section yet
    assert load_config(p).experience_level == "intermediate" and p.read_text().startswith("# top comment")
    set_mapping_value(p, "ui", "refresh_seconds", 30)             # existing section, new key
    assert load_config(p).refresh_seconds == 30
    set_monthly_contribution(p, 750)
    assert load_config(p).monthly_contribution_gbp == 750
    with pytest.raises(ConfigError):
        set_monthly_contribution(p, -5)


def test_append_watch_item_keeps_comments_targets_and_validity(cfgfile):
    before = cfgfile.read_text()
    append_watch_item(cfgfile, {"ticker": "BARC.L", "name": "Barclays PLC", "kind": "stock", "sleeve": "uk_div", "sector": "Financial Services"})
    c = load_config(cfgfile)
    w = c.watch("BARC.L")
    assert len(c.watchlist) == 21 and w.target_weight_pct == 0 and w.sleeve == "uk_div" and w.sector == "Financial Services"
    after = cfgfile.read_text()
    assert after.startswith(before.rstrip("\n")[:200]) and "# added from the terminal search bar" in after
    assert all(line in after for line in before.splitlines() if line.strip().startswith("#"))
    assert abs(sum(x.target_weight_pct for x in c.watchlist) + c.cash_target_pct - 100) < 1e-9


def test_append_watch_item_rejects_duplicates_bad_sleeve_and_rolls_back(cfgfile):
    with pytest.raises(ConfigError, match="already"):
        append_watch_item(cfgfile, {"ticker": "MSFT", "name": "x", "kind": "stock"})
    with pytest.raises(ConfigError, match="sleeve"):
        append_watch_item(cfgfile, {"ticker": "ZZZ", "name": "x", "kind": "stock", "sleeve": "nope"})
    original = cfgfile.read_text()
    with pytest.raises(Exception):                                # kind that breaks nothing but sleeve invalid after write? force a failure
        append_watch_item(cfgfile, {"ticker": "TST", "name": "x", "kind": "stock", "sleeve": "uk_div", "ter_pct": "not-a-number"})
    assert cfgfile.read_text() == original and len(load_config(cfgfile).watchlist) == 20           # rolled back


def test_append_handles_names_with_yaml_special_characters(cfgfile):
    append_watch_item(cfgfile, {"ticker": "BT-A.L", "name": "BT Group: Class #A [test] 'quoted'", "kind": "stock", "sleeve": "uk_div"})
    assert load_config(cfgfile).watch("BT-A.L").name == "BT Group: Class #A [test] 'quoted'"


@pytest.mark.parametrize("t,name,qt,sector,exp_kind,exp_sleeve", [
    ("BARC.L", "Barclays PLC", "EQUITY", "Financial Services", "stock", "uk_div"),
    ("TSLA", "Tesla, Inc.", "EQUITY", "Consumer Cyclical", "stock", "global_stock"),
    ("IWDA.L", "iShares Core MSCI World UCITS ETF", "ETF", None, "etf", "core_etf"),
    ("VMID.L", "Vanguard FTSE 250 UCITS ETF", "EQUITY", None, "etf", "core_etf"),            # name says UCITS ETF even if the feed says EQUITY
    ("BNKR.L", "Bankers Investment Trust Ord", "EQUITY", None, "trust", "trust"),
    ("PCT.L", "Polar Capital Technology Trust plc", "EQUITY", None, "trust", "trust"),
    ("TRUSTCO", "Trustpilot Group", "EQUITY", None, "stock", "global_stock"),                  # 'Trust' inside a word is not a trust
])
def test_suggest_watch_item(t, name, qt, sector, exp_kind, exp_sleeve):
    s = suggest_watch_item(t, name=name, quote_type=qt, sector=sector, currency=None)
    assert (s["kind"], s["sleeve"]) == (exp_kind, exp_sleeve) and s["ticker"] == t
    assert ("sector" in s) == bool(sector and exp_kind == "stock")
