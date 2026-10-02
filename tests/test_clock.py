"""The app has one clock, and it is Sydney's.

Two things are pinned here. That `core.clock` really answers in
Australia/Sydney -- including across the DST switch, which a fixed +10:00
offset would get wrong for half the year -- and that no module has quietly
gone back to `date.today()` or `datetime.now(timezone.utc)`. The second is
the one that matters: the bug this replaced was a cache stamping observations
with UTC's date, which is yesterday for most of a Sydney working day.
"""

from __future__ import annotations

import ast
import pathlib
import re
from datetime import date, datetime, timezone

from core import clock

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGES = ("core", "data", "ui", "state", "export", "scripts", "nextprint")

# Epoch-seconds arithmetic (file mtimes, TTLs) is a difference between two
# readings of the same clock, so it carries no timezone and is left alone.
BANNED = re.compile(r"\bdate\.today\(\)|\bdatetime\.now\(|\butcnow\(\)")


def _modules() -> list[pathlib.Path]:
    out = [ROOT / "app.py"]
    for pkg in PACKAGES:
        out += sorted((ROOT / pkg).rglob("*.py"))
    return [p for p in out if p.name != "clock.py"]


# ------------------------------------------------------------ the timezone

def test_the_timezone_is_sydney():
    assert clock.TZ_NAME == "Australia/Sydney"


def test_now_is_timezone_aware_and_on_sydney_time():
    n = clock.now()
    assert n.tzinfo is not None
    assert n.utcoffset().total_seconds() in (10 * 3600, 11 * 3600)   # AEST / AEDT


def test_the_offset_follows_daylight_saving_rather_than_a_fixed_ten_hours():
    """A fixed +10:00 would be an hour out from October to April."""
    winter = datetime(2026, 7, 1, 12, tzinfo=clock.TZ).utcoffset()
    summer = datetime(2026, 12, 1, 12, tzinfo=clock.TZ).utcoffset()
    assert winter.total_seconds() == 10 * 3600
    assert summer.total_seconds() == 11 * 3600


def test_today_is_the_sydney_calendar_date_not_utcs():
    """Through to 10am AEST (11am AEDT) UTC is still on yesterday. Whichever
    side of that line the suite runs, Sydney is never behind UTC."""
    assert clock.today() == clock.now().date()
    assert clock.today() >= datetime.now(timezone.utc).date()


def test_today_returns_a_plain_date():
    assert isinstance(clock.today(), date)
    assert clock.year() == clock.today().year


# ---------------------------------------------------------------- the stamp

def test_the_stamp_is_naive_so_stored_records_keep_their_format():
    """saved_at and run stamps are sorted as strings, slugified into filenames
    and re-read with fromisoformat; a "+10:00" suffix would change all three."""
    s = clock.stamp()
    assert "+" not in s and s.endswith(tuple("0123456789"))
    parsed = datetime.fromisoformat(s)
    assert parsed.tzinfo is None
    assert parsed.date() == clock.today()


def test_the_stamp_reads_sydney_wall_clock_not_utc():
    assert datetime.fromisoformat(clock.stamp()).hour == clock.now().hour


# ------------------------------------------------------------- no backsliding

def test_no_module_reads_the_clock_behind_core_clock():
    offenders = []
    for path in _modules():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if BANNED.search(line) and "noqa: clock" not in line:
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{i}  {line.strip()}")
    assert not offenders, (
        "read the clock through core.clock (Sydney), not directly:\n  "
        + "\n  ".join(offenders))


def test_core_clock_imports_nothing_but_the_standard_library():
    """It is imported by core, data, ui, state and export alike, so it must not
    be able to drag an adapter -- or Streamlit -- in behind it."""
    tree = ast.parse((ROOT / "core" / "clock.py").read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    assert roots <= {"__future__", "datetime", "zoneinfo"}, roots
