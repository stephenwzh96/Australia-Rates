"""Next print: no look-ahead, conservative release rules, and the tests' maths."""

import math
from datetime import date

import numpy as np

from nextprint import evaluate as E
from nextprint import inputs as I
from nextprint import model as M
from nextprint import panel as P
from nextprint import releases as R


def _months(start: date, n: int) -> list[date]:
    return [R.add_months(start, k) for k in range(n)]


def _data(n: int = 300, seed: int = 7) -> dict:
    """A synthetic world where the unemployment rate drifts and the inputs
    carry some of next month's move."""
    rng = np.random.default_rng(seed)
    months = _months(date(1995, 1, 1), n)
    u = 6 + np.cumsum(rng.normal(0, 0.1, n))
    lab = np.full(n, 10_000.0)
    lf = {I.UNEMPLOYED: [(m, float(x * 100)) for m, x in zip(months, u)],
          I.LABOUR_FORCE_LEVEL: [(m, float(v)) for m, v in zip(months, lab)],
          "employed": [(m, float(10_000 - x * 100)) for m, x in zip(months, u)],
          "underemployment": [(m, float(8 + x / 10)) for m, x in zip(months, u)]}
    rba = {"GICNBC": [(m, float(5 - 10 * (u[min(i + 1, n - 1)] - u[i]) + rng.normal()))
                      for i, m in enumerate(months)],
           "GICWMICS": [(m, float(100 + rng.normal(0, 3))) for m in months],
           "GLFOSVT": [(m, float(200 + rng.normal(0, 5))) for m in months if m.month in (2, 5, 8, 11)]}
    return {"lf": lf, "rba": rba, "lf_releases": {}}


def _truncate(data: dict, as_of: date) -> dict:
    """Only what was public before `as_of`, by each series' own release rule."""
    timing = {i.code: i.timing for i in I.INPUTS}
    lf = {}
    for k, rows in data["lf"].items():
        lf[k] = [(d, v) for d, v in rows if R.lf_release(d, {}) < as_of]
    rba = {k: [(d, v) for d, v in rows if R.rule_for(timing[k], {})(d) < as_of]
           for k, rows in data["rba"].items()}
    return {"lf": lf, "rba": rba, "lf_releases": {}}


def test_live_forecast_ignores_the_future():
    data = _data()
    as_of = date(2016, 6, 20)
    full = P.build(data, as_of)
    cut = P.build(_truncate(data, as_of), as_of)
    assert full.live_month == cut.live_month
    a, b = full.frame.iloc[-1], cut.frame.iloc[-1]
    for k in M.KEYS:
        x, y = a[f"x_{k}"], b[f"x_{k}"]
        assert (math.isnan(x) and math.isnan(y)) or x == y, k
    fa, fb = M.forecasts(full.frame).iloc[-1], M.forecasts(cut.frame).iloc[-1]
    assert math.isclose(fa[f"f_{M.COMBO}"], fb[f"f_{M.COMBO}"], abs_tol=1e-12)


def test_release_rule_is_never_later_than_the_exact_dates():
    # Every release from Dec 2019 came on a Thursday between the 11th and the
    # 25th of the next month; the rule (2nd Thursday) must sit on or before.
    for m in _months(date(2019, 12, 1), 81):
        assert R.lf_rule(m).day <= 14 and R.lf_rule(m) > R.month_end(m)


def test_quarterly_growth_skips_the_suspension():
    obs = [(date(2008, 5, 31), 100.0), (date(2009, 11, 30), 90.0)]
    s = P.Series.build(obs, [date(2008, 7, 1), date(2010, 1, 1)])
    x, _ = P.transform(I.QGROWTH, s, date(2010, 2, 1), date(2010, 1, 1))
    assert math.isnan(x)


def test_hac_test_matches_a_plain_t_without_autocorrelation():
    rng = np.random.default_rng(1)
    d = rng.normal(0.3, 1.0, 400)
    p = E._hac_upper_p(d, lags=0)
    t = d.mean() / (d.std() / math.sqrt(len(d)))
    assert math.isclose(p, 0.5 * math.erfc(t / math.sqrt(2)), rel_tol=1e-9)
    assert p < 0.01


def test_published_rounding_is_half_up():
    assert P.round_rate(4.65) == 4.7 and P.round_rate(4.6499) == 4.6


def test_a_print_is_not_out_before_its_late_date_without_an_exact_one():
    """Pre-2019 replay: the Feb 2018 print counts as out only from the 28th of
    March, never from the early rule's 8th."""
    data = _data()
    as_of = date(2018, 3, 12)
    p = P.build(data, as_of)
    assert p.live_month == date(2018, 2, 1)
