# RBA event-pricing dashboard

A live Streamlit rerun of the event-pricing framework for any RBA Monetary
Policy Board meeting, priced off ASX **IB** (30 day interbank cash rate) and
**IR** (90 day bank bill) futures. An Australian sibling of the FOMC version.

Every derived number carries a `?` control holding the formula, a plain-English
gloss, and the substitution using the *current* inputs. That substitution is the
audit trail: it is what makes a points-vs-probability slip visible on screen
instead of three steps downstream.

## Run it

```bash
python -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
.venv/bin/streamlit run app.py
```

Opens on <http://localhost:8501>. `?meeting=YYYY-MM-DD` jumps straight to a
meeting, so a meeting can be bookmarked.

**No API keys.** Both data sources are free and keyless — see *Data* below.

## Data

| Source | What | Key? |
|---|---|---|
| ASX (Markit JSON) | IB strip, 18 monthly contracts; IR strip, 18 contracts to 2030 | no |
| RBA table F1 | daily cash rate target, AONIA, BBSW 1/3/6-month | no |
| RBA table H5 | labour force, unemployment level, job vacancies | no |
| ABS Labour Force | unemployment, underemployment, youth rates (Excel time series) | no |
| ABS Labour Force Detailed | unemployed by duration of job search | no |
| ABS CPI table 18 | quarterly expenditure-class indexes, index-point contributions | no |
| ABS CPI appendix 1a | 87 seasonally adjusted class indexes, trimmed mean, from 1982 | no |
| ABS CPI table 6 | monthly trimmed mean and ex-volatiles measures | no |

A failed HTTP call never blocks the Verdict. With both sources dead and no
cache, every field is still typeable and the pricing, sizing and export all
still compute — there is a test for exactly that.

Three findings from wiring these up are worth knowing, because each one would
have quietly corrupted numbers:

- **Yahoo Finance carries no ASX rate futures.** It serves live ZQ and SR3, but
  every ASX symbol convention 404s. The US version's price feed has no
  Australian equivalent, hence the ASX endpoint — which is the JSON behind the
  exchange's own price pages, not a published API. The last good strip is
  cached to disk in case it disappears.
- **The feed's `dateExpiry` is wrong by two days** — consistently, on all 36
  contracts. It is a vendor artifact, not a contract term, so delivery months
  are parsed from the contract symbol instead.
- **`priceContract` is the last *traded* price, not a daily mark.** On the back
  of the IB strip it runs up to 17.5bp stale. The official settlement price is
  used in preference, with the bid/ask midpoint and last trade as ranked
  fallbacks.

## What differs from the US version

The payoff algebra is identical — the same worked example gives 32% implied,
+5.50bp EV, 32% breakeven and 68.75%/17.2% Kelly on both, and the test suite
pins those to catch a port that damaged the engine. What genuinely changed:

- **IB is the structural twin of fed funds.** It settles on the simple average
  of the cash rate over every calendar day of the delivery month, weekends and
  holidays carrying the previous fix. So the meeting-path decomposition ports
  unchanged, denominator intact.
- **IR is not SOFR.** SR3 settles on a compounded average across an IMM quarter;
  IR settles on a *single* 3-month BBSW fix. There is no window to un-blend and
  no partial capture — capture is binary. And BBSW is a bank credit rate, so an
  IR price is the policy path *plus* a credit spread.
- **ACT/365, not ACT/360.** IB's DV01 is A$24.66, not A$25.00. IR's DV01 is
  *level-dependent* (A$24.30 at 3%, A$23.94 at 6%) because a bank bill is priced
  by discounting, where SR3 is flat $25.00 by construction.
- **Late-month meetings.** Several RBA decisions land on the 28th or 29th. The
  September 2026 decision covers 1 of 30 days of its month, so the meeting-month
  contract captures 3.3% of the move and inverting it would turn a 1bp error
  into 30bp of "priced move". The app reads those off the following contract and
  labels every row clean or inverted.
- **The blackout is five days, not seventeen** — the Wednesday before the
  meeting to the decision, per the RBA's own public-speaking arrangements.
- **SMP, not SEP.** The forecast round lands with the Feb/May/Aug/Nov decisions.
- **Nine board members, and votes are never attributed.** The RBA publishes an
  unattributed count ("decided by six votes to three"). The roster encodes *your*
  read and can never be scored against a published tally — the Vote count tab
  says so on screen.
- **The BBSW/OIS basis is derived, not read.** The RBA retired its OIS series in
  December 2022, so the OIS leg comes off the IB strip — which is a live,
  tradeable OIS curve, and the better construction anyway.
- **Volatility is calibrated on rates, not contract prices.** The ASX feed has
  no history at all. A bank bill future is quoted `100 - yield`, so a basis
  point of daily 3-month BBSW is a basis point of contract price — the right
  units, and the reason no contract-capture rescale is applied to it. It is
  still a proxy: BBSW spans about 1.5 meetings, so read it as the short end's
  daily noise rather than this one meeting's, and type an override when the
  distinction matters.

## The Labour tab

Nine full-employment indicators, each scored as a **z-score against its own
2000–2020 average** and plotted at two dates so the direction of travel reads
alongside the level. Right of zero is a tighter labour market, which means the
five slack measures have their sign flipped — otherwise a low unemployment rate
would plot on the same side as a low vacancies ratio.

A z-score, deliberately, rather than the RBA's own gap-from-trend version of the
same panel. The RBA publishes neither the filters it detrends with nor how it
rescales each series into unemployment-rate units, so that chart can only be
read off, never rebuilt. `z = (x − mean) / sd` over a stated window has no such
freedom: every term comes from the data and one date range, and the window is a
control on screen rather than a constant buried in the arithmetic.

Six of the nine compute live from ABS and RBA sources, and all six reproduce an
independently published rendering of the same panel — four exactly, two to that
source's own rounding:

| Indicator | Computed | Published |
|---|---|---|
| Unemployment Rate | 4.4283 | 4.4283 |
| Underemployment Rate | 6.5093 | 6.5093 |
| Underutilisation Rate | 10.9376 | 10.938 |
| Medium-term Unemployment Rate | 2.5273 | 2.5273 |
| Youth Unemployment | 10.6657 | 10.666 |
| Vacancies-to-Unemployment | 47.9761 | 47.973 |

Three — firms reporting labour constraints, employment intentions (NAB Business
Survey) and job ads as a share of the labour force (ANZ-Indeed) — are commercial
with no free feed, and they **keep their rows on the chart marked "awaiting
data"** rather than disappearing. A panel that quietly drops what it cannot
measure is the wrong shape: the gap should be visible.

They take a typed reading plus the window mean and standard deviation. A z
cannot be typed directly on purpose: nothing on screen would show what it had
been measured against, and it would not move when the reading did.

The search for a free substitute came up empty and the negative result is worth
recording. All 71 RBA statistical tables republish exactly two NAB series —
business conditions (`GICNBC`) and business inflation expectations (`GBUSEXP`) —
and neither is what this panel needs. Trading Economics discontinued its guest
API tier (HTTP 410), and it licenses these series from NAB and ANZ anyway, so
going through it would not confer a licence. Jobs and Skills Australia's
Internet Vacancy Index is a genuinely free monthly job-ads series and the
natural substitute, but it blocks automated access. The honest place for these
three is a typed field fed from a terminal that already licences them.

Two things the tab surfaces rather than hides. The **two ABS releases sit on
different months** — headline Labour Force at June 2026, Detailed still at March
after the April survey changes — so the medium-term unemployment rate is months
staler than the rest and the panel says which row is holding it back. And the
**Total is an unweighted mean reported with its row count**, because these
indicators overlap heavily (underutilisation is literally unemployment plus
underemployment) and any weighting would be a second undocumented judgement on
top of the window choice.

## The Inflation tab

Five readings of one quarterly CPI, two to a row. The headline is what the
Board targets; these say whether it is a *monetary* problem — a 3% print made
of two extreme items is a different thing from one where two-thirds of the
basket is above target, and only the second is something a cash rate fixes.

**The 87 expenditure classes are not a hardcoded list.** The ABS flattens four
levels of hierarchy — All groups, 11 groups, sub-groups, classes — into one
ordered column block with nothing marking depth, so counting every series would
count Bread once on its own and again inside Bread and cereal products. The
tree is recovered from the data instead: a parent's index-point contribution is
the sum of its children's, so reading the list backwards and letting each
series claim the shortest run of unclaimed neighbours that adds up to it
rebuilds the hierarchy with no external list at all. It self-corrects when the
ABS moves a class, which the April 2026 Labour Force renaming is a live
reminder they do.

Two rules make that parse survive contact with real data, and both were forced
by a specific failure:

- **A parent needs at least two children.** Pork and Lamb and goat both publish
  0.27, so a greedy parse made Lamb the only child of Pork — and Lamb then
  disappeared from Meat and seafoods, whose remaining children no longer
  reached it, so it too was misread, and so on up to All groups CPI. One
  coincidence four levels down corrupted every boundary above it. A one-child
  aggregate is arithmetically identical to a leaf anyway, so refusing to infer
  one costs nothing.
- **The balance must hold in every published quarter.** Automotive fuel came to
  3.46 in March 2026 and so did Maintenance and repair plus Other services;
  in December (3.29 against 3.43) and June (3.39 against 3.51) they are nowhere
  near each other. A structure is a property of the classification, so a real
  parent balances in all of them and a coincidence is a fact about one
  quarter's prices.

The result reconciles: 87 leaves summing to 102.36 index points against a
published All groups CPI of 102.31, a residual of 0.05 that is the ABS's own
rounding. That number is on screen, because it is the one figure that says
whether the parse worked.

**Weights are reconstructed, and the chart says so.** The ABS publishes an
index-point contribution for the last three quarters only, and the breadth
charts need a weight for every quarter back to 1990. The All groups
contribution equals the All groups index exactly, so a contribution *is* an
index point and dividing by a class's own index recovers its expenditure
weight. Multiplying that weight back through the index gives its contribution
at any date — a fixed-weight Laspeyres, not what the ABS would have published
in 2013, since the basket is re-weighted annually and this holds the latest one
fixed.

**Two things are judgements, and they live outside the code.** The composition
buckets (administered prices, tradables, domestic market services) and the
cyclical/non-cyclical split are not published series — different houses draw
them differently. They sit in `meetings/_cpi_classification.json` as editable
data, every class the file does not mention falls into a visible residual
rather than being dropped, and a test asserts every name in it is a real
expenditure class and that the two classifications agree with each other.

**The breadth chart is two stacked panels, not the source's dual axis.** With
two independent y-scales the crossings and relative amplitudes are artefacts of
where the scales were pinned, and sliding one changes which series appears to
lead. Sharing an x-axis answers the same question with every comparison real,
and each panel keeps its own 1993–2019 reference line.

**Release prep** and the LLM research agent are still absent.
`core/econ_calendar.py` survives in slim form (dates only, no series values)
because the Monte Carlo's release-day volatility multipliers are keyed off it;
without it a CPI Wednesday would be priced like a quiet Tuesday. On Australian
data quarterly CPI comes out at roughly 5x a quiet day — by a wide margin the
loudest release on the calendar. The Inflation tab's vintage line reads its
next-release date off that same calendar.

## Caveats

- **The roster is unfilled.** Nine statutory seats are seeded but only the two
  executive names, and every hawk–dove score is a neutral 2.5 placeholder. This
  app does not assert policy views for people it has not sourced. Fill the names
  from rba.gov.au and set the scores from each member's own speeches.
- **ABS release dates are rule-derived**, matching long-standing practice, not
  scraped from the ABS calendar. Confirm one before trading on it.
- Not a recommendation, and not financial advice. The framework encodes *your*
  probability estimate, and that estimate is doing all the work.

## Layout

```
app.py                Streamlit entry: sidebar, eight sections, Verdict rail
core/                 Pure calculation -- no Streamlit imports
  pricing.py          Steps 1-5   implied prob, EV, breakeven, sensitivity, Kelly
  strip.py            Step 1a     IB -> per-meeting priced path
  bbsw.py             Step 1b     bank bills, spans, basis, cash-equivalent
  path.py             Step 6      MTM ladder, exit map, stop-cost decomposition
  kills.py            Step 7      kill criteria and the two-strikes rule
  votes.py            Step 8      bloc arithmetic, decomposition, conditional EV
  contracts.py        IB/IR mechanics, capture, ACT/365 DV01s
  rba_calendar.py     meeting dates, blackout, SMP flag
  holidays.py         Sydney business days
  sizing.py           Step 5      Kelly -> dollars -> lots
  model.py            one computation of everything, shared by app and export
  employment.py       full-employment z-scores, sign conventions, panel
  inflation.py        CPI hierarchy, breadth, composition, cycle split
data/asx.py           IB and IR strips, settlement-first
data/rba.py           statistical tables, keyless CSV
data/abs.py           ABS Labour Force and CPI Excel time series
state/store.py        per-meeting JSON, clone-forward
ui/                   components, charts, theme, style
export/report.py      Markdown mirroring the sections
meetings/             saved state, plus _roster.json, _calendar.json and
                      _cpi_classification.json
tests/                golden values, contract mechanics, feed traps
```

`core/` never imports Streamlit, so every number on screen comes from a function
a test can call directly.

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

81 tests. Beyond the golden values, they pin the things that had to be
re-derived rather than translated: the ACT/365 DV01s, the IB averaging window,
IR's binary capture and its last-trading-day rule, Anzac Day never substituting
in NSW, and the strip's refusal to invent a spot rate it cannot recover.

Eleven are regressions against bugs this codebase actually had, not
hypotheticals — a circular basis that returned the IB path unchanged, a
volatility estimate divided by a contract capture that has no meaning for a
rate series, a bill tenor starting a day early, a negative move size crashing
the app, a corrupt save file wedging it on every rerun, stop-path warnings
nagging about an unfilled form, Pork adopting Lamb and unpicking the CPI
hierarchy above it, Automotive fuel doing the same on one quarter's
coincidence, an aggregate stepping when a class entered mid-series, and free
child care in June 2020 driving a compounded index to 15,000 per cent. Each was
checked by reintroducing the bug and confirming the test fails.

The CPI fixtures are cuts of the real June 2026 basket rather than invented
numbers, because every one of those traps is a fact about the published data
and an invented fixture would not have caught any of them.
