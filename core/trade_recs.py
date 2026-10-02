"""Trade recs: three trades from the whole desk -- prompt, schema and parsing.

The brief the model reads is assembled from the app's own tabs (`ui.recs_brief`);
this module only knows how to ask, what shape the answer must take, and how to
read it back without trusting it. It stays pure: no fetching, no Streamlit.

Three horizons, in the order a desk runs them: the next tier-1 print, the next
RBA decision, and a two-to-three-month position. Every trade must carry the
levels it would be put on at, the reasons in two kinds -- fundamental (why the
economy or the Board points this way) and quantitative (the figures that
say the price is wrong) -- and the one thing that would prove it wrong.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

from .speeches import extract_json

PROMPT_VERSION = 2

#: key -> (label, what the horizon runs to)
HORIZONS: dict[str, tuple[str, str]] = {
    "event": ("Next tier-1 print", "through the release"),
    "meeting": ("To the next RBA", "through the decision"),
    "medium": ("Two to three months", "a position, not an event"),
}
HORIZON_ORDER = tuple(HORIZONS)

DIRECTIONS = ("receive", "pay", "steepener", "flattener", "other")

MEDIUM_DAYS = (60, 90)

MAX_REASONS = 6

SYSTEM = (
    "You are the senior Australian rates strategist whose trade ideas the desk actually "
    "puts on: known for calls that are specific, levelled and falsifiable, and "
    "for saying so when the price is already right. You work from the desk's "
    "own numbers first -- the brief you are given is live and internally "
    "consistent -- and use the web for what the desk cannot see: the latest "
    "news, RBA speakers and minutes, previews and consensus for the next ABS release, and the "
    "sell-side's newest notes. You never invent a level, a consensus or a "
    "quote; a figure you cannot source is left out. You write for traders: "
    "short sentences, numbers with units, no hedging filler."
)


# ------------------------------------------------------------------ horizons

@dataclass(frozen=True)
class Horizon:
    key: str
    label: str
    ends: date                  # the exit date the trade is sized to
    anchor: str                 # what sits at that date, in words


def horizons(as_of: date, event_day: date | None, event_name: str,
             meeting_end: date | None, meeting_label: str) -> list[Horizon]:
    """The three horizons with real dates. The medium one ends 90 days out,
    the far edge of "two to three months", so it spans whatever sits between."""
    out: list[Horizon] = []
    if event_day is not None:
        out.append(Horizon("event", HORIZONS["event"][0], event_day, event_name))
    if meeting_end is not None:
        out.append(Horizon("meeting", HORIZONS["meeting"][0], meeting_end,
                           f"{meeting_label} RBA"))
    out.append(Horizon("medium", HORIZONS["medium"][0],
                       as_of + timedelta(days=MEDIUM_DAYS[1]), "90 days out"))
    return out


# -------------------------------------------------------------------- schema

_SOURCED = {"type": "object", "additionalProperties": False,
            "required": ["source"]}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["regime", "desk_view", "recommendations", "sources"],
    "properties": {
        "regime": {"type": "string",
                   "description": "The regime call in under ten words."},
        "desk_view": {"type": "string",
                      "description": "Three to five sentences: where the economy, the "
                                     "Board and the price stand, and the gap between them."},
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["horizon", "title", "expression", "instrument", "direction",
                             "entry", "target", "stop", "entry_bp", "target_bp", "stop_bp",
                             "conviction", "sizing", "exit_by", "catalyst", "thesis",
                             "fundamental", "quantitative", "risks", "wrong_if"],
                "properties": {
                    "horizon": {"type": "string", "enum": list(HORIZON_ORDER)},
                    "title": {"type": "string"},
                    "expression": {"type": "string"},
                    "instrument": {"type": "string"},
                    "direction": {"type": "string", "enum": list(DIRECTIONS)},
                    "entry": {"type": "string"},
                    "target": {"type": "string"},
                    "stop": {"type": "string"},
                    "entry_bp": {"type": ["number", "null"]},
                    "target_bp": {"type": ["number", "null"]},
                    "stop_bp": {"type": ["number", "null"]},
                    "conviction": {"type": "integer"},
                    "sizing": {"type": "string"},
                    "exit_by": {"type": "string",
                                "description": "The exit date as YYYY-MM-DD."},
                    "catalyst": {"type": "string"},
                    "thesis": {"type": "string"},
                    "fundamental": {"type": "array", "items": {
                        **_SOURCED, "required": ["point", "source"],
                        "properties": {"point": {"type": "string"},
                                       "source": {"type": "string"}}}},
                    "quantitative": {"type": "array", "items": {
                        **_SOURCED, "required": ["figure", "reading", "source"],
                        "properties": {"figure": {"type": "string",
                                                  "description": "The number alone, under 16 "
                                                                 "characters: '+36.9bp', '48% vs 38%'."},
                                       "reading": {"type": "string"},
                                       "source": {"type": "string"}}}},
                    "risks": {"type": "array", "items": {
                        **_SOURCED, "required": ["risk", "source"],
                        "properties": {"risk": {"type": "string"},
                                       "source": {"type": "string"}}}},
                    "wrong_if": {"type": "string"},
                },
            },
        },
        "sources": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["title", "url"],
            "properties": {"title": {"type": "string"}, "url": {"type": "string"}}}},
    },
}


# -------------------------------------------------------------------- prompt

def prompt(brief: str, as_of: date, spans: list[Horizon]) -> str:
    """The research brief: the desk's numbers, then what to recommend."""
    lines = "\n".join(
        f'  - "{h.key}": {h.label.lower()}, exit by {h.ends:%a %d %b %Y} ({h.anchor})'
        for h in spans)
    return f"""Today is {as_of:%A %d %B %Y}. Recommend exactly one Australian \
rates trade for each of these horizons:

{lines}

THE DESK'S BRIEF -- live numbers from the dashboard, as of today. Treat these \
as the price and the data; quote them exactly.

{brief}

WHAT TO DO

1. Read the brief end to end, then search the web for what it cannot hold:
   the latest RBA speeches, minutes and Senate Estimates testimony, the
   published consensus and previews for the next ABS release, the newest
   sell-side notes on the RBA, and any news since the brief's data that moves
   the path. Prefer primary sources (RBA, ABS, ASX) and named wires (AFR,
   Reuters, Bloomberg) or bank research.
2. For each horizon choose the ONE trade with the best reward for the risk.
   Use instruments the desk trades: ASX 30-day interbank cash rate futures
   (IB, e.g. IBX6), 90-day bank bill futures (IR, e.g. IRZ6), RBA-dated OIS,
   or ACGB curve trades (3s10s via the YM and XM futures). For the
   event and meeting horizons the natural expression is the priced step for a
   meeting, in bp -- quote entry, target and stop in the SAME units the brief
   prices it in, and put those numbers in entry_bp / target_bp / stop_bp.
   Where no single bp level fits (a curve trade), give the level in its own
   units and leave the *_bp fields null.
3. The entry must be at or near today's level in the brief. A target the
   price cannot plausibly reach by the exit date is not a target.
4. "fundamental": two to five reasons -- the economy, the Board, the
   reaction function. "quantitative": three to six figures that say the price
   is wrong: "figure" is the number alone, short ("+16.5bp", "54% vs 66%",
   "z +2.6"); any working goes in "reading", with what it means and its
   source. Cite the brief as
   "app:<section>" (for example "app:Pricing", "app:SMP & forecasts") and anything
   from the web by its URL.
5. "risks": one to three things that would hurt the trade, sourced the same
   way. "wrong_if": the single observable that proves the thesis wrong.
6. conviction is 1 (a probe) to 5 (the best trade on the desk). sizing is in
   risk terms ("half normal risk", "full risk: 1x the desk's Kelly size").
   exit_by is the exit date as YYYY-MM-DD. Keep the thesis to three
   sentences.
7. If the market is priced right for a horizon, say so in the thesis and give
   the least-bad expression at conviction 1 -- never force a view.
8. Do not repeat the desk's current position back unless the evidence
   supports it; challenge it where it does not.

Return the JSON object the output schema describes: the regime call, the desk
view, the three recommendations and every web source you relied on."""


# --------------------------------------------------------------------- types

@dataclass(frozen=True)
class Point:
    """One sourced reason. `figure` is set for a quantitative one."""
    text: str
    source: str
    figure: str = ""

    @property
    def source_tag(self) -> str:
        return source_tag(self.source)

    @property
    def link(self) -> str:
        return self.source if _is_url(self.source) else ""


@dataclass(frozen=True)
class Rec:
    horizon: str
    title: str
    expression: str
    instrument: str
    direction: str
    entry: str
    target: str
    stop: str
    entry_bp: float | None
    target_bp: float | None
    stop_bp: float | None
    conviction: int
    sizing: str
    exit_by: date | None
    catalyst: str
    thesis: str
    fundamental: tuple[Point, ...]
    quantitative: tuple[Point, ...]
    risks: tuple[Point, ...]
    wrong_if: str

    @property
    def horizon_label(self) -> str:
        return HORIZONS.get(self.horizon, (self.horizon.title(), ""))[0]

    @property
    def reward_bp(self) -> float | None:
        if self.entry_bp is None or self.target_bp is None:
            return None
        return abs(self.target_bp - self.entry_bp)

    @property
    def risk_bp(self) -> float | None:
        if self.entry_bp is None or self.stop_bp is None:
            return None
        return abs(self.stop_bp - self.entry_bp)

    @property
    def reward_risk(self) -> float | None:
        r, k = self.reward_bp, self.risk_bp
        return None if r is None or not k else r / k

    @property
    def levels_coherent(self) -> bool:
        """Target and stop on opposite sides of entry -- anything else is a
        typo in the answer, and the ticket says so rather than drawing it."""
        e, t, s = self.entry_bp, self.target_bp, self.stop_bp
        if e is None or t is None or s is None:
            return True
        return (t - e) * (s - e) < 0


@dataclass(frozen=True)
class Source:
    title: str
    url: str


@dataclass(frozen=True)
class Recs:
    as_of: date
    regime: str = ""
    desk_view: str = ""
    recs: tuple[Rec, ...] = ()
    sources: tuple[Source, ...] = ()
    error: str | None = None
    dropped: tuple[str, ...] = ()       # why a recommendation was not shown

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.recs)

    def for_horizon(self, key: str) -> Rec | None:
        return next((r for r in self.recs if r.horizon == key), None)


# -------------------------------------------------------------------- parse

def _is_url(s: str) -> bool:
    return isinstance(s, str) and s.strip().lower().startswith(("http://", "https://"))


def source_tag(source: str) -> str:
    """'Pricing' for an app citation, the domain for a URL, '' for nothing."""
    s = (source or "").strip()
    if not s:
        return ""
    if s.lower().startswith("app:"):
        return s[4:].strip() or "app"
    if _is_url(s):
        host = urlparse(s).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    return s[:40]


def _text(v: Any, limit: int = 600) -> str:
    if not isinstance(v, str):
        return ""
    return " ".join(v.split()).strip()[:limit]


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        out = float(v)
    elif isinstance(v, str):
        m = re.search(r"[+-]?\d+(?:\.\d+)?", v.replace(",", ""))
        if not m:
            return None
        out = float(m.group())
    else:
        return None
    return out if math.isfinite(out) else None


def _date(v: Any) -> date | None:
    if not isinstance(v, str):
        return None
    m = re.search(r"\d{4}-\d{2}-\d{2}", v)
    try:
        return date.fromisoformat(m.group()) if m else None
    except ValueError:
        return None


def _points(rows: Any, text_key: str, figure_key: str | None = None) -> tuple[Point, ...]:
    """Sourced reasons only: a reason without a source is an opinion the
    ticket cannot trace, and a quantitative one without a figure is not one."""
    if not isinstance(rows, list):
        return ()
    out: list[Point] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text, source = _text(row.get(text_key)), _text(row.get("source"), 300)
        figure = _text(row.get(figure_key), 60) if figure_key else ""
        if not text or not source or (figure_key and not figure):
            continue
        out.append(Point(text, source, figure))
    return tuple(out[:MAX_REASONS])


def _rec(item: dict) -> tuple[Rec | None, str]:
    horizon = _text(item.get("horizon")).lower()
    if horizon not in HORIZONS:
        return None, f"a recommendation with an unknown horizon ({horizon or 'blank'})"
    label = HORIZONS[horizon][0].lower()
    expression = _text(item.get("expression"), 160)
    entry, target, stop = (_text(item.get(k), 120) for k in ("entry", "target", "stop"))
    fundamental = _points(item.get("fundamental"), "point")
    quantitative = _points(item.get("quantitative"), "reading", "figure")
    if not expression or not (entry and target and stop):
        return None, f"the {label} trade came back without levels"
    if not fundamental or not quantitative:
        return None, f"the {label} trade came back without sourced reasons of both kinds"
    direction = _text(item.get("direction")).lower()
    conviction = _num(item.get("conviction"))
    return Rec(
        horizon=horizon,
        title=_text(item.get("title"), 90) or expression,
        expression=expression,
        instrument=_text(item.get("instrument"), 120),
        direction=direction if direction in DIRECTIONS else "other",
        entry=entry, target=target, stop=stop,
        entry_bp=_num(item.get("entry_bp")),
        target_bp=_num(item.get("target_bp")),
        stop_bp=_num(item.get("stop_bp")),
        conviction=int(min(max(round(conviction or 1), 1), 5)),
        sizing=_text(item.get("sizing"), 160),
        exit_by=_date(item.get("exit_by")),
        catalyst=_text(item.get("catalyst"), 200),
        thesis=_text(item.get("thesis"), 900),
        fundamental=fundamental,
        quantitative=quantitative,
        risks=_points(item.get("risks"), "risk"),
        wrong_if=_text(item.get("wrong_if"), 300),
    ), ""


def parse(raw: str, as_of: date) -> Recs:
    """The agent's answer, read defensively. Never raises."""
    payload = extract_json(raw or "")
    if payload is None:
        return Recs(as_of, error="The strategist's reply held no JSON object.")
    recs: dict[str, Rec] = {}
    dropped: list[str] = []
    for item in payload.get("recommendations") or []:
        if not isinstance(item, dict):
            continue
        rec, why = _rec(item)
        if rec is None:
            dropped.append(why)
        elif rec.horizon in recs:
            dropped.append(f"a second {rec.horizon_label.lower()} trade (the first is kept)")
        else:
            recs[rec.horizon] = rec
    sources = tuple(Source(_text(s.get("title"), 160) or source_tag(_text(s.get("url"), 400)),
                           _text(s.get("url"), 400))
                    for s in payload.get("sources") or []
                    if isinstance(s, dict) and _is_url(_text(s.get("url"), 400)))
    ordered = tuple(recs[k] for k in HORIZON_ORDER if k in recs)
    return Recs(as_of, regime=_text(payload.get("regime"), 120),
                desk_view=_text(payload.get("desk_view"), 1600),
                recs=ordered, sources=sources,
                error=None if ordered else "No usable recommendation came back.",
                dropped=tuple(dropped))


# ------------------------------------------------------------ persistence

def _point_dict(p: Point) -> dict:
    return {"text": p.text, "source": p.source, "figure": p.figure}


def to_dict(r: Recs) -> dict:
    return {
        "as_of": r.as_of.isoformat(), "regime": r.regime, "desk_view": r.desk_view,
        "error": r.error, "dropped": list(r.dropped),
        "sources": [{"title": s.title, "url": s.url} for s in r.sources],
        "recs": [{
            "horizon": x.horizon, "title": x.title, "expression": x.expression,
            "instrument": x.instrument, "direction": x.direction,
            "entry": x.entry, "target": x.target, "stop": x.stop,
            "entry_bp": x.entry_bp, "target_bp": x.target_bp, "stop_bp": x.stop_bp,
            "conviction": x.conviction, "sizing": x.sizing,
            "exit_by": x.exit_by.isoformat() if x.exit_by else None,
            "catalyst": x.catalyst, "thesis": x.thesis,
            "fundamental": [_point_dict(p) for p in x.fundamental],
            "quantitative": [_point_dict(p) for p in x.quantitative],
            "risks": [_point_dict(p) for p in x.risks],
            "wrong_if": x.wrong_if,
        } for x in r.recs],
    }


def _points_back(rows: Any) -> tuple[Point, ...]:
    return tuple(Point(_text(p.get("text")), _text(p.get("source"), 300), _text(p.get("figure"), 60))
                 for p in rows or [] if isinstance(p, dict))


def from_dict(d: dict) -> Recs:
    """A saved run, tolerant of a hand-edited or older-shape file."""
    recs = []
    for x in d.get("recs") or []:
        if not isinstance(x, dict) or x.get("horizon") not in HORIZONS:
            continue
        recs.append(Rec(
            horizon=x["horizon"], title=_text(x.get("title"), 90),
            expression=_text(x.get("expression"), 160), instrument=_text(x.get("instrument"), 120),
            direction=x.get("direction") if x.get("direction") in DIRECTIONS else "other",
            entry=_text(x.get("entry"), 120), target=_text(x.get("target"), 120),
            stop=_text(x.get("stop"), 120),
            entry_bp=_num(x.get("entry_bp")), target_bp=_num(x.get("target_bp")),
            stop_bp=_num(x.get("stop_bp")),
            conviction=int(min(max(_num(x.get("conviction")) or 1, 1), 5)),
            sizing=_text(x.get("sizing"), 160), exit_by=_date(x.get("exit_by")),
            catalyst=_text(x.get("catalyst"), 200), thesis=_text(x.get("thesis"), 900),
            fundamental=_points_back(x.get("fundamental")),
            quantitative=_points_back(x.get("quantitative")),
            risks=_points_back(x.get("risks")), wrong_if=_text(x.get("wrong_if"), 300)))
    order = {k: i for i, k in enumerate(HORIZON_ORDER)}
    recs.sort(key=lambda r: order[r.horizon])
    return Recs(_date(d.get("as_of")) or date.min, regime=_text(d.get("regime"), 120),
                desk_view=_text(d.get("desk_view"), 1600), recs=tuple(recs),
                sources=tuple(Source(_text(s.get("title"), 160), _text(s.get("url"), 400))
                              for s in d.get("sources") or [] if isinstance(s, dict)),
                error=d.get("error"), dropped=tuple(d.get("dropped") or ()))
