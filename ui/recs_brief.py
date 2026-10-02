"""The desk brief for Trade recs: every tab's live numbers, as plain text.

The US module, on the Australian tabs. Each section reads the same sources and
core functions its tab draws from, so the strategist sees the numbers on
screen -- not a second computation of them. A section that fails says so in
one line and the brief goes on without it.

Everything is capped at the as-of date, like the tabs themselves.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

from core import (bank_calls, econ_calendar, growth as growth_mod, labor, macro as macro_mod,
                  release_prep, smp as smp_mod, speeches, trade_recs)
from data import asx
from state import store
from ui import sources as src

#: How far ahead the calendar section reads: past the medium horizon's end.
CALENDAR_DAYS = trade_recs.MEDIUM_DAYS[1] + 5


@dataclass
class Brief:
    as_of: date
    text: str
    spans: list[trade_recs.Horizon]
    sections: list[tuple[str, bool]] = field(default_factory=list)   # (title, loaded)

    @property
    def missing(self) -> list[str]:
        return [t for t, ok in self.sections if not ok]


def _ordinal(n: float) -> str:
    k = int(round(n))
    tail = "th" if 10 <= k % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(k % 10, "th")
    return f"{k}{tail}"


def _age(stamp: object, as_of: date) -> str:
    """'9 days old' for a saved run, so the strategist knows to look for newer."""
    try:
        d = date.fromisoformat(str(stamp)[:10])
    except ValueError:
        return "age unknown"
    days = (as_of - d).days
    return "today" if days <= 0 else f"{days} day{'s' if days != 1 else ''} old"


def _bp(x: float | None, d: int = 1) -> str:
    return "n/a" if x is None or not math.isfinite(x) else f"{x:+.{d}f}bp"


def _capped(obs, as_of: date) -> list:
    return [o for o in obs if o.date <= as_of]


# ------------------------------------------------------------------ sections

def _desk(ctx) -> list[str]:
    M = ctx.M
    opens, closes = M.blackout
    out = [f"As of: {ctx.as_of:%a %d %b %Y} (Sydney); the desk's meeting in focus is "
           f"{M.meeting.label}, {M.days_to_meeting} days to the decision (2:30pm Sydney)"
           f"{' — a Statement on Monetary Policy meeting' if M.meeting.has_smp else ''}.",
           f"Blackout {opens:%d %b}–{closes:%d %b}{' — in blackout now' if M.in_blackout else ''}."]
    if M.priced and M.summary is not None:
        s = M.summary
        out.append(f"The desk's current position (challenge it if the evidence disagrees): "
                   f"{M.trade_side} a {M.move_size:g}bp {M.direction} priced at "
                   f"{M.move_points:g}bp on {M.contract_label}; market-implied {s.implied:.0%}, "
                   f"desk q {s.q:.1%}, EV {s.ev:+.2f}bp.")
        rung = M.sizing_ladder.selected if M.sizing_ladder and M.sizing_ladder.ok else None
        if rung:
            out.append(f"Desk sizing: {rung.label} Kelly = {rung.contracts} lots, "
                       f"A${rung.position_dv01:,.0f}/bp, max loss A${rung.max_loss:,.0f} "
                       f"against an A${M.daily_limit:,.0f} daily limit.")
    note = (ctx.S.get("path") or {}).get("note")
    if note:
        out.append(f"Desk note: {note.strip()}")
    return out


def _pricing(ctx) -> list[str]:
    mk, sp = ctx.market, ctx.path
    live = [q for q in mk.ib_live if q.ok]
    if not live:
        raise RuntimeError("no IB quotes")
    curve_asof = asx.latest_asof(live)
    spot = ctx.spot
    out = [f"Spot cash rate {spot:.2f}% (RBA target / AONIA)." if spot is not None
           else "Spot cash rate unavailable.",
           f"ASX 30-day interbank (IB) settlements as of {curve_asof:%d %b %Y}."
           if curve_asof else "ASX 30-day interbank (IB) strip."]
    if sp is not None and sp.ok:
        out.append("Meeting ladder (step = the change priced at that meeting, cum = since spot; "
                   "P = step / move size; ! = contract not settling):")
        for s in sp.steps:
            out.append(f"  {s.label}: {s.code}{' !' if not s.reliable else ''} step "
                       f"{_bp(s.step_bp)} cum {_bp(s.cum_bp)} P {s.prob:.0%} → "
                       f"{s.r_after:.3f}%")
        if sp.terminal is not None:
            pk, tr = sp.peak, sp.trough
            out.append(f"Terminal {sp.terminal:.3f}% ({_bp(sp.total_bp, 0)} total)"
                       + (f"; peak {_bp(pk.cum_bp, 0)} at {pk.label}" if pk else "")
                       + (f"; trough {_bp(tr.cum_bp, 0)} at {tr.label}" if tr else "") + ".")
    out.append("IB strip (contract: price, implied cash rate): "
               + "; ".join(f"{q.code} {q.price:.3f} ({q.implied_rate:.3f}%)" for q in live[:14]))
    ir = [q for q in mk.ir_live if q.ok]
    if ir:
        out.append("90-day bank bill (IR) strip: " + "; ".join(
            f"{q.code} {q.price:.3f} ({q.implied_rate:.3f}%)" for q in ir[:8]))
    bbsw = src.capped(src.bbsw_history(), ctx.as_of)
    if bbsw:
        out.append(f"3-month BBSW {bbsw[-1][1]:.3f}% ({bbsw[-1][0]:%d %b}).")
    return out


def _vol(ctx) -> list[str]:
    M = ctx.M
    if M.quiet_sigma_bp is None:
        raise RuntimeError("no settlement history for the sized contract")
    out = [f"Quiet-day volatility on {M.contract_label}: {M.quiet_sigma_bp:.2f}bp/day."]
    mult = sorted(M.vol_multipliers.items(), key=lambda kv: -kv[1].applied)
    if mult:
        out.append("Release-day variance vs a quiet day (applied, shrunk by sample size): "
                   + "; ".join(f"{n} {m.applied:.2f}x (n={m.n_event})" for n, m in mult[:8]))
    return out


def _calendar(ctx, spans: list[trade_recs.Horizon]) -> list[str]:
    end = ctx.as_of + timedelta(days=CALENDAR_DAYS)
    events = econ_calendar.merged_calendar(ctx.as_of, end, [], ctx.meetings)
    cards = store.load_release_cards()
    out = ["Horizons: " + "; ".join(f"{h.label} → {h.ends:%a %d %b} ({h.anchor})" for h in spans)]
    out.append("Scheduled ABS releases and RBA events, tier 1 and 2 (1 = market-moving):")
    seen = set()
    for e in events:
        if e.weight > 2 or (e.when, e.label) in seen:
            continue
        seen.add((e.when, e.label))
        out.append(f"  {e.when:%a %d %b}: {e.label} (tier {e.weight})")
    meetings = [m for m in ctx.meetings if ctx.as_of <= m.end <= end]
    out.append("RBA decisions in the window: "
               + ", ".join(f"{m.label}{' (SMP)' if m.has_smp else ''}" for m in meetings))
    for key, card in sorted(cards.items()):
        when = key.split("|")[0]
        if when < ctx.as_of.isoformat():
            continue
        subs = card.get("sub_releases") or {}
        typed = [f"{k}: expect {v.get('expectation')}, dovish {v.get('dovish')}, "
                 f"hawkish {v.get('hawkish')}" for k, v in subs.items()
                 if isinstance(v, dict) and v.get("expectation") is not None]
        if typed:
            out.append(f"Desk reaction card {key}: " + "; ".join(typed))
    return out


def _board(ctx) -> list[str]:
    M = ctx.M
    a, d = M.arithmetic, M.decomposition
    out = [f"Monetary Policy Board: {a.total_voters} members, all vote; a {M.direction} needs "
           f"{a.votes_needed}; {a.mover_count} move candidates. Blocs: "
           + "; ".join(f"{b.bloc} {b.count} (mean hawk score {b.mean_score:.1f})" for b in a.blocs),
           f"Desk read: P(bloc votes for the move) {d.p_bloc:.0%} × P(centre joins) "
           f"{d.p_centre:.0%} = {d.q:.1%}.",
           "Roster (hawk-dove score 0 dove – 5 hawk; M = move candidate; the RBA never "
           "attributes a vote, so this is the desk's read):"]
    for v in sorted(M.roster, key=lambda v: -v.score):
        out.append(f"  {v.name} ({v.role}, {v.bloc}) {v.score:.1f}{' M' if v.is_mover else ''}")
    votes = [x for x in src.fetch_board_votes(ctx.as_of) if x.votes][-6:]
    if votes:
        out.append("Published vote splits, latest six decisions: " + "; ".join(
            f"{x.when:%b %y} {x.action} to {x.level:.2f}% ({x.split}"
            + (f", {x.dissents} {x.dissent_side}" if x.dissents and x.dissent_side else "") + ")"
            for x in votes))
    run = next((h for h in store.load_speech_history() if h.get("roundup")), None)
    if run:
        roundup = speeches.roundup_from_dict(run["roundup"])
        out.append(f"RBA speakers {roundup.start:%d %b}–{roundup.end:%d %b} (desk's last "
                   f"speech roundup, {_age(run.get('stamp'), ctx.as_of)} -- search for anything "
                   f"said since):")
        for ap in sorted(roundup.appearances, key=lambda x: x.when or date.min, reverse=True)[:12]:
            out.append(f"  {ap.when:%d %b} {ap.member} — {ap.lean}, policy {ap.rate_stance}; "
                       f"inflation: {ap.inflation[:140]}; labour: {ap.labour[:120]}"
                       if ap.when else f"  {ap.member} — {ap.lean}")
    return out


def _smp(ctx) -> list[str]:
    released = tuple(((m.end.year, m.end.month), m.end) for m in ctx.meetings if m.has_smp)
    rounds = src.fetch_smp_rounds(ctx.as_of, released)
    if not rounds:
        raise RuntimeError("no Statement forecast round")
    table = smp_mod.build(rounds[0], rounds[1] if len(rounds) > 1 else None)
    if not table.ok:
        raise RuntimeError("the forecast table did not parse")
    out = [f"Statement on Monetary Policy forecasts ({table.latest}), change vs "
           f"{table.prior or 'the prior round'} in brackets; columns {', '.join(table.columns)}:"]
    for row in table.rows:
        out.append(f"  {row.metric.label}: " + ", ".join(
            ("—" if c.value is None else f"{c.value:.1f}")
            + (f" ({c.change:+.1f})" if c.change else "") for c in row.cells))
    return out


def _calls(ctx) -> list[str]:
    run = next(iter(h for h in store.load_call_history()
                    if not (h.get("book") or {}).get("error")), None)
    if run is None:
        return ["No saved sell-side call book; search the web for the latest RBA calls "
                "(CBA, Westpac, NAB, ANZ, Macquarie and the global houses)."]
    book = bank_calls.book_from_dict(run["book"])
    tally = bank_calls.tally(book)
    out = [f"Desk's last street-call search, {_age(run.get('stamp'), ctx.as_of)} "
           f"(for the {run.get('meeting', '')} meeting -- search for newer notes): "
           + ", ".join(f"{k} {v}" for k, v in tally.items())
           + f"; consensus {bank_calls.consensus_call(book)}; median net path "
           f"6m {bank_calls.median_path(book, '6m')} moves, 12m "
           f"{bank_calls.median_path(book, '12m')} moves."]
    for c in book.calls:
        out.append(f"  {c.bank}: next {c.next_call}; 6m {c.path_6m}, 12m {c.path_12m}"
                   + (f", end rate {c.end_rate:.2f}%" if c.end_rate is not None else "")
                   + (f"; changed from {c.prev_call}" if c.move else "")
                   + (f" (note {c.as_of:%d %b})" if c.as_of else ""))
    return out


def _macro(ctx) -> list[str]:
    bundle = {k: src.capped(v, ctx.as_of) for k, v in src.fetch_macro_bundle().items()}
    readings = macro_mod.dashboard(bundle)
    groups = macro_mod.groups(readings)
    total = macro_mod.total(readings)
    out = [f"Momentum z-scores vs 2000–2020 (+ = stronger economy): total {total:+.2f}; "
           + "; ".join(f"{g.group} {g.z:+.2f}" for g in groups if g.z is not None)]
    for r in readings:
        if r.ok:
            out.append(f"  {r.indicator.label}: {r.last:,.2f} ({r.indicator.transform_label}, "
                       f"{r.as_of:%b %Y}{'' if r.live else ', slide reading'}), z {r.z:+.2f}")
    return out


def _labour(ctx) -> list[str]:
    live = labor.as_published(src.fetch_labor_series(), ctx.as_of, {})
    month = labor.complete_month(live, labor.CHANGE_MONTHS)
    readings = labor.dashboard(labor.through_month(live, month), change_months=labor.CHANGE_MONTHS)
    s = labor.summarise(readings)
    out = [f"Slack dashboard ({s['n']} indicators, data through {month:%b %Y}, percentile vs "
           f"{labor.BASE_START:%Y}–now, higher = tighter): median {_ordinal(s['median_pct'])}; "
           f"over {labor.CHANGE_MONTHS} months {s['tightening']} tightening, {s['cooling']} "
           f"cooling, {s['flat']} little changed."]
    for r in sorted((r for r in readings if r.ok), key=lambda r: r.current_pct)[:6]:
        out.append(f"  loosest: {r.indicator.label} {_ordinal(r.current_pct)} ({r.travel})")
    return out


def _inflation(ctx) -> list[str]:
    cpi = src.cpi_series()
    out = []
    for key, label in (("trimmed_q", "Trimmed mean CPI, q/q"),
                       ("trimmed_yr", "Trimmed mean CPI, year-ended"),
                       ("headline_q", "Headline CPI, q/q"),
                       ("monthly_trimmed_yr", "Monthly CPI trimmed mean, year-ended"),
                       ("monthly_ex_volatiles_yr", "Monthly CPI ex volatiles, year-ended")):
        rows = [(d, v) for d, v in (cpi.get(key) or []) if d <= ctx.as_of]
        if rows:
            d, v = rows[-1]
            out.append(f"{label}: {v:.2f}% ({d:%b %Y})")
    g3 = src.rba_table("g3")
    for code, label in (("GCONEXP", "Consumer inflation expectations (Melbourne Institute)"),
                        ("GMAREXPY", "Market economists' expectations, 1 year"),
                        ("GMAREXPYY", "Market economists' expectations, 2 years")):
        rows = [(d, v) for d, v in (g3.get(code) or []) if d <= ctx.as_of]
        if rows:
            out.append(f"{label}: {rows[-1][1]:.1f}% ({rows[-1][0]:%b %Y})")
    wpi = [(d, v) for d, v in (src.rba_table("h4").get("GWPIQP") or []) if d <= ctx.as_of]
    if wpi:
        out.append(f"Wage Price Index: {wpi[-1][1]:+.2f}% q/q ({wpi[-1][0]:%b %Y})")
    if not out:
        raise RuntimeError("no CPI series")
    return out


def _growth(ctx) -> list[str]:
    hours, h1 = src.fetch_growth_inputs()
    series = growth_mod.build(_capped(hours, ctx.as_of),
                              {k: _capped(v, ctx.as_of) for k, v in h1.items()})
    live = [s for s in series if s.ok]
    out = [f"{r['indicator']}: {r['value']:+.2f}% y/y ({r['date']:%d %b %Y})"
           for r in growth_mod.latest_readings(live)]
    gap = growth_mod.gap(live)
    if gap is not None:
        out.append(f"Hours-worked tracker less GDP: {gap:+.2f}pp.")
    if not out:
        raise RuntimeError("no growth series")
    return out


def _analysis(ctx) -> list[str]:
    from ui.tabs import analysis

    res = analysis.result(ctx.as_of)
    if not res.ok:
        raise RuntimeError(res.message)
    L = res.live
    out = [f"Next unemployment print ({L['month']:%B %Y}, out {L['release']:%a %d %b}) — the "
           f"desk's out-of-sample model: last {L['prior_u']:.2f}% unrounded (printed "
           f"{L['prior_print']:.1f}%), expected change {L['forecast']:+.2f}pp."
           if L.get("month") and L.get("release") else "Next unemployment print model:"]
    if "likely_print" in L:
        out.append(f"Most likely print {L['likely_print']:.1f}% ({L['likely_p']:.0%}); "
                   f"P(higher) {L['p_up']:.0%}, P(unchanged) {L['p_flat']:.0%}, "
                   f"P(lower) {L['p_down']:.0%}.")
    street, _ = analysis.street_reads(res)
    if street:
        out.append(f"Street consensus for that print: {street['consensus']:.1f}%.")
    return out


SECTIONS: tuple[tuple[str, Callable | None], ...] = (
    ("Desk", _desk), ("Pricing", _pricing), ("Event volatility", _vol),
    ("Calendar", None), ("Board & RBA speak", _board), ("SMP & forecasts", _smp),
    ("Street calls", _calls), ("Macro", _macro), ("Labour", _labour),
    ("Inflation", _inflation), ("Growth", _growth), ("Analysis", _analysis),
)


# ---------------------------------------------------------------- horizons

def spans(ctx) -> list[trade_recs.Horizon]:
    """The three horizons with real dates: the next tier-1 day, the next
    decision, and 90 days out."""
    end = ctx.as_of + timedelta(days=CALENDAR_DAYS)
    events = econ_calendar.merged_calendar(ctx.as_of, end, [], ctx.meetings)
    days = release_prep.upcoming_tier1(events, ctx.as_of)
    first = min(days, key=lambda d: d.when) if days else None
    if first is None:
        tier1 = sorted((e for e in events if e.weight == 1), key=lambda e: e.when)
        ev_day, ev_name = (tier1[0].when, tier1[0].label) if tier1 else (None, "")
    else:
        same = sorted({d.release for d in days if d.when == first.when})
        ev_day, ev_name = first.when, " & ".join(same)
    nxt = next((m for m in sorted(ctx.meetings, key=lambda m: m.end) if m.end >= ctx.as_of), None)
    return trade_recs.horizons(ctx.as_of, ev_day, ev_name,
                               nxt.end if nxt else None, nxt.label if nxt else "")


def build(ctx, on_section: Callable[[str], None] | None = None) -> Brief:
    """The whole brief. Never raises: a failed section is one line saying so."""
    horizon_list = spans(ctx)
    parts, loaded = [], []
    for title, fn in SECTIONS:
        if on_section:
            on_section(title)
        try:
            lines = _calendar(ctx, horizon_list) if fn is None else fn(ctx)
            ok = True
        except Exception as exc:                            # noqa: BLE001
            lines, ok = [f"(unavailable: {type(exc).__name__}: {exc})"[:200]], False
        loaded.append((title, ok))
        parts.append(f"## {title}\n" + "\n".join(lines))
    return Brief(ctx.as_of, "\n\n".join(parts), horizon_list, loaded)
