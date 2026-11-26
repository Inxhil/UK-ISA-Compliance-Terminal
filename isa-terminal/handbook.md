# 📘 Personal Investment Handbook & SOP

> **Living document.** Edit freely. The terminal re-reads this file on every refresh and renders it in Pane 4.
> Only the fenced `yaml handbook-rules` block in §0 is machine-parsed; everything else is prose for you.
> **The philosophy, risk and allocation figures below are *starter placeholders* — replace them with your own.**
>
> *Quantitative modelling and personal handbook tool only. Not FCA-regulated financial advice.*

---

## 0. Machine-readable rules

Every threshold the **Terminal Signals** engine uses lives here. Change a number, press `r` in the terminal, and
the signals change. Rule IDs (`R-…`) are cited in each signal so you can see *which rule fired*.

```yaml handbook-rules
position:                       # R-CAP-01 / R-CAP-02 / R-REB-01
  max_single_position_pct: 15   # hard cap on any one holding, % of total ISA value
  cap_applies_to: [stock, trust]  # kinds the cap covers. Add `etf` to also cap broad index funds (core is exempt by default)
  max_sector_pct: 40            # look-through sector cap (incl. ETF constituents)
  underweight_trigger_pp: 2.0   # flag ACCUMULATE when this many pp below target
  overweight_trigger_pp: 3.0    # flag STOP-ADDING when this many pp above target
costs:                          # R-OCF-01
  max_ocf_pct_passive: 0.25     # index trackers / ETFs
  max_ocf_pct_active: 0.80      # actively managed funds / investment trusts
quality:                        # R-DIV-01 / R-VAL-01
  min_dividend_cover: 1.5       # EPS / DPS; only tested on dividend payers (yield > 0.5%)
  min_dividend_yield_for_cover_test_pct: 0.5
  max_peg: 2.5
  max_forward_pe: 40
analyst:                        # R-UPS-01 / R-UPS-02
  min_upside_pct: 15            # mean target vs price needed for a conviction ACCUMULATE
  min_analysts: 5               # fewer than this and consensus is ignored
  review_below_target_pct: -5   # price this far ABOVE the mean target => REVIEW
technicals:                     # R-RSI-01 / R-TRD-01 / R-VAL-02
  rsi_oversold: 30
  rsi_overbought: 70
  caution_below_sma200_pct: -10
  rich_vs_5y_avg_pct: 40        # price premium to 5-year average price flagged as RICH
  cheap_vs_5y_avg_pct: -15
tax:                            # R-TAX-01 / R-TAX-02 / R-BED-01
  alert_days: [90, 30, 7]       # escalate MEDIUM -> HIGH -> CRITICAL as the deadline nears
  practical_cutoff_days: 3      # brokers need cleared funds before 5 April; plan to be done this early
  bed_and_isa_min_gain_gbp: 100 # ignore Bed & ISA candidates with trivial gains
cash:                           # R-CSH-01
  reserve_target_pct: 5
  max_cash_drag_pct: 8
drawdown:                       # R-DD-01  (tiers are cumulative, measured from the trailing 52-week high)
  reference_ticker: "VWRP.L"
  tiers:
    - {drop_pct: 10, deploy_pct_of_reserve: 25}
    - {drop_pct: 20, deploy_pct_of_reserve: 35}
    - {drop_pct: 30, deploy_pct_of_reserve: 40}
scoring:                        # composite conviction score weights (renormalised if a factor is missing)
  valuation: 30
  consensus: 25
  momentum: 15
  allocation: 20
  quality: 10
```

---

## 1. Investment philosophy

1. **Time in the market, not timing the market.** 15+ year horizon. Contributions are automatic and monthly;
   the signals below tell me *where* the next pound goes, not *whether* to invest.
2. **Core–satellite.** ~55% in low-cost global index trackers (the core). ≤ 45% in satellites: a growth trust,
   and a handful of high-conviction individual stocks.
3. **Own the wrapper first.** The ISA allowance is use-it-or-lose-it. Fill the £20,000 before taking taxable risk.
4. **Costs compound.** Blended OCF target ≤ 0.20%. Pay for active management only where I can name the edge.
5. **Rebalance with new cash, not sales.** Inside an ISA a sale is tax-neutral, but it burns conviction and
   transaction costs, and *does not* free up allowance. Dilute overweights by directing contributions elsewhere.
6. **Conviction ≠ concentration.** Any single position is capped (R-CAP-01) however much I like it.
7. **Write it down.** Every discretionary trade needs a one-line reason in the review log (§10).

## 2. Risk tolerance

| Dimension | Statement |
|---|---|
| Horizon | 15–25 years (retirement-oriented; LISA locked until 60 unless first home) |
| Max drawdown I can sit through without selling | **−35%** portfolio drawdown |
| Risk capacity | Stable income; 6-month emergency fund held **outside** the ISA |
| Asset class | 100% equities inside the ISA; bonds / cash reserve handled elsewhere (small 5% dry-powder cash sleeve here) |
| Currency | Comfortable with ~55–60% USD look-through exposure; no hedging |
| Leverage / derivatives / penny stocks | **Never** |

## 3. Target allocation

Per-ticker target weights live in `config.yaml` (so the buy-planner can read them). They must sum to 100%.

| Sleeve | Target | Holdings |
|---|---|---|
| Global core index | 55% | VWRP.L 35%, VUAG.L 20% |
| Growth trust | 12% | SMT.L |
| Single stocks | 28% | MSFT 12%, AZN.L 10%, NVDA 6% |
| Cash / dry powder | 5% | uninvested ISA cash |

**Sector guard-rails (look-through, incl. ETF constituents):** Technology ≤ 40% (R-CAP-02) · Healthcare 8–15% ·
Financial Services ≥ 10% · no other sector > 20%.
**Region (indicative):** US 55–65% · UK 8–12% · Europe ex-UK 8–12% · Asia/EM 10–15%.

## 4. Screening rules

Applied to every holding and watchlist name. A failure raises a **SCREEN FAIL** / **REVIEW** signal citing the rule.

| ID | Rule | Applies to | Threshold (§0) |
|---|---|---|---|
| **R-OCF-01** | Ongoing charge (OCF/TER) must not exceed the limit | ETFs, trusts, funds | passive ≤ 0.25% · active ≤ 0.80% |
| **R-DIV-01** | Dividend cover (EPS ÷ DPS) must be at least 1.5× | dividend payers (yield > 0.5%) | `min_dividend_cover` |
| **R-VAL-01** | PEG ≤ 2.5 **or** forward P/E ≤ 40 (one must pass) | single stocks | `max_peg`, `max_forward_pe` |
| **R-UPS-01** | Mean analyst target ≥ +15% above price, from ≥ 5 analysts, to qualify as a *conviction* ACCUMULATE | single stocks | `min_upside_pct` |
| **R-UPS-02** | Price > 5% *above* the mean target ⇒ REVIEW (consensus says it's priced in) | single stocks | `review_below_target_pct` |
| **R-RSI-01** | RSI(14) < 30 = oversold (accelerate DCA); > 70 = overbought (stage entries, don't chase) | all | `rsi_*` |
| **R-TRD-01** | Price > 10% below the 200-day SMA ⇒ CAUTION (don't catch a falling knife: scale in) | all | `caution_below_sma200_pct` |
| **R-VAL-02** | Price > +40% vs its own 5-year average ⇒ RICH; < −15% ⇒ CHEAP (a *price-based proxy*, not P/E) | all | `rich_/cheap_vs_5y_avg_pct` |

## 5. Position & concentration rules

| ID | Rule |
|---|---|
| **R-CAP-01** | **No single stock or investment trust may exceed 15% of total ISA value.** Breach ⇒ TRIM WARNING. First response: dilute with new cash; sell only if dilution would take > 3 months. *Broad index ETFs (the core sleeve, e.g. VWRP 35% / VUAG 20%) are exempt by default — a diversified tracker is not single-name risk; they are policed by the sector cap instead. Change `cap_applies_to` in §0 to include `etf` if you want them capped too.* |
| **R-CAP-02** | No look-through sector may exceed 40%. |
| **R-REB-01** | Flag ACCUMULATE when a holding is ≥ 2 pp under target; flag STOP-ADDING when ≥ 3 pp over target. |
| **R-CSH-01** | Hold ~5% cash; deploy anything above 8% (cash drag). |
| **R-DD-01** | Drawdown deployment tiers — see §9. |

## 6. SOP — monthly contribution (≈ 10 minutes)

- [ ] Open the terminal → **Pane 1 (Command Desk)**. Read any CRITICAL / HIGH signals first.
- [ ] Enter this month's cash in the **Monthly Cash** box; read the **Buy Plan** (exact £ per ticker, no selling).
- [ ] Sanity-check each line against its signal rationale. Skip a line only if you can write down why (§10).
- [ ] Pay cash into the ISA, wait for cleared funds, place orders (limit orders on LSE; avoid the first/last 15 min).
- [ ] Update `data/holdings.csv` and `data/contributions.csv` (or import the broker CSV — `python -m engine.broker_import`).
- [ ] Confirm **Allowance Left** in the status bar matches the broker's own figure.

## 7. SOP — April tax-year-end top-up (new year starts 6 April)

**Before 5 April (statutory deadline = end of 5 April, London time):**
- [ ] **T-90 days:** Terminal shows the first MEDIUM `TAX ALERT`. Compute `remaining ÷ months left` and set the monthly standing order to hit it.
- [ ] **T-30 days:** Alert escalates to HIGH. Decide the lump-sum source (savings, bonus, GIA Bed & ISA proceeds).
- [ ] Check **flexible-ISA headroom**: withdrawals taken *this* tax year that are still replaceable (Pane 1 → ISA ledger). Only on accounts marked flexible. Replacement credit expires on 5 April.
- [ ] **LISA:** every unused £1 of the £4,000 sub-limit forfeits £0.25 of government bonus. Top up to £4,000 if the money is not needed before 60.
- [ ] **T-7 days (CRITICAL):** fund by the *practical cut-off* (deadline minus `practical_cutoff_days`). Bank transfers and debit-card limits cause misses.
- [ ] Verify nothing is over-subscribed (Terminal shows a red **BREACH** banner if net used > £20,000 or LISA > £4,000).
- [ ] Do **not** rush into a bad trade to use up cash — the *subscription* is what counts; cash can sit in the ISA.

**After 6 April:**
- [ ] Roll the ledger: the terminal switches tax year automatically. Re-set the monthly standing order.
- [ ] Re-run the buy plan with a fresh £20,000 allowance; schedule Bed & ISA (§8) early in the year.

## 8. SOP — Bed & ISA (CGT annual exempt amount £3,000)

Selling a GIA holding and re-buying it in the ISA **uses ISA allowance** and **crystallises a CGT gain** in the GIA.

- [ ] List GIA holdings with unrealised gains (Pane 1 → *Bed & ISA* signal, or `holdings.csv` rows with a GIA account).
- [ ] Remaining CGT allowance = **£3,000 − gains already realised this tax year** (`cgt.realised_gains_ytd_gbp` in `config.yaml`).
- [ ] Prefer positions with the **smallest gain per £ of value** first; realise losses first (they offset gains).
- [ ] Sell only enough to keep the realised gain ≤ remaining allowance. (Model assumes Section 104 average cost — check your broker's cost basis.)
- [ ] Confirm ISA headroom ≥ proceeds. If not, split across tax years (a second Bed & ISA after 6 April uses a fresh allowance).
- [ ] Re-buy inside the ISA the same day to stay invested. Allow for spread and (on UK-incorporated shares) 0.5% stamp duty — ETFs are exempt.
- [ ] Record the disposal (date, proceeds, gain) — it must go on the Self Assessment *capital gains* page if proceeds or gains exceed the reporting thresholds.
- [ ] Update `cgt.realised_gains_ytd_gbp`.

## 9. SOP — market drawdown deployment

Reference: trailing 52-week high of `drawdown.reference_ticker` (default `VWRP.L`). Dry powder = uninvested ISA cash above the floor.

| Drawdown from high | Action | Handbook ID |
|---|---|---|
| −10% | Deploy **25%** of reserve cash into core underweights (VWRP/VUAG first) | R-DD-01 tier 1 |
| −20% | Deploy a further **35%** — satellites allowed if the signal engine shows ≥ 15% analyst upside | R-DD-01 tier 2 |
| −30% | Deploy the remaining **40%**; pause any trims; re-read §2 — *am I still within my −35% tolerance?* | R-DD-01 tier 3 |

Checklist:
- [ ] Don't sell to raise cash during a drawdown (§1.5). Use new contributions + the reserve sleeve.
- [ ] Deploy in **tranches over ≥ 2 weeks** unless RSI(14) < 30 on the core tracker.
- [ ] Log each tranche in §10 with the drawdown % at the time.
- [ ] If the drawdown has already exceeded my tolerance, **stop and review the plan**, don't add leverage or switch strategy mid-crash.

## 10. Weekly review & Investment Committee memo

Every Sunday (Pane 4 → **Weekly Memo**):
1. Portfolio health score and what moved it.
2. Top 3 signals and the handbook rule behind each.
3. Allowance pace vs. required monthly run-rate.
4. Decisions taken / deferred, with a one-line reason.

### Review log

| Date | Decision | Reason | Rule |
|---|---|---|---|
| 2026-11-18 | Set up Command Centre; no trades | Baseline | — |
