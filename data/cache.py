"""On-disk incremental cache for dated series.

Every series this app pulls -- FRED observations, FRED release dates, contract
price history -- is an append-mostly list of `(date, value)`. Re-downloading
two years of it on every cold start is wasteful and slow, and it makes the app
useless the moment a source is unreachable. This stores what has already been
seen and asks each source only for what is missing.

THE REVISION PROBLEM
--------------------
"Append-mostly" is not "append-only". Statistical agencies revise: payrolls
get revised for roughly two months after first print, and benchmark revisions
rewrite years at once. A naive append-only cache would freeze the first print
forever and quietly diverge from the source.

So the policy is: everything older than `REVISION_WINDOW_DAYS` is trusted from
cache, everything newer is always refetched and overwrites what was there.
That keeps the request small while staying correct for ordinary revisions.
`stale_after` on top of that forces a periodic full refresh, which is what
eventually picks up a benchmark revision to older history.

FAILURE POLICY
--------------
Matches every adapter in this package: never raise. A corrupt or unreadable
cache file is discarded and refetched; a failed fetch falls back to whatever
is cached, so the app keeps working offline on the last good data rather than
showing nothing.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .common import Observation

SCHEMA = 1
CACHE_DIR = Path(__file__).resolve().parent / "cache"

# Rewrite this much of the recent tail on every fetch: long enough to catch the
# ~2-month revision window on payrolls, short enough that the request stays small.
REVISION_WINDOW_DAYS = 100

# Beyond this, refetch the whole series rather than just the tail -- the only
# way a benchmark revision to older history ever gets picked up.
DEFAULT_STALE_AFTER_DAYS = 30


def _safe(key: str) -> str:
    """Filesystem-safe stem. Shared by `_path` and `clear` so a prefix match
    is done in the same alphabet the files were written in."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in key)


def _path(key: str) -> Path:
    return CACHE_DIR / f"{_safe(key)}.json"


@dataclass(frozen=True)
class CachedSeries:
    key: str
    observations: list[Observation]
    fetched_at: date | None

    @property
    def last_date(self) -> date | None:
        return self.observations[-1].date if self.observations else None

    def is_stale(self, today: date, stale_after: int = DEFAULT_STALE_AFTER_DAYS) -> bool:
        return self.fetched_at is None or (today - self.fetched_at).days >= stale_after


def load(key: str) -> CachedSeries | None:
    """Whatever is on disk for `key`, or None. Never raises: an unreadable or
    malformed file is treated as absent so the caller simply refetches."""
    p = _path(key)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if raw.get("schema") != SCHEMA:
            return None
        obs = [Observation(date.fromisoformat(d), float(v))
               for d, v in raw.get("observations", [])]
        stamp = raw.get("fetched_at")
        return CachedSeries(key, sorted(obs, key=lambda o: o.date),
                            date.fromisoformat(stamp) if stamp else None)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def save(key: str, observations: Iterable[Observation],
         fetched_at: date | None = None) -> None:
    """Write atomically -- temp file then replace -- so an interrupted write
    can never leave a half-written file that the next load has to survive."""
    rows = sorted({o.date: o.value for o in observations}.items())
    payload = {
        "schema": SCHEMA,
        "key": key,
        "fetched_at": (fetched_at or datetime.now(timezone.utc).date()).isoformat(),
        "observations": [[d.isoformat(), v] for d, v in rows],
    }
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":"))
            os.replace(tmp, _path(key))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError:
        pass          # a cache that cannot be written is a slow app, not a broken one


def _write_atomic(key: str, payload: dict) -> None:
    """The temp-file-then-replace dance from `save`, on an arbitrary payload."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":"))
            os.replace(tmp, _path(key))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError:
        pass


def write_json(key: str, rows: list[dict]) -> None:
    """Cache a snapshot that is not a (date, value) series.

    The ASX price feed returns a whole strip of contracts at once, with no
    history behind it -- there is nothing for `save`'s observation model to
    hold. Kept here rather than in `data.asx` so snapshot and series caches
    share one directory, one atomic-write path and one `clear`.
    """
    _write_atomic(key, {"schema": SCHEMA, "key": key,
                        "fetched_at": datetime.now(timezone.utc).date().isoformat(),
                        "rows": rows})


def read_json(key: str) -> list[dict]:
    """Snapshot written by `write_json`, or [] if absent or unreadable."""
    try:
        raw = json.loads(_path(key).read_text(encoding="utf-8"))
        if raw.get("schema") != SCHEMA:
            return []
        rows = raw.get("rows")
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


def merge(cached: Sequence[Observation],
          fresh: Sequence[Observation]) -> list[Observation]:
    """Fresh wins on any overlapping date -- that is what applies revisions."""
    by_date = {o.date: o.value for o in cached}
    by_date.update({o.date: o.value for o in fresh})
    return [Observation(d, v) for d, v in sorted(by_date.items())]


def get_series(
    key: str,
    fetch: Callable[[date | None], list[Observation]],
    today: date | None = None,
    stale_after: int = DEFAULT_STALE_AFTER_DAYS,
    revision_window: int = REVISION_WINDOW_DAYS,
    recheck_after: int = 0,
) -> list[Observation]:
    """Cached series, refreshed incrementally.

    `fetch(since)` must return observations from `since` onward, or the whole
    series when `since` is None. Called with None on a cold or stale cache and
    with a recent date otherwise, so the common path is a small request.

    A fetch that returns nothing is treated as a failed fetch, not as an empty
    series: the cached data is returned unchanged rather than being wiped by a
    transient outage.

    `recheck_after` is the minimum days between network checks. The default 0
    rechecks on every call, which is what a live price wants. A monthly
    statistic does not: thirty-odd series each making a round-trip to confirm
    nothing has changed since this morning is seconds of latency to learn
    nothing. Pass 1 for a series that publishes at most daily and the cache
    answers from disk until tomorrow.
    """
    now = today or datetime.now(timezone.utc).date()
    cached = load(key)

    if (recheck_after and cached and cached.observations and cached.fetched_at
            and (now - cached.fetched_at).days < recheck_after
            and not cached.is_stale(now, stale_after)):
        return list(cached.observations)

    if cached is None or not cached.observations or cached.is_stale(now, stale_after):
        fresh = fetch(None)
        if not fresh:
            return list(cached.observations) if cached else []
        save(key, fresh, now)
        return list(fresh)

    since = max(cached.observations[0].date,
                (cached.last_date or now) - timedelta(days=revision_window))
    fresh = fetch(since)
    if not fresh:
        return list(cached.observations)
    merged = merge(cached.observations, fresh)
    save(key, merged, now)
    return merged


def clear(prefix: str = "") -> int:
    """Delete cached series whose key starts with `prefix`; returns the count.

    The disk cache deliberately survives a Streamlit cache clear -- that is
    the point of it -- so a "refresh all sources" button that only cleared
    the in-memory layer would re-read the same files and look like it had
    done nothing, especially for a series behind the daily recheck guard.
    Never raises: a refresh that cannot delete is a slow refresh, not a
    broken app.
    """
    gone = 0
    try:
        if not CACHE_DIR.exists():
            return 0
        for f in CACHE_DIR.glob("*.json"):
            if not prefix or f.stem.startswith(_safe(prefix)):
                try:
                    f.unlink()
                    gone += 1
                except OSError:
                    continue
    except OSError:
        return gone
    return gone
