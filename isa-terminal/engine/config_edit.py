"""Surgical, comment-preserving edits to config.yaml.

``yaml.dump`` would destroy the comments and hand-tuned layout that make config.yaml readable, so edits here are
line-based: set one key under a top-level mapping, or append one watchlist item. Every write is atomic, keeps a ``.bak``
copy, is re-validated with :func:`engine.config.load_config`, and is rolled back if the result no longer loads.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Mapping

from engine.config import EXPERIENCE_LEVELS, SLEEVES, ConfigError, load_config

_TOP_KEY = re.compile(r"^[A-Za-z_][\w-]*\s*:")


def _read(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _write_validated(path: Path, lines: list[str]) -> None:
    """Write atomically (with backup), validate by loading, roll back on failure."""
    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines).rstrip("\n") + "\n")
        os.replace(tmp, path)
        load_config(path)                                    # raises ConfigError if we broke something
    except Exception:
        shutil.copy2(backup, path)
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return f"{value:g}" if isinstance(value, float) else str(value)
    return json.dumps(str(value), ensure_ascii=False) if re.search(r"[:#\[\]{},&*!|>'\"%@`]|^\s|\s$", str(value)) else str(value)


def set_mapping_value(path: str | Path, section: str, key: str, value: Any, comment: str | None = None) -> None:
    """Set ``section.key`` (a top-level mapping's direct child), preserving everything else, incl. the line's comment."""
    path = Path(path)
    lines = _read(path)
    sec = next((i for i, l in enumerate(lines) if re.match(rf"^{re.escape(section)}\s*:\s*(#.*)?$", l)), None)
    if sec is None:
        lines += ["", f"{section}:", f"  {key}: {_scalar(value)}" + (f"   # {comment}" if comment else "")]
        return _write_validated(path, lines)
    end = next((i for i in range(sec + 1, len(lines)) if lines[i].strip() and not lines[i].startswith((" ", "\t", "#"))), len(lines))
    pat = re.compile(rf"^(\s+){re.escape(key)}\s*:\s*[^#\n]*?(\s+#.*)?$")
    for i in range(sec + 1, end):
        m = pat.match(lines[i])
        if m:
            lines[i] = f"{m.group(1)}{key}: {_scalar(value)}" + (m.group(2) or "")
            return _write_validated(path, lines)
    last = max([i for i in range(sec + 1, end) if lines[i].strip()] or [sec])
    lines.insert(last + 1, f"  {key}: {_scalar(value)}" + (f"   # {comment}" if comment else ""))
    _write_validated(path, lines)


def set_experience_level(path: str | Path, level: str) -> None:
    level = level.strip().lower()
    if level not in EXPERIENCE_LEVELS:
        raise ConfigError(f"experience level must be one of {list(EXPERIENCE_LEVELS)}, got '{level}'")
    set_mapping_value(path, "ui", "experience_level", level, "beginner | intermediate | advanced")


def set_monthly_contribution(path: str | Path, gbp: float) -> None:
    if gbp < 0:
        raise ConfigError("monthly contribution must be >= 0")
    set_mapping_value(path, "profile", "monthly_contribution_gbp", float(gbp))


def append_watch_item(path: str | Path, item: Mapping[str, Any]) -> None:
    """Append one ticker to ``watchlist:`` (watch-only, target 0%) without touching comments or other items."""
    path = Path(path)
    ticker = str(item["ticker"]).strip()
    existing = load_config(path)
    if existing.watch(ticker):
        raise ConfigError(f"{ticker} is already in the watchlist")
    sleeve = item.get("sleeve")
    if sleeve is not None and sleeve not in SLEEVES:
        raise ConfigError(f"sleeve must be one of {sorted(SLEEVES)}")
    block = [f"  - ticker: {_scalar(ticker)}", f"    name: {_scalar(item.get('name') or ticker)}", f"    kind: {item.get('kind', 'stock')}"]
    if sleeve:
        block.append(f"    sleeve: {sleeve}")
    block.append("    target_weight_pct: 0")
    for k in ("sector", "isin"):
        if item.get(k):
            block.append(f"    {k}: {_scalar(item[k])}")
    if item.get("ter_pct") is not None:
        block.append(f"    ter_pct: {float(item['ter_pct']):g}")
    if item.get("distribution") in ("acc", "dist"):
        block.append(f"    distribution: {item['distribution']}")
    lines = _read(path)
    start = next((i for i, l in enumerate(lines) if re.match(r"^watchlist\s*:\s*(#.*)?$", l)), None)
    if start is None:
        lines += ["", "watchlist:"]
        start = len(lines) - 1
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() and _TOP_KEY.match(lines[i])), len(lines))
    insert_at = max([i for i in range(start + 1, end) if lines[i].strip() and not lines[i].lstrip().startswith("#")] or [start]) + 1
    lines[insert_at:insert_at] = ["", f"  # added from the terminal search bar", *block]
    _write_validated(path, lines)


# ------------------------------------------------------------------------------------------------ suggestions
_TRUST = re.compile(r"\b(investment\s+trust|trust|investment\s+co(mpany)?)\b", re.I)


def suggest_watch_item(ticker: str, *, name: str | None, quote_type: str | None, sector: str | None,
                       currency: str | None, ter_pct: float | None = None) -> dict[str, Any]:
    """Best-effort config entry for an ad-hoc ticker. Conservative: unknown => plain stock, watch-only (0%)."""
    t = ticker.upper()
    uk = t.endswith(".L")
    qt = (quote_type or "").upper()
    nm = name or t
    if qt in ("ETF", "ETP", "MUTUALFUND") or re.search(r"\b(UCITS|ETF|ETP)\b", nm, re.I):
        kind = "etf"
    elif uk and _TRUST.search(nm):
        kind = "trust"
    else:
        kind = "stock"
    sleeve = {"etf": "core_etf", "trust": "trust"}.get(kind, "uk_div" if uk else "global_stock")
    item: dict[str, Any] = {"ticker": t, "name": nm, "kind": kind, "sleeve": sleeve}
    if sector and kind == "stock":
        item["sector"] = sector
    if ter_pct is not None and kind != "stock":
        item["ter_pct"] = ter_pct
    return item
