"""Shared data classes for the market-data layer (kept import-light so providers and market_intel can both use them)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping


class DataUnavailable(RuntimeError):
    """A provider could not serve this request (network, plan limit, rate limit, unknown symbol …)."""


@dataclass(frozen=True)
class RawQuote:
    """A last/previous-close pair in the **vendor's raw unit** (pence for GBp/GBX tickers).

    ``currency`` is the vendor-reported code (``GBp``, ``GBX``, ``GBP``, ``USD`` …) when the same response carries it
    (Twelve Data / Finnhub do; a yfinance batch download does not, so metadata supplies it). ``kind`` is the instrument
    type when known (``INDEX``/``CURRENCY`` are points/rates and are never currency-converted).
    """
    last: float
    prev: float | None
    asof: datetime | None = None
    currency: str | None = None
    provider: str | None = None          # TD | FH | YF | ST
    kind: str | None = None

    def to_json(self) -> dict:
        return {"last": self.last, "prev": self.prev, "asof": self.asof.isoformat() if self.asof else None,
                "ccy": self.currency, "src": self.provider, "kind": self.kind}

    @staticmethod
    def from_json(d: Mapping) -> "RawQuote":
        return RawQuote(float(d["last"]), None if d.get("prev") is None else float(d["prev"]),
                        datetime.fromisoformat(d["asof"]) if d.get("asof") else None,
                        d.get("ccy"), d.get("src"), d.get("kind"))


def unit_jump(new: float | None, old: float | None) -> float | None:
    """Detect a 100x unit slip between two prices of the *same* security (pence <-> pounds mix-up).

    Returns the factor (100.0 or 0.01) when ``new/old`` is within 10% of it, else ``None``. Real one-refresh moves are
    nowhere near 100x, so this is a safe tripwire that protects against a provider flipping GBX/GBP labelling.
    """
    if not new or not old or new <= 0 or old <= 0:
        return None
    r = new / old
    if 90.0 <= r <= 110.0:
        return 100.0
    if 0.009 <= r <= 0.0111:
        return 0.01
    return None
