"""Tiny thread-safe JSON disk cache with TTL and stale-on-error reads (no extra dependencies)."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class CacheHit:
    value: Any
    age_s: float
    fresh: bool


class DiskCache:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # -- helpers -----------------------------------------------------------
    def _path(self, key: str) -> Path:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", key)[:60]
        digest = hashlib.sha1(key.encode()).hexdigest()[:10]
        return self.root / f"{slug}.{digest}.json"

    # -- API ---------------------------------------------------------------
    def get(self, key: str, ttl: float) -> CacheHit | None:
        """Return the stored value (fresh or stale) or ``None`` if never stored / unreadable."""
        p = self._path(key)
        with self._lock:
            try:
                blob = json.loads(p.read_text())
                age = time.time() - float(blob["ts"])
                return CacheHit(_decode(blob["value"]), age, age <= ttl)
            except (OSError, ValueError, KeyError, TypeError):
                return None

    def set(self, key: str, value: Any) -> None:
        p = self._path(key)
        payload = json.dumps({"ts": time.time(), "key": key, "value": _encode(value)})
        with self._lock:
            fd, tmp = tempfile.mkstemp(dir=self.root, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(payload)
                os.replace(tmp, p)  # atomic
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def clear(self) -> None:
        with self._lock:
            for f in self.root.glob("*.json"):
                f.unlink(missing_ok=True)


def _encode(v: Any) -> Any:
    if isinstance(v, pd.DataFrame):
        return {"__df__": v.to_json(orient="split", date_format="iso"), "attrs": {k: x for k, x in v.attrs.items() if isinstance(x, (str, int, float, type(None)))}}
    return v


def _decode(v: Any) -> Any:
    if isinstance(v, dict) and "__df__" in v:
        df = pd.read_json(io.StringIO(v["__df__"]), orient="split")
        df.attrs.update(v.get("attrs") or {})
        return df
    return v
