"""Macro dashboard: the seed reproduces the slide, and the live path re-scores.

The Westpac CIO "Australian Macroeconomic Indicators" slide (August 2026 SoMP
round) is the reference. Every seed number in `core.macro` is one of its three
printed columns, and `seed_sd` is `|current - mean| / |z|` from them, so the
table has to come back byte-for-byte as the picture:

    Labour Market                 -0.798
    Inflation                      0.744
    Housing                       -0.323
    Consumer                       0.255
    Cyclical Economic Indicators   0.049
    Total                         -0.006

No network: the live path is exercised with small hand-built series.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import macro as m


# --------------------------------------------------------------------------
# The seed reproduces the slide
# --------------------------------------------------------------------------

SLIDE_GROUPS = {
    "Labour Market": -0.798,
    "Inflation": 0.744,
    "Housing": -0.323,
    "Consumer": 0.255,
    "Cyclical Economic Indicators": 0.049,
}


def test_seed_group_scores_match_slide():
    readings = m.dashboard()
    got = {g.group: g.z for g in m.groups(readings)}
    for group, want in SLIDE_GROUPS.items():
        assert got[group] == pytest.approx(want, abs=1e-3), group


def test_seed_total_matches_slide():
    readings = m.dashboard()
    # The slide's Total is the equal-weighted mean of all 22 printed z-scores,
    # not the mean of the five group means (which would be -0.014).
    assert m.total(readings) == pytest.approx(-0.006, abs=6e-4)


def test_every_indicator_scores_from_the_seed():
    readings = m.dashboard()
    assert len(readings) == 23
    assert all(r.ok for r in readings)
    assert all(not r.live for r in readings)


def test_seed_sd_is_backed_out_of_the_printed_z():
    # Job Vacancies: current -2.11, mean 1.09, printed z -0.578.
    ind = m.BY_KEY["job_vacancies"]
    assert ind.seed_sd == pytest.approx(abs(-2.11 - 1.09) / 0.578, rel=1e-9)
    r = next(x for x in m.dashboard() if x.indicator.key == "job_vacancies")
    assert r.z == pytest.approx(-0.578, abs=1e-3)


def test_bad_news_indicators_are_sign_flipped():
    # A rising unemployment rate (positive 3m/3m change, above its ~0 mean)
    # must score NEGATIVE -- weaker economy.
    r = next(x for x in m.dashboard() if x.indicator.key == "unemployment")
    assert r.indicator.bad_news
    assert r.z < 0
    # ...and every non-flipped row with current > mean scores positive.
    for x in m.dashboard():
        if x.indicator.bad_news:
            continue
        if x.indicator.seed_current > x.indicator.seed_mean:
            assert x.z > 0


# --------------------------------------------------------------------------
# Transforms
# --------------------------------------------------------------------------

def _monthly(start: date, values):
    out, y, mth = [], start.year, start.month
    for v in values:
        out.append((date(y, mth, 1), v))
        y, mth = (y + 1, 1) if mth == 12 else (y, mth + 1)
    return out


def _quarterly(start: date, values):
    out, y, mth = [], start.year, start.month
    for v in values:
        out.append((date(y, mth, 1), v))
        y, mth = (y + 1, mth - 9) if mth >= 10 else (y, mth + 3)
    return out


def test_pass_q_collapses_to_one_point_per_quarter():
    xf = m.transform("pass_q", _quarterly(date(2020, 1, 1), [0.5, 0.6, 0.7]))
    assert [d.month for d, _ in xf] == [1, 4, 7]
    assert [v for _, v in xf] == [0.5, 0.6, 0.7]


def test_qoq_lvl_is_percent_change_on_the_prior_quarter():
    xf = m.transform("qoq_lvl", _quarterly(date(2020, 1, 1), [100.0, 102.0, 101.0]))
    assert xf[0][1] == pytest.approx(2.0)
    assert xf[1][1] == pytest.approx((101.0 / 102.0 - 1.0) * 100.0, rel=1e-9)


def test_chg_3m3m_is_the_point_change_in_the_3m_average():
    # Flat at 5.0 for six months, then step to 5.3 -- the 3m/3m change picks
    # the step up over three months.
    xf = m.transform("chg_3m3m",
                     _monthly(date(2020, 1, 1),
                              [5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.3, 5.3, 5.3]))
    assert xf[-1][1] == pytest.approx(0.3, abs=1e-9)


def test_ma3_smooths_three_months():
    xf = m.transform("ma3", _monthly(date(2020, 1, 1), [0.3, 0.6, 0.9, 1.2]))
    assert xf[0][1] == pytest.approx(0.6)
    assert xf[1][1] == pytest.approx(0.9)


def test_unknown_or_empty_transform_returns_nothing():
    assert m.transform("", _quarterly(date(2020, 1, 1), [1.0, 2.0])) == []
    assert m.transform("pass_q", []) == []


def test_transform_skips_unparseable_paste_rows_without_raising():
    messy = [(date(2020, 1, 1), 0.5), (date(2020, 4, 1), None),
             (date(2020, 7, 1), "n/a"), ("2020-10-01", 0.9),
             (date(2021, 1, 1), 0.7)]
    xf = m.transform("pass_q", messy)
    assert [v for _, v in xf] == [0.5, 0.7]


# --------------------------------------------------------------------------
# The live path re-scores against the seed mean/sd
# --------------------------------------------------------------------------

def test_live_series_overrides_the_seed_reading():
    ind = m.BY_KEY["gdp"]
    # A quarterly q/q series whose last point is +1.5 %.
    live = {"gdp": _quarterly(date(2018, 1, 1), [0.5] * 30 + [1.5])}
    r = next(x for x in m.dashboard(live) if x.indicator.key == "gdp")
    assert r.live
    assert r.last == pytest.approx(1.5)
    assert r.z == pytest.approx((1.5 - ind.seed_mean) / ind.seed_sd, rel=1e-9)


def test_live_falls_back_to_seed_when_the_transform_cannot_compute():
    # One observation -> qoq_lvl has no prior quarter -> [].
    r = next(x for x in m.dashboard({"job_vacancies": [(date(2026, 6, 1), 10.0)]})
             if x.indicator.key == "job_vacancies")
    assert not r.live
    assert r.last == pytest.approx(m.BY_KEY["job_vacancies"].seed_current)


def test_a_pasted_history_for_a_commercial_row_scores_it_live():
    # NAB business conditions: 3m m.a. of a net balance. Feed a flat series.
    live = {"nab_business_conditions": _monthly(date(2020, 1, 1), [7.0] * 12)}
    r = next(x for x in m.dashboard(live)
             if x.indicator.key == "nab_business_conditions")
    assert r.live and r.last == pytest.approx(7.0)


# --------------------------------------------------------------------------
# Momentum history
# --------------------------------------------------------------------------

def test_momentum_history_needs_at_least_three_groups():
    # Only one group wired -> no point clears the >=3 group guard.
    live = {"gdp": _quarterly(date(2010, 1, 1), [0.5] * 40)}
    assert m.momentum_history(live, date(2011, 1, 1)) == []


def test_momentum_contributions_stack_to_the_total():
    live = {
        "gdp": _quarterly(date(2010, 1, 1), [0.7] * 40),
        "cpi_trimmed_mean": _quarterly(date(2010, 1, 1), [0.6] * 40),
        "wpi": _quarterly(date(2010, 1, 1), [0.8] * 40),
        "unemployment": _monthly(date(2010, 1, 1), [5.0] * 120),
    }
    hist = m.momentum_history(live, date(2012, 1, 1))
    assert hist
    for pt in hist:
        assert sum(pt.contributions.values()) == pytest.approx(pt.total, abs=1e-9)


def test_momentum_last_bar_matches_the_table_over_the_wired_subset():
    # A monthly labour series that ends in August -- off the quarterly grid, so
    # the final pinned point is what makes the last bar current.
    unemp = _monthly(date(2005, 1, 1), [5.0] * 259 + [5.4] * 3)   # ...ends 2026-08
    live = {
        "gdp": _quarterly(date(2005, 1, 1), [0.5] * 80 + [1.2]),
        "cpi_trimmed_mean": _quarterly(date(2005, 1, 1), [0.6] * 81),
        "company_profit": _quarterly(date(2005, 1, 1), [1.0] * 81),
        "unemployment": unemp,
    }
    readings = m.dashboard(live)
    assert any(r.indicator.key == "unemployment" and r.live for r in readings)
    wired = {r.indicator.group for r in readings if r.live}
    grp = {g.group: g.z for g in m.groups([r for r in readings if r.live])}
    last = m.momentum_history(live, date(2007, 1, 1))[-1]
    for g in wired:
        assert g in last.by_group
        assert last.by_group[g] == pytest.approx(grp[g], abs=1e-6)


def test_quarterly_survey_averages_are_not_smoothed_again():
    from core import macro
    q = [(date(2025, 3, 31), 4.0), (date(2025, 6, 30), 5.0), (date(2025, 9, 30), 6.0)]
    assert [v for _, v in macro.transform("ma3", q)] == [4.0, 5.0, 6.0]


def test_own_baseline_needs_most_of_the_window():
    from core import macro
    monthly = [(date(2000 + i // 12, i % 12 + 1, 1), float(i % 5)) for i in range(252)]
    mean, sd = macro.own_baseline(monthly)
    assert abs(mean - 2.0) < 0.05 and sd > 1
    assert macro.own_baseline(monthly[-100:]) is None
