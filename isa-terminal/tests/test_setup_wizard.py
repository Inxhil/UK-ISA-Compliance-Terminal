import csv
import os
import shutil
import stat
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

import setup_isa as W
from engine.config import load_config
from engine.isa_engine import LONDON, AccountSpec, compute_isa_status, parse_ledger_csv

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=LONDON)
TD, FH = "td0123456789abcdefABCDEF012345", "fh0123456789abcdefABCDEF0123456789abcd"          # fake, format-valid keys


@pytest.fixture
def proj(tmp_path):
    for f in ("config.yaml", "handbook.md"):
        shutil.copy(ROOT / f, tmp_path / f)
    shutil.copytree(ROOT / "data", tmp_path / "data", ignore=shutil.ignore_patterns("samples"))
    return tmp_path


class Script:
    """Scripted conversation: feeds answers to input()/getpass() and records everything printed."""
    def __init__(self, typed=(), hidden=()):
        self.typed, self.hidden, self.out, self.prompts = list(typed), list(hidden), [], []

    def input(self, prompt=""):
        self.prompts.append(prompt)
        return self.typed.pop(0)

    def getpass(self, prompt=""):
        self.prompts.append(prompt)
        return self.hidden.pop(0)

    def say(self, s):
        self.out.append(s)

    @property
    def transcript(self):
        return "\n".join(self.out + self.prompts)


def run(proj, **kw):
    s = Script(kw.pop("typed", ()), kw.pop("hidden", ()))
    res = W.run_wizard(proj, input_fn=s.input, getpass_fn=s.getpass, out=s.say, now=NOW, **kw)
    return res, s


def status(proj):
    cfg = load_config(proj / "config.yaml")
    accts = {a.id: AccountSpec(a.id, a.kind, a.flexible) for a in cfg.accounts.values()}
    return compute_isa_status(parse_ledger_csv(cfg.contributions_csv), accts, as_of=NOW)


# ------------------------------------------------------------------------------------------- parsing
@pytest.mark.parametrize("t,exp", [("1", "beginner"), ("2", "intermediate"), ("3", "advanced"), ("beg", "beginner"), ("INT", "intermediate"),
                                   ("Advanced", "advanced"), ("", "advanced"), ("  adv ", "advanced")])
def test_parse_level(t, exp):
    assert W.parse_level(t) == exp


@pytest.mark.parametrize("bad", ["4", "0", "x", "b e", "expert"])
def test_parse_level_rejects(bad):
    with pytest.raises(W.WizardError):
        W.parse_level(bad)


@pytest.mark.parametrize("t,exp", [("7500", "7500.00"), ("£7,500", "7500.00"), (" 1 234.5 ", "1234.50"), ("", None), ("0", "0.00")])
def test_parse_money(t, exp):
    assert W.parse_money(t) == (Decimal(exp) if exp else None)


@pytest.mark.parametrize("bad", ["-5", "abc", "12x", "£-1"])
def test_parse_money_rejects(bad):
    with pytest.raises(W.WizardError):
        W.parse_money(bad)


def test_key_validation_and_masking():
    assert W.valid_key(TD) and W.valid_key(FH) and not W.valid_key("short") and not W.valid_key("has space in it 1234567890")
    assert W.mask("abcdef123456") == "••••••56" and TD not in W.mask(TD) and W.mask("ab") == "set"


# ------------------------------------------------------------------------------------------- full conversations
def test_guided_run_sets_everything_and_the_engine_reads_it_back(proj):
    # level=Beginner, remaining £7,500 -> T212_ISA (default), LISA £1,000, keep sample holdings, monthly £600, keys via hidden prompt
    res, s = run(proj, typed=["1", "7500", "", "1000", "n", "600"], hidden=[TD, FH])
    cfg = load_config(proj / "config.yaml")
    assert cfg.experience_level == "beginner" and cfg.monthly_contribution_gbp == 600
    st = status(proj)
    assert st.remaining == Decimal("7500.00") and st.net_used == Decimal("12500.00") and not st.has_breach     # exactly what was typed
    assert st.lisa_subscriptions == Decimal("1000.00") and st.lisa_remaining == Decimal("3000.00")
    rows = list(csv.DictReader(open(proj / "data" / "contributions.csv")))
    assert [(r["account"], r["amount_gbp"], r["date"]) for r in rows] == [("T212_ISA", "11500.00", "2026-04-06"), ("HL_LISA", "1000.00", "2026-04-06")]
    assert (proj / "data" / "contributions.csv.bak").exists()                                                    # old ledger kept
    assert "AZN.L" in (proj / "data" / "holdings.csv").read_text()                                                # sample holdings untouched
    env = W.read_env_keys(proj / ".env")
    assert env == {"TWELVEDATA_API_KEY": TD, "FINNHUB_API_KEY": FH} and stat.S_IMODE(os.stat(proj / ".env").st_mode) == 0o600
    assert res.level == "beginner" and res.used == Decimal("12500.00") and set(res.keys_set) == {"TWELVEDATA_API_KEY", "FINNHUB_API_KEY"}


def test_api_keys_are_never_echoed_anywhere(proj):
    res, s = run(proj, typed=["3", "", "", ""], hidden=[TD, FH])
    blob = s.transcript + repr(res) + "\n".join(str(a) for a in res.actions)
    assert TD not in blob and FH not in blob
    assert "hidden" in blob and "mode 600" in blob


def test_enter_everywhere_changes_nothing_that_matters(proj):
    before = {p.name: p.read_text() for p in (proj / "data").iterdir()}
    cfg_before = load_config(proj / "config.yaml")
    res, s = run(proj, typed=["", "", "", ""], hidden=["", ""])
    assert {p.name: p.read_text() for p in (proj / "data").iterdir()} == before            # ledger/holdings untouched, no .bak
    assert not (proj / ".env").exists() and not res.keys_set
    assert load_config(proj / "config.yaml").experience_level == cfg_before.experience_level
    assert "allowance (kept existing ledger)" in res.skipped and "Twelve Data key" in res.skipped


def test_invalid_answers_reprompt_instead_of_crashing(proj):
    res, s = run(proj, typed=["wizard", "9", "2", "25000", "-5", "abc", "7500", "NOPE", "T212_ISA", "99999", "50000", "0", "n", "1k", "500"],
                 hidden=["", ""])
    txt = s.transcript
    assert "is not a level" in txt and "can't be more than the £20,000" in txt and "can't be negative" in txt and "is not an amount" in txt
    assert "choose one of" in txt and "can't be more than the £" in txt
    assert load_config(proj / "config.yaml").experience_level == "intermediate" and status(proj).remaining == Decimal("7500.00")
    assert load_config(proj / "config.yaml").monthly_contribution_gbp == 500


def test_too_many_bad_answers_skips_the_step(proj):
    res, s = run(proj, typed=["x", "y", "z", "q", "", "", ""], hidden=["", ""])
    assert load_config(proj / "config.yaml").experience_level == "advanced"                 # unchanged default
    assert "too many invalid answers" in s.transcript


def test_fresh_portfolio_backs_up_the_sample_and_writes_an_empty_file(proj):
    # level, remaining=full limit, account, (no LISA question: nothing used), fresh portfolio = y, monthly = Enter
    run(proj, typed=["", "20000", "", "y", ""], hidden=["", ""])
    h = list(csv.reader(open(proj / "data" / "holdings.csv")))
    assert h == [["account", "ticker", "quantity", "avg_cost_gbp", "as_of", "note"]]
    assert "VWRP.L" in (proj / "data" / "holdings.csv.bak").read_text()
    st = status(proj)
    assert st.remaining == Decimal("20000.00") and st.net_used == 0                         # remaining = full limit -> nothing used
    assert list(csv.DictReader(open(proj / "data" / "contributions.csv"))) == []


def test_fully_used_allowance_and_no_lisa_account_prompt(proj):
    res, s = run(proj, typed=["", "0", "", "", "n", ""], hidden=["", ""])
    assert status(proj).remaining == 0 and status(proj).net_used == Decimal("20000.00")
    assert any("Lifetime ISA" in p for p in s.prompts)


def test_existing_env_is_merged_and_masked_not_overwritten(proj):
    (proj / ".env").write_text("# my keys\nTWELVEDATA_API_KEY=oldoldoldoldoldoldold12\nOTHER_SETTING=keepme\n")
    res, s = run(proj, typed=["", "", "", ""], hidden=["", FH])                                # keep TD (Enter), set Finnhub
    env = W.read_env_keys(proj / ".env")
    assert env["TWELVEDATA_API_KEY"] == "oldoldoldoldoldoldold12" and env["FINNHUB_API_KEY"] == FH and env["OTHER_SETTING"] == "keepme"
    assert (proj / ".env").read_text().startswith("# my keys\n")
    assert "already set" in s.transcript and "oldoldoldoldoldoldold12" not in s.transcript


def test_malformed_key_is_rejected_and_not_saved(proj):
    res, s = run(proj, typed=["", "", "", ""], hidden=["not a key!", ""])
    assert not (proj / ".env").exists() and "Twelve Data key (invalid format)" in res.skipped
    assert "doesn't look like a Twelve Data key" in s.transcript


# ------------------------------------------------------------------------------------------- non-interactive + CLI
def test_non_interactive_applies_only_the_given_flags(proj):
    res = W.run_wizard(proj, answers={"level": "beginner", "remaining": "12000", "monthly": "250"}, interactive=False, out=lambda s: None, now=NOW)
    cfg = load_config(proj / "config.yaml")
    assert cfg.experience_level == "beginner" and cfg.monthly_contribution_gbp == 250 and status(proj).remaining == Decimal("12000.00")
    assert not (proj / ".env").exists() and not res.keys_set


def test_cli_main_end_to_end_and_error_codes(proj, capsys):
    rc = W.main(["--non-interactive", "--level", "intermediate", "--remaining", "£5,000", "--monthly", "400", "--fresh-portfolio",
                 "--finnhub-key", FH], root=proj, out=lambda s: None)
    assert rc == 0
    cfg = load_config(proj / "config.yaml")
    assert cfg.experience_level == "intermediate" and status(proj).remaining == Decimal("5000.00")
    assert W.read_env_keys(proj / ".env") == {"FINNHUB_API_KEY": FH}
    assert W.main(["--non-interactive", "--remaining", "99999"], root=proj, out=lambda s: None) == 1
    assert "can't be more than" in capsys.readouterr().err
    assert W.main(["--non-interactive", "--account", "NOPE", "--remaining", "100"], root=proj, out=lambda s: None) == 1


def test_ctrl_c_cancels_cleanly_without_a_traceback(proj, capsys):
    def boom(_):
        raise KeyboardInterrupt
    assert W.main([], root=proj, input_fn=boom, getpass_fn=boom, out=lambda s: None) == 130
    assert "cancelled" in capsys.readouterr().err


def test_wizard_never_writes_into_the_real_project(proj):
    real = {p: p.stat().st_mtime for p in (ROOT / "config.yaml", ROOT / "data" / "holdings.csv", ROOT / "data" / "contributions.csv")}
    run(proj, typed=["1", "100", "", "", "y", "100"], hidden=[TD, ""])
    real_env = ROOT / ".env"                      # git-ignored, so absent on a fresh checkout / unzip
    leaked = real_env.exists() and "td0123456789" in real_env.read_text()
    assert {p: p.stat().st_mtime for p in real} == real and not leaked
