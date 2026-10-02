"""Everything the study reads, from the app's own adapters, behind their caches.

  * Labour Force (X29, seasonally adjusted persons): unemployed and labour
    force levels (the unrounded rate is their ratio), employment and the
    underemployment rate -- `data.abs`, cached per release.
  * NAB conditions and Westpac-MI sentiment (RBA H3), job vacancies (RBA H5)
    -- `data.rba`, cached daily.
  * The day each Labour Force release came out, from its own page on
    abs.gov.au (pages exist from the December 2019 release). A past release's
    date never changes, so each is cached for good; only months not yet
    cached are fetched.

No Streamlit here and no clock: the tab passes the as-of date in.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import requests

from data import abs as absmod
from data import rba
from nextprint import inputs as I

LF_PAGE = ("https://www.abs.gov.au/statistics/labour/employment-and-unemployment/"
           "labour-force-australia/{slug}")
LF_INDEX = ("https://www.abs.gov.au/statistics/labour/employment-and-unemployment/"
            "labour-force-australia")
CACHE = Path(__file__).resolve().parent.parent / "data" / "cache" / "nextprint_lf_releases.json"
TIMEOUT = 30

X29_HEADERS = {
    I.UNEMPLOYED: "Unemployed total ;  Persons ;",
    I.LABOUR_FORCE_LEVEL: "Labour force total ;  Persons ;",
    "employed": "Employed total ;  Persons ;",
    "underemployment": "Underemployment rate (proportion of labour force) ;  Persons ;",
}
RBA_TABLES = {"GICNBC": "h3", "GICWMICS": "h3", "GLFOSVT": "h5"}


def _pairs(obs) -> list[tuple[date, float]]:
    return [(date(o.date.year, o.date.month, 1), float(o.value)) for o in obs
            if o.value is not None]


def labour_force() -> dict[str, list[tuple[date, float]]]:
    wb = absmod.labour_force_rates()
    return {k: _pairs(wb.find(h)) for k, h in X29_HEADERS.items()}


def rba_series() -> dict[str, list[tuple[date, float]]]:
    out = {}
    for code, table in RBA_TABLES.items():
        try:
            out[code] = _pairs(rba.table_cached(table).get(code) or [])
        except Exception:                                    # noqa: BLE001
            out[code] = []
    return out


# ------------------------------------------------------- release dates ---

def _read_cache() -> dict[str, str]:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _released_on(slug: str) -> date | None:
    try:
        r = requests.get(LF_PAGE.format(slug=slug), headers=absmod.HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException:
        return None
    m = re.search(r'field__label">Released</div>\s*<div class="field__item">\s*([0-9/]+)', r.text)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1).strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def _slugs() -> list[str]:
    """Every release the ABS lists, newest first (`aug-2026`, ...)."""
    try:
        r = requests.get(LF_INDEX, headers=absmod.HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException:
        return []
    found = re.findall(r'labour-force-australia/([a-z]{3}-\d{4})"', r.text)
    latest = absmod.latest_release(absmod.LF_LANDING)
    return list(dict.fromkeys(([latest] if latest else []) + found))


def lf_release_dates(refresh: bool = False) -> dict[date, date]:
    """Reference month -> the day its Labour Force release came out, for every
    release the ABS still has a page for. Cached for good, month by month."""
    cached = _read_cache()
    if refresh or not cached:
        missing = [s for s in _slugs() if s not in cached]
        if missing:
            with ThreadPoolExecutor(max_workers=8) as ex:
                for slug, day in zip(missing, ex.map(_released_on, missing)):
                    if day is not None:
                        cached[slug] = day.isoformat()
            try:
                CACHE.parent.mkdir(parents=True, exist_ok=True)
                CACHE.write_text(json.dumps(cached, indent=0, sort_keys=True), encoding="utf-8")
            except OSError:
                pass
    out = {}
    for slug, day in cached.items():
        try:
            month = datetime.strptime(slug, "%b-%Y").date()
            out[month] = date.fromisoformat(day)
        except ValueError:
            continue
    return out


def load_all(refresh: bool = False) -> dict:
    return {"lf": labour_force(), "rba": rba_series(),
            "lf_releases": lf_release_dates(refresh)}


def fingerprint(data: dict) -> tuple:
    """What a built result depends on: the last observation of every series
    and the release dates known."""
    last = lambda rows: rows[-1] if rows else None                    # noqa: E731
    return (tuple((k, last(v)) for k, v in sorted(data["lf"].items())),
            tuple((k, last(v)) for k, v in sorted(data["rba"].items())),
            len(data["lf_releases"]))


def clear() -> None:
    """Forget the cached release dates so the next load re-reads the index
    (past dates are re-fetched only if missing)."""
    try:
        CACHE.unlink()
    except OSError:
        pass
