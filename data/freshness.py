"""When a cached copy has to be checked against its source again.

The ABS publishes at 11:30 Sydney time on business days, and the RBA republishes
its statistical tables (H3, H4, H5, G3, D1) the same morning. A day-granular
check -- "fetched today? then keep it" -- is wrong on exactly the days that
matter: a copy read at 9am holds last month's Labour Force all through release
day, and a process-lifetime memo of the release's landing page held it until
the server restarted.

`due(checked, now)` is true when

* the copy has never been checked, or
* it is older than `CEILING` (a backstop for anything off the calendar), or
* a release instant has passed since it was checked, or within `SETTLE` before
  it -- a page posted a few minutes late keeps being looked for -- and it is
  older than `FLOOR`, so a busy release morning does not hammer the source.

`token(now)` is the same rule in a form a process memo can be keyed on: it
changes whenever a copy checked under the previous token could have become due.

Every time is timezone-aware, in Sydney (`core.clock`).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from core import clock

#: ABS release time; the RBA's republished tables follow within the hour.
RELEASE_TIME = time(11, 30)
FLOOR = timedelta(minutes=15)
SETTLE = timedelta(hours=3)
CEILING = timedelta(hours=24)


def release_instants(now: datetime, days: int = 4) -> list[datetime]:
    """The 11:30 releases of the last `days` business days, up to `now`."""
    local = now.astimezone(clock.TZ)
    out = []
    for back in range(days, -1, -1):
        d = local.date() - timedelta(days=back)
        if d.weekday() >= 5:
            continue
        at = datetime.combine(d, RELEASE_TIME, clock.TZ)
        if at <= local:
            out.append(at)
    return out


def due(checked: datetime | None, now: datetime | None = None) -> bool:
    """Whether a copy last checked at `checked` must be checked again."""
    now = now or clock.now()
    if checked is None:
        return True
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=clock.TZ)
    age = now - checked
    if age >= CEILING:
        return True
    if age < FLOOR:
        return False
    return any(checked < r + SETTLE for r in release_instants(now))


def token(now: datetime | None = None) -> tuple:
    """A memo key that moves whenever `due` could turn true for a copy read
    under the previous one: each day, each release instant, and every `FLOOR`
    while a release is settling."""
    now = (now or clock.now()).astimezone(clock.TZ)
    passed = release_instants(now)
    last = passed[-1] if passed else None
    settling = last is not None and now < last + SETTLE
    slot = int((now - last) / FLOOR) if settling else -1
    return (now.date(), last, slot)
