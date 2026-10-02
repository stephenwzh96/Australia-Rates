"""The verdict rail, the export buttons and the header tape."""

from __future__ import annotations

import html
from pathlib import Path

import streamlit as st

from core import clock, econ_calendar, rba_calendar, sizing
from export import report
from state import store
from ui import components as C
from ui.context import Ctx
from ui.theme import pnl_colour

#: Short release names so a "What to watch" line never wraps.
SHORT_RELEASE = {
    econ_calendar.LABOUR: "Labour Force",
    econ_calendar.CPI_Q: "CPI (quarterly)",
    econ_calendar.CPI_M: "Monthly CPI",
    econ_calendar.WPI: "WPI",
    econ_calendar.GDP: "GDP",
    econ_calendar.DECISION: "RBA decision",
    econ_calendar.SMP: "Decision + SMP",
    econ_calendar.MINUTES: "Minutes",
}


def trade_name(M) -> tuple[str, str]:
    title = f"{M.meeting.end:%b %Y}"
    if not M.priced:
        return title, "not priced yet"
    code = (M.contract_label or "").split(" — ")[0].strip()
    bits = [f"{M.side} {M.size:g}bp {M.direction}", f"{M.points:g}bp priced"]
    if code and code.lower() != "custom dv01":
        bits.append(code)
    return title, " · ".join(bits)


def _sec(label: str) -> None:
    st.markdown(f'<div class="fomc-verdict-sec">{label}</div>', unsafe_allow_html=True)


def _rows(rows: list[str]) -> None:
    st.markdown("".join(rows), unsafe_allow_html=True)


def render(ctx: Ctx, root: Path) -> None:
    M, P = ctx.M, ctx.P
    with st.container(border=True, key="verdict"):
        title, qualifier = trade_name(M)
        C.sub(title, qualifier)
        if not M.priced:
            st.caption("Enter the points priced to produce a verdict.")
        else:
            _trade(M, P)
            _view(M, P)
            _size(M, P)
            _watch(ctx, P)
        for w in M.warnings:
            st.warning(w)

    if M.priced:
        md = report.render(M)
        stamp = clock.now().strftime("%Y%m%dT%H%M")
        e1, e2 = st.columns(2)
        e1.download_button("Export Markdown", md, file_name=f"{ctx.key}-{stamp}.md",
                           mime="text/markdown", width="stretch")
        if e2.button("Save to runs/", width="stretch"):
            path = store.save_run(ctx.key, md, stamp)
            st.success(f"Wrote {path.relative_to(root)}")


def _trade(M, P) -> None:
    _sec("The trade")
    leg = "Receive" if (M.side == "fade") == (M.direction == "hike") else "Pay"
    to_decision = (f"{M.days_to_meeting}d" if M.days_to_meeting > 0
                   else "today" if M.days_to_meeting == 0 else f"decided {M.meeting.end:%d %b}")
    rows = [C.verdict_row("Leg", f"{leg} — {M.side} {M.size:g}bp {M.direction}", P.accent, emph=True),
            C.verdict_row("To the decision", to_decision + (" · in blackout" if M.in_blackout else ""))]
    by_label = {x.label: x for x in M.exit_levels}
    for label, name in (("Target -- reassess", "Take profit"), ("Target -- partial", "Partial TP"),
                        ("Stop -- reassess", "Stop loss"), ("Stop -- hard floor", "Hard floor")):
        x = by_label.get(label)
        if x is None:
            continue
        value = f"{x.level:g}bp · {x.implied:.0%} · {x.mtm:+.0f}bp"
        if label == "Target -- partial" and M.stop:
            value = f"{M.stop.partial_share:.0%} off at {x.level:g}bp · {x.mtm:+.0f}bp"
        rows.append(C.verdict_row(name, value, pnl_colour(x.mtm, P)))
    _rows(rows)


def _view(M, P) -> None:
    s = M.summary
    _sec("The view and the edge")
    cond = M.conditional_ev_at
    _rows([
        C.verdict_row("Market-implied probability", f"{s.implied:.0%}"),
        C.verdict_row("Your probability", f"{s.q:.1%}", P.accent),
        C.verdict_row("Margin of safety", f"{s.margin * 100:.1f}pp"),
        C.verdict_row("Expected value", f"{s.ev:+.2f}bp", pnl_colour(s.ev, P), emph=True),
        C.verdict_row("Max gain / max loss", f"{s.gain:+g} / {s.loss:+g}  (1 : {s.rr:.1f})"),
        C.verdict_row("EV if the bloc votes yes",
                      f"{cond:+.2f}bp" if cond is not None and M.decomposition.p_centre > 0
                      else "—", pnl_colour(cond or 0, P)),
    ])


def _size(M, P) -> None:
    _sec("The size")
    rung = M.sizing_ladder.selected if M.sizing_ladder and M.sizing_ladder.ok else None
    if not rung:
        _rows([C.verdict_row(f"{sizing.fraction_label(M.kelly_fraction)} Kelly",
                             "no edge — do not size")])
        return
    util = (f"{rung.max_loss / M.daily_limit:.0%} of A${M.daily_limit:,.0f}"
            if M.daily_limit > 0 else "no cap")
    _rows([
        C.verdict_row("Instrument", f"{M.contract_label} (A${M.dv01:,.2f}/bp)"
                      + (f" ← A${M.quoted_dv01:,.2f} × {M.capture:.0%}" if M.capture < 1 - 1e-9 else ""),
                      P.accent),
        C.verdict_row(f"{rung.label} Kelly size",
                      f"{rung.contracts:,} lots (A${rung.position_dv01:,.0f}/bp)", P.accent, emph=True),
        C.verdict_row("Max gain / max loss (A$)", f"+{rung.max_gain:,.0f} / -{rung.max_loss:,.0f}"),
        C.verdict_row("Expected P&L (A$)", f"{rung.expected_pnl:+,.0f}", pnl_colour(rung.expected_pnl, P)),
        C.verdict_row("Growth captured", f"{rung.growth_share:.0%} of full ({rung.annualised:.0%} a.r.)"),
        C.verdict_row("Daily limit", f"{rung.status} · {util}",
                      P.good if rung.within_daily_limit else P.critical,
                      emph=not rung.within_daily_limit),
    ])


def _watch(ctx: Ctx, P) -> None:
    M = ctx.M
    _sec("What to watch")
    a, d = M.arithmetic, M.decomposition
    ahead = [e for e in M.calendar_events if M.as_of <= e.when <= M.meeting.end]
    tier1 = sorted((e for e in ahead if e.weight == 1), key=lambda e: e.when)
    tier2 = [e for e in ahead if e.weight == 2]
    rows = [C.verdict_row("Vote count", "roster not set" if a.total_voters == 0
                          else f"{a.mover_count}/{a.total_voters} movers · need {a.votes_needed}")]
    if a.blocs:
        rows.append(C.verdict_row("Blocs", " · ".join(f"{b.bloc} {b.count}" for b in a.blocs)))
    rows.append(C.verdict_row("P(bloc) × P(centre)",
                              f"{d.p_bloc:.0%} × {d.p_centre:.0%} = {d.q:.1%}", P.accent))
    if d.override_q is not None and abs(d.override_q - d.q) > 0.0005:
        rows.append(C.verdict_row("Recorded q", f"{d.override_q:.1%}", P.accent, emph=True))
    rows.append(C.verdict_row("Decision", f"{M.meeting.end:%a %d %b} · {M.direction} "
                              f"{M.size:g}bp" + (" · SMP" if M.meeting.has_smp else ""),
                              P.accent, emph=True))
    if M.days_to_meeting >= 0:
        rows.append(C.verdict_row("Tier 1 before the decision",
                                  f"{len(tier1)} · next {tier1[0].when:%d %b}" if tier1 else "none"))
        if tier2:
            rows.append(C.verdict_row("Tier 2 before the decision", f"{len(tier2)}"))
    rows.append(C.verdict_row("Blackout", "live — no Board speakers" if M.in_blackout
                              else f"opens {M.blackout[0]:%d %b}"))
    _rows(rows)
    seen = set()
    for e in tier1:
        if (e.when, e.release or e.label) in seen:
            continue
        seen.add((e.when, e.release or e.label))
        name = SHORT_RELEASE.get(e.release, e.release or e.label)
        _rows([f'<div class="fomc-verdict-row"><span class="k">{e.when:%a %d %b} · '
               f'{html.escape(str(name))}</span><span class="v"></span></div>'])


def header(ctx: Ctx) -> None:
    """The tape across the top of every tab.

    Left: which meeting, how long until it, and the facts that change how the
    days before it trade (the as-of, the blackout, whether a Statement on
    Monetary Policy comes with it). Right: the trade's own figures, so the
    price, the two probabilities, the edge and the size are in view on every
    tab, not only beside the ones that compute them."""
    M, P = ctx.M, ctx.P
    opens, closes = M.blackout
    behind = rba_calendar.as_of_note(ctx.as_of, ctx.today)
    days = M.days_to_meeting
    countdown = f"{days}d" if days > 0 else "today" if days == 0 else "decided"
    urgent = 0 <= days <= 7
    facts = [(f"as of {ctx.as_of:%d %b %Y}" + (f" · {behind}" if behind else ""),
              P.warning if behind else None)]
    if not M.meeting.verified:
        facts.append(("date unverified", P.warning))
    facts.append((f"blackout {opens:%d %b}–{closes:%d %b}" + (" · live" if M.in_blackout else ""),
                  P.serious if M.in_blackout else None))
    facts.append(("SMP meeting" if M.meeting.has_smp else "no SMP", None))

    figures: list[tuple[str, str, str | None]] = []
    cash = ctx.market.spot.get("cash")
    if cash is not None:
        figures.append(("cash", f"{cash:.2f}%", None))
    if M.priced:
        s = M.summary
        figures += [("priced", f"{M.points:g}bp", None),
                    ("mkt", f"{s.implied:.0%}", None),
                    ("you", f"{s.q:.1%}", P.accent),
                    ("EV", f"{s.ev:+.2f}bp", pnl_colour(s.ev, P))]
        rung = M.sizing_ladder.selected if M.sizing_ladder and M.sizing_ladder.ok else None
        if rung:
            figures.append(("size", f"{rung.contracts:,} lots", None))
            figures.append(("risk", f"-A${rung.max_loss:,.0f}",
                            None if rung.within_daily_limit else P.critical))
    else:
        figures.append(("trade", "not priced", P.muted))
    C.tape(f"{M.meeting.end:%B %Y} RBA", countdown + (" to decision" if days > 0 else ""),
           P.warning if urgent else None, facts, figures)
