"""Every ASX settlement the app has seen, kept day by day -- a contract history
the exchange does not publish.

The US Curve tab charts a contract's trailing six months off Yahoo's daily
closes. The ASX feed carries no history (its `days` parameter is inert), so
here each successful strip fetch files its settlement prices under the
settlement date they are for. Run the app on a trading day and that day's
marks are kept; over weeks a real per-contract history accrues.

Only true settlements are kept -- not a bid/ask midpoint or a last trade the
feed fell back to, which are not daily marks. A day is written once and later
fetches of the same settlement date only fill contracts it lacked.

Stored outside `data/cache/` on purpose: "Refresh all data" clears the cache,
and this is the one file that cannot be refetched.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

DIR = Path(__file__).resolve().parent / "snapshots"


def _path(kind: str) -> Path:
    return DIR / f"asx-{kind}.json"


def _read(kind: str) -> dict[str, dict[str, float]]:
    try:
        raw = json.loads(_path(kind).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def record(kind: str, quotes) -> int:
    """File the settlements in `quotes` under their own settlement dates.
    Returns how many prices were added. Never raises."""
    days = _read(kind)
    added = 0
    for q in quotes:
        if q.source != "settlement" or q.price is None or q.asof is None:
            continue
        day = days.setdefault(q.asof.isoformat(), {})
        if q.symbol not in day:
            day[q.symbol] = float(q.price)
            added += 1
    if added:
        try:
            DIR.mkdir(parents=True, exist_ok=True)
            _path(kind).write_text(json.dumps(days, sort_keys=True, indent=0), encoding="utf-8")
        except OSError:
            return 0
    return added


def history(kind: str, symbol: str) -> list[tuple[date, float]]:
    """One contract's settlement prices, oldest first."""
    out = []
    for day, prices in _read(kind).items():
        if symbol in prices:
            try:
                out.append((date.fromisoformat(day), float(prices[symbol])))
            except (ValueError, TypeError):
                continue
    return sorted(out)


def coverage(kind: str) -> tuple[date | None, int]:
    """(first settlement date kept, number of days kept)."""
    days = sorted(_read(kind))
    return (date.fromisoformat(days[0]) if days else None), len(days)
