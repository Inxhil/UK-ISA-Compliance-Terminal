"""ISA allowance engine, tax-year calendar, no-sell rebalancer and Bed & ISA planner.

Single source of truth for UK ISA tax constants: ``TAX_YEAR_RULES``. See CLAUDE.md section 1.

Money in the ledger is ``Decimal`` (pence-exact). The rebalancer solves in float and then quantises to the
penny with a largest-remainder pass so orders sum *exactly* to the cash being invested.
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
D0 = Decimal("0")
PENNY = Decimal("0.01")
ISA_KINDS = frozenset({"SS_ISA", "CASH_ISA", "IFISA", "LISA"})


def D(x: object) -> Decimal:
    """Exact Decimal from str/int/float/Decimal (floats go via repr so 0.1 stays 0.1)."""
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def q2(x: Decimal) -> Decimal:
    return x.quantize(PENNY, rounding=ROUND_HALF_UP)


# =============================================================================================
# Tax-year rules
# =============================================================================================
@dataclass(frozen=True)
class TaxYearRules:
    start_year: int
    annual_limit: Decimal           # unified across S&S / Cash / IFISA / LISA
    lisa_limit: Decimal             # counts INSIDE annual_limit
    lisa_bonus_rate: Decimal        # government bonus; never a subscription
    cgt_annual_exempt: Decimal
    confirmed: bool = True          # False => carried forward from the previous year, verify when announced

    @property
    def label(self) -> str:
        return f"{self.start_year}/{str(self.start_year + 1)[2:]}"


TAX_YEAR_RULES: dict[int, TaxYearRules] = {
    2023: TaxYearRules(2023, D(20000), D(4000), D("0.25"), D(6000)),
    2024: TaxYearRules(2024, D(20000), D(4000), D("0.25"), D(3000)),
    2025: TaxYearRules(2025, D(20000), D(4000), D("0.25"), D(3000)),
    2026: TaxYearRules(2026, D(20000), D(4000), D("0.25"), D(3000)),
    # 2027/28: S&S ISA limits carried forward. (A lower Cash-ISA-only limit for under-65s was announced for
    # April 2027 — not modelled; it does not affect S&S ISA tracking.) Verify when the year starts.
    2027: TaxYearRules(2027, D(20000), D(4000), D("0.25"), D(3000), confirmed=False),
}


def rules_for(start_year: int) -> TaxYearRules:
    """Rules for a tax year; unknown years fall back to the nearest known year (flagged ``confirmed=False``)."""
    if start_year in TAX_YEAR_RULES:
        return TAX_YEAR_RULES[start_year]
    known = sorted(TAX_YEAR_RULES)
    base = TAX_YEAR_RULES[max([y for y in known if y <= start_year] or [known[0]])]
    return TaxYearRules(start_year, base.annual_limit, base.lisa_limit, base.lisa_bonus_rate,
                        base.cgt_annual_exempt, confirmed=False)


# =============================================================================================
# Calendar
# =============================================================================================
def to_london(dt: datetime) -> datetime:
    """Aware -> London local time. Naive datetimes are assumed to already be London local time."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=LONDON)
    return dt.astimezone(LONDON)


def now_london() -> datetime:
    return datetime.now(timezone.utc).astimezone(LONDON)


@dataclass(frozen=True)
class TaxYear:
    start_year: int
    start: date          # 6 April
    end: date            # 5 April next year (inclusive)
    deadline: datetime   # tz-aware: 00:00 London on 6 April = end of 5 April
    label: str

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end


def tax_year(start_year: int) -> TaxYear:
    return TaxYear(start_year, date(start_year, 4, 6), date(start_year + 1, 4, 5),
                   datetime(start_year + 1, 4, 6, 0, 0, 0, tzinfo=LONDON),
                   f"{start_year}/{str(start_year + 1)[2:]}")


def tax_year_start_for(d: date | datetime) -> int:
    """Start year of the tax year containing ``d``. Datetimes are converted to London first, so
    ``2027-04-05T23:30Z`` (= 00:30 BST on 6 April) belongs to the *next* tax year."""
    if isinstance(d, datetime):
        d = to_london(d).date()
    return d.year if (d.month, d.day) >= (4, 6) else d.year - 1


def tax_year_for(d: date | datetime | None = None) -> TaxYear:
    return tax_year(tax_year_start_for(d if d is not None else now_london()))


@dataclass(frozen=True)
class Countdown:
    deadline: datetime
    total_seconds: int
    days: int
    hours: int
    minutes: int
    seconds: int
    expired: bool

    def short(self) -> str:
        if self.expired:
            return "EXPIRED"
        return f"{self.days}d {self.hours:02d}h {self.minutes:02d}m"

    def long(self) -> str:
        if self.expired:
            return "deadline passed"
        return f"{self.days}d {self.hours:02d}h {self.minutes:02d}m {self.seconds:02d}s"


def countdown_to_deadline(now: datetime | None = None, ty: TaxYear | None = None) -> Countdown:
    """Real elapsed time to the end of 5 April (London). Computed in UTC: Python subtracts same-tzinfo aware
    datetimes as *wall-clock*, which would be an hour out across a DST change."""
    now = to_london(now) if now is not None else now_london()
    ty = ty or tax_year_for(now)
    delta = ty.deadline.astimezone(timezone.utc) - now.astimezone(timezone.utc)
    secs = int(delta.total_seconds())
    if secs <= 0:
        return Countdown(ty.deadline, 0, 0, 0, 0, 0, True)
    return Countdown(ty.deadline, secs, secs // 86400, (secs % 86400) // 3600, (secs % 3600) // 60, secs % 60, False)


# =============================================================================================
# Ledger & allowance
# =============================================================================================
LEDGER_TYPES = frozenset({"deposit", "withdrawal", "transfer_in", "transfer_out", "bonus"})


@dataclass(frozen=True)
class AccountSpec:
    id: str
    kind: str = "SS_ISA"
    flexible: bool = False
    broker: str = ""

    @property
    def is_isa(self) -> bool:
        return self.kind in ISA_KINDS


@dataclass(frozen=True)
class LedgerEntry:
    date: date
    account: str
    type: str
    amount: Decimal
    note: str = ""


class LedgerError(ValueError):
    pass


def parse_ledger_csv(path: str | Path) -> list[LedgerEntry]:
    """Read ``contributions.csv`` (date,account,type,amount_gbp,note). Fails loudly on bad rows."""
    entries: list[LedgerEntry] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        need = {"date", "account", "type", "amount_gbp"}
        if not reader.fieldnames or not need <= {h.strip() for h in reader.fieldnames}:
            raise LedgerError(f"{path}: need columns {sorted(need)}, found {reader.fieldnames}")
        for n, row in enumerate(reader, start=2):
            try:
                typ = row["type"].strip().lower()
                if typ not in LEDGER_TYPES:
                    raise LedgerError(f"unknown type '{typ}' (valid: {sorted(LEDGER_TYPES)})")
                amt = Decimal(row["amount_gbp"].strip())
                if amt <= 0:
                    raise LedgerError("amount must be positive (use type=withdrawal for outflows)")
                entries.append(LedgerEntry(date.fromisoformat(row["date"].strip()), row["account"].strip(), typ,
                                           amt, (row.get("note") or "").strip()))
            except (ValueError, ArithmeticError, LedgerError) as exc:
                raise LedgerError(f"{path} line {n}: {exc}") from exc
    return entries


@dataclass(frozen=True)
class AccountUsage:
    account: str
    kind: str
    flexible: bool
    subscriptions: Decimal
    eligible_withdrawals: Decimal
    net_used: Decimal              # max(0, S - W_eligible)
    replacement_credit: Decimal    # max(0, W_eligible - S): re-subscribable into THIS account without using allowance


@dataclass(frozen=True)
class ISAStatus:
    tax_year: TaxYear
    rules: TaxYearRules
    as_of: date
    subscriptions: Decimal             # gross deposits into ISA accounts (incl. LISA)
    eligible_withdrawals: Decimal      # flexible-ISA withdrawals in this tax year
    net_used: Decimal                  # sum of per-account net_used
    remaining: Decimal                 # statutory allowance still available (limit - net_used, >= 0)
    replacement_credit: Decimal        # extra room, usable only in the account the cash left
    headroom: Decimal                  # remaining + replacement_credit = max further cash into ISAs
    breach: Decimal                    # net_used above the annual limit (should be 0)
    lisa_subscriptions: Decimal
    lisa_remaining: Decimal
    lisa_bonus_forgone: Decimal        # remaining LISA room x bonus rate
    lisa_breach: Decimal
    non_flexible_withdrawals: Decimal  # informational: no replacement room
    ignored_transfers: Decimal         # informational: transfers in/out are not subscriptions
    ignored_bonus: Decimal             # informational: LISA bonus is not a subscription
    per_account: tuple[AccountUsage, ...]
    warnings: tuple[str, ...] = ()

    @property
    def limit(self) -> Decimal:
        return self.rules.annual_limit

    @property
    def pct_used(self) -> float:
        return float(self.net_used / self.limit * 100) if self.limit else 0.0

    @property
    def has_breach(self) -> bool:
        return self.breach > 0 or self.lisa_breach > 0

    def max_new_subscription(self, account: str | None = None) -> Decimal:
        """Largest further payment that stays legal, optionally for one account (LISA is capped by its sub-limit)."""
        base = self.remaining
        for u in self.per_account:
            if account is not None and u.account == account:
                base += u.replacement_credit
                if u.kind == "LISA":
                    return max(D0, min(base, self.lisa_remaining))
        return max(D0, base)


def compute_isa_status(ledger: Iterable[LedgerEntry], accounts: Mapping[str, AccountSpec], *,
                       as_of: date | datetime | None = None, start_year: int | None = None) -> ISAStatus:
    """Allowance position for one tax year.

    Per ISA account ``a``:  ``net_used_a = max(0, S_a - W_a)`` and ``credit_a = max(0, W_a - S_a)`` where ``S_a``
    are this-year deposits and ``W_a`` are this-year withdrawals from a **flexible** account (a withdrawal made in
    an earlier tax year never counts). Transfers and LISA bonus are ignored. GIA rows are ignored.
    """
    if isinstance(as_of, datetime):
        as_of = to_london(as_of).date()
    as_of = as_of or now_london().date()
    ty = tax_year(start_year if start_year is not None else tax_year_start_for(as_of))
    rules = rules_for(ty.start_year)

    subs: dict[str, Decimal] = {}
    wds: dict[str, Decimal] = {}
    nonflex_wd = ign_tr = ign_bonus = D0
    warnings: list[str] = []
    unknown: set[str] = set()

    for e in ledger:
        if not ty.contains(e.date) or e.date > as_of:
            continue
        spec = accounts.get(e.account)
        if spec is None:
            unknown.add(e.account)
            spec = AccountSpec(e.account, "SS_ISA", False)   # conservative: ISA, not flexible
        if not spec.is_isa:
            continue
        if e.type == "deposit":
            subs[e.account] = subs.get(e.account, D0) + e.amount
        elif e.type == "withdrawal":
            if spec.flexible and spec.kind != "LISA":
                wds[e.account] = wds.get(e.account, D0) + e.amount
            else:
                nonflex_wd += e.amount
        elif e.type in ("transfer_in", "transfer_out"):
            ign_tr += e.amount
        elif e.type == "bonus":
            ign_bonus += e.amount

    if unknown:
        warnings.append(f"Unknown account(s) {sorted(unknown)} treated as non-flexible ISAs — add them to config.yaml")

    usage: list[AccountUsage] = []
    for aid in sorted(set(subs) | set(wds)):
        spec = accounts.get(aid) or AccountSpec(aid)
        s, w = subs.get(aid, D0), wds.get(aid, D0)
        usage.append(AccountUsage(aid, spec.kind, spec.flexible, s, w, max(D0, s - w), max(D0, w - s)))

    total_s = sum((u.subscriptions for u in usage), D0)
    total_w = sum((u.eligible_withdrawals for u in usage), D0)
    net_used = sum((u.net_used for u in usage), D0)
    credit = sum((u.replacement_credit for u in usage), D0)
    remaining = max(D0, rules.annual_limit - net_used)
    breach = max(D0, net_used - rules.annual_limit)
    lisa_s = sum((u.subscriptions for u in usage if u.kind == "LISA"), D0)
    lisa_rem = max(D0, rules.lisa_limit - lisa_s)
    lisa_breach = max(D0, lisa_s - rules.lisa_limit)
    if not rules.confirmed:
        warnings.append(f"{rules.label} limits are carried forward from the previous year — verify once announced")

    return ISAStatus(
        tax_year=ty, rules=rules, as_of=as_of, subscriptions=total_s, eligible_withdrawals=total_w,
        net_used=net_used, remaining=remaining, replacement_credit=credit, headroom=remaining + credit,
        breach=breach, lisa_subscriptions=lisa_s, lisa_remaining=lisa_rem,
        lisa_bonus_forgone=q2(lisa_rem * rules.lisa_bonus_rate), lisa_breach=lisa_breach,
        non_flexible_withdrawals=nonflex_wd, ignored_transfers=ign_tr, ignored_bonus=ign_bonus,
        per_account=tuple(usage), warnings=tuple(warnings))


@dataclass(frozen=True)
class Pace:
    days_left: float
    months_left: float
    required_per_month: Decimal
    practical_cutoff: date
    days_to_cutoff: int


def allowance_pace(status: ISAStatus, now: datetime | None = None, practical_cutoff_days: int = 3) -> Pace:
    """Monthly run-rate needed to use the remaining allowance by the deadline."""
    now = to_london(now) if now is not None else now_london()
    cd = countdown_to_deadline(now, status.tax_year)
    days = cd.total_seconds / 86400.0
    months = max(days / 30.4375, 1e-9)
    cutoff = status.tax_year.end - timedelta(days=practical_cutoff_days)
    req = q2(status.remaining / D(months)) if not cd.expired and status.remaining > 0 else D0
    return Pace(days, months, req, cutoff, (cutoff - now.date()).days)


# =============================================================================================
# No-sell rebalancer: invest new cash to move weights toward targets without selling
# =============================================================================================
@dataclass(frozen=True)
class Order:
    ticker: str
    amount: Decimal                 # GBP, 2dp
    units: float | None
    price_gbp: float | None
    pre_weight: float               # of total incl. reserve cash, before
    post_weight: float              # of total incl. reserve cash, after
    target_weight: float            # original target (fraction of total)
    reason: str = ""


@dataclass(frozen=True)
class RebalancePlan:
    orders: tuple[Order, ...]
    cash_in: Decimal
    deployed: Decimal
    undeployed: Decimal
    pre_drift: float                # 0.5 * sum|w - t| over investable names (invested-normalised)
    post_drift: float
    post_weights: Mapping[str, float]
    warnings: tuple[str, ...] = ()


def _waterfill(values: Mapping[str, float], weights: Mapping[str, float], cash: float,
               cap_room: Mapping[str, float]) -> dict[str, float]:
    """Allocate ``cash`` so that (value_i + x_i)/w_i is equalised across funded names (water-filling),
    with 0 <= x_i <= cap_room_i. Exact by bisection on the common 'water level' T."""
    names = list(weights)
    if not names or cash <= 0:
        return {n: 0.0 for n in names}

    def alloc(T: float) -> dict[str, float]:
        return {n: min(max(weights[n] * T - values.get(n, 0.0), 0.0), cap_room.get(n, math.inf)) for n in names}

    max_room = sum(min(cap_room.get(n, math.inf), 1e18) for n in names)
    target = min(cash, max_room)
    lo, hi = 0.0, 1.0
    while sum(alloc(hi).values()) < target and hi < 1e18:
        hi *= 2.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if sum(alloc(mid).values()) < target:
            lo = mid
        else:
            hi = mid
    x = alloc(hi)
    s = sum(x.values())
    if s > 0:  # remove the bisection residue
        scale = target / s
        x = {n: v * scale if cap_room.get(n, math.inf) - v * scale >= -1e-9 else v for n, v in x.items()}
    return x


def _quantise_to_pence(raw: Mapping[str, float], total: Decimal) -> dict[str, Decimal]:
    """Largest-remainder rounding so the penny amounts sum EXACTLY to ``total``."""
    floors: dict[str, Decimal] = {}
    fracs: list[tuple[Decimal, str]] = []
    for k, v in raw.items():
        d = D(repr(v)) if v > 0 else D0
        f = d.quantize(PENNY, rounding=ROUND_DOWN)
        floors[k] = f
        fracs.append((d - f, k))
    short = int(((total - sum(floors.values(), D0)) / PENNY).to_integral_value(rounding=ROUND_HALF_UP))
    for _, k in sorted(fracs, key=lambda t: (-t[0], t[1]))[:max(short, 0)]:
        floors[k] += PENNY
    return floors


def plan_rebalance(values: Mapping[str, float], targets: Mapping[str, float], cash: Decimal | float | str, *,
                   prices: Mapping[str, float] | None = None, fractional: bool = True, min_order: float = 0.0,
                   max_weight: float | None = None, max_weights: Mapping[str, float] | None = None,
                   reserve_cash: float = 0.0, exclude: Sequence[str] = (),
                   reasons: Mapping[str, str] | None = None) -> RebalancePlan:
    """Spend new ``cash`` on the most under-target names — never selling.

    ``values``  current GBP value per ticker (investable positions only);
    ``targets`` target weights as fractions of the total (only >0 are considered, then renormalised);
    ``max_weight`` hard cap per name as a fraction of (values + reserve_cash + cash); ``max_weights`` per-ticker
    overrides (e.g. only single stocks are capped; core index ETFs are exempt);
    ``reserve_cash`` uninvestable cash that still counts in the weight denominator;
    ``exclude`` tickers that may not receive cash (e.g. trim-warning or screen-fail names).
    """
    cash_d = q2(D(cash))
    warnings: list[str] = []
    if cash_d <= 0:
        return RebalancePlan((), cash_d, D0, D0, 0.0, 0.0, {}, ("No cash to deploy",))
    excl = set(exclude)
    elig = {t: w for t, w in targets.items() if w > 0 and t not in excl}
    if not elig:
        return RebalancePlan((), cash_d, D0, cash_d, 0.0, 0.0, {}, ("No eligible targets — all names excluded or 0%",))
    wsum = sum(elig.values())
    w_norm = {t: w / wsum for t, w in elig.items()}
    vals = {t: float(values.get(t, 0.0)) for t in elig}
    base_total = sum(float(v) for v in values.values()) + reserve_cash
    cash_f = float(cash_d)
    post_total = base_total + cash_f
    reasons = reasons or {}

    def cap_of(t: str) -> float | None:
        return (max_weights or {}).get(t, max_weight)

    def caps_for(active: Mapping[str, float]) -> dict[str, float]:
        return {t: max(0.0, c * post_total - vals[t]) for t in active if (c := cap_of(t)) is not None}

    active = dict(w_norm)
    plan_x: dict[str, float] = {}
    for _ in range(len(w_norm) + 2):
        plan_x = _waterfill({t: vals[t] for t in active}, active, cash_f, caps_for(active))
        small = [t for t, x in plan_x.items() if 0 < x < min_order and len(active) > 1]
        if not small or min_order <= 0:
            break
        # drop the smallest sub-minimum line and re-allocate its cash to the rest
        drop = min(small, key=lambda t: plan_x[t])
        active.pop(drop)
        s = sum(active.values())
        active = {t: w / s for t, w in active.items()}

    plan_x = {t: x for t, x in plan_x.items() if x > 1e-9}
    deployed_target = min(cash_f, sum(plan_x.values()) if plan_x else 0.0)

    units: dict[str, float | None] = {t: None for t in plan_x}
    if fractional or not prices:
        if not fractional:
            warnings.append("Whole-share mode requested but no prices supplied — using fractional amounts")
        amounts = _quantise_to_pence(plan_x, q2(D(repr(deployed_target))) if plan_x else D0)
        for t in amounts:
            p = (prices or {}).get(t)
            units[t] = (float(amounts[t]) / p) if p else None
    else:
        shares: dict[str, int] = {}
        left = cash_f
        for t, x in plan_x.items():
            p = prices.get(t)
            if not p:
                warnings.append(f"{t}: no price — skipped in whole-share mode")
                continue
            shares[t] = int(x // p)
            left -= shares[t] * p
        # spend the remainder one share at a time on the most under-weight name that still fits
        for _ in range(10_000):
            cands = [t for t in plan_x if prices.get(t) and prices[t] <= left + 1e-9
                     and (cap_of(t) is None or vals[t] + (shares.get(t, 0) + 1) * prices[t] <= cap_of(t) * post_total)]
            if not cands:
                break
            t = min(cands, key=lambda c: (vals[c] + shares.get(c, 0) * prices[c]) / w_norm[c])
            shares[t] = shares.get(t, 0) + 1
            left -= prices[t]
        amounts = {t: q2(D(repr(n * prices[t]))) for t, n in shares.items() if n > 0}
        units = {t: float(n) for t, n in shares.items() if n > 0}
        if min_order > 0:
            amounts = {t: a for t, a in amounts.items() if a >= D(min_order)}

    orders: list[Order] = []
    for t, amt in amounts.items():
        if amt <= 0:
            continue
        p = (prices or {}).get(t)
        orders.append(Order(
            ticker=t, amount=amt, units=units.get(t), price_gbp=p,
            pre_weight=vals[t] / base_total if base_total else 0.0,
            post_weight=(vals[t] + float(amt)) / post_total if post_total else 0.0,
            target_weight=float(targets[t]), reason=reasons.get(t, "")))
    orders.sort(key=lambda o: o.amount, reverse=True)
    deployed = sum((o.amount for o in orders), D0)

    inv = sum(vals.values())
    def drift(v: Mapping[str, float], tot: float) -> float:
        return 0.5 * sum(abs(v.get(t, 0.0) / tot - w_norm[t]) for t in w_norm) if tot > 0 else 0.0
    post_vals = {t: vals[t] + float(next((o.amount for o in orders if o.ticker == t), D0)) for t in vals}
    plan = RebalancePlan(
        orders=tuple(orders), cash_in=cash_d, deployed=deployed, undeployed=cash_d - deployed,
        pre_drift=drift(vals, inv), post_drift=drift(post_vals, inv + float(deployed)),
        post_weights={t: v / post_total for t, v in post_vals.items()}, warnings=tuple(warnings))
    if plan.undeployed > D("0.005") and fractional:
        plan = RebalancePlan(**{**plan.__dict__, "warnings": plan.warnings + (
            f"£{plan.undeployed:,.2f} left undeployed (position caps reached)",)})
    return plan


# =============================================================================================
# Bed & ISA
# =============================================================================================
@dataclass(frozen=True)
class GIAPosition:
    ticker: str
    quantity: float
    avg_cost_gbp: float          # Section 104 pooled average cost per unit
    price_gbp: float

    @property
    def gain_per_unit(self) -> float:
        return self.price_gbp - self.avg_cost_gbp

    @property
    def unrealised_gain(self) -> float:
        return self.gain_per_unit * self.quantity


@dataclass(frozen=True)
class BedISALine:
    ticker: str
    units: float
    proceeds: float
    realised_gain: float
    fraction_of_position: float


@dataclass(frozen=True)
class BedISAPlan:
    lines: tuple[BedISALine, ...]
    total_proceeds: float
    total_gain: float
    cgt_allowance_start: float
    cgt_allowance_left: float
    isa_headroom_start: float
    isa_headroom_left: float
    limited_by: str                 # 'cgt_allowance' | 'isa_headroom' | 'all_positions_moved' | 'nothing_to_do'
    warnings: tuple[str, ...] = ()


def plan_bed_and_isa(positions: Sequence[GIAPosition], cgt_allowance_remaining: float, isa_headroom: float, *,
                     fractional: bool = True, min_gain_gbp: float = 0.0) -> BedISAPlan:
    """Choose GIA sales whose realised gain fits inside the remaining CGT exempt amount and ISA headroom.

    Lowest gain-per-£ first (losses first — they offset gains). Assumes Section 104 average cost; same-day and
    30-day matching are not modelled.
    """
    allowance = float(cgt_allowance_remaining)
    budget = float(isa_headroom)
    lines: list[BedISALine] = []
    limited = "nothing_to_do"
    order = sorted((p for p in positions if p.quantity > 0 and p.price_gbp > 0),
                   key=lambda p: p.gain_per_unit / p.price_gbp)
    moved_all = True
    for p in order:
        g = p.gain_per_unit
        max_by_isa = budget / p.price_gbp
        if g > 0:
            if abs(p.unrealised_gain) < min_gain_gbp and p.unrealised_gain >= 0:
                continue
            max_by_cgt = allowance / g if allowance > 0 else 0.0
        else:
            max_by_cgt = p.quantity
        units = min(p.quantity, max_by_cgt, max_by_isa)
        if not fractional:
            units = math.floor(units + 1e-12)
        if units <= 1e-9:
            moved_all = False
            limited = "isa_headroom" if max_by_isa < min(p.quantity, max_by_cgt) + 1e-9 else "cgt_allowance"
            continue
        if units < p.quantity - 1e-9:
            moved_all = False
            limited = "isa_headroom" if abs(units - max_by_isa) < 1e-9 else "cgt_allowance"
        gain = g * units
        lines.append(BedISALine(p.ticker, units, units * p.price_gbp, gain, units / p.quantity))
        allowance -= gain
        budget -= units * p.price_gbp
    if lines and moved_all:
        limited = "all_positions_moved"
    total_p = sum(l.proceeds for l in lines)
    total_g = sum(l.realised_gain for l in lines)
    warns = ["Section 104 average cost assumed; same-day / 30-day matching not modelled.",
             "Sale + re-purchase: allow for spread, and 0.5% stamp duty on UK-incorporated shares (ETFs exempt)."]
    return BedISAPlan(tuple(lines), total_p, total_g, float(cgt_allowance_remaining), allowance,
                      float(isa_headroom), budget, limited, tuple(warns))
