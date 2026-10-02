"""What the street expects from one release -- prompt and parsing."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Sequence

from .release_prep import SubRelease
from .agent_json import extract_json

RECENT_PRINTS = 6

SYSTEM = (
    "You are a senior Australian rates strategist preparing a desk for an ABS "
    "data release. "
    "You report only figures you can source to a named publisher, you never "
    "estimate a consensus yourself, and you treat 'no published consensus "
    "found' as a normal and useful answer. A number without a source is worse "
    "than no number, because the desk will trade off it."
)


@dataclass(frozen=True)
class RecentPrint:
    """One past release of this series, against what was expected."""
    when: date | None
    actual: float
    consensus: float | None
    source: str

    @property
    def surprise(self) -> float | None:
        if self.consensus is None:
            return None
        return self.actual - self.consensus

    @property
    def dated(self) -> str:
        return self.when.strftime("%b %Y") if self.when else "date unclear"


@dataclass(frozen=True)
class Expectation:
    """The street's number for one sub-release."""
    series_key: str
    consensus: float
    source: str
    range_low: float | None = None
    range_high: float | None = None
    forecasters: int | None = None
    prior: float | None = None
    prior_revisable: bool = False
    recent: tuple[RecentPrint, ...] = ()

    @property
    def has_range(self) -> bool:
        return self.range_low is not None and self.range_high is not None

    def surprise_run(self) -> tuple[int, int]:
        """`(above consensus, below consensus)` across the sourced history."""
        above = sum(1 for r in self.recent if (r.surprise or 0) > 0)
        below = sum(1 for r in self.recent if (r.surprise or 0) < 0)
        return above, below


@dataclass(frozen=True)
class SwingFactor:
    text: str
    source: str


@dataclass(frozen=True)
class Research:
    release: str
    when: date
    expectations: tuple[Expectation, ...] = ()
    swing_factors: tuple[SwingFactor, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def empty(self) -> bool:
        return not self.expectations and not self.swing_factors

    def for_series(self, series_key: str) -> Expectation | None:
        return next((e for e in self.expectations
                     if e.series_key == series_key), None)


def prompt(release: str, when: date, specs: Sequence[SubRelease]) -> str:
    """The research brief for one report."""
    lines = "\n".join(
        f'  - "{s.series_key}" — {s.label}, quoted as '
        f'{_basis_phrase(s)} to {s.decimals} decimal place'
        f'{"" if s.decimals == 1 else "s"}'
        for s in specs)
    return f"""Find the published market consensus for the {release} release \
scheduled for {when:%A %d %B %Y}, for each of these numbers:

{lines}

This is an Australian Bureau of Statistics release. Use published forecast \
surveys -- the Bloomberg or Reuters consensus as reported in wire coverage \
(AFR, Reuters, Bloomberg), Trading Economics, and the previews the major \
Australian banks publish in the week before the release (CBA, Westpac, NAB, \
ANZ, Macquarie). Report the MEDIAN consensus, and the range across \
forecasters where the survey publishes one.

Quote every figure in the units listed above and nothing else. An employment \
consensus is a monthly CHANGE in thousands; a quarterly CPI, trimmed mean, WPI \
or GDP consensus is the QUARTER-ON-QUARTER percent change unless its line says \
year-ended; the monthly CPI is quoted YEAR-ENDED; an unemployment or \
participation rate is a level.

RULES, in order of importance:

1. Every number needs a working source URL naming who published the consensus.
   If you cannot source it, leave that sub-release out entirely.
2. Never estimate, average or infer a consensus yourself. You are reporting
   what a survey published, not forming a view.
3. Use only the series listed above. Do not add other numbers from the report.
4. If no consensus is published for one of them, omit it. Omitting one is
   normal; inventing one is not.
5. For the last {RECENT_PRINTS} releases of each series, report what printed
   and what the consensus had been, each with its own source. Where you cannot
   source a past pair, leave that entry out rather than filling the gap.
6. "swing_factors" is what the previews actually flag for THIS print --
   a methodology change, a strike, a weather distortion, a seasonal-adjustment
   quirk, an unusual survey week. One sentence each, each with a source. Return
   an empty list if the previews flag nothing; that is a normal answer.

Return ONLY a JSON object in this exact shape, and nothing else:

{{
  "sub_releases": [
    {{
      "series_key": "exact key from the list above",
      "consensus": 0.0,
      "range_low": 0.0,
      "range_high": 0.0,
      "forecasters": 0,
      "prior": 0.0,
      "prior_revisable": true,
      "source": "https://...",
      "recent": [
        {{"date": "YYYY-MM-DD", "actual": 0.0, "consensus": 0.0,
          "source": "https://..."}}
      ]
    }}
  ],
  "swing_factors": [
    {{"text": "one sentence", "source": "https://..."}}
  ]
}}"""


def _basis_phrase(spec: SubRelease) -> str:
    return {"change": "a monthly change in thousands",
            "m/m": "a month-over-month percent change",
            "q/q": "a quarter-on-quarter percent change",
            "y/y": "a year-ended percent change"}.get(spec.basis, "a level in percent")


def _num(value: Any) -> float | None:
    """A finite number from whatever the model returned."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        out = float(value)
        return out if math.isfinite(out) else None
    if not isinstance(value, str):
        return None
    cleaned = value.strip().replace(",", "").rstrip("%").strip()
    if cleaned[-1:].lower() == "k":
        cleaned = cleaned[:-1]
    m = re.match(r"^[+-]?\d*\.?\d+$", cleaned)
    if not m:
        return None
    try:
        out = float(cleaned)
    except ValueError:
        return None
    return out if math.isfinite(out) else None


def _int(value: Any) -> int | None:
    got = _num(value)
    return None if got is None else int(got)


def _url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    got = value.strip()
    return got if got.lower().startswith(("http://", "https://")) else ""


def _text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip()


def _when(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    m = re.search(r"\d{4}-\d{2}-\d{2}", value)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group())
    except ValueError:
        return None


def _recent(rows: Any) -> tuple[RecentPrint, ...]:
    """Past prints, newest first. A row without an actual or a source is
    dropped -- the pair is the whole point, and an unsourced one is a claim."""
    if not isinstance(rows, list):
        return ()
    out: list[RecentPrint] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        actual = _num(row.get("actual"))
        source = _url(row.get("source"))
        if actual is None or not source:
            continue
        out.append(RecentPrint(_when(row.get("date")), actual,
                               _num(row.get("consensus")), source))
    out.sort(key=lambda r: r.when or date.min, reverse=True)
    return tuple(out[:RECENT_PRINTS])


def parse(raw: str, release: str, when: date,
          specs: Sequence[SubRelease]) -> Research:
    """Readings from the agent's answer. Never raises."""
    payload = extract_json(raw or "")
    if payload is None:
        return Research(release, when, error="No JSON object in the agent's reply.")

    allowed = {s.series_key for s in specs}
    seen: set[str] = set()
    rows: list[Expectation] = []
    for item in payload.get("sub_releases") or []:
        if not isinstance(item, dict):
            continue
        key = _text(item.get("series_key"))
        source = _url(item.get("source"))
        consensus = _num(item.get("consensus"))
        if key not in allowed or key in seen or not source or consensus is None:
            continue
        seen.add(key)
        low, high = _num(item.get("range_low")), _num(item.get("range_high"))
        if low is not None and high is not None and low > high:
            low, high = high, low
        rows.append(Expectation(
            series_key=key, consensus=consensus, source=source,
            range_low=low, range_high=high,
            forecasters=_int(item.get("forecasters")),
            prior=_num(item.get("prior")),
            prior_revisable=bool(item.get("prior_revisable")),
            recent=_recent(item.get("recent")),
        ))

    factors: list[SwingFactor] = []
    for item in payload.get("swing_factors") or []:
        if not isinstance(item, dict):
            continue
        text, source = _text(item.get("text")), _url(item.get("source"))
        if text and source:
            factors.append(SwingFactor(text, source))

    order = {s.series_key: i for i, s in enumerate(specs)}
    rows.sort(key=lambda e: order.get(e.series_key, len(order)))
    return Research(release, when, tuple(rows), tuple(factors))


def to_dict(r: Research) -> dict:
    return {
        "release": r.release, "when": r.when.isoformat(),
        "expectations": [
            {"series_key": e.series_key, "consensus": e.consensus,
             "source": e.source, "range_low": e.range_low,
             "range_high": e.range_high, "forecasters": e.forecasters,
             "prior": e.prior, "prior_revisable": e.prior_revisable,
             "recent": [{"date": p.when.isoformat() if p.when else None,
                         "actual": p.actual, "consensus": p.consensus,
                         "source": p.source} for p in e.recent]}
            for e in r.expectations
        ],
        "swing_factors": [{"text": f.text, "source": f.source}
                          for f in r.swing_factors],
        "error": r.error,
    }


def from_dict(d: dict) -> Research:
    """Rebuild a saved run. Tolerant of a hand-edited or older-shape file, the
    same way `parse` is tolerant of a wayward reply."""
    return Research(
        release=str(d.get("release") or ""),
        when=_when(d.get("when")) or date.min,
        expectations=tuple(
            Expectation(
                series_key=str(e.get("series_key") or ""),
                consensus=_num(e.get("consensus")) or 0.0,
                source=str(e.get("source") or ""),
                range_low=_num(e.get("range_low")),
                range_high=_num(e.get("range_high")),
                forecasters=_int(e.get("forecasters")),
                prior=_num(e.get("prior")),
                prior_revisable=bool(e.get("prior_revisable")),
                recent=_recent(e.get("recent")),
            )
            for e in d.get("expectations") or [] if isinstance(e, dict)
        ),
        swing_factors=tuple(
            SwingFactor(str(f.get("text") or ""), str(f.get("source") or ""))
            for f in d.get("swing_factors") or [] if isinstance(f, dict)
        ),
        error=d.get("error"),
    )
