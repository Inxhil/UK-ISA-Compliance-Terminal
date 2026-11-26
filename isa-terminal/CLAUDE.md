# CLAUDE.md — UK Stocks & Shares ISA Command Centre (v0.2)

Persistent rules for any AI assistant working in this repository. **These override convenience.**
If a request conflicts with a rule below, say so and ask — do not silently work around it.

## 0. What this project is

A local, dark-mode, Bloomberg-style **Textual** terminal (`terminal.py`) plus a pure-Python quant core (`engine/`)
that tracks a UK Stocks & Shares ISA against a living personal handbook (`handbook.md`) and `config.yaml`.

- **Not FCA-regulated advice.** It is a quantitative modelling + personal-handbook tool. Output is phrased as
  *signals / flags / rule triggers*, never "you should buy X". The footer disclaimer is mandatory and must read exactly:
  `Quantitative modelling and personal handbook tool only. Not FCA-regulated financial advice.`
- **Zero paid dependencies, keys optional.** With **no keys** the app is exactly v0.1: `yfinance` (free) is the only
  market-data source. With free-tier keys in `.env` (`TWELVEDATA_API_KEY`, `FINNHUB_API_KEY`) a **provider chain**
  (`engine/providers.py`) takes over (see §3). Every code path must still work with no keys, one key, or both.
- **Privacy.** Holdings, contributions and balances never leave the machine. Only ticker symbols (and search text the user
  typed into the search bar) are sent to data providers. Never put portfolio values, account IDs or the user's identity in a
  URL, header, log line or query. **API keys go in HTTP headers only — never in a URL — and are never printed, logged, put in
  an exception message, a test fixture, the README, a commit or a chat reply.** `.env` is git-ignored, mode 600, and must stay
  that way; `setup_isa.py` reads keys with a hidden prompt. Tests use obvious fake keys.

## 1. UK ISA tax law — 2026/27 (single source of truth: `engine/isa_engine.py::TAX_YEAR_RULES`)

| Item | Rule |
|---|---|
| Tax year | **6 April 2026 – 5 April 2027** (inclusive). The deadline is the **end of 5 April 2027, Europe/London time** = `2027-04-06T00:00:00+01:00` (BST). Never compute it in UTC, and never hard-code a UTC offset — use `zoneinfo.ZoneInfo("Europe/London")`. |
| Annual subscription limit | **£20,000, unified** across Stocks & Shares ISA, Cash ISA, Innovative Finance ISA and Lifetime ISA. |
| Lifetime ISA (LISA) | **£4,000 sub-limit that counts *inside* the £20,000** (not on top). The 25% government bonus (max £1,000/yr) is **not** a subscription and must never be counted against the limit. LISA is **never** flexible; withdrawals create no replacement room. |
| Flexible ISA | Cash withdrawn from a **flexible** ISA can be replaced **in the same tax year** without using allowance. Rules the engine enforces: (a) only accounts flagged `flexible: true` in `config.yaml` qualify; (b) only withdrawals made **in the same tax year** count — a withdrawal in March can't be replaced in April; (c) replacement credit is tracked **per account** (conservative reading — verify with the provider); (d) a withdrawal never *increases* the annual limit, it only lets you re-subscribe the same amount; (e) the net used is `max(0, subscriptions − eligible withdrawals)` per account. |
| Transfers | Transfers *between ISA providers* are **not** subscriptions (`transfer_in` / `transfer_out` rows are ignored for the allowance). |
| Bed & ISA | Selling in a GIA and re-buying inside the ISA **is** a new subscription (counts against £20,000) **and** a CGT disposal in the GIA. |
| CGT annual exempt amount | **£3,000** (2026/27). Gains inside the ISA are tax-free; GIA gains above the exempt amount are taxable. Engine assumes Section 104 pooled average cost; same-day / 30-day matching is **not** modelled. |
| Over-subscription | If net used > £20,000 or LISA > £4,000, flag a **CRITICAL breach**. Never silently cap it. |

Rules:
1. **Never edit a tax constant to make a test pass.** If a boundary test fails, the logic is wrong.
2. Tax-year logic must be **date-boundary exact**: 2027-04-05 → 2026/27; 2027-04-06 → 2027/28. A UTC timestamp
   of `2027-04-05T23:30Z` is already **6 April in London (BST)** and therefore belongs to the *next* tax year.
3. Rules are keyed by tax-year start year. Adding 2027/28 means adding one row, not editing logic.
   (Announced for April 2027 but **not modelled**: reduced Cash-ISA-only limit for under-65s. S&S ISA tracking is unaffected.)
4. Each user-facing tax statement must be traceable to a constant in `TAX_YEAR_RULES` or a rule ID in `handbook.md`.

## 2. LSE `GBp` (pence) vs `GBP` (pounds) and FX normalisation

London prices are the #1 source of 100× errors. Rules:

1. **Currency codes are case-sensitive.** `GBp` / `GBX` = **pence** (÷100). `GBP` = pounds. **Never** call
   `.upper()` / `.lower()` on a currency before comparing — that collapses pence into pounds.
2. **Never infer pence from the `.L` suffix.** Yahoo reports `AZN.L`/`SMT.L` in `GBp` but `VWRP.L`/`VUAG.L` in `GBP`.
   Use the currency Yahoo reports (chart metadata is the most reliable source), and allow a per-ticker
   `price_unit` override in `config.yaml` for the known mislabelled cases.
3. **Normalise every price-like field the same way** — last price, previous close, 52-week range, SMAs, and
   analyst targets (low / mean / median / high). Reconcile analyst targets against the live price: a target/price
   ratio near 100 or 0.01 means a unit mismatch and must be corrected, not displayed.
4. **Indices are points, not money.** `^FTSE`, `^GSPC` etc. (`instrumentType == INDEX`) are never currency-converted,
   even if Yahoo labels them `GBP`/`USD`.
5. **FX direction:** `GBPUSD=X` quotes **USD per 1 GBP** (≈1.3). `price_gbp = price_usd / GBPUSD`. A generic pair is
   `GBP{CCY}=X` = units of CCY per 1 GBP. If the FX rate is missing, **do not guess** — return `None` and surface it.
6. **Ratios are unit-free; compute them in native units** (upside %, P/E vs average, RSI, SMA crossovers). Only
   convert to GBP for *display of money* and for *portfolio weights/values*. Don't apply today's FX to history
   and then compute indicators on it.
7. **Every provider reports its own currency code and each is normalised by the same rules.** Twelve Data uses `GBX`
   (pence) for LSE stocks, Yahoo uses `GBp`, both mean pence; a code that is not in `engine/currency.py::SUBUNITS` (e.g. `GBx`)
   is **refused, not guessed** (price shown as unavailable). Providers return the *raw* vendor currency; only `currency.py`
   converts. A **unit-jump tripwire** (`models.unit_jump`) rejects a new price that is ~100x (or ~0.01x) the last good price of
   the same security (a provider flipping pence/pounds) and serves the last good value instead.
8. **Indices and FX pairs need no currency** (Twelve Data's forex quote has none): they are points/rates and are always shown.
9. **Dividend yield** is computed as trailing-12-month dividends ÷ price using the *same raw unit* from the same
   history call. Yahoo's `dividendYield` is now in **percent** (0.77 = 0.77%) while `trailingAnnualDividendYield`
   is a fraction and is wrong by 100× for some `GBp` tickers. Treat both as fallbacks only. **Twelve Data history has no
   dividends column**: yield is then *unknown* (never 0) and comes from Finnhub's `currentDividendYieldTTM` (percent).

## 3. Engineering standards

- Python ≥ 3.11, type hints everywhere, `from __future__ import annotations`.
- **Money:** the ISA allowance ledger uses `decimal.Decimal` (pence-exact). Orders are quantised to the penny with a
  largest-remainder pass so they sum **exactly** to the cash being invested. Market values may be `float`.
- **Pure functions first.** Calculations (`currency`, `technicals`, `isa_engine`, `recommender` rules) take plain data
  and have no I/O, so they are unit-testable without the network.
- **All network I/O lives in `engine/market_intel.py` and `engine/providers.py`** (clients + pure parsers) — nowhere else.
  It must: cache to `.cache/`, serve stale-on-error, batch requests, avoid yfinance's threaded downloader (SQLite
  `database is locked`), and degrade to clearly-labelled **DEMO** data if the network is unavailable. Demo data must never be
  presentable as live.
- **Provider routing (verified live on free keys, Nov 2026):** US quotes -> Finnhub; US daily history -> Twelve Data;
  FX (`GBPUSD=X`) -> Twelve Data forex; analyst consensus + US fundamentals + news -> Finnhub; **LSE (`.L`) -> yfinance** (Twelve
  Data refuses LSE on the free plan: 1 credit probe, remembered 24h; a paid plan is auto-detected and used); price targets ->
  yfinance (Finnhub's is premium); gilt -> Bank of England IADB `IUDMNPY`; Stooq is a keyless last resort.
  The status bar shows who served the data, e.g. `[LIVE · TD + FH + YF]`.
- **Respect rate limits by design, not by waiting:** Twelve Data free tier = 8 credits/min, 800/day. `engine/ratelimit.py`
  (`CreditBudget`, persisted to `.cache/`) is spent in priority order (held positions first); when it runs out the chain falls
  back to another provider or the cache. Never add a call path that can block the UI waiting for a quota to refill.
- **Experience levels (`ui.experience_level`: beginner | intermediate | advanced) change wording and density, never numbers.**
  All translation lives in `engine/literacy.py` (pure). Beginner text must contain no RSI/PEG/SMA/OCF/rule IDs — there is a
  property test over every signal the engine can emit; extend it when adding a signal type.
- **Edit `config.yaml` only through `engine/config_edit.py`** (line-based, comment-preserving, validated, with a `.bak`);
  never `yaml.dump` it. Links from the news feed open only if `http(s)` (`terminal.safe_url`).
- **Fixtures in `tests/fixtures/` are real recorded API responses** (trimmed, no keys). Re-record rather than hand-edit.
- **Fail loudly on bad input files, softly on bad market data.** A broker CSV with unknown headers raises an error
  listing the headers it saw; a missing analyst field renders as `—`, never as `0`.
- Tests live in `tests/` and run offline: `python -m pytest -q`. Live smoke tests are opt-in (`RUN_LIVE=1`).
- Do not add a paid API, a database server, or a heavyweight dependency without asking.
- **R-CAP-01 (15% single-position cap) applies to the kinds listed in the handbook's `position.cap_applies_to`**
  (default `[stock, trust]`). Broad index ETFs (the core sleeve, VWRP 35% / VUAG 20%) are exempt by default because a
  diversified tracker is not single-name risk; they are policed by the look-through sector cap (R-CAP-02). Never hard-code
  that list — read it from the rules.
- Demo fallback is **all-or-nothing**: never splice a demo price/FX/metadata value into live data, and never fabricate soft
  data (analysts, news, currency) when only that endpoint failed — show `—` / "unavailable" instead.
- Keep `handbook.md` the source of policy numbers (caps, thresholds). Don't duplicate them as literals in code — read
  them via `engine/config.py::load_handbook_rules`. Signals must cite the handbook rule ID that fired
  (`R-CAP-01`, `R-OCF-01`, …).

## 4. Commands

```bash
pip install -r requirements.txt
python terminal.py                 # live (falls back to DEMO if offline)
python terminal.py --demo          # force synthetic demo data
python terminal.py --live          # never fall back to demo data
python terminal.py --snapshot      # print desk + memo to stdout and exit
python terminal.py --level beginner   # override ui.experience_level for this run
python setup_isa.py                # first-run wizard: level, remaining allowance, monthly £, API keys (hidden prompt)
python -m pytest -q                # offline unit tests
RUN_LIVE=1 python -m pytest -q tests/test_live_smoke.py   # real network smoke test (uses the keys in .env; a few TD credits)
python -m engine.broker_import --broker t212 --account T212_ISA path/to/export.csv   # dry run; add --write to save
```

## 5. When editing the handbook

`handbook.md` is read by humans *and* parsed by code. Only the fenced block tagged ```` ```yaml handbook-rules ````
is machine-read; keep it valid YAML. Prose around it is free-form. After editing, run
`python -m pytest -q tests/test_config.py` to confirm the rules still parse.
