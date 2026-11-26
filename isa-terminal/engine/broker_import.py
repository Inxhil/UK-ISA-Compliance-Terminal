"""Broker CSV import: Trading 212, Hargreaves Lansdown, Vanguard UK  ->  holdings.csv / contributions.csv rows.

**Honesty note.** Broker export layouts change without notice and I could not verify them against live exports.
So the parsers are *alias-driven* (header names are matched case/punctuation-insensitively against a list of known
variants) and **fail loudly** — listing the headers they saw — rather than guessing. If your export doesn't parse,
add its header names to ``ALIASES`` below; nothing else needs to change. All parsers share one normalised model.

Design choices:
* GBP unit cost is derived from the **total** column (÷ quantity), which includes fees/stamp duty and avoids relying on
  exchange-rate conventions. If a pence unit is declared in a header (``(p)``, ``GBX``) it is divided by 100.
* Dry-run by default; ``--write`` backs up the target CSV first, replaces that account's holdings, and appends only new
  ledger rows (dedup on date+account+type+amount).
* Cash balances are **not** derived (they need full history); set the ``CASH`` row in holdings.csv yourself.
"""
from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from engine.config import AppConfig, WatchItem, load_config
from engine.currency import split_currency
from engine.isa_engine import LedgerEntry
from engine.portfolio import HoldingRow


class BrokerImportError(ValueError):
    pass


# --------------------------------------------------------------------------- header aliases
def _n(h: str) -> str:
    """Normalise a header: lowercase, drop punctuation/units, collapse spaces."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9£%/ ]+", " ", h.lower())).strip()


ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "t212": {
        "action": ("action", "type"), "time": ("time", "date", "date time", "datetime"), "isin": ("isin",),
        "symbol": ("ticker", "symbol", "instrument"), "name": ("name", "instrument name"),
        "qty": ("no of shares", "shares", "quantity", "no shares"),
        "price": ("price share", "price / share", "price per share"),
        "price_ccy": ("currency price share", "currency price / share", "price currency"),
        "total": ("total", "total gbp", "total £", "amount", "amount gbp"),
        "total_ccy": ("currency total",), "notes": ("notes", "note"),
    },
    "hl": {
        "time": ("trade date", "date", "settle date"), "ref": ("reference", "ref"),
        "name": ("description", "stock", "security", "investment", "details"),
        "price": ("unit cost p", "unit cost", "price p", "price", "unit price"),
        "qty": ("quantity", "units", "shares", "units held"), "total": ("value £", "value", "amount £", "amount", "total"),
        "cost": ("cost £", "cost"), "symbol": ("stock code", "epic", "ticker", "sedol"),
    },
    "vanguard": {
        "time": ("date", "trade date", "transaction date"),
        "name": ("investment", "investment name", "fund", "product", "security", "details"),
        "action": ("transaction type", "type", "transaction", "action", "description"),
        "qty": ("quantity", "units", "shares"), "price": ("price", "unit price", "price £"),
        "total": ("cost", "value", "amount", "net amount", "total", "cost £", "value £"), "symbol": ("ticker", "symbol"),
    },
}


def unit_from_header(raw: str) -> str | None:
    """'GBp' if the header declares pence ((p), pence, GBX), 'GBP' if pounds (£, GBP), else None."""
    h = raw.lower()
    if "(p)" in h or "pence" in h or "gbx" in h or re.search(r"\bp\)$", h):
        return "GBp"
    if "£" in h or "gbp" in h:
        return "GBP"
    return None


class Columns:
    def __init__(self, headers: Sequence[str], broker: str):
        self.headers = list(headers)
        self.norm = {_n(h): h for h in headers if h}
        self.broker = broker

    def find(self, key: str, required: bool = False) -> str | None:
        for alias in ALIASES[self.broker].get(key, ()):
            if _n(alias) in self.norm:
                return self.norm[_n(alias)]
        if required:
            raise BrokerImportError(f"{self.broker}: required column '{key}' not found. Looked for "
                                    f"{list(ALIASES[self.broker][key])}. Headers in file: {self.headers}")
        return None


def detect_broker(headers: Sequence[str]) -> str | None:
    n = {_n(h) for h in headers}
    if {"action", "no of shares"} <= n or ("action" in n and "isin" in n):
        return "t212"
    if "unit cost p" in n or ("trade date" in n and ("value £" in n or "reference" in n)):
        return "hl"
    if ("investment" in n or "investment name" in n) and ("transaction type" in n or "type" in n or "cost" in n):
        return "vanguard"
    return None


# --------------------------------------------------------------------------- normalised model
@dataclass(frozen=True)
class Txn:
    date: date
    kind: str                       # BUY SELL DEPOSIT WITHDRAWAL TRANSFER_IN TRANSFER_OUT DIVIDEND INTEREST FEE OTHER
    account: str
    broker: str
    ticker: str | None = None       # Yahoo symbol, resolved via config
    raw_symbol: str | None = None
    name: str = ""
    quantity: float | None = None
    total_gbp: float = 0.0          # always positive; direction is in `kind`
    note: str = ""

    @property
    def unit_cost_gbp(self) -> float | None:
        return self.total_gbp / self.quantity if self.quantity else None


@dataclass
class ImportResult:
    txns: list[Txn] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unmapped: set[str] = field(default_factory=set)
    broker: str = ""
    file_kind: str = "transactions"


def parse_date(s: str) -> date:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%d/%m/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:19] if fmt == "%Y-%m-%d" else s, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise BrokerImportError(f"unrecognised date '{s}' (UK day-first formats and ISO are supported)") from exc


def parse_money(s: str | None) -> float | None:
    """'-2,750.00' / '(2,750.00)' / '£1,000' / '' -> float | None. Never treats blank as zero."""
    if s is None:
        return None
    t = s.strip().replace("£", "").replace(",", "").replace(" ", "")
    if t in ("", "-", "n/a", "N/A"):
        return None
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    try:
        v = float(Decimal(t))
    except InvalidOperation as exc:
        raise BrokerImportError(f"cannot parse number '{s}'") from exc
    return -v if neg else v


# --------------------------------------------------------------------------- symbol resolution
def _tokens(s: str) -> list[str]:
    return [t for t in re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split() if t not in ("plc", "the", "ord", "ordinary", "usd", "gbp")]


def names_match(a: str, b: str) -> bool:
    """Every token of the shorter name prefix-matches a token of the longer ('acc' ~ 'accumulating'). Needs >= 3 tokens."""
    ta, tb = _tokens(a), _tokens(b)
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(short) < 2:
        return False
    return all(any(l.startswith(s) or s.startswith(l) for l in long_) for s in short) and len(short) >= min(3, len(long_))


class SymbolResolver:
    def __init__(self, watch: Iterable[WatchItem], broker: str):
        self.by_isin = {w.isin: w.ticker for w in watch if w.isin}
        self.by_sym = {s.upper(): w.ticker for w in watch for k, s in w.broker_symbols.items() if k == broker}
        self.by_yahoo = {w.ticker.upper(): w.ticker for w in watch}
        self.names = [(w.name, w.ticker) for w in watch]

    def resolve(self, *, isin: str | None = None, symbol: str | None = None, name: str | None = None) -> str | None:
        if isin and isin.strip().upper() in {i.upper() for i in self.by_isin}:
            return next(t for i, t in self.by_isin.items() if i.upper() == isin.strip().upper())
        if symbol:
            s = symbol.strip().upper()
            if s in self.by_sym:
                return self.by_sym[s]
            if s in self.by_yahoo:
                return self.by_yahoo[s]
        if name:
            for n, t in self.names:
                if names_match(name, n):
                    return t
        return None


def _csv_rows(path: str | Path) -> tuple[list[str], list[dict[str, str]]]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rd = csv.DictReader(fh)
        if not rd.fieldnames:
            raise BrokerImportError(f"{path}: empty file / no header row")
        return list(rd.fieldnames), [r for r in rd if any((v or "").strip() for v in r.values())]


# --------------------------------------------------------------------------- Trading 212
_T212_KIND = [(r"^(market|limit|stop|stop limit) buy", "BUY"), (r"^(market|limit|stop|stop limit) sell", "SELL"),
              (r"^deposit", "DEPOSIT"), (r"^withdrawal", "WITHDRAWAL"), (r"^dividend", "DIVIDEND"),
              (r"interest", "INTEREST"), (r"fee|charge", "FEE")]


def parse_trading212(path: str | Path, account: str, watch: Iterable[WatchItem]) -> ImportResult:
    headers, rows = _csv_rows(path)
    c = Columns(headers, "t212")
    action, tcol = c.find("action", True), c.find("time", True)
    qty, total = c.find("qty"), c.find("total", True)
    total_ccy = c.find("total_ccy")
    watch = list(watch)
    res, rs = ImportResult(broker="t212"), SymbolResolver(watch, "t212")
    total_unit = unit_from_header(total)
    for n, r in enumerate(rows, start=2):
        act = (r[action] or "").strip()
        kind = next((k for pat, k in _T212_KIND if re.search(pat, act.lower())), "OTHER")
        if kind == "OTHER":
            res.warnings.append(f"line {n}: skipped action '{act}'")
            continue
        ccy = (r.get(total_ccy) or "").strip() if total_ccy else (total_unit or "GBP")
        if ccy and ccy != "GBP":
            res.warnings.append(f"line {n}: '{act}' total is in {ccy}, not GBP — skipped (account currency must be GBP)")
            continue
        amt = parse_money(r[total])
        if amt is None:
            res.warnings.append(f"line {n}: '{act}' has no total — skipped")
            continue
        sym = (r.get(c.find("symbol") or "") or "").strip() or None
        isin = (r.get(c.find("isin") or "") or "").strip() or None
        nm = (r.get(c.find("name") or "") or "").strip()
        ticker = rs.resolve(isin=isin, symbol=sym, name=nm) if kind in ("BUY", "SELL", "DIVIDEND") else None
        if kind in ("BUY", "SELL") and ticker is None:
            res.unmapped.add(sym or isin or nm)
        q = parse_money(r.get(qty)) if qty else None
        res.txns.append(Txn(parse_date(r[tcol]), kind, account, "t212", ticker, sym, nm, abs(q) if q else None, abs(amt),
                            (r.get(c.find("notes") or "") or "").strip()))
    return res


# --------------------------------------------------------------------------- Hargreaves Lansdown
def parse_hargreaves(path: str | Path, account: str, watch: Iterable[WatchItem]) -> ImportResult:
    headers, rows = _csv_rows(path)
    c = Columns(headers, "hl")
    tcol, ncol, total = c.find("time", True), c.find("name", True), c.find("total", True)
    qty, price, ref = c.find("qty"), c.find("price"), c.find("ref")
    price_unit = unit_from_header(price) if price else None
    watch = list(watch)
    res, rs = ImportResult(broker="hl"), SymbolResolver(watch, "hl")
    for n, r in enumerate(rows, start=2):
        desc = (r[ncol] or "").strip()
        val = parse_money(r[total])
        q = parse_money(r.get(qty)) if qty else None
        refv = (r.get(ref) or "").strip().upper() if ref else ""
        if val is None:
            res.warnings.append(f"line {n}: no value — skipped")
            continue
        d = parse_date(r[tcol])
        low = desc.lower()
        if q:                                                           # a trade
            # direction: wording first, then quantity sign, then cash-flow sign (cash in = sale, cash out = purchase)
            if re.search(r"\bsold\b|\bsale\b", low):
                kind = "SELL"
            elif re.search(r"\bbought\b|\bpurchase\b", low):
                kind = "BUY"
            elif q < 0:
                kind = "SELL"
            else:
                kind = "SELL" if val > 0 else "BUY"
            if q < 0 and val < 0:
                res.warnings.append(f"line {n}: negative quantity with negative value — check direction")
            ticker = rs.resolve(name=desc, symbol=(r.get(c.find("symbol") or "") or None))
            if ticker is None:
                res.unmapped.add(desc)
            pu = (price_unit or "GBp")                                   # HL quotes unit cost in pence
            cost = parse_money(r.get(price)) if price else None
            if cost is not None and pu == "GBp" and q and abs(abs(val) - abs(q) * cost / 100) / max(abs(val), 1) > 0.05:
                res.warnings.append(f"line {n}: value £{abs(val):,.2f} differs >5% from qty×unit cost — fees/stamp duty or unit mismatch?")
            res.txns.append(Txn(d, kind, account, "hl", ticker, None, desc, abs(q), abs(val), refv))
            continue
        if re.search(r"transfer", low):
            kind = "TRANSFER_IN" if val > 0 else "TRANSFER_OUT"
        elif re.search(r"payment received|deposit|subscription|contribution|faster payment|cheque|direct debit", low) and val > 0:
            kind = "DEPOSIT"
        elif re.search(r"withdraw", low) or (val < 0 and re.search(r"payment", low)):
            kind = "WITHDRAWAL"
        elif re.search(r"dividend|distribution|income", low):
            kind = "DIVIDEND"
        elif re.search(r"interest", low):
            kind = "INTEREST"
        elif re.search(r"fee|charge|commission", low):
            kind = "FEE"
        else:
            res.warnings.append(f"line {n}: skipped '{desc}'")
            continue
        res.txns.append(Txn(d, kind, account, "hl", None, None, desc, None, abs(val), refv))
    return res


# --------------------------------------------------------------------------- Vanguard UK
def parse_vanguard(path: str | Path, account: str, watch: Iterable[WatchItem]) -> ImportResult:
    headers, rows = _csv_rows(path)
    c = Columns(headers, "vanguard")
    tcol, ncol, acol = c.find("time", True), c.find("name", True), c.find("action", True)
    total, qty = c.find("total", True), c.find("qty")
    watch = list(watch)
    res, rs = ImportResult(broker="vanguard"), SymbolResolver(watch, "vanguard")
    for n, r in enumerate(rows, start=2):
        act, nm = (r[acol] or "").strip().lower(), (r[ncol] or "").strip()
        amt = parse_money(r[total])
        if amt is None:
            res.warnings.append(f"line {n}: no amount — skipped")
            continue
        d = parse_date(r[tcol])
        q = parse_money(r.get(qty)) if qty else None
        if re.search(r"\bbuy|purchase|bought", act):
            kind = "BUY"
        elif re.search(r"\bsell|sale|sold", act):
            kind = "SELL"
        elif re.search(r"transfer", act):
            kind = "TRANSFER_IN" if "in" in act or amt > 0 else "TRANSFER_OUT"
        elif re.search(r"deposit|subscription|contribution|payment in|cash in", act):
            kind = "DEPOSIT"
        elif re.search(r"withdraw|payment out|cash out", act):
            kind = "WITHDRAWAL"
        elif re.search(r"dividend|distribution|income", act):
            kind = "DIVIDEND"
        elif re.search(r"fee|charge", act):
            kind = "FEE"
        else:
            res.warnings.append(f"line {n}: skipped transaction type '{act}'")
            continue
        ticker = rs.resolve(name=nm, symbol=(r.get(c.find("symbol") or "") or None)) if kind in ("BUY", "SELL", "DIVIDEND") else None
        if kind in ("BUY", "SELL") and ticker is None:
            res.unmapped.add(nm)
        res.txns.append(Txn(d, kind, account, "vanguard", ticker, None, nm, abs(q) if q else None, abs(amt)))
    return res


PARSERS = {"t212": parse_trading212, "hl": parse_hargreaves, "vanguard": parse_vanguard}


def parse_file(path: str | Path, broker: str | None, account: str, watch: Iterable[WatchItem]) -> ImportResult:
    watch = list(watch)
    if broker in (None, "auto"):
        headers, _ = _csv_rows(path)
        broker = detect_broker(headers)
        if broker is None:
            raise BrokerImportError(f"could not auto-detect the broker from headers {headers}; pass --broker t212|hl|vanguard")
    if broker not in PARSERS:
        raise BrokerImportError(f"unknown broker '{broker}' (choose from {sorted(PARSERS)})")
    res = PARSERS[broker](path, account, watch)
    if res.unmapped:
        res.warnings.append(f"{len(res.unmapped)} instrument(s) not in config.yaml watchlist: {sorted(res.unmapped)} — add ISIN/broker_symbols or name")
    return res


# --------------------------------------------------------------------------- conversions
def to_ledger(txns: Iterable[Txn]) -> list[LedgerEntry]:
    m = {"DEPOSIT": "deposit", "WITHDRAWAL": "withdrawal", "TRANSFER_IN": "transfer_in", "TRANSFER_OUT": "transfer_out"}
    return [LedgerEntry(t.date, t.account, m[t.kind], Decimal(str(round(t.total_gbp, 2))), t.note or f"imported {t.broker}")
            for t in sorted(txns, key=lambda x: x.date) if t.kind in m]


def to_holdings(txns: Iterable[Txn], as_of: date | None = None) -> list[HoldingRow]:
    """Average-cost (Section 104 style) positions from BUY/SELL history. Requires the file to cover the full history."""
    book: dict[tuple[str, str], list[float]] = {}
    for t in sorted((x for x in txns if x.kind in ("BUY", "SELL") and x.ticker and x.quantity), key=lambda x: x.date):
        qty_cost = book.setdefault((t.account, t.ticker), [0.0, 0.0])
        if t.kind == "BUY":
            qty_cost[0] += t.quantity
            qty_cost[1] += t.total_gbp
        else:
            avg = qty_cost[1] / qty_cost[0] if qty_cost[0] else 0.0
            sell = min(t.quantity, qty_cost[0])
            qty_cost[0] -= sell
            qty_cost[1] -= avg * sell
    stamp = (as_of or date.today()).isoformat()
    return [HoldingRow(a, tk, round(q, 6), round(c / q, 4), stamp, "imported")
            for (a, tk), (q, c) in sorted(book.items()) if q > 1e-9]


def write_holdings(path: Path, new_rows: Sequence[HoldingRow], accounts_replaced: set[str]) -> int:
    """Backup then replace the given accounts' (non-CASH) rows. Returns rows written."""
    keep: list[dict[str, str]] = []
    if path.exists():
        shutil.copy2(path, path.with_suffix(".csv.bak"))
        with open(path, newline="", encoding="utf-8-sig") as fh:
            keep = [r for r in csv.DictReader(fh) if not (r["account"] in accounts_replaced and r["ticker"] != "CASH")]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["account", "ticker", "quantity", "avg_cost_gbp", "as_of", "note"])
        w.writeheader()
        for r in keep:
            w.writerow({k: r.get(k, "") for k in w.fieldnames})
        for h in new_rows:
            w.writerow({"account": h.account, "ticker": h.ticker, "quantity": h.quantity, "avg_cost_gbp": h.avg_cost_gbp,
                        "as_of": h.as_of, "note": h.note})
    return len(new_rows)


def append_ledger(path: Path, entries: Sequence[LedgerEntry]) -> int:
    """Backup then append only entries not already present (dedup on date+account+type+amount)."""
    existing: set[tuple[str, str, str, Decimal]] = set()
    if path.exists():
        shutil.copy2(path, path.with_suffix(".csv.bak"))
        with open(path, newline="", encoding="utf-8-sig") as fh:
            existing = {(r["date"], r["account"], r["type"], Decimal(r["amount_gbp"])) for r in csv.DictReader(fh)}
    fresh = [e for e in entries if (e.date.isoformat(), e.account, e.type, e.amount) not in existing]
    new_file = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(["date", "account", "type", "amount_gbp", "note"])
        for e in fresh:
            w.writerow([e.date.isoformat(), e.account, e.type, f"{e.amount:.2f}", e.note])
    return len(fresh)


# --------------------------------------------------------------------------- CLI
def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m engine.broker_import", description=__doc__.split("\n\n")[0])
    ap.add_argument("file")
    ap.add_argument("--broker", choices=["auto", "t212", "hl", "vanguard"], default="auto")
    ap.add_argument("--account", required=True, help="account id from config.yaml, e.g. T212_ISA")
    ap.add_argument("--config", default=None)
    ap.add_argument("--write", action="store_true", help="write to data/*.csv (a .bak copy is made); default is a dry run")
    a = ap.parse_args(argv)
    cfg: AppConfig = load_config(a.config)
    if a.account not in cfg.accounts:
        print(f"unknown account '{a.account}'. Known: {sorted(cfg.accounts)}", file=sys.stderr)
        return 2
    try:
        res = parse_file(a.file, a.broker, a.account, cfg.watchlist)
    except BrokerImportError as exc:
        print(f"IMPORT FAILED: {exc}", file=sys.stderr)
        return 1
    ledger, holdings = to_ledger(res.txns), to_holdings(res.txns)
    kinds: dict[str, int] = {}
    for t in res.txns:
        kinds[t.kind] = kinds.get(t.kind, 0) + 1
    print(f"{res.broker}: {len(res.txns)} transactions {kinds}")
    print(f"  -> {len(ledger)} ledger rows, {len(holdings)} positions (average cost)")
    for h in holdings:
        print(f"     {h.account:10} {h.ticker:8} {h.quantity:>12,.4f} @ £{h.avg_cost_gbp:,.4f}")
    for w in res.warnings:
        print(f"  ! {w}")
    print("  note: holdings are only correct if the file covers the account's FULL history; cash is not derived.")
    if a.write:
        print(f"  wrote {write_holdings(cfg.holdings_csv, holdings, {a.account})} holdings rows -> {cfg.holdings_csv} (backup .bak)")
        print(f"  appended {append_ledger(cfg.contributions_csv, ledger)} new ledger rows -> {cfg.contributions_csv} (backup .bak)")
    else:
        print("  dry run — re-run with --write to save.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
