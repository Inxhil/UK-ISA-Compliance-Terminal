"""Loaders for config.yaml, the machine-readable rules block in handbook.md, and .env.

Policy numbers (caps, thresholds) come from handbook.md; structure (accounts, watchlist, targets) from config.yaml.
"""
from __future__ import annotations

import dataclasses
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- handbook rules
@dataclass(frozen=True)
class PositionRules:
    max_single_position_pct: float = 15.0
    max_sector_pct: float = 40.0
    underweight_trigger_pp: float = 2.0
    overweight_trigger_pp: float = 3.0
    cap_applies_to: tuple[str, ...] = ("stock", "trust")   # kinds subject to the single-position cap (R-CAP-01)


@dataclass(frozen=True)
class CostRules:
    max_ocf_pct_passive: float = 0.25
    max_ocf_pct_active: float = 0.80


@dataclass(frozen=True)
class QualityRules:
    min_dividend_cover: float = 1.5
    min_dividend_yield_for_cover_test_pct: float = 0.5
    max_peg: float = 2.5
    max_forward_pe: float = 40.0


@dataclass(frozen=True)
class AnalystRules:
    min_upside_pct: float = 15.0
    min_analysts: int = 5
    review_below_target_pct: float = -5.0


@dataclass(frozen=True)
class TechRules:
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    caution_below_sma200_pct: float = -10.0
    rich_vs_5y_avg_pct: float = 40.0
    cheap_vs_5y_avg_pct: float = -15.0


@dataclass(frozen=True)
class TaxRules:
    alert_days: tuple[int, ...] = (90, 30, 7)
    practical_cutoff_days: int = 3
    bed_and_isa_min_gain_gbp: float = 100.0


@dataclass(frozen=True)
class CashRules:
    reserve_target_pct: float = 5.0
    max_cash_drag_pct: float = 8.0


@dataclass(frozen=True)
class DrawdownTier:
    drop_pct: float
    deploy_pct_of_reserve: float


@dataclass(frozen=True)
class DrawdownRules:
    reference_ticker: str = "VWRP.L"
    tiers: tuple[DrawdownTier, ...] = (DrawdownTier(10, 25), DrawdownTier(20, 35), DrawdownTier(30, 40))


@dataclass(frozen=True)
class ScoringRules:
    valuation: float = 30.0
    consensus: float = 25.0
    momentum: float = 15.0
    allocation: float = 20.0
    quality: float = 10.0


@dataclass(frozen=True)
class HandbookRules:
    position: PositionRules = field(default_factory=PositionRules)
    costs: CostRules = field(default_factory=CostRules)
    quality: QualityRules = field(default_factory=QualityRules)
    analyst: AnalystRules = field(default_factory=AnalystRules)
    technicals: TechRules = field(default_factory=TechRules)
    tax: TaxRules = field(default_factory=TaxRules)
    cash: CashRules = field(default_factory=CashRules)
    drawdown: DrawdownRules = field(default_factory=DrawdownRules)
    scoring: ScoringRules = field(default_factory=ScoringRules)


_RULES_BLOCK = re.compile(r"```yaml[ \t]+handbook-rules[ \t]*\n(.*?)\n```", re.DOTALL)


class ConfigError(ValueError):
    """Raised for invalid config.yaml / handbook rules (fail loudly on bad input files)."""


def _build(cls: type, data: dict[str, Any] | None, where: str):
    data = data or {}
    if not isinstance(data, dict):
        raise ConfigError(f"handbook rules: '{where}' must be a mapping, got {type(data).__name__}")
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ConfigError(f"handbook rules: unknown key(s) in '{where}': {sorted(unknown)} (valid: {sorted(names)})")
    return cls(**data)


def parse_handbook_rules(markdown: str) -> HandbookRules:
    """Extract and validate the ```yaml handbook-rules``` block. Missing block => defaults."""
    m = _RULES_BLOCK.search(markdown)
    if not m:
        return HandbookRules()
    try:
        raw = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"handbook.md rules block is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("handbook.md rules block must be a YAML mapping")

    sections = {f.name: f.type for f in dataclasses.fields(HandbookRules)}
    unknown = set(raw) - set(sections)
    if unknown:
        raise ConfigError(f"handbook rules: unknown section(s) {sorted(unknown)} (valid: {sorted(sections)})")

    classes = {"position": PositionRules, "costs": CostRules, "quality": QualityRules, "analyst": AnalystRules,
               "technicals": TechRules, "tax": TaxRules, "cash": CashRules, "scoring": ScoringRules}
    kwargs: dict[str, Any] = {name: _build(cls, raw.get(name), name) for name, cls in classes.items() if name in raw}
    if "position" in kwargs:
        pos = kwargs["position"]
        kinds = tuple(str(k) for k in pos.cap_applies_to)
        bad = set(kinds) - {"stock", "trust", "etf"}
        if bad:
            raise ConfigError(f"position.cap_applies_to: unknown kind(s) {sorted(bad)} (valid: stock, trust, etf)")
        kwargs["position"] = dataclasses.replace(pos, cap_applies_to=kinds)
    if "tax" in kwargs:
        tax = kwargs["tax"]
        kwargs["tax"] = dataclasses.replace(tax, alert_days=tuple(sorted((int(d) for d in tax.alert_days), reverse=True)))
    if "drawdown" in raw:
        dd = raw["drawdown"] or {}
        tiers = tuple(sorted((_build(DrawdownTier, t, "drawdown.tiers[]") for t in dd.get("tiers", [])),
                             key=lambda t: t.drop_pct))
        rest = {k: v for k, v in dd.items() if k != "tiers"}
        base = _build(DrawdownRules, rest, "drawdown")
        kwargs["drawdown"] = dataclasses.replace(base, tiers=tiers) if tiers else base
    rules = HandbookRules(**kwargs)
    if rules.position.max_single_position_pct <= 0 or rules.position.max_single_position_pct > 100:
        raise ConfigError("position.max_single_position_pct must be in (0, 100]")
    return rules


def load_handbook_rules(path: str | Path | None = None) -> HandbookRules:
    p = Path(path) if path else ROOT / "handbook.md"
    return parse_handbook_rules(p.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- config.yaml
ISA_KINDS = {"SS_ISA", "CASH_ISA", "IFISA", "LISA"}
VALID_KINDS = ISA_KINDS | {"GIA"}
# watchlist "sleeves" (the category pills in pane 2) and the literacy levels (ui.experience_level)
SLEEVES = {"core_etf": "Core ETFs", "uk_div": "UK & Div", "trust": "Trusts", "global_stock": "Global Stocks"}
EXPERIENCE_LEVELS = ("beginner", "intermediate", "advanced")


@dataclass(frozen=True)
class AccountCfg:
    id: str
    broker: str
    kind: str
    flexible: bool = False

    @property
    def is_isa(self) -> bool:
        return self.kind in ISA_KINDS


@dataclass(frozen=True)
class WatchItem:
    ticker: str
    name: str
    kind: str                      # etf | trust | stock
    target_weight_pct: float = 0.0
    sector: str | None = None
    ter_pct: float | None = None
    distribution: str | None = None
    yield_override_pct: float | None = None
    price_unit: str = "auto"
    isin: str | None = None
    broker_symbols: dict[str, str] = field(default_factory=dict)
    look_through: dict[str, float] = field(default_factory=dict)
    alerts: dict[str, float] = field(default_factory=dict)
    sleeve: str | None = None                  # core_etf | uk_div | trust | global_stock
    aliases: tuple[str, ...] = ()              # extra company names used to tag news ("Google" for GOOGL)

    @property
    def is_fund(self) -> bool:
        return self.kind in ("etf", "trust")

    @property
    def is_passive(self) -> bool:
        return self.kind == "etf"


@dataclass(frozen=True)
class AppConfig:
    root: Path
    monthly_contribution_gbp: float
    fractional_shares: bool
    min_order_gbp: float
    holdings_csv: Path
    contributions_csv: Path
    handbook_path: Path
    cache_dir: Path
    reports_dir: Path
    accounts: dict[str, AccountCfg]
    watchlist: tuple[WatchItem, ...]
    cash_target_pct: float
    cgt_realised_ytd: float
    tape: tuple[dict[str, str], ...]
    refresh_seconds: int
    tape_scroll_hz: float
    cache_ttl: dict[str, float]
    default_alerts: dict[str, float]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    experience_level: str = "advanced"

    def watch(self, ticker: str) -> WatchItem | None:
        for w in self.watchlist:
            if w.ticker == ticker:
                return w
        return None

    @property
    def tickers(self) -> list[str]:
        return [w.ticker for w in self.watchlist]

    @property
    def targets(self) -> dict[str, float]:
        """Target weights as *fractions* of total ISA value, tickers with target > 0 only."""
        return {w.ticker: w.target_weight_pct / 100.0 for w in self.watchlist if w.target_weight_pct > 0}


def load_config(path: str | Path | None = None) -> AppConfig:
    p = Path(path) if path else ROOT / "config.yaml"
    root = p.resolve().parent
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p}: invalid YAML: {exc}") from exc

    accounts: dict[str, AccountCfg] = {}
    for a in raw.get("accounts", []):
        acc = AccountCfg(id=str(a["id"]), broker=str(a.get("broker", "")), kind=str(a["kind"]),
                         flexible=bool(a.get("flexible", False)))
        if acc.kind not in VALID_KINDS:
            raise ConfigError(f"account {acc.id}: kind '{acc.kind}' not in {sorted(VALID_KINDS)}")
        if acc.id in accounts:
            raise ConfigError(f"duplicate account id {acc.id}")
        accounts[acc.id] = acc

    items: list[WatchItem] = []
    seen: set[str] = set()
    for w in raw.get("watchlist", []):
        t = str(w["ticker"])
        if t in seen:
            raise ConfigError(f"duplicate ticker {t} in watchlist")
        seen.add(t)
        items.append(WatchItem(
            ticker=t, name=str(w.get("name", t)), kind=str(w.get("kind", "stock")),
            target_weight_pct=float(w.get("target_weight_pct", 0) or 0), sector=w.get("sector"),
            ter_pct=None if w.get("ter_pct") is None else float(w["ter_pct"]),
            distribution=w.get("distribution"),
            yield_override_pct=None if w.get("yield_override_pct") is None else float(w["yield_override_pct"]),
            price_unit=str(w.get("price_unit", "auto")), isin=w.get("isin"),
            broker_symbols={str(k): str(v) for k, v in (w.get("broker_symbols") or {}).items()},
            look_through={str(k): float(v) for k, v in (w.get("look_through") or {}).items()},
            alerts={str(k): float(v) for k, v in (w.get("alerts") or {}).items()},
            sleeve=(str(w["sleeve"]) if w.get("sleeve") else None), aliases=tuple(str(a) for a in (w.get("aliases") or ())),
        ))
        if items[-1].sleeve is not None and items[-1].sleeve not in SLEEVES:
            raise ConfigError(f"{t}: sleeve '{items[-1].sleeve}' not in {sorted(SLEEVES)}")

    cash_target = float(raw.get("cash_target_pct", 0) or 0)
    total = sum(i.target_weight_pct for i in items) + cash_target
    if items and abs(total - 100.0) > 0.01:
        raise ConfigError(f"target weights + cash_target_pct must sum to 100, got {total:.2f}")

    prof, data, ui = raw.get("profile", {}), raw.get("data", {}), raw.get("ui", {}) or {}
    level = str(ui.get("experience_level", "advanced")).strip().lower()
    if level not in EXPERIENCE_LEVELS:
        raise ConfigError(f"ui.experience_level '{level}' must be one of {list(EXPERIENCE_LEVELS)}")

    def rp(key: str, default: str) -> Path:
        v = Path(data.get(key, default))
        return v if v.is_absolute() else root / v

    return AppConfig(
        root=root,
        monthly_contribution_gbp=float(prof.get("monthly_contribution_gbp", 0)),
        fractional_shares=bool(prof.get("fractional_shares", True)),
        min_order_gbp=float(prof.get("min_order_gbp", 0)),
        holdings_csv=rp("holdings_csv", "data/holdings.csv"),
        contributions_csv=rp("contributions_csv", "data/contributions.csv"),
        handbook_path=rp("handbook", "handbook.md"),
        cache_dir=rp("cache_dir", ".cache"),
        reports_dir=rp("reports_dir", "reports"),
        accounts=accounts, watchlist=tuple(items), cash_target_pct=cash_target,
        cgt_realised_ytd=float((raw.get("cgt") or {}).get("realised_gains_ytd_gbp", 0) or 0),
        tape=tuple(raw.get("tape", [])),
        refresh_seconds=int(ui.get("refresh_seconds", 60)),
        tape_scroll_hz=float(ui.get("tape_scroll_hz", 6)),
        cache_ttl={k: float(v) for k, v in (raw.get("cache_ttl_seconds") or {}).items()},
        default_alerts={k: float(v) for k, v in (raw.get("alerts") or {}).items()},
        raw=raw, experience_level=level,
    )


# --------------------------------------------------------------------------- .env
def load_env(path: str | Path | None = None) -> dict[str, str]:
    """Minimal .env reader (KEY=VALUE, # comments, optional quotes). Does not override real env vars.

    Returns only the keys it read; never logs values.
    """
    p = Path(path) if path else ROOT / ".env"
    loaded: dict[str, str] = {}
    if not p.exists():
        return loaded
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v:
            loaded[k] = v
            os.environ.setdefault(k, v)
    return loaded
