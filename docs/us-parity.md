# Re-benchmark against the FOMC app: chart-by-chart analysis

Every panel in the FOMC (US) app, what it shows and where its data comes from,
and the best Australian replication. **Every AU source below was probed live on
2 Oct 2026**: the series ID, frequency and latest period are what came back, not
what was assumed.

Grades:

- **Exact**: the same measure, published in Australia.
- **Close**: the same construction on the nearest AU series. The difference is
  stated on screen.
- **Substitute**: no AU counterpart exists, so the chart answers the same
  trading question with a different series. The reason is stated on screen.
- **Absent**: nothing in Australia can stand in. The panel says so instead of
  faking it.

Status: **done** (in commit `0b147bc`, *Restructure onto the FOMC app's front
end*), **build** (data verified, not yet built), or **blocked** (needs a
decision).

---

## 1. What the probe changed

Five things the AUS app's own notes say that the data no longer supports:

| Claim in the AUS app | What the probe found |
|---|---|
| "RBA D1 credit growth froze at the June 2019 EFS changeover; Private Credit stays on the seed" | Only the old *total* (`DGFAC12`) froze. **`DGFACNF12` (total credit excluding financial businesses, 12-month ended) is live to Aug 2026.** Private Credit can go live. |
| Westpac-MI consumer sentiment and NAB business conditions are "commercial, no free feed" | **RBA H3 publishes both**: `GICWMICS` (to Sep 2026) and `GICNBC` (to Aug 2026). Also private dwelling approvals (`GISPSDA`). |
| Melbourne Institute inflation expectations are "commercial" | **RBA G3** carries consumer (`GCONEXP`), business, union officials, market economists (1y and 2y) and the 10-year break-even, quarterly to Sep 2026. |
| "The RBA never attributes a vote", so nothing published can be checked | True for names, but **every statement publishes the split.** 2026 so far: Feb unanimous, Mar 5–4 to hike, May 8–1 to hike, Jun / Aug / Sep unanimous. That is a published layer a roster read can be reconciled against, the role the US dot plot plays. |
| Retail trade is the consumer series | **ABS discontinued Retail Trade (`RT`) at Jun 2025.** Its replacement, the **Household Spending Indicator (`HSI_M`), runs monthly to Aug 2026** by 14 categories. That is the AU twin of the US retail panels. |

Two further finds matter for the inflation tab:

- **The complete monthly CPI is in the ABS data API** (`CPI` v2.0.0): 161 index
  items in the full group → sub-group → class tree. It has seven measures,
  including *contribution to the monthly % change* (measure 6), so contribution
  stacks and the item heatmap can be built exactly.
- **The monthly CPI history starts in April 2024** (28 months). Anything that
  needs decades still runs on the quarterly CPI back to 1982.

---

## 2. Front end and pattern

| US element | AU replication | Grade | Status |
|---|---|---|---|
| `ui/tabs/` per section, `ui/sources.py`, `ui/sidebar.py`, `ui/verdict.py`, `ui/context.py` | Same modules. `Ctx` also carries both ASX strips and the curve read off them | Exact | done |
| Stylesheet, theme tokens, Streamlit config (Archivo + JetBrains Mono, amber select) | Copied; AU categorical palette and AU release families added | Exact | done |
| Header tape: meeting, countdown, as-of, blackout, SEP flag, live figures | Same; "SMP meeting" for SEP, cash rate added to the figures | Exact | done |
| Sticky top bar with the section `C.choice` | Same | Exact | done |
| `C.bar` / `C.board` / `C.choice` / `C.heading` control standard | Same components | Exact | done |
| Verdict rail: trade, view and edge, size, what to watch, export | Same, in A$ | Exact | done |
| Sidebar: refresh, meeting, as-of + Today, trade, sizing, view, save, recall/delete | Same. Picking a contract jumps to its meeting; a new meeting sizes in its own contract | Exact | done |
| Section order | Trade recs · Curve · Sizing · Path · Releases · Vote count · **SMP & forecasts** · Street calls · Macro · Labour · Inflation · Growth · Analysis · Data | Exact (SEP renamed) | 9 of 14 done |
| Pricing tab, kill criteria | Retired, as in the US version | Exact | done |
| "Your view, in moves" for a contract spanning several meetings (`core/moves.py`) | Port. It matters more here: an IR bill's 90 days usually span 1–2 meetings | Exact | build |

---

## 3. Curve

| US panel | US data | AU replication | AU data (verified) | Grade | Status |
|---|---|---|---|---|---|
| Control bar: instrument, spot EFFR, refresh | CME via Yahoo | Instrument IB / IR, spot cash, refresh | ASX settlement JSON | Exact | done |
| Board: curve as of, terminal, peak, trough, meetings priced | ZQ strip | Same off IB | ASX IB | Exact | done |
| Meeting ladder + priced path (step chart) | ZQ | Same; late-month meetings read off the next contract | ASX IB | Exact | done |
| Live vs typed, "Use the live number" | ZQ | Same | ASX IB | Exact | done |
| Strip table + cross-checks | ZQ | Same; cross-check note names the ES rate, not IORB | ASX IB | Exact | done |
| SR3 view: spans, basis vs fed funds path, basis chart | SR3, compounded SOFR | IR view: 90-day span, basis to the IB path, basis chart | ASX IR | Close: IR settles on **one** BBSW fix, not a compounded window. BBSW carries bank credit | done |
| SR3 scenario price grid | SR3 + SOFR fixings | **IR scenario grid**: 100 − (90-day mean cash under the path + held BBSW basis) | ASX IR, RBA F1 | Close: same desk tool, IR settlement maths | done |
| Calendar spreads, where the algebra closes | SR3 | Same on IR | ASX IR | Exact | done |
| Instrument history: one contract's trailing 6 months, "move bp" vs EFFR | Yahoo daily closes | ASX publishes **no** contract history (its `days` parameter is inert). (a) Settlement-rate history: AONIA for IB, BBSW for IR, against today's strip. (b) **New:** a daily snapshot of every ASX settlement, accrued to disk from now on, so a real contract history builds up | RBA F1 daily; own snapshots | (a) Substitute, (b) Exact once accrued | (a) done, (b) build |
| Meeting spread matrix | ZQ / SR3 | Same: one row per meeting (IB), one per contract (IR) | ASX | Exact | done |

---

## 4. Sizing and Path

| US panel | AU replication | Grade | Status |
|---|---|---|---|
| EV vs q curve + sensitivity table | Same | Exact | done |
| Kelly ladder board, growth curve, capital at risk, rung table | Same, A$, IB A$24.66/bp and IR level-dependent DV01 | Exact | done |
| Path risk MTM ladder | Same | Exact | done |
| Exit levels, exit table, stop-cost decomposition | Same | Exact | done |
| Monte Carlo fan + release markers + stop/target sweeps + multiplier table | Same. Volatility comes from 3-month BBSW's quiet days (no ASX price history) | Close: a vol proxy, stated on screen | done |
| Multi-meeting levels (moves mode) | Port with `core/moves.py` | Exact | build |

---

## 5. Releases

| US panel | US data | AU replication | AU data | Grade | Status |
|---|---|---|---|---|---|
| "Around the meeting" calendar, tier chips, past-quarter expander, family tints | FRED/OMB schedule | ABS/RBA rule-dated calendar | rule + typed | Exact | done |
| Actual column (latest revision for the reference period) | FRED | Unemployment rate, trimmed mean q/q and m/m y/y, WPI q/q, GDP q/q, decision outcome | ABS LF, CPI, RBA H4, ABS `ANA_EXP`, RBA F1 | Exact | done |
| Reaction card: three numbers, moves and q per scenario, playbook table, trigger lines, playbook-levels and P&L charts, saved cards | strip + FRED specs | Same, AU specs: Labour Force (employment Δk, unemployment rate ▼), CPI (trimmed mean q/q, headline q/q), monthly CPI (trimmed mean y/y, headline y/y), WPI q/q, GDP q/q. Market level and anchor price off IB | ASX IB | Exact | build |
| "What the street expects" consensus search | research agent | Same agent, AU releases | agent | Exact | **blocked** (§12) |

---

## 6. Vote count

| US panel | US data | AU replication | AU data | Grade | Status |
|---|---|---|---|---|---|
| Vote arithmetic board | roster | Same: 9-member Monetary Policy Board, majority, Governor's casting vote | roster | Exact | done |
| Roster editor (scores now vs last meeting, blocs, movers, score history) | roster file | Same columns; AU seats (Governor, Deputy, Treasury Secretary, 6 external) | `_roster.json` | Exact | partial: editor done; score roll and history to build |
| "Where the committee sits": hawk-dove transition chart with bloc icons | roster | Same chart | roster | Exact | build |
| *(no US equivalent)* | — | **Published vote history**: unattributed split at every decision since the 2025 reform. Checks whether your roster read reproduces the last split | RBA statements, scraped | AU-only addition: the "published layer" the US gets from dots | build |
| Speech roundup (agent, writes scores back) | research agent | Same for Board members' speeches and appearances | agent | Exact | **blocked** (§12) |

---

## 7. SEP & dots → SMP & forecasts

| US panel | US data | AU replication | AU data | Grade | Status |
|---|---|---|---|---|---|
| SEP median table with change vs prior round | FRED median-dot series | **SMP detailed forecast table**: GDP, consumption, unemployment, underutilisation, WPI, CPI, trimmed mean, cash-rate assumption, by half-year horizon, with the change vs the prior SMP | RBA SMP *Outlook* table 3.1 (scraped, every round); history in `*-by-horizon.xls` | Exact | build |
| Published dot plot (distribution, current vs prior round) | Fed SEP fig. 2 | **SMP cash-rate assumption path**, current vs prior round, against today's IB strip. The RBA publishes no dots: its "path" is the market path at the forecast date, so the useful read is how far the market has moved since | SMP table + ASX | Substitute: no distribution exists | build |
| "Your named read": one dot per participant | roster | Same editor and per-horizon dot panels for the 9 members, checked against the published median **and the last published vote split** | roster | Close: no published distribution to match | build |
| Balanced-approach rule grid on SEP medians | SEP + FRED | Same rule on SMP trimmed mean and unemployment forecasts: π* = 2.5 (band midpoint), editable r* and u* (RBA NAIRU ≈ 4.5) | SMP + RBA F1 | Exact | build |
| Atlanta Fed Taylor-rule heatmap (5 r* × 6 gaps) | Atlanta Fed workbook | **Computed in-app**: r* estimates (RBA published range low/mid/high, real-rate rule-of-thumb) × gap measures (unemployment gap at several NAIRU estimates, underutilisation gap, output-gap proxy). Shaded against the cash rate | RBA H1/H5/G1, editable inputs | Substitute: no AU publisher, so the grid is built, not read | build |

---

## 8. Street calls, Trade recs (research agent)

| US panel | AU replication | Grade | Status |
|---|---|---|---|
| Street call book: each bank's call for the meeting, path, consensus, dispersion, changes | Same for the AU desks (CBA, Westpac, NAB, ANZ, Macquarie, UBS, Goldman, JPM, Citi, Deutsche, Barclays, Morgan Stanley, HSBC AU) | Exact | **blocked** (§12) |
| Trade recs: three trades (next tier-1 print, next meeting, 2–3 months) from a brief of every tab | Same brief built from the AU tabs; trades in IB / IR / spreads | Exact | **blocked** (§12) |

---

## 9. Macro

The US tab scores US indicators; the AUS tab reproduces the Westpac CIO
*Australian Macroeconomic Indicators* slide (23 series, five groups), which is
the right Australian benchmark. Both keep the same layout.

| US panel | AU replication | Grade | Status |
|---|---|---|---|
| Heading + total/group board, dot chart beside the grouped z-table | Same layout; Slide / Live switch | Exact | done |
| Momentum signal (stacked group contributions + total) | Same | Exact | done |
| Live coverage | **Wire 6 more rows from the probe**: Westpac-MI sentiment (H3), NAB business conditions (H3), dwelling approvals (H3), MI inflation expectations (G3, quarterly), private credit (D1 `DGFACNF12`), household spending (ABS `HSI_M`). That leaves ANZ-Indeed job ads and ANZ-Roy Morgan confidence commercial | Exact | build |

---

## 10. Labour

| US panel | US data | AU replication | AU data (verified) | Grade | Status |
|---|---|---|---|---|---|
| Overall indicator: ~35 indicators percentile-ranked, current vs a year ago, by 7 channels, "waiting on" releases | BLS/JOLTS/claims/Fed surveys | **Same method, ~20 AU indicators in the same 7 channels.** Slack: U, underemployment, underutilisation, youth U, medium-term U share. Participation: participation Δ, emp-pop Δ. Employment & hours: employment Δ%, hours Δ%, full-time share. Hiring: U→E and N→E flow rates. Firing: E→U flow rate. Stated demand: vacancies/LF, V/U, NAB capacity, NAB conditions. Worker confidence: job-switching proxy, consumer sentiment | ABS LF, LF Detailed, LMS1 gross flows, RBA H5, H3; NAB capacity pasted | Close: no claims, JOLTS or Fed PMIs; flows and vacancies stand in. Channel memberships listed | build |
| Slack heatmap (percentiles by month) | same | Same | same | Close | build |
| Share of indicators tightening (diffusion, m/m and y/y) | same | Same | same | Close | build |
| Diffusion by channel, breadth inside each channel | same | Same | same | Close | build |
| Series, direction and definitions table | same | Same | same | Exact | build |
| *(AU)* Full-employment z-panel, capacity vs unemployment, job flows vs WPI | — | Kept: the RBA-framed AU charts the user built | ABS, RBA, NAB | AU-only | done |
| Labour-market grid: unemployment, LFPR, emp-pop, jobs–workers gap, layoffs & quits, net job gains | FRED | Unemployment, participation, emp-pop (H5); **jobs–workers gap = (employed + vacancies − LF)/LF** (H5); **job loss (E→U) and job switching** instead of layoffs/quits; **monthly employment change** (H5). Shading for technical recessions computed from H1 GDP | RBA H5, H1; ABS LMS1 | Exact ×4, Substitute ×1 (no JOLTS) | build |
| Unemployment-rate decomposition into gross flows (6 panels) | CPS flows | **Same identity on ABS LMS1 gross flows** (EU, UE, NU, UN, participation, residual) | ABS LMS1 (already cached) | Exact | build |
| Payrolls by component (Private ex H&E, E&H, Government) + 3mma lines | CES | **Filled jobs by industry, quarterly**: market sector ex health & education, health care + education, public administration. The same non-market-sector question the RBA asks | ABS `LABOUR_ACCT_Q` (to 2026-Q2) | Close: quarterly, not monthly | build |
| Sector wage-growth heatmap (AHE y/y, 12 months, row-scaled) | CES AHE | **WPI y/y by industry** (19 industries + public/private), last 8 quarters, same row-scaled colouring | ABS `WPI` v1.2.0 (to 2026-Q2) | Close: quarterly; WPI is the better measure (fixed composition) | build |

---

## 11. Inflation

| US panel | US data | AU replication | AU data (verified) | Grade | Status |
|---|---|---|---|---|---|
| Sequential core trend (CPI and PCE panels, m/m with 3m/6m annualised, 2% pace line) + latest readings | FRED | **Monthly panel**: trimmed mean SA, headline SA, ex-volatiles (m/m, 3m and 6m annualised) from Apr 2024. **Quarterly panel**: trimmed mean q/q annualised since 1982. Line at the 2.5% pace | ABS `CPI` v2.0.0 (`999902`, `999901`, `999904`); ABS appendix 1a | Exact (two panels, as US) | build |
| Breadth (share of basket above threshold) | BEA PCE detail | Same over 87 CPI classes, by count and weight | ABS CPI classes | Exact | done |
| PCE dispersion (m/m vs share of spend above it, eras with fits) | BEA + Richmond Fed | Same construction, quarterly classes since 1990, eras and balanced-share read | ABS appendix 1a + weights | Close: quarterly | build |
| FRBSF contribution stacks (goods/services groupings) + granular diverging | FRBSF workbooks | **Contribution stacks from the monthly CPI's own contribution measure**: groups (food, alcohol & tobacco, clothing, housing, furnishings, health, transport, communication, recreation, education, insurance & financial) and a goods / services / volatile split | ABS `CPI` measure 6 | Exact | build |
| CPI breakdown monitor (item tree, weights, last 6 months m/m heat grid) | BLS tree | **Monthly CPI tree** (group → sub-group → class), weights, last 6 months m/m, same red/blue heat | ABS `CPI` v2.0.0 + `CPI_WEIGHTS` | Exact | build |
| *(AU)* Composition stack, measures the Board targets, breadth vs trimmed mean, cyclical split | — | Kept | ABS | AU-only | done |
| Household inflation expectations (NY Fed SCE point and density, UMich 5–10y) | NY Fed, UMich | **Consumer (MI) 1y, business 3m, union 1y/2y, market economists 1y/2y, 10y break-even** | RBA G3 (quarterly, to Sep 2026) | Close: quarterly, and a richer set | build |
| Employment Cost Index | BLS | **Wage Price Index**: the same fixed-composition design; total, private, public, q/q and y/y | RBA H4, ABS `WPI` | Exact | build |

---

## 12. Growth

| US panel | US data | AU replication | AU data (verified) | Grade | Status |
|---|---|---|---|---|---|
| Growth: weekly tracker (WEI + 13-wk avg) vs real GDP and GDI y/y; WEI-less-GDP board | FRED | Real GDP and real GDI y/y (exact) against a **monthly labour-input tracker: hours worked y/y and its 3-month average**. There is no AU weekly index; hours are the standard monthly GDP proxy (GDP = hours × productivity) | RBA H1, H5 | GDP/GDI exact; WEI → substitute | build |
| Retail control group (1m and 3m annualised, 12m) | Census | **Household Spending Indicator, total** (1m and 3m annualised, 12m), with the discretionary / non-discretionary split | ABS `HSI_M` (to Aug 2026) | Close: HSI also covers services | build |
| Household finances (income, income ex transfers, PCE, monthly) | BEA | Real household disposable income y/y, household consumption y/y, saving ratio (quarterly) | RBA H2 | Close: quarterly | build |
| Retail category bubbles (m/m vs y/y, sized by share) | Census | **HSI's 14 categories**, same bubbles | ABS `HSI_M` | Exact | build |

---

## 13. Analysis: next unemployment print

| US element | AU replication | Grade | Status |
|---|---|---|---|
| Pre-registered inputs, rolling 15-year single-input fits, forecast = mean push | Same method. Inputs: flow steady-state gap u* − u (LMS1, exact), employment growth 3m, hours worked 3m, E→U and U→E flow changes, vacancies Δ, NAB business conditions, consumer sentiment Δ, participation Δ | Close: no claims or JOLTS | build |
| Real-time vintages (ALFRED) | ABS publishes no vintage archive, so latest-vintage data with the revision caveat on screen | Absent: disclosed | build |
| Probabilities from past errors, track record, forecast vs actual, edge over time, inputs that lead, challenger | Same | Exact | build |
| Model vs market (Kalshi odds) | No AU unemployment market exists. The panel is replaced by "model vs the street" (consensus from the reaction card, when entered) | Absent → substitute | build |

---

## 14. Data

| US panel | AU replication | Status |
|---|---|---|
| Source health in two columns, refresh, catalogue tables, sparklines | Same: ASX, RBA F1/H4/H5, ABS LF/LMS1/CPI, SDMX. To add: G3, H1–H3, D1, HSI, WPI, `LABOUR_ACCT_Q`, monthly CPI, SMP | done; additions build |
| Freshness by release calendar, vintage registry (`data/vintage.py`, audits) | The AUS app caches by release period (`data.abs`) and rechecks daily. Porting the US vintage registry is a separate, larger job; worth doing after parity | later |

---

## 15. Absolute differences, and how each is handled

| US has | Australia has | Handling |
|---|---|---|
| Named dots (anonymised distribution) | No dots; an unattributed vote split | Published vote history + SMP cash-rate path; the named read reconciles against both |
| SEP medians from FRED | SMP forecast table on a web page | Scrape table 3.1 each round; cache every round |
| JOLTS, initial claims | Neither | Gross flows (E→U, U→E, N→E) and vacancies |
| Weekly Economic Index | None | Monthly hours worked as the activity tracker |
| Kalshi unemployment market | None | Model vs street consensus |
| ALFRED vintages | None | Latest vintage, disclosed |
| Atlanta Fed Taylor utility | None | Grid computed in-app from published inputs |
| Contract price history (Yahoo) | None from ASX | Settlement-rate cross-check now; own daily snapshots going forward |
| FRED key | All sources keyless | Nothing to configure |

---

## 16. Decision needed

The four agent-driven panels (consensus search, speech roundup, street call
book, trade recs) run on a research agent: the Claude Agent SDK, or WorkBuddy
as the US app also allows. The AUS venv has neither installed. Everything else
above is buildable with what is already installed.

## 17. Build order

1. Multi-meeting moves view (sidebar, verdict, path).
2. Releases reaction card (no consensus until §16 is settled).
3. Labour: percentile dashboard, heatmap, diffusion, channels, labour-market
   grid, flow decomposition, jobs by industry, WPI heatmap.
4. Inflation: monthly sequential trend, contributions, CPI monitor,
   dispersion, expectations (G3), WPI.
5. Growth tab.
6. SMP & forecasts tab.
7. Vote count: hawk-dove chart, score roll, published vote history.
8. Macro live wiring (H3, G3, D1, HSI).
9. Analysis tab.
10. Curve: daily settlement snapshots.
11. Agent tabs, once §16 is decided.
12. Code review, then the front-end design pass.
