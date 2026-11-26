import csv
from decimal import Decimal
from pathlib import Path

import pytest

from engine.broker_import import (BrokerImportError, ImportResult, append_ledger, detect_broker, main, names_match,
                                  parse_date, parse_file, parse_hargreaves, parse_money, parse_trading212,
                                  parse_vanguard, to_holdings, to_ledger, unit_from_header, write_holdings)
from engine.config import load_config
from engine.portfolio import HoldingRow

ROOT = Path(__file__).resolve().parent.parent
S = ROOT / "data" / "samples"
WATCH = load_config(ROOT / "config.yaml").watchlist


# ------------------------------------------------------------------ primitives
@pytest.mark.parametrize("s,v", [("-2,750.00", -2750.0), ("£1,000", 1000.0), ("(2,750.00)", -2750.0), ("1000", 1000.0),
                                 ("", None), (None, None), ("  ", None), ("-", None)])
def test_parse_money(s, v):
    assert parse_money(s) == v


def test_parse_money_rejects_garbage():
    with pytest.raises(BrokerImportError):
        parse_money("abc")


@pytest.mark.parametrize("s,d", [("2026-05-01 14:32:10", (2026, 5, 1)), ("2026-05-01", (2026, 5, 1)), ("01/05/2026", (2026, 5, 1)),
                                 ("13/05/2026", (2026, 5, 13)), ("01 May 2026", (2026, 5, 1)), ("01-05-2026", (2026, 5, 1))])
def test_parse_date_is_uk_day_first(s, d):
    assert parse_date(s).timetuple()[:3] == d


def test_slash_dates_are_never_read_as_us_month_first():
    assert parse_date("03/04/2026").month == 4          # 3 April, not 4 March


def test_parse_date_bad():
    with pytest.raises(BrokerImportError, match="unrecognised date"):
        parse_date("yesterday")


@pytest.mark.parametrize("h,u", [("Unit cost (p)", "GBp"), ("Price (pence)", "GBp"), ("Price GBX", "GBp"), ("Value (£)", "GBP"),
                                 ("Total (GBP)", "GBP"), ("Quantity", None)])
def test_unit_from_header(h, u):
    assert unit_from_header(h) == u


def test_name_matching():
    assert names_match("Vanguard FTSE All-World UCITS ETF Accumulating", "Vanguard FTSE All-World UCITS ETF (Acc)")
    assert names_match("Microsoft Corp", "Microsoft Corporation")
    assert not names_match("Vanguard S&P 500 UCITS ETF (USD) Accumulating", "Vanguard FTSE All-World UCITS ETF (Acc)")
    assert not names_match("Scottish Mortgage Investment Trust Ordinary Shares", "Vanguard FTSE All-World UCITS ETF (Acc)")
    assert not names_match("Microsoft", "Microsoft Corporation")        # single token: too weak to trust


# ------------------------------------------------------------------ Trading 212
class TestTrading212:
    @pytest.fixture
    def res(self):
        return parse_trading212(S / "t212_history_sample.csv", "T212_ISA", WATCH)

    def test_kinds_and_mapping_by_isin(self, res):
        kinds = [t.kind for t in res.txns]
        assert kinds == ["DEPOSIT", "BUY", "BUY", "BUY", "DIVIDEND", "SELL", "WITHDRAWAL", "INTEREST", "BUY"]
        assert [t.ticker for t in res.txns if t.kind in ("BUY", "SELL")] == ["VWRP.L", "MSFT", "AZN.L", "VWRP.L", None]

    def test_totals_are_gbp_and_positive_direction_in_kind(self, res):
        by = {t.note or t.raw_symbol or t.kind: t for t in res.txns}
        w = next(t for t in res.txns if t.kind == "WITHDRAWAL")
        assert w.total_gbp == 800.0
        azn = next(t for t in res.txns if t.ticker == "AZN.L")
        assert azn.total_gbp == pytest.approx(134.67) and azn.unit_cost_gbp == pytest.approx(134.67)     # GBX instrument, GBP total

    def test_pence_priced_stock_cost_basis_is_sane_pounds_not_pence(self, res):
        assert next(t for t in res.txns if t.ticker == "AZN.L").unit_cost_gbp < 1000

    def test_skips_and_warns_on_unsupported_or_non_gbp(self, res):
        txt = " ".join(res.warnings)
        assert "skipped action 'Currency conversion'" in txt and "not GBP" in txt
        assert "Unknown Holdings plc" in res.unmapped or "UNKN" in res.unmapped

    def test_average_cost_holdings(self, res):
        h = {(r.account, r.ticker): r for r in to_holdings(res.txns)}
        v = h[("T212_ISA", "VWRP.L")]
        assert v.quantity == 4 and v.avg_cost_gbp == pytest.approx(823.50 / 6, abs=1e-3)       # a sale doesn't change avg cost
        assert h[("T212_ISA", "MSFT")].avg_cost_gbp == pytest.approx(132.60 / 0.35, abs=1e-2)
        assert ("T212_ISA", "UNKN") not in h and len(h) == 3                                    # unmapped excluded, not guessed

    def test_ledger_rows(self, res):
        led = to_ledger(res.txns)
        assert [(l.type, l.amount) for l in led] == [("deposit", Decimal("1000.00")), ("withdrawal", Decimal("800.00"))]


# ------------------------------------------------------------------ Hargreaves Lansdown
class TestHargreaves:
    @pytest.fixture
    def res(self):
        return parse_hargreaves(S / "hl_transactions_sample.csv", "HL_ISA", WATCH)

    def test_classification(self, res):
        assert [t.kind for t in res.txns] == ["DEPOSIT", "BUY", "BUY", "SELL", "FEE", "INTEREST", "WITHDRAWAL", "TRANSFER_IN"]
        assert [t.ticker for t in res.txns if t.kind in ("BUY", "SELL")] == ["VWRP.L", "SMT.L", "SMT.L"]

    def test_amounts_use_absolute_value_and_thousands_commas(self, res):
        assert res.txns[0].total_gbp == 3000.0 and res.txns[1].total_gbp == 2750.0 and res.txns[-1].total_gbp == 12000.0

    def test_unit_cost_pence_is_consistent_with_value(self, res):
        assert not any("differs" in w for w in res.warnings)                 # 13750p x 20 / 100 == £2,750
        buy = res.txns[1]
        assert buy.unit_cost_gbp == pytest.approx(137.50)                    # £ per unit, derived from the total

    def test_unit_cost_mismatch_is_flagged(self, tmp_path):
        p = tmp_path / "hl.csv"
        p.write_text("Trade date,Reference,Description,Unit cost (p),Quantity,Value (£)\n"
                     "07/04/2026,B1,Vanguard FTSE All-World UCITS ETF Accumulating,137.50,20,\"-2,750.00\"\n")   # pounds given as pence
        assert any("differs" in w for w in parse_hargreaves(p, "HL_ISA", WATCH).warnings)

    def test_transfer_in_is_not_a_deposit(self, res):
        led = to_ledger(res.txns)
        assert [l.type for l in led] == ["deposit", "withdrawal", "transfer_in"]

    def test_holdings_after_buy_and_partial_sell(self, res):
        h = {r.ticker: r for r in to_holdings(res.txns)}
        assert h["VWRP.L"].quantity == 20 and h["VWRP.L"].avg_cost_gbp == pytest.approx(137.5)
        assert h["SMT.L"].quantity == 100 and h["SMT.L"].avg_cost_gbp == pytest.approx(2108.25 / 150, abs=1e-3)


# ------------------------------------------------------------------ Vanguard
class TestVanguard:
    def test_parse(self):
        res = parse_vanguard(S / "vanguard_transactions_sample.csv", "VG_ISA", WATCH)
        assert [t.kind for t in res.txns] == ["DEPOSIT", "BUY", "BUY", "SELL"]
        assert [t.ticker for t in res.txns[1:]] == ["VUAG.L"] * 3
        h = to_holdings(res.txns)[0]
        assert h.quantity == 24 and h.avg_cost_gbp == pytest.approx((1928.16 + 1083.00) / 28, abs=1e-3)


# ------------------------------------------------------------------ auto-detect, failure modes, writing
def test_autodetect_all_three():
    for f, b in [("t212_history_sample.csv", "t212"), ("hl_transactions_sample.csv", "hl"), ("vanguard_transactions_sample.csv", "vanguard")]:
        assert parse_file(S / f, "auto", "X", WATCH).broker == b


def test_unknown_layout_fails_loudly_and_lists_headers(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("foo,bar\n1,2\n")
    with pytest.raises(BrokerImportError, match=r"auto-detect.*\['foo', 'bar'\]"):
        parse_file(p, "auto", "X", WATCH)
    p.write_text("Action,Foo\nDeposit,1\n")
    with pytest.raises(BrokerImportError, match=r"required column.*Headers in file: \['Action', 'Foo'\]"):
        parse_file(p, "t212", "X", WATCH)
    with pytest.raises(BrokerImportError, match="unknown broker"):
        parse_file(p, "ibkr", "X", WATCH)


def test_empty_file_and_blank_rows(tmp_path):
    p = tmp_path / "e.csv"
    p.write_text("")
    with pytest.raises(BrokerImportError, match="empty"):
        parse_file(p, "t212", "X", WATCH)


def test_write_holdings_replaces_only_that_account_and_keeps_cash_and_backup(tmp_path):
    h = tmp_path / "holdings.csv"
    h.write_text("account,ticker,quantity,avg_cost_gbp,as_of,note\nT212_ISA,VWRP.L,1,100,2026-01-01,old\nT212_ISA,CASH,50,1,2026-01-01,\nHL_ISA,SMT.L,5,10,2026-01-01,keep\n")
    n = write_holdings(h, [HoldingRow("T212_ISA", "MSFT", 2, 300.0, "2026-11-18", "imported")], {"T212_ISA"})
    rows = list(csv.DictReader(open(h)))
    assert n == 1 and {(r["account"], r["ticker"]) for r in rows} == {("HL_ISA", "SMT.L"), ("T212_ISA", "CASH"), ("T212_ISA", "MSFT")}
    assert (tmp_path / "holdings.csv.bak").read_text().count("VWRP.L") == 1


def test_append_ledger_dedups(tmp_path):
    p = tmp_path / "c.csv"
    p.write_text("date,account,type,amount_gbp,note\n2026-05-01,T212_ISA,deposit,1000.00,x\n")
    res = parse_trading212(S / "t212_history_sample.csv", "T212_ISA", WATCH)
    assert append_ledger(p, to_ledger(res.txns)) == 1                         # the deposit already exists; withdrawal is new
    assert append_ledger(p, to_ledger(res.txns)) == 0
    from engine.isa_engine import parse_ledger_csv
    assert len(parse_ledger_csv(p)) == 2


def test_cli_is_dry_run_by_default_and_never_writes(tmp_path, capsys):
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text((ROOT / "config.yaml").read_text())
    (tmp_path / "data").mkdir()
    before = {n: (ROOT / "data" / n).read_text() for n in ("holdings.csv", "contributions.csv")}
    rc = main([str(S / "t212_history_sample.csv"), "--account", "T212_ISA", "--config", str(cfgp)])
    out = capsys.readouterr().out
    assert rc == 0 and "dry run" in out and "VWRP.L" in out and "FULL history" in out
    assert not (tmp_path / "data" / "holdings.csv").exists()
    assert {n: (ROOT / "data" / n).read_text() for n in before} == before


def test_cli_write_and_errors(tmp_path, capsys):
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text((ROOT / "config.yaml").read_text())
    (tmp_path / "data").mkdir()
    assert main([str(S / "hl_transactions_sample.csv"), "--account", "HL_ISA", "--config", str(cfgp), "--write"]) == 0
    assert "SMT.L" in (tmp_path / "data" / "holdings.csv").read_text()
    assert main([str(S / "hl_transactions_sample.csv"), "--account", "NOPE", "--config", str(cfgp)]) == 2
    bad = tmp_path / "bad.csv"
    bad.write_text("x,y\n1,2\n")
    assert main([str(bad), "--account", "HL_ISA", "--config", str(cfgp)]) == 1
    assert "IMPORT FAILED" in capsys.readouterr().err
