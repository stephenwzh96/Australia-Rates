"""The pre-registered inputs and the settings of the study, fixed in advance.

Every input has exactly one transform and an expected sign. Adding or dropping
an input is one line here; tuning a transform on the results is data snooping.

Transforms follow the US build's rule, not per-input choices: bounded
diffusion and net-balance indexes enter as LEVELS (no trend); rates and counts
that trend enter as CHANGES, so a slow drift over the estimation window cannot
pose as a signal. A sentiment index enters as its monthly change, as UMich
does in the US build.

The Australian set is the closest free equivalent of each US channel:
  business survey   NAB business conditions (RBA H3)   -- ISM, Philly, Empire
  household survey  Westpac-MI consumer sentiment (H3) -- UMich
  vacancies         ABS job vacancies (RBA H5)         -- JOLTS hires
  hiring momentum   Labour Force employment            -- payrolls
  slack             Labour Force underemployment       -- (no US input)
Not available: weekly claims (Australia has no weekly series; JobSeeker counts
start in 2020), the flow gap (the ABS gross flows are unadjusted and cover the
matched sample only -- adjusting them needs future data, which would leak),
and job ads (ANZ-Indeed is commercial).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# Transforms (see panel.py).
LEVEL = "level"          # latest released value
DIFF1 = "diff1"          # latest released value minus the month before
DIFF3 = "diff3"          # latest released value minus three months before
GROWTH3 = "growth3"      # average monthly log growth over the last three months, %
QGROWTH = "qgrowth"      # log growth on the quarter before, % (quarterly series)

# Release timing (see releases.py).
LABOUR_FORCE = "labour_force"   # printed in an earlier Labour Force release
NAB = "nab"
WESTPAC_MI = "westpac_mi"
VACANCIES = "vacancies"


@dataclass(frozen=True)
class Input:
    key: str
    label: str
    channel: str      # what it measures, in a trader's words
    source: str       # "lf" | "rba"
    code: str         # series id (RBA) or key in the LF bundle
    transform: str
    sign: int         # expected sign on the next change in unemployment (+1: higher input, higher rate)
    timing: str
    unit: str = ""


INPUTS: tuple[Input, ...] = (
    Input("nab", "NAB business conditions", "business survey", "rba", "GICNBC", LEVEL, -1,
          NAB, "net balance"),
    Input("sentiment", "Westpac-MI sentiment", "household survey", "rba", "GICWMICS", DIFF1,
          -1, WESTPAC_MI, "m/m chg"),
    Input("vacancies", "Job vacancies", "job finding", "rba", "GLFOSVT", QGROWTH, -1,
          VACANCIES, "% q/q"),
    Input("employment", "Employment", "hiring momentum", "lf", "employed", GROWTH3, -1,
          LABOUR_FORCE, "% m/m, 3m avg"),
    Input("underemployment", "Underemployment rate", "slack", "lf", "underemployment",
          DIFF3, +1, LABOUR_FORCE, "3m chg, pp"),
)

BY_KEY = {i.key: i for i in INPUTS}

# Labour Force series (X29, seasonally adjusted, persons).
UNEMPLOYED = "unemployed"
LABOUR_FORCE_LEVEL = "labour_force"

# Study settings, as the US build.
WINDOW_YEARS = 15          # rolling estimation window
MIN_TRAIN = 72             # months of training rows before a model forecasts
MIN_ERRORS = 60            # past out-of-sample errors before print probabilities are given
EVAL_START = date(1990, 1, 1)
COMMON_START = date(2016, 1, 1)   # first month every input has a working model (sentiment from 2010)
ROLLING_MONTHS = 60        # track-record window

#: The challenger: a ridge regression on all inputs at once, fixed on this day
#: before it was tracked. Its backtest was seen when it was chosen, so only
#: prints released after this date count towards replacing the average.
CHALLENGER_REGISTERED = date(2026, 10, 2)
CHALLENGER_MIN_PRINTS = 36
CHALLENGER_P = 0.10

#: Target months excluded from estimation and evaluation. COVID runs to 2022-03
#: so no transform (at most three months long) reaches into the 2020-21 swings
#: (JobKeeper held employment while hours collapsed).
COVID = (date(2020, 3, 1), date(2022, 3, 1))
