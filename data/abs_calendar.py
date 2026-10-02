"""The ABS's own release calendar: the exact day each headline report is due.

`core.econ_calendar` dates every release by rule (the Labour Force on the
Thursday of the third week, and so on). The rules are right most months and
wrong some: the August 2026 Labour Force came on the 24th, the fourth Thursday.
The ABS publishes its future releases month by month at
abs.gov.au/release-calendar/future-releases/YYYYMM, so the next six months are
read from there, cached for a day, and an exact date replaces the rule's in the
same month. A page that cannot be read leaves the rule in place.
"""

from __future__ import annotations

import html
import json
import re
import time
from datetime import date, datetime
from pathlib import Path

import requests

from data.abs import HEADERS

PAGE = "https://www.abs.gov.au/release-calendar/future-releases/{ym}"
CACHE = Path(__file__).resolve().parent / "cache" / "abs_release_calendar.json"
TTL = 60 * 60 * 24
TIMEOUT = 30

#: The headline publications the app's calendar carries, by their exact ABS
#: title, and the `core.econ_calendar` release each one is. The CPI title
#: covers both the quarterly and the monthly print: the calendar assigns it to
#: whichever its rule puts in that month.
TITLES = {
    "Labour Force, Australia": "Labour Force",
    "Consumer Price Index, Australia": "CPI",
    "Wage Price Index, Australia": "Wage Price Index",
    "Australian National Accounts: National Income, Expenditure and Product":
        "National Accounts (GDP)",
}


def parse(page: str) -> dict[str, list[date]]:
    """{release: [dates]} from one month's future-releases page."""
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page)))
    out: dict[str, list[date]] = {}
    for title, release in TITLES.items():
        # The exact title, not a longer one that starts with it ("Labour
        # Force, Australia, Detailed" is a different, later publication).
        pat = re.compile(r"(?:Mon|Tues|Wednes|Thurs|Fri)day (\d{1,2} \w+ \d{4}) "
                         r"\d{1,2}:\d{2}[ap]m AE[SD]T " + re.escape(title) + r"(?![,:])")
        for m in pat.finditer(text):
            try:
                out.setdefault(release, []).append(
                    datetime.strptime(m.group(1), "%d %B %Y").date())
            except ValueError:
                continue
    return {k: sorted(set(v)) for k, v in out.items()}


def _months(start: date, n: int) -> list[str]:
    out, y, m = [], start.year, start.month
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def future(today: date, months: int = 6) -> dict[str, list[date]]:
    """Exact release days for the next `months` months, cached for a day.
    `{}` (the rules stand) when nothing could be read."""
    try:
        cached = json.loads(CACHE.read_text(encoding="utf-8"))
        if time.time() - CACHE.stat().st_mtime < TTL and cached.get("from") == today.isoformat()[:7]:
            return {k: [date.fromisoformat(d) for d in v] for k, v in cached["dates"].items()}
    except (OSError, ValueError, KeyError):
        cached = None
    merged: dict[str, set[date]] = {}
    got_any = False
    for ym in _months(today, months):
        try:
            r = requests.get(PAGE.format(ym=ym), headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
        except requests.RequestException:
            continue
        got_any = True
        for k, v in parse(r.text).items():
            merged.setdefault(k, set()).update(v)
    if not got_any:
        if cached:
            return {k: [date.fromisoformat(d) for d in v] for k, v in cached["dates"].items()}
        return {}
    out = {k: sorted(v) for k, v in merged.items()}
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({"from": today.isoformat()[:7],
                                     "dates": {k: [d.isoformat() for d in v]
                                               for k, v in out.items()}}), encoding="utf-8")
    except OSError:
        pass
    return out
