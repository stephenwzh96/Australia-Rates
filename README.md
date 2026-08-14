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

## Not included

The **Labour** and **Inflation** tabs are deliberately absent — different
figures are coming. **Release prep** and the LLM research agent came out with
them. `core/econ_calendar.py` survives in slim form (dates only, no series
values) because the Monte Carlo's release-day volatility multipliers are keyed
off it; without it a CPI Wednesday would be priced like a quiet Tuesday. On
Australian data quarterly CPI comes out at roughly 5x a quiet day — by a wide
margin the loudest release on the calendar.

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
app.py                Streamlit entry: sidebar, six sections, Verdict rail
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
data/asx.py           IB and IR strips, settlement-first
data/rba.py           statistical tables, keyless CSV
state/store.py        per-meeting JSON, clone-forward
ui/                   components, charts, theme, style
export/report.py      Markdown mirroring the sections
meetings/             saved state, plus _roster.json and _calendar.json
tests/                golden values, contract mechanics, feed traps
```

`core/` never imports Streamlit, so every number on screen comes from a function
a test can call directly.

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

44 tests. Beyond the golden values, they pin the things that had to be
re-derived rather than translated: the ACT/365 DV01s, the IB averaging window,
IR's binary capture and its last-trading-day rule, Anzac Day never substituting
in NSW, and the strip's refusal to invent a spot rate it cannot recover.

Seven are regressions against bugs this codebase actually had, not
hypotheticals — a circular basis that returned the IB path unchanged, a
volatility estimate divided by a contract capture that has no meaning for a
rate series, a bill tenor starting a day early, a negative move size crashing
the app, a corrupt save file wedging it on every rerun, and stop-path warnings
nagging about an unfilled form. Each was checked by reintroducing the bug and
confirming the test fails.
