"""Price-unit and currency normalisation (pure functions, no I/O).

The rules here are the project's defence against 100x pricing errors. See CLAUDE.md section 2.

* Currency codes are **case-sensitive**: ``GBp``/``GBX`` are pence, ``GBP`` is pounds.
* Never infer pence from a ``.L`` suffix — use the currency the data vendor reports.
* ``GBPUSD=X`` is USD per 1 GBP, so ``gbp = usd / rate``.
* Indices are points, not money, and are never converted.
"""
from __future__ import annotations

from typing import Mapping

# code -> (major currency, divisor).  Case-sensitive on purpose.
SUBUNITS: dict[str, tuple[str, float]] = {
    "GBp": ("GBP", 100.0),
    "GBX": ("GBP", 100.0),
    "ZAc": ("ZAR", 100.0),   # South African cents
    "ILA": ("ILS", 100.0),   # Israeli agorot
}

PRICE_UNIT_AUTO = "auto"


def split_currency(code: str | None, override: str | None = None) -> tuple[str | None, float]:
    """Return ``(major_currency, divisor)`` for a vendor currency code.

    ``divisor`` is what raw prices must be divided by to reach major units
    (100 for pence, 1 otherwise). ``override`` is the per-ticker ``price_unit`` from
    config.yaml (``"GBP"`` / ``"GBp"``); ``"auto"``/``None`` means trust the vendor.
    """
    if override and override != PRICE_UNIT_AUTO:
        code = override
    if not code:
        return None, 1.0
    code = code.strip()
    if code in SUBUNITS:
        return SUBUNITS[code]
    return code, 1.0


def normalise_price(value: float | None, currency: str | None, *, override: str | None = None,
                    is_index: bool = False) -> float | None:
    """Raw vendor price -> price in *major* currency units (pence -> pounds). Indices pass through."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    if is_index:
        return v
    _, divisor = split_currency(currency, override)
    return v / divisor


def fx_symbol(ccy: str) -> str | None:
    """Yahoo symbol quoting *units of ``ccy`` per 1 GBP* (e.g. ``GBPUSD=X``). ``None`` for GBP."""
    return None if ccy == "GBP" else f"GBP{ccy}=X"


def to_gbp(value_major: float | None, ccy_major: str | None, fx: Mapping[str, float]) -> float | None:
    """Convert a value already in *major* units to GBP.

    ``fx[ccy]`` = units of ``ccy`` per 1 GBP. Returns ``None`` (never a guess) if the rate is missing.
    """
    if value_major is None or ccy_major is None:
        return None
    if ccy_major == "GBP":
        return float(value_major)
    rate = fx.get(ccy_major)
    if not rate or rate <= 0:
        return None
    return float(value_major) / float(rate)


def price_to_gbp(value: float | None, currency: str | None, fx: Mapping[str, float], *,
                 override: str | None = None, is_index: bool = False) -> float | None:
    """Raw vendor price (any unit) -> GBP pounds."""
    if is_index:
        return normalise_price(value, currency, is_index=True)
    major, divisor = split_currency(currency, override)
    if value is None or major is None:
        return None
    try:
        v = float(value) / divisor
    except (TypeError, ValueError):
        return None
    return to_gbp(v, major, fx)


def reconcile_unit(value: float | None, reference_price: float | None) -> tuple[float | None, str | None]:
    """Fix a price-like field (e.g. an analyst target) that is off from ``reference_price`` by ~100x.

    Real targets live roughly within 0.2x-5x of the price. If the ratio is ~100 (pounds vs pence mix-up)
    divide by 100; if ~0.01 multiply by 100. Returns ``(value, note)`` where ``note`` explains any fix.
    """
    if value is None or reference_price is None or reference_price <= 0:
        return value, None
    ratio = value / reference_price
    if ratio > 20:
        fixed = value / 100.0
        if 0.2 <= fixed / reference_price <= 5:
            return fixed, f"unit mismatch corrected (/100): {value:g} vs price {reference_price:g}"
    elif 0 < ratio < 0.05:
        fixed = value * 100.0
        if 0.2 <= fixed / reference_price <= 5:
            return fixed, f"unit mismatch corrected (x100): {value:g} vs price {reference_price:g}"
    return value, None
