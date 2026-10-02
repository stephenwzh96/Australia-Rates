"""The pre-release reaction card -- a playbook, written before the number lands.

The FOMC version's card, on the Australian calendar. Two things differ:

* `core.econ_calendar.Event` here carries the report (`release`) but no FRED
  series key, so the numbers a card prices come from the report itself
  (`RELEASE_SPECS`) rather than from the calendar lines.
* Australian CPI is quarterly with a monthly series beside it, and the
  quarterly trimmed mean is the number the Board targets -- so the CPI card
  prices the trimmed mean q/q, and the monthly card the trimmed mean y/y the
  RBA quotes. The second tier is carded too (`CARD_TIERS`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Iterable, Sequence

from . import sizing
from .contracts import CAPTURE_WARNING_THRESHOLD
from .econ_calendar import CPI_M, CPI_Q, GDP, LABOUR, WPI, Event
from .pricing import Direction, Side, implied_probability

SCENARIOS: tuple[str, ...] = ("dovish", "base", "hawkish")

SCENARIO_SIGN: dict[str, int] = {"dovish": -1, "base": 0, "hawkish": +1}

DEFAULT_MIN_EDGE_BP = 1.5

ACTION_REC = "rec"
ACTION_PAY = "pay"
ACTION_STAND = "stand down"
ACTION_UNSET = "no move set"


@dataclass(frozen=True)
class SubRelease:
    """One number inside a report, and how to read it."""
    series_key: str
    label: str
    basis: str                  # "change" | "level" | "q/q" | "y/y" | "m/m"
    unit: str                   # "k" | "%"
    decimals: int
    hawkish_sign: int           # +1 = a higher print is hawkish; -1 = inverted

    @property
    def inverted(self) -> bool:
        return self.hawkish_sign < 0

    @property
    def polarity_note(self) -> str:
        """Spelled out on screen, every time, for every series -- not just the
        inverted one. A note that appears only on the exception is a note you
        stop reading."""
        return "lower print = hawkish" if self.inverted else "higher print = hawkish"

    def fmt(self, value: float | None) -> str:
        if value is None:
            return "—"
        if self.unit == "k":
            return f"{value:+,.1f}k"
        return f"{value:.{self.decimals}f}%"


SPECS: dict[str, SubRelease] = {
    "LF_EMP": SubRelease("LF_EMP", "Employment change", "change", "k", 1, +1),
    "LF_UR": SubRelease("LF_UR", "Unemployment rate", "level", "%", 1, -1),
    "LF_PR": SubRelease("LF_PR", "Participation rate", "level", "%", 1, +1),
    "CPIQ_TM": SubRelease("CPIQ_TM", "Trimmed mean CPI", "q/q", "%", 1, +1),
    "CPIQ_HL": SubRelease("CPIQ_HL", "Headline CPI", "q/q", "%", 1, +1),
    "CPIQ_TM_YY": SubRelease("CPIQ_TM_YY", "Trimmed mean CPI, year-ended", "y/y", "%", 1, +1),
    "CPIM_TM": SubRelease("CPIM_TM", "Monthly trimmed mean", "y/y", "%", 1, +1),
    "CPIM_HL": SubRelease("CPIM_HL", "Monthly headline CPI", "y/y", "%", 1, +1),
    "WPI_QQ": SubRelease("WPI_QQ", "Wage Price Index", "q/q", "%", 1, +1),
    "WPI_YY": SubRelease("WPI_YY", "Wage Price Index, year-ended", "y/y", "%", 1, +1),
    "GDP_QQ": SubRelease("GDP_QQ", "Real GDP", "q/q", "%", 1, +1),
    "GDP_YY": SubRelease("GDP_YY", "Real GDP, year-ended", "y/y", "%", 1, +1),
}

#: The numbers each Australian report carries, in its own reading order.
RELEASE_SPECS: dict[str, tuple[str, ...]] = {
    LABOUR: ("LF_EMP", "LF_UR", "LF_PR"),
    CPI_Q: ("CPIQ_TM", "CPIQ_HL", "CPIQ_TM_YY"),
    CPI_M: ("CPIM_TM", "CPIM_HL"),
    WPI: ("WPI_QQ", "WPI_YY"),
    GDP: ("GDP_QQ", "GDP_YY"),
}

#: What drives the first thirty seconds. For the Labour Force it is the rate,
#: not the jobs number: the Board reads full employment off the rate, and the
#: employment change is the noisier of the two on the survey's rotation.
LEAD_SERIES: dict[str, str] = {
    LABOUR: "LF_UR",
    CPI_Q: "CPIQ_TM",
    CPI_M: "CPIM_TM",
    WPI: "WPI_QQ",
    GDP: "GDP_QQ",
}

#: Australian prep covers the second tier too: the monthly CPI, the WPI and the
#: National Accounts each move the IB strip, and a desk preps all of them.
CARD_TIERS = (1, 2)


def _f(x: Any) -> float | None:
    """A finite float, or None. Same tolerant coercion `core.model` uses on
    saved state -- a hand-edited card must not crash the page."""
    try:
        if x is None:
            return None
        out = float(x)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _q(x: Any) -> float | None:
    """A probability, or None. Anything outside [0, 1] is not a probability
    and is dropped rather than clamped: 1.4 is a typo, and clamping it to
    "certain" would size a position off a number the trader never meant."""
    out = _f(x)
    if out is None or not 0.0 <= out <= 1.0:
        return None
    return out


@dataclass(frozen=True)
class ReleaseDay:
    """One report on one date, with the sub-releases the card can price."""
    release: str
    when: date
    events: tuple[Event, ...]

    @property
    def key(self) -> str:
        """Stable storage key. Date first so the file sorts chronologically."""
        return f"{self.when.isoformat()}|{self.release}"

    @property
    def label(self) -> str:
        return f"{self.when:%a %d %b} — {self.release}"

    @property
    def specs(self) -> tuple[SubRelease, ...]:
        """Priceable sub-releases, in the report's own reading order."""
        return tuple(SPECS[k] for k in RELEASE_SPECS.get(self.release, ()))

    @property
    def skipped(self) -> tuple[str, ...]:
        """Lines in this report with no spec: none, since specs come from the report."""
        return ()

    @property
    def default_lead(self) -> str:
        specs = self.specs
        if not specs:
            return ""
        preferred = LEAD_SERIES.get(self.release)
        if preferred and any(s.series_key == preferred for s in specs):
            return preferred
        return specs[0].series_key

    def days_out(self, as_of: date) -> int:
        return (self.when - as_of).days


def upcoming_tier1(events: Iterable[Event], as_of: date) -> list[ReleaseDay]:
    """Every future report on a carded tier (`CARD_TIERS`) that this module can price."""
    groups: dict[tuple[date, str], list[Event]] = {}
    for e in events:
        if not e.release or e.when < as_of:
            continue
        groups.setdefault((e.when, e.release), []).append(e)

    out: list[ReleaseDay] = []
    for (when, release), rows in groups.items():
        if not any(e.weight in CARD_TIERS for e in rows):
            continue
        day = ReleaseDay(release, when, tuple(rows))
        if day.specs:
            out.append(day)
    out.sort(key=lambda d: (d.when, d.release))
    return out


def all_release_days(events: Iterable[Event]) -> list[ReleaseDay]:
    """Every report (past or future) on a carded tier this module can
    price. Unlike `upcoming_tier1`, nothing is filtered by `as_of` -- used to
    keep saved cards for releases that have already printed selectable and
    reviewable, instead of orphaning them behind the upcoming-only filter."""
    groups: dict[tuple[date, str], list[Event]] = {}
    for e in events:
        if not e.release:
            continue
        groups.setdefault((e.when, e.release), []).append(e)
    out: list[ReleaseDay] = []
    for (when, release), rows in groups.items():
        if not any(e.weight in CARD_TIERS for e in rows):
            continue
        day = ReleaseDay(release, when, tuple(rows))
        if day.specs:
            out.append(day)
    out.sort(key=lambda d: (d.when, d.release))
    return out


@dataclass(frozen=True)
class TradeContext:
    """Everything the playbook needs from the meeting, in one object."""
    points: float
    size: float
    q: float
    direction: Direction
    capture: float = 1.0
    dv01: float = 0.0
    max_drawdown: float = 0.0
    daily_limit: float = 0.0
    bets_per_year: int = 8
    kelly_fraction: float = 0.25
    anchor_price: float | None = None
    contract_label: str = ""
    meeting_label: str = ""
    market_level_bp: float | None = None
    side: str = "back"

    @property
    def direction_sign(self) -> int:
        """`points` is bp priced TOWARD `direction`. When the meeting is priced
        for a cut, a hawkish print takes points DOWN -- which is the whole job
        of this sign, and the single easiest thing to get backwards."""
        return 1 if self.direction == "hike" else -1

    @property
    def anchor(self) -> float:
        """The level scenarios reprice FROM: the live market when the strip
        can say it, else the meeting's own entry."""
        return self.market_level_bp if self.market_level_bp is not None else self.points

    @property
    def fair(self) -> float:
        """The level your own q says is fair, in bp priced."""
        return self.size * self.q


def context_from_model(M: Any,
                       market_level_bp: float | None = None) -> TradeContext | None:
    """Read the context off a built `core.model.Model`."""
    if M.points is None or M.summary is None:
        return None
    capture = float(M.capture or 1.0)
    usable = capture >= CAPTURE_WARNING_THRESHOLD
    return TradeContext(
        points=float(M.points),
        size=float(M.size),
        q=float(M.summary.q),
        direction=M.direction,
        capture=capture if usable else 1.0,
        dv01=float(M.dv01 or 0.0),
        max_drawdown=float(M.max_drawdown or 0.0),
        daily_limit=float(M.daily_limit or 0.0),
        bets_per_year=int(M.bets_per_year or 8),
        kelly_fraction=float(M.kelly_fraction or 0.25),
        anchor_price=_f((M.state.get("path") or {}).get("current_price")),
        contract_label=M.contract_label,
        meeting_label=M.label,
        market_level_bp=market_level_bp,
        side=str(M.side or "back"),
    )


@dataclass(frozen=True)
class ScenarioOutcome:
    """One scenario column, fully costed."""
    name: str
    move_bp: float | None       # magnitude entered; None = nothing entered yet
    new_points: float
    new_implied: float
    edge_bp: float
    best_side: Side
    action: str
    clamped: bool               # the raw level fell outside [0, size]
    delta_rate_bp: float        # contract rate move, capture applied
    delta_price: float
    price: float | None         # absolute level, only when an anchor exists
    lots: int
    breach: bool                # sized past the daily limit
    pnl_bp: float = 0.0
    pnl_usd: float | None = None
    pay_level: float | None = None
    side: str = "back"
    q: float = 0.0
    fair_bp: float = 0.0
    own_edge_bp: float = 0.0

    @property
    def tradeable(self) -> bool:
        return self.action in (ACTION_REC, ACTION_PAY)


def action_for(best_side: Side, direction: Direction) -> str:
    """`rec` or `pay` for a side/direction pair."""
    return ACTION_REC if (best_side == "fade") == (direction == "hike") else ACTION_PAY


def _size_at(level: float, side: Side, ctx: TradeContext) -> tuple[int, bool]:
    """`(lots, breaches_daily_limit)` at the Kelly rung already selected, sized
    at the level the position is actually put on."""
    if ctx.dv01 <= 0 or ctx.max_drawdown <= 0:
        return 0, False
    ladder = sizing.build_ladder(
        level, ctx.size, ctx.q, side, ctx.max_drawdown, ctx.dv01,
        ctx.daily_limit, ctx.bets_per_year,
        selected=sizing.snap_fraction(ctx.kelly_fraction))
    rung = ladder.selected if ladder.ok else None
    if rung is None:
        return 0, False
    return rung.contracts, not rung.within_daily_limit


def resolve(scenario: str, move_bp: float | None, ctx: TradeContext,
            min_edge_bp: float = DEFAULT_MIN_EDGE_BP,
            pay_level_bp: float | None = None,
            receive_level_bp: float | None = None,
            owned_side: Side | None = None) -> ScenarioOutcome:
    """Price one scenario column."""
    sign = SCENARIO_SIGN.get(scenario, 0)
    entered = _f(move_bp)
    anchor = ctx.anchor
    base = pay_level_bp if pay_level_bp is not None else anchor
    rec_level = receive_level_bp if receive_level_bp is not None else anchor

    if sign != 0 and entered is None:
        return ScenarioOutcome(
            name=scenario, move_bp=None, new_points=anchor,
            new_implied=implied_probability(anchor, ctx.size),
            edge_bp=0.0, best_side="fade", action=ACTION_UNSET, clamped=False,
            delta_rate_bp=0.0, delta_price=0.0, price=ctx.anchor_price,
            lots=0, breach=False, pay_level=base, side="back",
            q=ctx.q, fair_bp=ctx.fair)

    move = abs(entered or 0.0)
    raw = anchor + move * sign * ctx.direction_sign
    new_points = min(max(raw, 0.0), ctx.size)
    clamped = abs(raw - new_points) > 1e-9

    gap = new_points - ctx.fair
    edge_bp = abs(gap)
    best_side: Side = "fade" if gap > 0 else "back"
    action = ACTION_STAND if edge_bp < min_edge_bp else action_for(best_side, ctx.direction)

    delta_rate_bp = (new_points - anchor) * ctx.capture * ctx.direction_sign
    delta_price = -delta_rate_bp / 100.0
    price = None if ctx.anchor_price is None else ctx.anchor_price + delta_price

    if owned_side is not None:
        trade_level, trade_side = base, owned_side
    elif action == ACTION_REC and receive_level_bp is not None:
        trade_level, trade_side = rec_level, "fade"
    else:
        trade_level, trade_side = base, "back"
    pnl_bp = ((trade_level - new_points) if trade_side == "fade"
              else (new_points - trade_level))
    own_edge_bp = ((trade_level - ctx.fair) if trade_side == "fade"
                   else (ctx.fair - trade_level))

    lots, breach = 0, False
    if action in (ACTION_REC, ACTION_PAY):
        lots, breach = _size_at(trade_level, trade_side, ctx)
    pnl_usd = pnl_bp * lots * ctx.dv01 if lots else None

    return ScenarioOutcome(
        name=scenario, move_bp=move if sign != 0 else 0.0,
        new_points=new_points,
        new_implied=implied_probability(new_points, ctx.size),
        edge_bp=edge_bp, best_side=best_side, action=action, clamped=clamped,
        delta_rate_bp=delta_rate_bp, delta_price=delta_price, price=price,
        lots=lots, breach=breach, pnl_bp=pnl_bp, pnl_usd=pnl_usd,
        pay_level=trade_level, side=trade_side,
        q=ctx.q, fair_bp=ctx.fair, own_edge_bp=own_edge_bp)


def scenario_context(scenario: str, card: dict, ctx: TradeContext
                     ) -> TradeContext:
    """`ctx` carrying THIS scenario's q."""
    q = scenario_q(scenario, card)
    return ctx if q is None else replace(ctx, q=q)


def scenario_q(scenario: str, card: dict) -> float | None:
    """The card's own q for one scenario, or None for "unchanged"."""
    if scenario == "base":
        return None
    stored = (card.get("scenario_q") or {}).get(scenario)
    return _q(stored)


def playbook(card: dict, ctx: TradeContext) -> list[ScenarioOutcome]:
    """All three scenario columns, dovish first."""
    moves = card.get("moves_bp") or {}
    min_edge = _f(card.get("min_edge_bp"))
    if min_edge is None or min_edge < 0:
        min_edge = DEFAULT_MIN_EDGE_BP
    pay_level = _f(card.get("pay_level_bp"))
    rec_level = _f(card.get("receive_level_bp"))
    owned_side: Side | None = None
    if card.get("mode") != "flat":
        rec_level = None
        owned_side = "fade" if str(ctx.side) == "fade" else "back"
    return [resolve(name, moves.get(name), scenario_context(name, card, ctx),
                    min_edge,
                    pay_level_bp=pay_level, receive_level_bp=rec_level,
                    owned_side=owned_side)
            for name in SCENARIOS]


def sigma_hint(multiplier: Any, quiet_sigma_bp: float | None,
               capture: float) -> float | None:
    """This release's own measured day size, in bp OF THE EVENT."""
    if multiplier is None or quiet_sigma_bp is None or capture <= 0:
        return None
    if not getattr(multiplier, "usable", False):
        return None
    return quiet_sigma_bp * math.sqrt(multiplier.applied) / capture


def blank_card(day: ReleaseDay) -> dict:
    """A fresh card for one report. Numbers start empty, never at zero."""
    return {
        "release": day.release,
        "when": day.when.isoformat(),
        "sub_releases": {
            s.series_key: {k: None for k in ("expectation", *SCENARIOS)}
            for s in day.specs
        },
        "moves_bp": {"dovish": None, "base": 0.0, "hawkish": None},
        "scenario_q": {"dovish": None, "base": None, "hawkish": None},
        "pay_level_bp": None,
        "receive_level_bp": None,
        "mode": "flat",
        "lots_owned": 0,
        "lead": day.default_lead,
        "tiebreak": "",
        "min_edge_bp": DEFAULT_MIN_EDGE_BP,
    }


def normalise(card: dict | None, day: ReleaseDay) -> dict:
    """A saved card reconciled against the report as the calendar now has it."""
    base = blank_card(day)
    if not isinstance(card, dict):
        return base
    saved = card.get("sub_releases")
    if not isinstance(saved, dict):
        saved = {}
    for key, row in base["sub_releases"].items():
        prior = saved.get(key)
        if isinstance(prior, dict):
            for field in row:
                row[field] = _f(prior.get(field))
    moves = card.get("moves_bp") or {}
    base["moves_bp"]["dovish"] = _f(moves.get("dovish"))
    base["moves_bp"]["hawkish"] = _f(moves.get("hawkish"))
    saved_q = card.get("scenario_q") or {}
    base["scenario_q"]["dovish"] = _q(saved_q.get("dovish"))
    base["scenario_q"]["hawkish"] = _q(saved_q.get("hawkish"))
    base["pay_level_bp"] = _f(card.get("pay_level_bp"))
    base["receive_level_bp"] = _f(card.get("receive_level_bp"))
    base["mode"] = card.get("mode") if card.get("mode") in ("position", "flat") else "flat"
    try:
        base["lots_owned"] = max(0, int(float(card.get("lots_owned") or 0)))
    except (TypeError, ValueError):
        base["lots_owned"] = 0
    lead = card.get("lead")
    if any(s.series_key == lead for s in day.specs):
        base["lead"] = lead
    base["tiebreak"] = str(card.get("tiebreak") or "")
    min_edge = _f(card.get("min_edge_bp"))
    if min_edge is not None and min_edge >= 0:
        base["min_edge_bp"] = min_edge
    if isinstance(card.get("research"), dict):
        base["research"] = card["research"]
    return base


def order_problems(day: ReleaseDay, card: dict) -> list[str]:
    """Scenario numbers sitting on the wrong side of base, in each series' own
    polarity."""
    rows = card.get("sub_releases") or {}
    out: list[str] = []
    for spec in day.specs:
        row = rows.get(spec.series_key) or {}
        base = _f(row.get("base"))
        if base is None:
            continue
        for name, want in (("hawkish", +1), ("dovish", -1)):
            value = _f(row.get(name))
            if value is None or value == base:
                continue
            if (value - base) * spec.hawkish_sign * want < 0:
                side = "dovish" if want > 0 else "hawkish"
                out.append(
                    f"{spec.label}: the {name} number ({spec.fmt(value)}) is on "
                    f"the {side} side of base ({spec.fmt(base)}) — "
                    f"{spec.polarity_note}."
                )
    return out


def q_problems(card: dict, ctx: TradeContext) -> list[str]:
    """Scenario q's that move your view the wrong way for the print."""
    base = ctx.q
    out: list[str] = []
    for name, want in (("hawkish", +1), ("dovish", -1)):
        q = scenario_q(name, card)
        if q is None or q == base:
            continue
        if (q - base) * want * ctx.direction_sign < 0:
            expect = "more" if want * ctx.direction_sign > 0 else "less"
            out.append(
                f"The {name} q ({q:.0%}) sits the wrong side of your base q "
                f"({base:.0%}): a {name} print makes a {ctx.direction} {expect} "
                f"likely, so its q should be "
                f"{'higher' if expect == 'more' else 'lower'}, not "
                f"{'higher' if q > base else 'lower'}."
            )
    return out


def surprise_side(spec: SubRelease, value: float | None,
                  expectation: float | None) -> str | None:
    """`"hawkish"`, `"dovish"`, or None for in-line / not entered."""
    v, e = _f(value), _f(expectation)
    if v is None or e is None:
        return None
    gap = (v - e) * spec.hawkish_sign
    if gap == 0:
        return None
    return "hawkish" if gap > 0 else "dovish"


def trigger_lines(day: ReleaseDay, card: dict,
                  outcomes: Sequence[ScenarioOutcome]) -> list[str]:
    """The two lines you actually read at 8:30, and nothing else."""
    lead = card.get("lead")
    spec = next((s for s in day.specs if s.series_key == lead), None)
    row = (card.get("sub_releases") or {}).get(lead) or {}
    out: list[str] = []
    for outcome in outcomes:
        if outcome.name == "base" or not outcome.tradeable:
            continue
        number = _f(row.get(outcome.name))
        threshold = ("" if spec is None or number is None
                     else f"{spec.label} {'≥' if _hits_at_or_above(spec, outcome.name) else '≤'} "
                          f"{spec.fmt(number)}")
        word = "pay" if outcome.side == "back" else "receive"
        where = (f" at {outcome.pay_level:.1f}bp"
                 if outcome.pay_level is not None else "")
        pnl_bp = outcome.pnl_bp or 0.0  # normalise -0.0 for the +.1f display
        line = (f"{threshold or outcome.name.title()} → market "
                f"{outcome.new_points:.1f}bp · your {word}{where} "
                f"{pnl_bp:+.1f}bp")
        if outcome.pnl_usd is not None:
            line += f" ({outcome.pnl_usd:+,.0f})"
        if outcome.lots:
            line += f" · {outcome.lots} lots"
        out.append(line)
    return out


def _hits_at_or_above(spec: SubRelease, scenario: str) -> bool:
    """Whether this scenario is reached by printing AT OR ABOVE its number."""
    want = SCENARIO_SIGN.get(scenario, 0)
    return want * spec.hawkish_sign > 0
