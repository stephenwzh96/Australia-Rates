"""The as-of rule: every statistic shows from its release day, never from its
reference date, and the release calendar uses the ABS's own dates."""

from __future__ import annotations

import pathlib
import re
from datetime import date

import pytest

from core import econ_calendar as cal
from core import publication as pub
from data import cache
from data.common import Observation

ROOT = pathlib.Path(__file__).resolve().parent.parent

FAMILIES = [v for k, v in vars(pub).items() if k.isupper() and isinstance(v, str)
            and k not in ("RULE_TEXT",) and v in pub.RULE_TEXT]


@pytest.mark.parametrize("family", FAMILIES)
def test_no_rule_dates_a_print_before_its_period_is_over(family):
    for d in (date(2025, 1, 1), date(2025, 3, 31), date(2025, 5, 15), date(2025, 12, 31)):
        public = pub.public_on(family, d)
        if family == pub.SEED:
            assert public == d
            continue
        if family == pub.MARKET:                 # a daily rate's period is its day
            assert public > d
            continue
        period_end = (date(d.year, (d.month - 1) // 3 * 3 + 3, 28) if family in pub.QUARTERLY
                      else date(d.year, d.month, 28))
        assert public > period_end, (family, d, public)
        assert (public - d).days < 200, (family, d, public)


@pytest.mark.parametrize("family", FAMILIES)
def test_every_family_is_described(family):
    assert pub.RULE_TEXT.get(family)


# Actual first-release days, read off the ABS / RBA / NAB release pages: a rule
# may be later (it errs late), never earlier, or a replay would leak.
ACTUAL = [
    (pub.LABOUR_FORCE, date(2026, 8, 1), date(2026, 9, 24)),     # the 4th-Thursday case
    (pub.CPI_QUARTERLY, date(2026, 6, 1), date(2026, 7, 29)),
    (pub.WPI, date(2026, 6, 1), date(2026, 8, 19)),
    (pub.NATIONAL_ACCOUNTS, date(2026, 4, 1), date(2026, 9, 2)),
    (pub.NAB, date(2026, 8, 31), date(2026, 9, 8)),
    (pub.WESTPAC_MI, date(2026, 9, 30), date(2026, 9, 9)),
    (pub.VACANCIES, date(2026, 8, 31), date(2026, 10, 1)),
]


@pytest.mark.parametrize("family,ref,released", ACTUAL)
def test_rules_are_never_earlier_than_a_known_release(family, ref, released):
    assert pub.public_on(family, ref) >= released


def test_exact_labour_force_days_win():
    exact = {date(2026, 8, 1): date(2026, 9, 24)}
    assert pub.public_on(pub.LABOUR_FORCE, date(2026, 8, 1), exact) == date(2026, 9, 24)
    assert pub.public_on(pub.LF_DETAILED, date(2026, 8, 1), exact) == date(2026, 10, 1)


def test_a_quarter_dated_either_end_lands_on_one_rule():
    """The RBA dates a quarter at its last day, the ABS API at its first."""
    assert (pub.public_on(pub.NATIONAL_ACCOUNTS, date(2026, 4, 1))
            == pub.public_on(pub.NATIONAL_ACCOUNTS, date(2026, 6, 30)))


def test_published_cuts_tuples_and_observations_alike():
    rows = [(date(2026, 4, 1), 0.4), (date(2026, 7, 1), 0.5)]
    obs = [Observation(d, v) for d, v in rows]
    as_of = date(2026, 9, 20)            # Jun quarter printed, Sep quarter not
    assert pub.published(rows, pub.NATIONAL_ACCOUNTS, as_of) == rows[:1]
    assert [o.date for o in pub.published(obs, pub.NATIONAL_ACCOUNTS, as_of)] == [rows[0][0]]
    with pytest.raises(KeyError):
        pub.published_map({"x": rows}, {}, as_of)


def test_every_macro_row_has_a_release_family():
    from core import macro
    assert set(macro.RELEASE) == set(macro.BY_KEY)
    assert all(f in pub.RULE_TEXT for f in macro.RELEASE.values())


def test_every_labour_series_has_a_release_family():
    from core import labor
    assert all(f in pub.RULE_TEXT for f in labor.CODE_FAMILY.values())


# Cutting a STATISTIC on its reference date leaks a print into any replayed
# as-of before its release. Only these lines may compare a date with the as-of
# directly, each for the reason given.
ALLOWED = {
    "ui/sources.py": "capped(): market rates and settlements, public the day they are for",
    "ui/tabs/inflation.py": "breadth points built from classes already cut by cpi_published",
    "ui/tabs/votes.py": "decision days are events, not observations",
}
_CUT = re.compile(r"(\bd|\.date|\bwhen|\.month)\s*<=\s*(ctx\.)?as_of\b")


def test_no_tab_cuts_a_statistic_on_its_reference_date():
    hits = []
    for path in sorted((ROOT / "ui").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _CUT.search(line):
                hits.append(f"{rel}:{n}  {line.strip()}")
    assert not hits, ("cut statistics by release family (src.published), not by date:\n  "
                      + "\n  ".join(hits))


def test_exact_dates_replace_the_rule_across_a_month_end():
    """The November 2026 CPI is due on 6 January 2027; the rule would put it on
    30 December. Inside the span the ABS calendar covers, exact dates replace
    the rule's -- not merely in the same month."""
    lo, hi = date(2026, 10, 1), date(2027, 3, 31)
    exact = (cal.ExactDates(cal.CPI_M, (date(2026, 11, 25), date(2027, 1, 6)), lo, hi),
             cal.ExactDates(cal.LABOUR, (date(2026, 10, 15),), lo, hi))
    ev = cal.recurring_events(date(2026, 10, 1), date(2027, 1, 31), None, exact)
    cpi_m = [e.when for e in ev if e.release == cal.CPI_M]
    assert cpi_m == [date(2026, 11, 25), date(2027, 1, 6)]
    labour = [e.when for e in ev if e.release == cal.LABOUR]
    assert labour == [date(2026, 10, 15)]          # the rest of the span awaits the ABS
    rules_only = cal.recurring_events(date(2026, 10, 1), date(2026, 10, 31))
    assert [e.when for e in rules_only if e.release == cal.LABOUR] == [date(2026, 10, 15)]


def test_a_revised_series_is_replaced_whole_never_spliced(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    old = [Observation(date(2020, 1, 1), 1.0), Observation(date(2026, 1, 1), 2.0)]
    cache.save("t", old, date(2026, 1, 2))
    revised = [Observation(date(2020, 1, 1), 1.5), Observation(date(2026, 1, 1), 2.1)]
    calls = []

    def fetch(since):
        calls.append(since)
        return revised if since is None else revised[1:]

    got = cache.get_series("t", fetch, today=date(2026, 1, 10), whole=True)
    assert calls == [None]
    assert [o.value for o in got] == [1.5, 2.1]       # the 2020 revision is picked up


def test_every_family_has_a_reader_facing_name():
    assert set(pub.LABEL) == set(pub.RULE_TEXT)
