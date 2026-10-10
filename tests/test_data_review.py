"""The 3 Oct 2026 data review: release-day freshness, today's as-of, gaps and
seasonals. Each test names the failure it pins."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from core import clock, employment as E, macro
from data import abs as absmod, cache, freshness

SYD = clock.TZ


def at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=SYD)


# ------------------------------------------------------------- release clock

def test_a_morning_copy_is_due_once_the_1130_release_is_out():
    """A day-granular check held a 9am read all release day."""
    checked = at(2026, 10, 15, 9, 0)          # a Thursday: Labour Force day
    assert not freshness.due(checked, at(2026, 10, 15, 11, 0))
    assert freshness.due(checked, at(2026, 10, 15, 11, 31))


def test_a_release_is_looked_for_through_its_settle_window_then_left():
    checked = at(2026, 10, 15, 11, 35)        # read just after 11:30, maybe before the post
    assert not freshness.due(checked, at(2026, 10, 15, 11, 45)), "inside the floor"
    assert freshness.due(checked, at(2026, 10, 15, 12, 0)), "still settling"
    late = at(2026, 10, 15, 15, 0)            # read after the settle window
    assert not freshness.due(late, at(2026, 10, 15, 22, 0))


def test_weekends_have_no_release():
    checked = at(2026, 10, 17, 9, 0)          # Saturday
    assert not freshness.due(checked, at(2026, 10, 17, 13, 0))


def test_the_ceiling_rechecks_anything_a_day_old():
    assert freshness.due(at(2026, 10, 17, 9, 0), at(2026, 10, 18, 9, 1))


def test_the_memo_token_moves_at_the_release_and_while_it_settles():
    before = freshness.token(at(2026, 10, 15, 11, 0))
    after = freshness.token(at(2026, 10, 15, 11, 31))
    later = freshness.token(at(2026, 10, 15, 11, 50))
    assert before != after != later
    assert freshness.token(at(2026, 10, 15, 16, 0)) == freshness.token(at(2026, 10, 15, 20, 0))


def test_the_landing_page_slug_is_not_held_for_the_life_of_the_process(monkeypatch):
    """`lru_cache` held the release month until a restart: a server running
    across a release kept last month's Labour Force and CPI."""
    pages = iter(['<a href="/x/aug-2026/62020X29.xlsx">', '<a href="/x/sep-2026/62020X29.xlsx">'])

    class R:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    monkeypatch.setattr(absmod.requests, "get", lambda *a, **k: R(next(pages)))
    absmod.clear_release_memo()
    monkeypatch.setattr(freshness, "token", lambda now=None: ("morning",))
    assert absmod.latest_release("landing-test") == "aug-2026"
    assert absmod.latest_release("landing-test") == "aug-2026", "memoised within a token"
    monkeypatch.setattr(freshness, "token", lambda now=None: ("after 11:30",))
    assert absmod.latest_release("landing-test") == "sep-2026"
    absmod.clear_release_memo()


def test_the_cache_records_the_moment_of_each_check(tmp_path, monkeypatch):
    from data.common import Observation
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    cache.save("k", [Observation(date(2026, 8, 1), 1.0)])
    assert cache.load("k").checked_at is not None
    calls = []
    got = cache.get_series("k", lambda since: calls.append(since) or [], due=lambda c: False)
    assert got and not calls, "not due: the copy is served without asking the source"


# ------------------------------------------------------------ today's as-of

def test_todays_as_of_shows_every_print_the_copy_holds(monkeypatch):
    """The release rules err late by design; applied at today's as-of they hid
    prints that were already out (August approvals until 10 Oct)."""
    from core import publication as pub
    from ui import sources as src
    monkeypatch.setattr(src, "fetch_lf_release_dates", lambda: {})
    monkeypatch.setattr(src.clock, "today", lambda: date(2026, 10, 3))
    rows = [(date(2026, 7, 1), 1.0), (date(2026, 8, 1), 2.0)]
    assert src.published(rows, pub.APPROVALS, date(2026, 10, 3)) == rows
    # A replay still waits for the rule: August approvals count from 10 Oct.
    assert src.published(rows, pub.APPROVALS, date(2026, 10, 2)) == rows[:1]


def test_todays_as_of_keeps_a_print_stamped_at_its_month_end(monkeypatch):
    """The RBA stamps a month at its last day, so October's Westpac-MI print
    (out 7 Oct, H3 row 31/10/2026) is dated after 11 Oct. Cutting today's copy
    on the stamp dropped it from Macro while Labour showed it."""
    from core import publication as pub
    from ui import sources as src
    monkeypatch.setattr(src, "fetch_lf_release_dates", lambda: {})
    monkeypatch.setattr(src.clock, "today", lambda: date(2026, 10, 11))
    rows = [(date(2026, 9, 30), 84.4), (date(2026, 10, 31), 80.4)]
    assert src.published(rows, pub.WESTPAC_MI, date(2026, 10, 11)) == rows
    assert src.published_map({"s": rows}, pub.WESTPAC_MI, date(2026, 10, 11)) == {"s": rows}
    # A month that has not begun is still not in today's copy.
    assert src.published(rows + [(date(2026, 11, 30), 1.0)], pub.WESTPAC_MI,
                         date(2026, 10, 11)) == rows
    # A replay still applies the rule: October counts from its month end.
    assert src.published(rows, pub.WESTPAC_MI, date(2026, 10, 10)) == rows[:1]


def test_next_monthly_cpi_includes_the_quarterly_day(monkeypatch):
    """September's monthly CPI prints with the September-quarter CPI on 28 Oct,
    a day the calendar files under the quarterly release alone: the monthly
    panels read "Next release: 25 Nov" on 11 Oct."""
    from ui import sources as src
    monkeypatch.setattr(src, "release_dates", lambda today: ())
    assert src.next_monthly_cpi(date(2026, 10, 11)) == date(2026, 10, 28)
    assert src.next_monthly_cpi(date(2026, 10, 29)) == date(2026, 11, 25)


def test_todays_map_still_refuses_an_unclassified_series(monkeypatch):
    from ui import sources as src
    monkeypatch.setattr(src.clock, "today", lambda: date(2026, 10, 3))
    with pytest.raises(KeyError):
        src.published_map({"mystery": [(date(2026, 8, 1), 1.0)]}, {}, date(2026, 10, 3))


# ------------------------------------------------------------------- gaps

def _quarters(values, start=date(2007, 2, 28), skip=()):
    out, y, m = [], start.year, start.month
    for v in values:
        if (y, m) not in skip:
            out.append((date(y, m, 28), v))
        y, m = (y + 1, m - 9) if m > 9 else (y, m + 3)
    return out


def test_vacancies_qoq_never_spans_the_2008_09_suspension():
    rows = [(date(2008, 5, 31), 100.0), (date(2009, 11, 30), 80.0), (date(2010, 2, 28), 90.0)]
    out = dict(macro.transform("qoq_lvl", rows))
    assert date(2009, 10, 1) not in out, "an 18-month change printed as one quarter's"
    assert out[date(2010, 1, 1)] == pytest.approx(12.5)


def test_monthly_changes_never_span_a_missing_month():
    rows = [(date(2019, m, 1), 100.0 + m) for m in (1, 2, 3, 4, 5)]
    rows += [(date(2019, m, 1), 100.0 + m) for m in (7, 8, 9, 10)]   # June missing
    months = [d for d, _ in macro.transform("mom3", rows)]
    assert date(2019, 7, 1) not in months and date(2019, 8, 1) not in months


# --------------------------------------------------------------- seasonals

def test_a_stable_seasonal_comes_out_and_the_level_stays():
    season = {m: (1.0 if m in (1, 2) else -0.2) for m in range(1, 13)}
    rows = [(date(2010 + i // 12, i % 12 + 1, 1), 5.0 + season[i % 12 + 1]) for i in range(72)]
    out = E.deseasonalise(rows)
    vals = [v for _, v in out]
    assert max(vals) - min(vals) < 1e-9
    assert sum(vals) / len(vals) == pytest.approx(sum(v for _, v in rows) / len(rows))


def test_a_quarterly_seasonal_comes_out():
    """Table 17 tenure: Feb, May, Aug, Nov."""
    season = {2: -0.6, 5: 0.1, 8: 0.2, 11: 0.3}
    rows = []
    for i in range(24):
        y, m = 2010 + i // 4, (2, 5, 8, 11)[i % 4]
        rows.append((date(y, m, 1), 19.0 + season[m]))
    vals = [v for _, v in E.deseasonalise(rows, period=4)]
    assert max(vals) - min(vals) < 1e-9


def test_too_short_a_series_is_left_alone():
    rows = [(date(2025, m, 1), float(m)) for m in range(1, 13)]
    assert E.deseasonalise(rows) == rows


def test_a_part_quarter_is_not_averaged_beside_whole_ones():
    rows = [(date(2026, m, 1), 20.0) for m in (4, 5, 6, 7, 8)]
    assert [d for d, _ in E.to_quarterly(rows, need=3)] == [date(2026, 4, 1)]
    assert len(E.to_quarterly(rows)) == 2, "the default passes a quarterly series through"
