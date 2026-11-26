import pytest

from engine.cache import DiskCache
from engine.config import ConfigError, load_config, load_env, load_handbook_rules, parse_handbook_rules

import pandas as pd


def test_real_handbook_rules_block_parses():
    r = load_handbook_rules()
    assert r.position.max_single_position_pct == 15
    assert r.costs.max_ocf_pct_passive == 0.25 and r.quality.min_dividend_cover == 1.5
    assert r.tax.alert_days == (90, 30, 7)
    assert [t.drop_pct for t in r.drawdown.tiers] == [10, 20, 30]
    assert sum(t.deploy_pct_of_reserve for t in r.drawdown.tiers) == 100
    assert r.drawdown.reference_ticker == "VWRP.L"
    assert r.position.cap_applies_to == ("stock", "trust")


def test_real_config_targets_sum_to_100_and_accounts_load():
    cfg = load_config()
    assert abs(sum(w.target_weight_pct for w in cfg.watchlist) + cfg.cash_target_pct - 100) < 1e-9
    assert {"VWRP.L", "VUAG.L", "SMT.L", "AZN.L", "MSFT", "NVDA"} <= set(cfg.tickers)
    assert cfg.accounts["HL_LISA"].kind == "LISA" and cfg.accounts["T212_ISA"].flexible
    assert cfg.accounts["HL_GIA"].is_isa is False
    assert cfg.targets["VWRP.L"] == pytest.approx(0.35) and "ISF.L" not in cfg.targets
    assert cfg.holdings_csv.exists() and cfg.contributions_csv.exists()


def test_missing_block_gives_defaults():
    assert parse_handbook_rules("# nothing here").position.max_single_position_pct == 15


def test_partial_override_and_validation():
    md = "```yaml handbook-rules\nposition:\n  max_single_position_pct: 10\n```"
    r = parse_handbook_rules(md)
    assert r.position.max_single_position_pct == 10 and r.position.max_sector_pct == 40   # others default


@pytest.mark.parametrize("block,msg", [
    ("position:\n  max_single_position_pct: 0", "must be in"),
    ("position:\n  typo_key: 1", "unknown key"),
    ("bogus_section:\n  a: 1", "unknown section"),
    ("position: [1, 2]", "must be a mapping"),
    ("position: {max_single_position_pct: 5\n", "not valid YAML"),
])
def test_bad_rules_fail_loudly(block, msg):
    with pytest.raises(ConfigError, match=msg):
        parse_handbook_rules(f"```yaml handbook-rules\n{block}\n```")


def test_bad_target_sum_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("cash_target_pct: 5\nwatchlist:\n  - {ticker: A, target_weight_pct: 50}\n")
    with pytest.raises(ConfigError, match="sum to 100"):
        load_config(p)


def test_duplicate_ticker_and_bad_account_kind(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("watchlist:\n  - {ticker: A}\n  - {ticker: A}\n")
    with pytest.raises(ConfigError, match="duplicate ticker"):
        load_config(p)
    p.write_text("accounts:\n  - {id: X, kind: PENSION}\n")
    with pytest.raises(ConfigError, match="kind"):
        load_config(p)


def test_env_loader_does_not_override_or_leak(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text('# c\nFINNHUB_API_KEY="abc123"\nEMPTY=\nOTHER=x=y\n')
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.setenv("OTHER", "keep")
    got = load_env(p)
    assert got["FINNHUB_API_KEY"] == "abc123" and "EMPTY" not in got
    import os
    assert os.environ["OTHER"] == "keep" and os.environ["FINNHUB_API_KEY"] == "abc123"
    assert load_env(tmp_path / "missing") == {}


def test_cache_ttl_stale_and_dataframe_roundtrip(tmp_path, monkeypatch):
    c = DiskCache(tmp_path)
    assert c.get("k", 10) is None
    c.set("k", {"a": 1})
    hit = c.get("k", 10)
    assert hit.fresh and hit.value == {"a": 1}
    stale = c.get("k", -1)
    assert stale is not None and not stale.fresh and stale.value == {"a": 1}
    df = pd.DataFrame({"Close": [1.0, 2.0]}, index=pd.to_datetime(["2026-01-01", "2026-01-02"]))
    c.set("df/X", df)
    back = c.get("df/X", 10).value
    assert list(back["Close"]) == [1.0, 2.0] and isinstance(back.index, pd.DatetimeIndex)
    (tmp_path / next(f for f in __import__("os").listdir(tmp_path) if f.startswith("k."))).write_text("{corrupt")
    assert c.get("k", 10) is None                       # corrupt file => miss, not crash


def test_cap_applies_to_validation():
    r = parse_handbook_rules("```yaml handbook-rules\nposition:\n  cap_applies_to: [stock, trust, etf]\n```")
    assert r.position.cap_applies_to == ("stock", "trust", "etf")
    with pytest.raises(ConfigError, match="cap_applies_to"):
        parse_handbook_rules("```yaml handbook-rules\nposition:\n  cap_applies_to: [crypto]\n```")
