"""The RBA's Statement on Monetary Policy forecast table, round by round.

The FOMC version reads the SEP medians from FRED. The RBA publishes its
forecasts as a table in each Statement's Outlook chapter ("Detailed Forecast
Table": GDP and its parts, the labour market, wages, CPI and trimmed mean, and
the cash-rate assumption, at half-yearly horizons), so this reads that table
for each round and caches it. A round, once published, never changes, so each
is fetched once and kept.

Never raises: a page that cannot be read returns None and the tab says so.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date

import requests

from . import cache

URL = "https://www.rba.gov.au/publications/smp/{year}/{mon}/outlook.html"
TIMEOUT = 30
HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")}
#: The Statement's four months.
ROUND_MONTHS = {2: "feb", 5: "may", 8: "aug", 11: "nov"}


@dataclass(frozen=True)
class Round:
    year: int
    month: int
    horizons: list[str]                 # "Jun 2026", "Dec 2026", ...
    rows: dict[str, list[float | None]]  # row label -> value per horizon
    finalised: str = ""                  # "Forecasts finalised on 5 August."

    @property
    def key(self) -> str:
        return f"{self.year}-{ROUND_MONTHS[self.month]}"

    @property
    def label(self) -> str:
        return f"{ROUND_MONTHS[self.month].title()} {self.year}"


def _cell(text: str) -> float | None:
    t = text.replace("−", "-").replace("–", "-").strip()
    try:
        return float(t)
    except ValueError:
        return None


def _clean(fragment: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse(page: str, year: int, month: int) -> Round | None:
    """The Detailed Forecast Table out of an Outlook chapter, or None."""
    for table in re.findall(r"<table.*?</table>", page, re.S):
        caption = re.search(r"<caption.*?>(.*?)</caption>", table, re.S)
        if not caption or "forecast table" not in _clean(caption.group(1)).lower():
            continue
        horizons: list[str] = []
        rows: dict[str, list[float | None]] = {}
        for tr in re.findall(r"<tr.*?</tr>", table, re.S):
            cells = [_clean(c) for c in re.findall(r"<t[hd].*?</t[hd]>", tr, re.S)]
            if not cells:
                continue
            if not horizons and len(cells) > 2 and all(re.match(r"[A-Z][a-z]{2} \d{4}", c)
                                                       for c in cells[1:] if c):
                horizons = [c for c in cells[1:] if c]
                continue
            if horizons and len(cells) == len(horizons) + 1:
                label = re.sub(r"\s*\([a-z]\)\s*", " ", cells[0]).strip()
                rows[label] = [_cell(c) for c in cells[1:]]
        if horizons and rows:
            tail = page[page.find(table) + len(table):][:400]
            fin = re.search(r"Forecasts finalised on ([^.]+)\.", _clean(tail))
            return Round(year, month, horizons, rows, fin.group(1) if fin else "")
    return None


def fetch(year: int, month: int) -> Round | None:
    """One round, from the disk cache or the RBA site."""
    key = f"rba-smp-{year}-{ROUND_MONTHS[month]}"
    cached = cache.read_json(key)
    if cached:
        r = cached[0]
        return Round(year, month, r["horizons"], {k: v for k, v in r["rows"].items()},
                     r.get("finalised", ""))
    try:
        resp = requests.get(URL.format(year=year, mon=ROUND_MONTHS[month]), headers=HEADERS,
                            timeout=TIMEOUT)
        if resp.status_code != 200:
            return None
        got = parse(resp.text, year, month)
    except requests.RequestException:
        return None
    if got is not None:
        cache.write_json(key, [{"horizons": got.horizons, "rows": got.rows,
                                "finalised": got.finalised}])
    return got


def rounds_before(as_of: date, count: int = 2, released: dict[tuple[int, int], date] | None = None
                  ) -> list[Round]:
    """The latest `count` rounds published on or before `as_of`, newest first.

    `released` maps (year, month) to the decision day the Statement came out on;
    without it a round counts as published from the 1st of its month's last
    week, which is never later than the actual release."""
    out: list[Round] = []
    y, m = as_of.year, as_of.month
    while m not in ROUND_MONTHS:
        m -= 1
    tries = 0
    while len(out) < count and tries < 12:
        tries += 1
        rel = (released or {}).get((y, m), date(y, m, 22))
        if rel <= as_of:
            got = fetch(y, m)
            if got is not None:
                out.append(got)
        m -= 3
        if m < 1:
            y, m = y - 1, m + 12
    return out
