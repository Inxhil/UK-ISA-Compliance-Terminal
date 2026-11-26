#!/usr/bin/env python3
"""First-run wizard for the UK ISA Command Centre.

    python setup_isa.py                       # guided prompts
    python setup_isa.py --non-interactive --level beginner --remaining 7500 --monthly 500   # scripted

It configures, in order: your experience level, how much of this tax year's ISA allowance is left, your monthly
contribution, and (optionally) the Twelve Data / Finnhub API keys. Nothing is required: press Enter to skip any step.

Safety: existing files are backed up (``*.bak``) before they are changed, API keys are typed hidden and stored only in
``.env`` (mode 600, git-ignored), and nothing here ever prints a key back.

Quantitative modelling and personal handbook tool only. Not FCA-regulated financial advice.
"""
from __future__ import annotations

import argparse
import csv
import getpass
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from engine import DISCLAIMER, __version__
from engine.config import EXPERIENCE_LEVELS, ConfigError, load_config
from engine.config_edit import set_experience_level, set_monthly_contribution
from engine.isa_engine import now_london, rules_for, tax_year_for

LEVEL_HELP = {"beginner": "plain English, no jargon, a visual allowance bar",
              "intermediate": "yield, P/E, analyst view and the monthly buy planner",
              "advanced": "the full Bloomberg-style density: technicals, rule IDs, simulator"}
KEY_NAMES = {"td": "TWELVEDATA_API_KEY", "fh": "FINNHUB_API_KEY"}
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{16,64}$")


class WizardError(ValueError):
    pass


# =============================================================================================== pure parsing helpers
def parse_level(text: str | None, default: str = "advanced") -> str:
    """'1'/'2'/'3', a full name, or an unambiguous prefix ('beg', 'int', 'adv'). Blank => default."""
    t = (text or "").strip().lower()
    if not t:
        return default
    if t in ("1", "2", "3"):
        return EXPERIENCE_LEVELS[int(t) - 1]
    hits = [l for l in EXPERIENCE_LEVELS if l.startswith(t)]
    if len(hits) == 1:
        return hits[0]
    raise WizardError(f"'{text}' is not a level — type 1, 2, 3 or beginner / intermediate / advanced")


def parse_money(text: str | None) -> Decimal | None:
    """'£7,500' / '7500.50' -> Decimal; blank -> None; negatives and junk raise."""
    t = (text or "").strip().replace("£", "").replace(",", "").replace(" ", "")
    if not t:
        return None
    try:
        v = Decimal(t)
    except InvalidOperation:
        raise WizardError(f"'{text}' is not an amount — e.g. 7500 or £7,500") from None
    if v < 0:
        raise WizardError("amount can't be negative")
    return v.quantize(Decimal("0.01"))


def valid_key(k: str) -> bool:
    return bool(KEY_PATTERN.match(k.strip()))


def mask(secret: str) -> str:
    return "•" * 6 + secret[-2:] if len(secret) > 4 else "set"


# =============================================================================================== file operations
def write_env_keys(path: Path, updates: Mapping[str, str]) -> None:
    """Merge ``updates`` into ``.env`` (keeps unrelated lines and comments), chmod 600. Values are never logged."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    done: set[str] = set()
    out: list[str] = []
    for line in lines:
        k = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and k in updates:
            out.append(f"{k}={updates[k]}")
            done.add(k)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in updates.items() if k not in done]
    path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def read_env_keys(path: Path) -> dict[str, str]:
    keys: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                keys[k.strip()] = v.strip().strip('"').strip("'")
    return keys


def _backup(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))


def write_opening_ledger(path: Path, rows: Sequence[tuple[date, str, Decimal, str]]) -> None:
    """Replace contributions.csv with opening-balance rows (backup first)."""
    _backup(path)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "account", "type", "amount_gbp", "note"])
        for d, acct, amt, note in rows:
            w.writerow([d.isoformat(), acct, "deposit", f"{amt:.2f}", note])


def write_empty_holdings(path: Path) -> None:
    _backup(path)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerow(["account", "ticker", "quantity", "avg_cost_gbp", "as_of", "note"])


# =============================================================================================== the wizard
@dataclass
class WizardResult:
    actions: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    level: str | None = None
    used: Decimal | None = None
    keys_set: list[str] = field(default_factory=list)


def run_wizard(root: Path, *, answers: Mapping[str, Any] | None = None, interactive: bool = True,
               input_fn: Callable[[str], str] = input, getpass_fn: Callable[[str], str] = getpass.getpass,
               out: Callable[[str], None] = print, now: datetime | None = None) -> WizardResult:
    """Run the setup. ``answers`` pre-fills (and suppresses) prompts: level, remaining, lisa_used, account, monthly,
    fresh_portfolio, td_key, fh_key. With ``interactive=False`` anything not pre-filled is skipped."""
    ans = dict(answers or {})
    root = Path(root)
    cfg_path = root / "config.yaml"
    env_path = root / ".env"
    res = WizardResult()
    cfg = load_config(cfg_path)
    ty = tax_year_for(now or now_london())
    rules = rules_for(ty.start_year)

    def ask(key: str, prompt: str, parse: Callable[[str], Any], default: Any = None, retries: int = 4):
        if key in ans:
            return parse(str(ans[key])) if ans[key] is not None else default
        if not interactive:
            return default
        for _ in range(retries):
            try:
                return parse(input_fn(prompt))
            except WizardError as exc:
                out(f"  ✗ {exc}")
        out("  (too many invalid answers — skipping this step)")
        return default

    out(f"\nUK ISA Command Centre v{__version__} — setup\n{'─' * 64}\n{DISCLAIMER}\n")

    # 1 ── experience level ------------------------------------------------------------------------
    out("1/4  Experience level — how much detail do you want?")
    for i, l in enumerate(EXPERIENCE_LEVELS, 1):
        out(f"     {i}) {l.title():<13} {LEVEL_HELP[l]}")
    level = ask("level", f"     Choose 1-3 [{cfg.experience_level}]: ", lambda t: parse_level(t, cfg.experience_level), cfg.experience_level)
    if level != cfg.experience_level or "level" in ans:
        set_experience_level(cfg_path, level)
        res.actions.append(f"experience level → {level}")
    res.level = level

    # 2 ── remaining allowance ---------------------------------------------------------------------
    limit = rules.annual_limit
    out(f"\n2/4  ISA allowance for {ty.label} (limit £{limit:,.0f}, all ISAs combined, ends 5 April {ty.end.year})")
    remaining = ask("remaining", f"     How much is STILL REMAINING to use? (£, Enter = keep my current records): ",
                    lambda t: _check_remaining(parse_money(t), limit))
    if remaining is None:
        res.skipped.append("allowance (kept existing ledger)")
    else:
        used = limit - remaining
        isa_accts = [a.id for a in cfg.accounts.values() if a.kind in ("SS_ISA", "CASH_ISA", "IFISA")]
        lisa_accts = [a.id for a in cfg.accounts.values() if a.kind == "LISA"]
        if not isa_accts:
            raise WizardError("config.yaml has no ISA account — add one under `accounts:` first")
        acct = ask("account", f"     Which account holds it? {isa_accts} [{isa_accts[0]}]: ",
                   lambda t: _pick(t, isa_accts, isa_accts[0]), isa_accts[0])
        lisa_used = Decimal("0")
        if lisa_accts and used > 0:
            lisa_used = ask("lisa_used", f"     Of the £{used:,.2f} used, how much went into your Lifetime ISA? (£, Enter = none): ",
                            lambda t: _check_lisa(parse_money(t), used, rules.lisa_limit), Decimal("0")) or Decimal("0")
        rows: list[tuple[date, str, Decimal, str]] = []
        note = f"Opening balance from setup wizard: {ty.label} allowance already used"
        if used - lisa_used > 0:
            rows.append((ty.start, acct, used - lisa_used, note))
        if lisa_used > 0:
            rows.append((ty.start, lisa_accts[0], lisa_used, note + " (LISA)"))
        write_opening_ledger(root / "data" / "contributions.csv", rows)
        res.used = used
        res.actions.append(f"ledger → £{used:,.2f} used, £{remaining:,.2f} remaining for {ty.label} (previous file saved as .bak)")
        fresh = ask("fresh_portfolio", "     Replace the SAMPLE holdings with an empty portfolio? [y/N]: ", _yes, False)
        if fresh:
            write_empty_holdings(root / "data" / "holdings.csv")
            res.actions.append("holdings.csv emptied (sample data saved as .bak) — add yours, or import a broker CSV")

    # 3 ── monthly contribution --------------------------------------------------------------------
    out("\n3/4  Monthly investing")
    monthly = ask("monthly", f"     How much do you plan to invest each month? (£, Enter keeps £{cfg.monthly_contribution_gbp:,.0f}): ",
                  lambda t: parse_money(t))
    if monthly is not None:
        set_monthly_contribution(cfg_path, float(monthly))
        res.actions.append(f"monthly contribution → £{monthly:,.2f}")
    else:
        res.skipped.append("monthly contribution")

    # 4 ── API keys (optional) -----------------------------------------------------------------------
    out("\n4/4  Market-data API keys (optional — the app works without them using Yahoo Finance)")
    out("     Twelve Data: free key at twelvedata.com · Finnhub: free key at finnhub.io. Typed hidden; stored only in .env (mode 600).")
    existing = read_env_keys(env_path)
    updates: dict[str, str] = {}
    for short, label in (("td", "Twelve Data"), ("fh", "Finnhub")):
        name = KEY_NAMES[short]
        have = f" (already set {mask(existing[name])}; Enter keeps it)" if existing.get(name) else " (Enter to skip)"
        raw = ans.get(f"{short}_key")
        if raw is None and interactive:
            raw = getpass_fn(f"     {label} API key{have}: ")
        raw = (raw or "").strip()
        if not raw:
            res.skipped.append(f"{label} key")
            continue
        if not valid_key(raw):
            out(f"  ✗ that doesn't look like a {label} key (16-64 letters/digits) — not saved")
            res.skipped.append(f"{label} key (invalid format)")
            continue
        updates[name] = raw
        res.keys_set.append(name)
    if updates:
        write_env_keys(env_path, updates)
        res.actions.append("API key(s) saved to .env: " + ", ".join(res.keys_set) + " (hidden, mode 600)")

    out(f"\n{'─' * 64}\nDone.")
    for a in res.actions:
        out(f"  ✓ {a}")
    for s in res.skipped:
        out(f"  – skipped: {s}")
    out("\nNext:  python terminal.py        (press `l` any time to change level, `?` for help)\n")
    return res


def _check_remaining(v: Decimal | None, limit: Decimal) -> Decimal | None:
    if v is not None and v > limit:
        raise WizardError(f"remaining can't be more than the £{limit:,.0f} allowance")
    return v


def _check_lisa(v: Decimal | None, used: Decimal, lisa_limit: Decimal) -> Decimal | None:
    if v is not None and v > used:
        raise WizardError(f"can't be more than the £{used:,.2f} you've used in total")
    if v is not None and v > lisa_limit:
        raise WizardError(f"the Lifetime ISA limit is £{lisa_limit:,.0f} a year")
    return v


def _pick(text: str, options: Sequence[str], default: str) -> str:
    t = text.strip()
    if not t:
        return default
    hit = next((o for o in options if o.lower() == t.lower()), None)
    if hit is None:
        raise WizardError(f"choose one of {list(options)}")
    return hit


def _yes(text: str) -> bool:
    return text.strip().lower() in ("y", "yes")


# =============================================================================================== CLI
def main(argv: Sequence[str] | None = None, *, root: Path | None = None, input_fn: Callable[[str], str] = input,
         getpass_fn: Callable[[str], str] = getpass.getpass, out: Callable[[str], None] = print) -> int:
    ap = argparse.ArgumentParser(description="First-run wizard for the UK ISA Command Centre. " + DISCLAIMER)
    ap.add_argument("--non-interactive", action="store_true", help="never prompt; use only the flags below")
    ap.add_argument("--level", choices=EXPERIENCE_LEVELS)
    ap.add_argument("--remaining", help="£ of this tax year's allowance still unused")
    ap.add_argument("--lisa-used", help="£ of the used allowance that went into your LISA")
    ap.add_argument("--account", help="account id (from config.yaml) holding the used allowance")
    ap.add_argument("--monthly", help="£ you plan to invest each month")
    ap.add_argument("--fresh-portfolio", action="store_true", help="empty data/holdings.csv (sample saved as .bak)")
    ap.add_argument("--td-key", help="Twelve Data key (prefer the hidden prompt: command-line args are visible in `ps`)")
    ap.add_argument("--finnhub-key", help="Finnhub key (prefer the hidden prompt)")
    a = ap.parse_args(argv)
    ans: dict[str, Any] = {}
    for src, dst in (("level", "level"), ("remaining", "remaining"), ("lisa_used", "lisa_used"), ("account", "account"), ("monthly", "monthly"),
                     ("td_key", "td_key"), ("finnhub_key", "fh_key")):
        if getattr(a, src) is not None:
            ans[dst] = getattr(a, src)
    if a.fresh_portfolio:
        ans["fresh_portfolio"] = "y"
    try:
        run_wizard(root or Path(__file__).resolve().parent, answers=ans, interactive=not a.non_interactive,
                   input_fn=input_fn, getpass_fn=getpass_fn, out=out)
    except (WizardError, ConfigError) as exc:
        print(f"setup failed: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nsetup cancelled — nothing further was changed.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
