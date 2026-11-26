[README.md](https://github.com/user-attachments/files/35486517/README.md)
# UK Stocks & Shares ISA — Command Centre & Handbook · v0.2

A local, dark-mode, Bloomberg-style terminal for managing a UK Stocks & Shares ISA against **your own written handbook**.
Free data only (Twelve Data / Finnhub free tiers if you add keys, Yahoo Finance always, Bank of England for the gilt yield).
Nothing leaves your machine except ticker symbols and the search text you type.

> **Quantitative modelling and personal handbook tool only. Not FCA-regulated financial advice.**
> It turns rules *you* wrote into flags and arithmetic. It does not know your circumstances.
> https://github.com/Inxhil/UK-ISA-Compliance-Terminal/issues/1
> https://github.com/user-attachments/assets/44ffa516-0d66-45f5-af7a-6cfd05da92ef

<img width="1902" height="988" alt="DEMO PIC FIRST" src="https://github.com/user-attachments/assets/f80b067c-20c6-4b20-b422-1bdac0b1cc6c" />

<img width="1911" height="989" alt="DEMO PIC 1" src="https://github.com/user-attachments/assets/58115402-4586-486f-83b3-203cb46c4210" />

<img width="1916" height="993" alt="DEMO PIC 2" src="https://github.com/user-attachments/assets/964253cf-09c3-473d-b593-784f7f10035a" />

<img width="1919" height="995" alt="DEMO PIC 3" src="https://github.com/user-attachments/assets/9c2ff155-da47-4621-a70a-dcaff6e3ac72" />

```bash
pip install -r requirements.txt
python setup_isa.py           # optional first-run wizard: level, remaining allowance, monthly £, API keys
python terminal.py            # live data; falls back to clearly-labelled DEMO if offline
python terminal.py --demo     # synthetic data, no network (good for a first look)
python terminal.py --level beginner      # override the saved experience level for one run
python terminal.py --snapshot # print the weekly memo to stdout and exit
python -m pytest -q           # 400+ offline tests
```
Needs Python ≥ 3.11 and a terminal ≥ 150×40 (true-colour recommended).

## What's new in v0.2

| | |
|---|---|
| **Provider chain** | Twelve Data + Finnhub (free keys) → yfinance → Stooq, with disk caching, a persisted credit budget and a unit-jump tripwire. Status bar shows the active sources: `[LIVE · TD + FH + YF]`. |
| **Pane 5 — News & Catalysts** | Company + macro headlines with source, relative time, 2–3 sentence summary, ticker badges, and a clickable article link. Filters: *All Portfolio · Selected Ticker · Macro / UK Market*. A compact feed also sits in the Tear-Sheet. |
| **20-ticker universe** | Core ETFs · UK & Dividend · Investment Trusts · Global Stocks, with category pills in the Watchlist. |
| **Search any ticker** | Press `/`, type `BARC.L`, `TSLA`, `brk.b` or even `barc` → an on-demand tear-sheet, and one click (**★ Save to watchlist**) writes it to `config.yaml`. |
| **Experience levels** | `l` (or click the badge in the status bar) cycles **Beginner → Intermediate → Advanced**; persisted in `ui.experience_level`. |
| **Charts & income** | `1M · 6M · 1Y · 5Y` timeframe toggles and a projected **annual tax-free ISA dividend income** panel. |
| **`setup_isa.py`** | Guided first-run wizard (hidden prompts for keys; backups before any change). |

## The five panes — keys: `1-5`/`F1-F5`, `[` `]`, `r` refresh, `a` auto, `l` level, `f` filter, `t` timeframe, `/` search, `o` sort/open, `m` cash, `c` co-pilot, `Esc` leave a box, `?` help, `q` quit

| Pane | What it shows |
|---|---|
| **1 Command Desk** | Prioritised **Terminal Signals** (`ACCUMULATE - Underweight & >15% Analyst Upside`, `TRIM WARNING - Exceeds 15% Handbook Cap`, `TAX ALERT - £7,500 Allowance Expiring in 138 days`, `BED & ISA …`). Select one to see *why*. Beside it: this month's exact-£ **buy plan (no selling)** and the ISA ledger. |
| **2 Watchlist · Analysts** | All 20 names with sleeve pills (`All · Core ETFs · UK & Div · Trusts · Global Stocks`): price (GBP), day %, consensus, target, upside, yield, valuation, trend, weight vs target, top signal. |
| **3 Tear-sheet** | Candlesticks + SMA lines (`1M/6M/1Y/5Y`), analyst target range & rating distribution, fundamentals, **annual tax-free dividend income**, news, and the ISA Impact Simulator. Search bar + Save button for unlisted tickers. |
| **4 Handbook · Co-Pilot** | `handbook.md` rendered, plus a local rule-based co-pilot (`health`, `memo`, `tax`, `buy 750`, `explain NVDA`, `sim VUAG.L 2000`, `prompt`). Its wording follows your level. |
| **5 News · Catalysts** | Described above. `Enter` / `o` (or clicking the link) opens the selected article in your browser; only `http(s)` links ever open. |

### Experience levels

Levels change **wording and density, never numbers**.

| | Beginner | Intermediate | Advanced |
|---|---|---|---|
| Signals | plain-English labels + **"What This Means"** under each | one-line **rule summaries** | raw rule IDs (`R-CAP-01`) + full rationale |
| Watchlist | Valuation (*Good Value / Fair / Expensive*), Momentum (*Steady / Overheated / Beaten Down*), Analysts say, Income, Your ISA | yield, forward P/E, analyst rating & upside, sector | all 16 columns incl. RSI, SMA trend, score |
| Tear-sheet | "At a glance" badges, no SMA lines | fundamentals + **sector weights** | all technicals, `GBp`/`GBX` **unit-audit** line, ISA Impact Simulator |
| Desk extras | a visual **£20,000 allowance bar** | the monthly buy planner | the buy planner + ISA ledger |

## Data sources — what actually works on the free tiers (verified live, Nov 2026)

| Need | Source | Notes |
|---|---|---|
| US quotes | Finnhub `/quote` | real-time, 60 calls/min |
| US daily history | Twelve Data `/time_series` | 1 credit each; **no dividends column** → yield from Finnhub |
| FX (`GBPUSD=X`) | Twelve Data forex | falls back to Yahoo |
| **LSE (`.L`) quotes & history** | **yfinance** | **Twelve Data's free plan refuses LSE** ("available starting with the Grow plan"). The chain spends *one* credit to find out, remembers it for 24 h, and uses yfinance. If you upgrade, it detects LSE access automatically; Twelve Data's `GBX` pence are then normalised to pounds like Yahoo's `GBp`. |
| Analyst consensus (US) | Finnhub `/stock/recommendation` | |
| Price targets | yfinance | Finnhub's `/price-target` is premium |
| US fundamentals | Finnhub `/profile2` + `/metric` | yield/payout in percent, market cap in millions — handled |
| News | Finnhub `/company-news` (US) + `/news` (macro) · yfinance for UK names | UK company news needs yfinance's news endpoint |
| UK 10Y gilt | Bank of England IADB `IUDMNPY` | keyless; one polite retry on a throttled burst |
| Symbol search | Finnhub `/search` | |

Twelve Data's free tier is **8 credits/minute, 800/day**. A persisted budget (`.cache/td_budget.json`) is spent on held positions
first; when it runs out the chain simply uses another provider or the cache — startup never waits on a quota.

## Configuration

* **`.env`** (git-ignored, mode 600) — `TWELVEDATA_API_KEY`, `FINNHUB_API_KEY`. Both optional; copy `.env.example` or run `setup_isa.py`.
  Keys travel in HTTP headers only and are never logged. Without keys the app behaves exactly like v0.1.
* **`config.yaml`** — accounts (which ISAs are *flexible*: check each provider), the 20-ticker watchlist with `sleeve:` tags, target
  weights (must sum to 100 with `cash_target_pct`; only your six sample holdings carry targets — the other 14 are watch-only),
  per-ticker alerts, `ter_pct` (approximate OCFs — verify against the KID), `look_through` sector weights (approximate — refresh
  quarterly), `aliases` (extra names for tagging news), and `ui.experience_level`.
* **`handbook.md`** — the policy. Only the fenced `yaml handbook-rules` block is machine-read; edit a number, press `r`, signals change.
* Edits made by the app (`l`, **★ Save**, the wizard) are line-based and comment-preserving, validated, and keep a `.bak`.

## How it fits together

```
handbook.md ──(handbook-rules)──┐
config.yaml ── accounts · universe · targets · ui ──┤
data/*.csv ── holdings · contributions ─────────────┤
.env ── optional API keys                           ▼
providers.py (TD · FH · Stooq clients, parsers) ─► market_intel.py ─► analyst_screener.py ─► portfolio.py ─► recommender.py ─► desk.py ─► terminal.py
  ratelimit.py (credit budget) · news.py (pure)      cache · stale-on-error     consensus, RSI, SMA     GBP values,     signals + rule IDs     literacy.py (levels)
                                                     breaker · unit-jump guard  unit reconciliation      sectors, OCF    conviction score       income.py (dividends)
```

## ISA rules modelled (2026/27) — see `CLAUDE.md` §1
Tax year 6 Apr 2026 – 5 Apr 2027; deadline = 00:00 London on 6 April (BST-aware, computed in UTC to avoid DST drift); £20,000
unified allowance; £4,000 LISA sub-limit *inside* it (bonus never counts); flexible-ISA replacement (same tax year, flexible accounts
only, per account); transfers aren't subscriptions; Bed & ISA = subscription + CGT disposal against the £3,000 exempt amount
(Section 104 average cost). Not modelled: same-day/30-day CGT matching, Cash-ISA-only limit changes from April 2027, JISA.

## Currency handling — see `CLAUDE.md` §2
`GBp`/`GBX` ÷ 100, `GBP` as-is (case-sensitive; never inferred from `.L`), USD→GBP = `USD / GBPUSD=X`, indices & FX are points/rates,
unknown codes are refused not guessed, analyst targets are unit-reconciled against price, a 100× price jump between refreshes is
rejected, and yield comes from dividend history (or Finnhub), never from Yahoo's unit-unreliable fields.

## Importing broker CSVs
```bash
python -m engine.broker_import --broker t212 --account T212_ISA export.csv          # dry run
python -m engine.broker_import --broker hl   --account HL_ISA   export.csv --write  # backs up, then writes
```
Trading 212, Hargreaves Lansdown and Vanguard UK parsers are header-alias driven and **fail loudly, listing the headers they saw** (I could not verify them against real exports).

## Known limits (read these)
* **Valuation vs 5-year average** is *price* vs its own 5-year mean price, not a P/E comparison (no free historical-earnings source).
* **UK company news** depends on yfinance's news endpoint; on networks that block it the UK names show no headlines (US names and macro still work). Finnhub article links are Finnhub redirect URLs that forward to the publisher.
* The **Stooq** fallback is keyless and states no currency, so it can only supply a price when a currency is already known from an earlier source; it could not be reached from my test environment, so it is covered by parser tests only.
* Dividend income is a **trailing-12-month projection**, not a forecast. Accumulating funds are shown as reinvested, not cash.
* `ISIN`s in `config.yaml` are included only where I'm certain; check before relying on them for imports.
* Tests are offline by default; `RUN_LIVE=1 python -m pytest -q tests/test_live_smoke.py` exercises the real providers with your keys.
