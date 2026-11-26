"""Credit budgets for rate-limited APIs (Twelve Data free tier: 8 credits/minute, 800/day).

The budget is *persisted* (atomic JSON) so restarting the app, or running two instances, doesn't blow the quota.
Pure logic + a tiny file; no network. ``try_acquire`` never blocks — callers fall back to another provider or to the
cache when the budget is spent, which is how startup stays under the limit instead of waiting on it.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable


class CreditBudget:
    def __init__(self, per_minute: int, *, daily: int | None = None, path: str | Path | None = None,
                 clock: Callable[[], float] = time.time, reserve: int = 0):
        """``reserve`` credits are held back (never spent) so interactive actions always have headroom."""
        self.per_minute, self.daily, self.reserve = per_minute, daily, reserve
        self.path = Path(path) if path else None
        self._clock = clock
        self._lock = threading.RLock()
        self._stamps: list[float] = []
        self._day: str = ""
        self._day_used = 0
        self._penalty_until = 0.0          # set after the provider itself says "429"
        self._load()

    # -- persistence ---------------------------------------------------------------------------
    def _load(self) -> None:
        if not self.path:
            return
        try:
            d = json.loads(self.path.read_text())
            self._stamps = [float(t) for t in d.get("stamps", [])]
            self._day, self._day_used = str(d.get("day", "")), int(d.get("day_used", 0))
            self._penalty_until = float(d.get("penalty_until", 0.0))
        except (OSError, ValueError, TypeError):
            pass

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            with os.fdopen(fd, "w") as fh:
                json.dump({"stamps": self._stamps, "day": self._day, "day_used": self._day_used,
                           "penalty_until": self._penalty_until}, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # -- accounting ----------------------------------------------------------------------------
    def _roll(self) -> None:
        now = self._clock()
        self._stamps = [t for t in self._stamps if now - t < 60.0]
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if day != self._day:
            self._day, self._day_used = day, 0

    def available(self) -> int:
        """Credits that can be spent right now (0 while a provider-side 429 penalty is running)."""
        with self._lock:
            self._roll()
            if self._clock() < self._penalty_until:
                return 0
            minute = self.per_minute - self.reserve - len(self._stamps)
            if self.daily is not None:
                minute = min(minute, self.daily - self._day_used)
            return max(0, minute)

    def try_acquire(self, n: int = 1) -> bool:
        with self._lock:
            if n <= 0:
                return True
            if self.available() < n:
                return False
            now = self._clock()
            self._stamps.extend([now] * n)
            self._day_used += n
            self._save()
            return True

    def refund(self, n: int = 1) -> None:
        """Give back credits for a call that failed before the provider counted it (e.g. network error)."""
        with self._lock:
            for _ in range(min(n, len(self._stamps))):
                self._stamps.pop()
            self._day_used = max(0, self._day_used - n)
            self._save()

    def penalise(self, seconds: float = 62.0) -> None:
        """The provider answered 'out of credits': stop calling it until the window has surely reset."""
        with self._lock:
            self._penalty_until = self._clock() + seconds
            self._save()

    def seconds_until(self, n: int = 1) -> float:
        with self._lock:
            self._roll()
            now = self._clock()
            if now < self._penalty_until:
                return self._penalty_until - now
            free = self.per_minute - self.reserve - len(self._stamps)
            if free >= n:
                return 0.0
            need = n - free
            return max(0.0, sorted(self._stamps)[min(need, len(self._stamps)) - 1] + 60.0 - now)
