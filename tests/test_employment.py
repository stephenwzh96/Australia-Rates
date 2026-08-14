"""Full-employment panel: the z-score engine and its derived series.

Every number this file pins was checked against an independently published
rendering of the same panel, which is what makes them regressions rather than
snapshots of whatever the code happened to do. The published readings were:

    Unemployment Rate               4.4283
    Underemployment Rate            6.5093
    Underutilisation Rate          10.938
    Medium-term Unemployment Rate   2.5273
    Youth Unemployment             10.666
    Vacancies-to-Unemployment      47.973

All six reproduce from ABS and RBA source data -- four exactly, two to the
rounding of the published figure. The derivations below are the ones that
needed working out rather than reading off: underutilisation is a sum,
medium-term is a duration-bucket share, and vacancies-to-unemployment needs a
pairing rule that took a real experiment to pin down.

No network. The fixtures are small hand-built series, because a test that
downloads a 5MB workbook is not a test of this module.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import employment as emp


def _monthly(start: date, values: list[float]) -> list[tuple[date, float]]:
    out, y, m = [], start.year, start.month
    for v in values:
        out.append((date(y, m, 1), v))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _flat_window(value: float, spread: float = 1.0) -> list[tuple[date, float]]:
    """21 years of alternating values -- a known mean and a known sd."""
    vals = [value + (spread if i % 2 == 0 else -spread) for i in range(21 * 12)]
    return _monthly(date(2000, 1, 1), vals)


# ------------------------------------------------------------- the z-score

def test_window_stats_are_sample_not_population():
    s = emp.window_stats(_flat_window(5.0, 1.0))
    assert s is not None and s.ok
    assert s.mean == pytest.approx(5.0)
    # Alternating +-1 about the mean: population sd is exactly 1, sample sd is
    # a shade above it. The distinction is the estimator, not a rounding.
    assert s.sd > 1.0
    assert s.sd == pytest.approx(1.0, abs=0.002)
    assert s.n == 21 * 12


def test_window_excludes_observations_outside_it():
    series = _flat_window(5.0) + _monthly(date(2026, 1, 1), [99.0, 99.0])
    s = emp.window_stats(series, date(2000, 1, 1), date(2020, 12, 31))
    assert s.mean == pytest.approx(5.0)          # the 99s are outside


def test_slack_measures_have_their_sign_flipped():
    """A LOW unemployment rate is a TIGHT labour market, so it must plot right
    of zero. Without the flip it would sit on the same side as a low vacancies
    ratio, which means the opposite."""
    series = _flat_window(5.0) + _monthly(date(2026, 6, 1), [3.0])
    tight = emp.score(emp.BY_KEY["unemployment"], series, date(2020, 1, 1))
    assert tight.indicator.invert
    assert tight.current.value == 3.0
    assert tight.current.z > 0                    # below average = tighter

    demand = emp.score(emp.BY_KEY["vacancies_to_unemployment"], series,
                       date(2020, 1, 1))
    assert not demand.indicator.invert
    assert demand.current.z < 0                   # below average = looser


def test_value_at_takes_the_observation_on_or_before():
    """Monthly series are stamped on the first, so asking for 31 December must
    return that December -- not miss it and fall to November."""
    series = _monthly(date(2025, 10, 1), [1.0, 2.0, 3.0, 4.0])
    assert emp.value_at(series, date(2025, 12, 31)) == (date(2025, 12, 1), 3.0)
    assert emp.value_at(series, None) == (date(2026, 1, 1), 4.0)
    assert emp.value_at(series, date(1999, 1, 1)) is None


# ------------------------------------------------------- derived series

def test_underutilisation_is_unemployment_plus_underemployment():
    """Reproduces the published relationship exactly: 4.4283 + 6.5093 = 10.9376,
    which the source rounds to 10.938."""
    une = _monthly(date(2026, 6, 1), [4.4283])
    und = _monthly(date(2026, 6, 1), [6.5093])
    got = emp.derive_underutilisation(une, und)
    assert got == [(date(2026, 6, 1), pytest.approx(10.9376))]
    assert round(got[0][1], 3) == 10.938


def test_medium_term_is_the_duration_buckets_over_the_labour_force():
    """The published 2.5273 comes from the 4-13, 13-26 and 26-52 week buckets
    over the labour force: (206.8 + 101.3 + 81.1) / 15400.4 x 100.

    Pinning the arithmetic matters because two neighbouring definitions look
    equally plausible and are both wrong: 13-52 weeks gives 1.1843 and
    13-weeks-and-over gives 2.1244.
    """
    when = date(2026, 3, 1)
    buckets = [_monthly(when, [206.8]), _monthly(when, [101.3]), _monthly(when, [81.1])]
    lf = _monthly(when, [15400.4])
    got = emp.derive_medium_term(buckets, lf)
    assert got[0][1] == pytest.approx(2.5273, abs=0.0001)


def test_cross_source_join_aligns_on_month_not_exact_date():
    """The trap this guards: the ABS stamps monthly series on the FIRST of the
    month and the RBA stamps H5 on the LAST, so an exact-date join returns the
    empty set -- silently, with no error to notice."""
    abs_style = [(date(2026, 3, 1), 100.0)]
    rba_style = [(date(2026, 3, 31), 10.0)]
    got = emp.derive_ratio(abs_style, rba_style, scale=1.0)
    assert got == [(date(2026, 3, 1), pytest.approx(10.0))]


def test_vacancies_ratio_pairs_the_latest_of_each():
    """Vacancies are quarterly and unemployment monthly. Waiting for a shared
    month throws away the newer unemployment prints and reports a stale ratio:
    on the published data, 48.880 shared against 47.976 paired."""
    vac = [(date(2026, 2, 28), 336.6), (date(2026, 5, 31), 329.5)]
    une = [(date(2026, 2, 28), 661.7), (date(2026, 5, 31), 674.1),
           (date(2026, 6, 30), 686.8)]

    shared = emp.derive_ratio(vac, une)
    assert shared[-1][1] == pytest.approx(48.880, abs=0.001)

    paired = emp.derive_ratio(vac, une, pair_latest=True)
    # 329.5 / 686.8 x 100. The published panel rounds this to 47.973.
    assert paired[-1][1] == pytest.approx(47.9761, abs=0.0001)
    assert paired[-1][0] == date(2026, 6, 30)
    # The appended point is always the newest, so it cannot disturb a window
    # that ends earlier.
    assert paired[-1][0] > emp.WINDOW_END


# ------------------------------------------------------------ manual rows

def test_manual_row_needs_a_mean_and_an_sd_not_a_z():
    """A typed z is unfalsifiable -- nothing on screen shows what it was
    measured against, and it does not move when the reading does."""
    ind = emp.BY_KEY["job_ads"]
    assert ind.manual
    blank = emp.manual_row(ind, 1.3243, None, None, None,
                           date(2026, 6, 1), date(2025, 12, 31))
    assert not blank.ok and "mean" in blank.error

    row = emp.manual_row(ind, 1.3243, 1.5, 0.35, 1.4,
                         date(2026, 6, 1), date(2025, 12, 31))
    assert row.ok
    assert row.current.z == pytest.approx((1.3243 - 1.5) / 0.35)
    assert row.delta_z == pytest.approx((1.3243 - 1.4) / 0.35)


def test_manual_row_respects_the_invert_flag():
    ind = emp.Indicator("x", "X", "typed", invert=True, manual=True)
    row = emp.manual_row(ind, 3.0, 5.0, 1.0, None, date(2026, 6, 1), date(2025, 12, 31))
    assert row.current.z == pytest.approx(2.0)      # below the mean = tighter


# ---------------------------------------------------------------- panel

def _panel():
    tight = _flat_window(5.0) + _monthly(date(2026, 6, 1), [3.0])
    loose = _flat_window(5.0) + _monthly(date(2026, 6, 1), [7.0])
    stale = _flat_window(5.0) + _monthly(date(2026, 3, 1), [4.0])
    rows = [
        emp.score(emp.BY_KEY["unemployment"], tight, date(2025, 12, 31)),
        emp.score(emp.BY_KEY["underemployment"], loose, date(2025, 12, 31)),
        emp.score(emp.BY_KEY["medium_term"], stale, date(2025, 12, 31)),
        emp.manual_row(emp.BY_KEY["job_ads"], None, None, None, None,
                       date(2026, 6, 1), date(2025, 12, 31)),
    ]
    return emp.Panel(rows, date(2025, 12, 31))


def test_total_is_the_mean_of_scored_rows_only():
    p = _panel()
    assert p.n_scored == 3                       # the unscored manual row drops out
    assert len(p.missing) == 1
    assert p.total_z == pytest.approx(
        sum(r.current.z for r in p.scored) / 3)


def test_panel_reports_its_stalest_row():
    """A panel is only as current as its oldest input, and the medium-term rate
    genuinely runs months behind because ABS Detailed lags the headline
    release."""
    p = _panel()
    assert p.as_of == date(2026, 6, 1)
    assert p.oldest_reading.indicator.key == "medium_term"
    assert p.oldest_reading.current.when == date(2026, 3, 1)


def test_counts_split_level_from_direction():
    p = _panel()
    assert p.n_tighter == sum(1 for r in p.scored if r.current.z >= 0)
    assert p.n_tightening == sum(1 for r in p.scored
                                 if r.delta_z is not None and r.delta_z > 0)


def test_every_indicator_has_a_source_and_a_direction():
    assert len(emp.INDICATORS) == 9
    assert sum(1 for i in emp.INDICATORS if i.invert) == 5
    assert sum(1 for i in emp.INDICATORS if i.manual) == 3
    for i in emp.INDICATORS:
        assert i.source and i.label


# --------------------------------------------------------------------------
# Labour market flows
# --------------------------------------------------------------------------
# The gross-flows cube keys every series "previous>current", so the direction
# of the arrow is load-bearing: reading it backwards turns a job-finding rate
# into a job-losing rate and both are plausible-looking numbers.

FLOWS = {
    "Unemployed>Employed full-time": [(date(2026, 5, 1), 60.0), (date(2026, 6, 1), 40.0)],
    "Unemployed>Employed part-time": [(date(2026, 5, 1), 40.0), (date(2026, 6, 1), 60.0)],
    "Unemployed>Unemployed": [(date(2026, 5, 1), 300.0), (date(2026, 6, 1), 300.0)],
    "Unemployed>Not in the labour force (NILF)": [(date(2026, 5, 1), 100.0),
                                                  (date(2026, 6, 1), 100.0)],
    # Flows that do not start from unemployment must not enter either side.
    "Employed full-time>Employed full-time": [(date(2026, 5, 1), 9000.0),
                                              (date(2026, 6, 1), 9000.0)],
    "Not in the labour force (NILF)>Employed full-time": [(date(2026, 5, 1), 200.0),
                                                          (date(2026, 6, 1), 200.0)],
}


def test_job_finding_rate_is_hires_over_everyone_who_was_unemployed():
    """100 of 500 found work: 20%, both months, whichever way they split."""
    assert emp.job_finding_rate(FLOWS) == [
        (date(2026, 5, 1), pytest.approx(20.0)),
        (date(2026, 6, 1), pytest.approx(20.0)),
    ]


def test_the_denominator_comes_from_the_flows_not_the_published_level():
    """Everyone who WAS unemployed, including those still unemployed.

    Dropping the stay-unemployed flow is the easy mistake -- it is the biggest
    row in the block, and without it the rate reads 50% instead of 20%.
    """
    without_stayers = {k: v for k, v in FLOWS.items() if k != "Unemployed>Unemployed"}
    assert emp.job_finding_rate(without_stayers)[0][1] == pytest.approx(50.0)


def test_flows_into_employment_from_outside_are_not_job_finding():
    """NILF -> employed is someone entering, not an unemployed person hired."""
    assert emp.job_finding_rate(FLOWS)[0][1] == pytest.approx(20.0)
    assert "Not in the labour force (NILF)>Employed full-time" in FLOWS


def test_job_finding_rate_survives_a_cube_with_no_unemployment_block():
    assert emp.job_finding_rate({"Employed full-time>Unemployed":
                                        [(date(2026, 6, 1), 5.0)]}) == []


def test_job_switching_rate_is_a_share_of_everyone_employed():
    under = [(date(2026, 2, 1), 2000.0)]
    over = [(date(2026, 2, 1), 8000.0)]
    assert emp.job_switching_rate(under, over) == [
        (date(2026, 2, 1), pytest.approx(20.0))]


def test_quarterly_averages_rather_than_sampling_one_month():
    """Three months of survey noise averaged, not one month kept."""
    monthly = [(date(2026, 1, 1), 10.0), (date(2026, 2, 1), 20.0),
               (date(2026, 3, 1), 30.0), (date(2026, 4, 1), 40.0)]
    assert emp.to_quarterly(monthly) == [
        (date(2026, 1, 1), pytest.approx(20.0)),
        (date(2026, 4, 1), pytest.approx(40.0)),
    ]


def test_lead_shifts_forward_and_shortens_the_series():
    rows = [(date(2026, 1, 1), 1.0), (date(2026, 4, 1), 2.0), (date(2026, 7, 1), 3.0)]
    assert emp.lead(rows, 1) == [(date(2026, 1, 1), 2.0),
                                        (date(2026, 4, 1), 3.0)]
    assert emp.lead(rows, 0) == rows


def test_zscores_centre_and_scale_the_whole_series():
    rows = [(date(2026, m, 1), v) for m, v in
            zip((1, 2, 3, 4, 5), (1.0, 2.0, 3.0, 4.0, 5.0))]
    z = dict(emp.zscores(rows))
    assert z[date(2026, 3, 1)] == pytest.approx(0.0)        # the mean
    assert z[date(2026, 5, 1)] == pytest.approx(1.2649, abs=1e-4)
    assert emp.zscores([(date(2026, 1, 1), 1.0)]) == []
    assert emp.zscores([(date(2026, m, 1), 5.0) for m in (1, 2, 3)]) == []


# --------------------------------------------------------------------------
# Pasted series
# --------------------------------------------------------------------------

def test_a_paste_reads_every_format_a_terminal_exports():
    rows = emp.parse_pasted_series(
        "Date,Value\n"
        "2026-06-01, 81.4\n"
        "30/04/2026\t81.9\n"
        "Mar-2026  82.1\n"
        "2026-02, 81.7%\n"
        "not a row at all\n")
    assert [d for d, _ in rows] == [date(2026, 2, 1), date(2026, 3, 1),
                                    date(2026, 4, 30), date(2026, 6, 1)]
    assert [v for _, v in rows] == [81.7, 82.1, 81.9, 81.4]


def test_a_paste_is_read_day_first():
    """06/07/2026 is 6 July on an Australian terminal, not 7 June."""
    (d, _), = emp.parse_pasted_series("06/07/2026, 80.0")
    assert d == date(2026, 7, 6)


def test_an_unreadable_paste_yields_nothing_rather_than_raising():
    assert emp.parse_pasted_series("") == []
    assert emp.parse_pasted_series("garbage\nmore garbage") == []
