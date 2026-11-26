"""News normalisation, summarising, tagging and filtering (pure functions — no network).

Providers hand back differently-shaped dicts (Finnhub, yfinance). Everything is normalised into :class:`NewsItem` here, so
the UI only ever deals with one shape: source, relative time, headline, 2–3 sentence summary, ticker badge(s), URL.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Mapping, Sequence

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“‘(£$])")
_ALNUM = re.compile(r"[^a-z0-9]+")

UK_TERMS = re.compile(
    r"\b(UK|U\.K\.|Britain|British|England|English|London|FTSE|sterling|pound|gilts?|Bank of England|BoE|Bailey|Reeves|"
    r"Chancellor|Starmer|Westminster|Downing Street|Brexit|ONS|HMRC|Ofgem|Ofcom|CMA|LSE)\b", re.I)

FILTER_LABELS = {"all": "All Portfolio", "ticker": "Selected Ticker", "macro": "Macro / UK Market"}
FILTER_ORDER = ("all", "ticker", "macro")


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str
    url: str
    summary: str
    ts: float | None                       # epoch seconds (UTC)
    tickers: tuple[str, ...] = ()
    scope: str = "company"                 # company | macro
    uk: bool = False
    category: str = ""


@dataclass(frozen=True)
class TagTarget:
    ticker: str
    name: str
    kind: str = "stock"
    aliases: tuple[str, ...] = ()


# --------------------------------------------------------------------------------------- text
def strip_html(text: object) -> str:
    if text is None:
        return ""
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", str(text)))).strip()


def _norm(s: str) -> str:
    return _ALNUM.sub(" ", s.lower()).strip()


def clean_summary(summary: object, headline: str = "", max_sentences: int = 3, max_chars: int = 360) -> str:
    """HTML-free summary of at most ``max_sentences`` sentences / ``max_chars``. Returns '' when the 'summary' just
    repeats the headline (common with RSS-derived items, e.g. ``'<headline>  Reuters'``)."""
    s = strip_html(summary)
    if not s:
        return ""
    ns, nh = _norm(s), _norm(headline)
    if nh and (ns == nh or ns.startswith(nh) and len(ns) - len(nh) < 25 or nh.startswith(ns)):
        return ""
    parts = [p for p in _SENT.split(s) if p.strip()]
    out = " ".join(parts[:max_sentences])
    if len(out) > max_chars:
        cut = out[:max_chars].rsplit(" ", 1)[0].rstrip(",;:- ")
        out = cut + "…"
    return out


def parse_ts(value: object) -> float | None:
    """Epoch seconds from an int/float epoch (s or ms) or an ISO-8601 string; ``None`` if unparseable."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except ValueError:
        return None


def rel_time(ts: float | None, now: float | None = None) -> str:
    """'just now' · '12m ago' · '5h ago' · '3d ago' · '2w ago' · '—'."""
    if ts is None:
        return "—"
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    d = now - ts
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    if d < 86400 * 14:
        return f"{int(d // 86400)}d ago"
    return f"{int(d // (86400 * 7))}w ago"


def is_uk_relevant(text: str) -> bool:
    return bool(UK_TERMS.search(text or ""))


def finnhub_to_ticker(sym: str) -> str:
    """Finnhub US class-share form ``BRK.B`` -> our Yahoo-style ``BRK-B``. (Never applied to UK ``.L`` symbols.)"""
    s = sym.strip().upper()
    return re.sub(r"^([A-Z]+)\.([A-Z])$", r"\1-\2", s)


def tag_tickers(text: str, targets: Sequence[TagTarget]) -> tuple[str, ...]:
    """Universe tickers mentioned in ``text``: the bare symbol as an upper-case word (>=3 chars), or — for single stocks —
    the first distinctive word of the company name, or a configured alias."""
    found: list[str] = []
    for t in targets:
        base = t.ticker.upper().removesuffix(".L").replace("-", ".")
        hit = len(base) >= 3 and re.search(rf"(?<![A-Za-z0-9]){re.escape(base)}(?![A-Za-z0-9])", text) is not None
        if not hit and t.kind == "stock":
            words = [w for w in re.findall(r"[A-Za-z][A-Za-z&'-]+", t.name) if w.lower() not in ("the", "plc", "inc", "corp", "corporation")]
            names = ([words[0]] if words and len(words[0]) >= 4 else []) + list(t.aliases)
            hit = any(re.search(rf"\b{re.escape(n)}\b", text, re.I) for n in names)
        elif not hit and t.aliases:
            hit = any(re.search(rf"\b{re.escape(n)}\b", text, re.I) for n in t.aliases)
        if hit:
            found.append(t.ticker)
    return tuple(found)


# --------------------------------------------------------------------------------------- normalisers
def from_finnhub(raw: Mapping, *, queried: str | None = None, scope: str = "company",
                 targets: Sequence[TagTarget] = ()) -> dict | None:
    """Finnhub ``/company-news`` or ``/news`` item -> provider-neutral dict (cache-friendly, JSON-safe)."""
    title = strip_html(raw.get("headline"))
    if not title:
        return None
    rel = [finnhub_to_ticker(x) for x in str(raw.get("related") or "").split(",") if x.strip()]
    return {"title": title, "publisher": str(raw.get("source") or ""), "link": str(raw.get("url") or ""),
            "published": None, "ts": parse_ts(raw.get("datetime")), "summary": strip_html(raw.get("summary")),
            "related": ([queried] if queried else []) + [r for r in rel if r != queried], "scope": scope,
            "category": str(raw.get("category") or "")}


def to_item(d: Mapping, targets: Sequence[TagTarget] = ()) -> NewsItem:
    """Provider-neutral dict -> :class:`NewsItem` (tags universe tickers, flags UK relevance, trims the summary)."""
    title = str(d.get("title") or "")
    summary = clean_summary(d.get("summary"), title)
    ts = d.get("ts") if d.get("ts") is not None else parse_ts(d.get("published"))
    related = tuple(d.get("related") or ())
    text = f"{title} {summary}"
    tagged = related or tag_tickers(text, targets)
    if related and d.get("scope") == "macro":
        tagged = tuple(dict.fromkeys(related + tag_tickers(text, targets)))
    return NewsItem(title=title, source=str(d.get("publisher") or d.get("source") or ""), url=str(d.get("link") or d.get("url") or ""),
                    summary=summary, ts=ts, tickers=tuple(tagged), scope=str(d.get("scope") or "company"),
                    uk=is_uk_relevant(text), category=str(d.get("category") or ""))


# --------------------------------------------------------------------------------------- merge & filter
def dedupe(items: Iterable[NewsItem]) -> list[NewsItem]:
    """One entry per headline (providers repeat the same wire story); prefers the item that has a summary."""
    best: dict[str, NewsItem] = {}
    for it in items:
        k = _norm(it.title)[:90]
        cur = best.get(k)
        if cur is None or (it.summary and not cur.summary) or (bool(it.summary) == bool(cur.summary) and len(it.tickers) > len(cur.tickers)):
            if cur is not None and cur.tickers and not it.tickers:
                it = NewsItem(**{**it.__dict__, "tickers": cur.tickers})
            best[k] = it
        elif it.tickers and not cur.tickers:
            best[k] = NewsItem(**{**cur.__dict__, "tickers": it.tickers})
    return list(best.values())


def filter_items(items: Iterable[NewsItem], mode: str, *, selected: str | None = None,
                 universe: Iterable[str] = ()) -> list[NewsItem]:
    """The three News Desk views.

    * ``all``    — everything about the portfolio/watchlist universe (company items + macro items that name a holding);
    * ``ticker`` — only items tagged with ``selected``;
    * ``macro``  — market & macro headlines, UK-relevant ones first.
    """
    uni = set(universe)
    items = list(items)
    if mode == "ticker":
        out = [i for i in items if selected and selected in i.tickers]
    elif mode == "macro":
        out = [i for i in items if i.scope == "macro"]
        out.sort(key=lambda i: (not i.uk, -(i.ts or 0)))
        return out
    elif mode == "all":
        out = [i for i in items if i.scope == "company" or (uni & set(i.tickers))]
    else:
        raise ValueError(f"unknown news filter '{mode}' (valid: {', '.join(FILTER_ORDER)})")
    out.sort(key=lambda i: -(i.ts or 0))
    return out
