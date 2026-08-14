"""Markdown export.

Structured like the source framework document -- Steps 1-8, Part II, then the
Verdict -- so every meeting produces the artifact you already work from.
"""

from __future__ import annotations

from datetime import datetime

from core import contracts, sizing
from core.model import Model

MOVE = {"hike": "hike", "cut": "cut"}


def _bp(x: float | None, places: int = 2) -> str:
    return "--" if x is None else f"{x:+.{places}f}bp"


def _pct(x: float | None, places: int = 1) -> str:
    return "--" if x is None else f"{x * 100:.{places}f}%"


def near_leg_pnl(token: str, m: Model) -> str:
    """Resolve the scenario table's `+points` / `max loss` tokens to real bp.

    Kept as tokens in the saved state so a scenario row stays correct after the
    entry level changes.
    """
    if not m.priced:
        return token
    return {"+points": f"{m.summary.gain:+g}bp",
            "max loss": f"{m.summary.loss:+g}bp"}.get(token.strip(), token)


def render(m: Model) -> str:
    if not m.priced:
        return (f"# {m.label} RBA -- not yet priced\n\n"
                f"No points priced recorded for the {m.meeting.label} meeting.\n")

    s = m.summary
    move = MOVE.get(m.direction, m.direction)
    verb = "fade" if m.side == "fade" else "back"
    L = [
        f"# Framework: Assessing an Event-Priced {'Receive' if m.side == 'fade' else 'Pay'} Trade",
        "",
        f"{m.label} RBA Monetary Policy Board, {m.meeting.label}. Prepared {m.as_of:%d %b %Y}"
        f" ({m.days_to_meeting} days to the decision).",
        f"Instrument: **{m.instrument or 'unspecified'}**. "
        f"You {verb} a {m.size:g}bp {move} priced at {m.points:g} points.",
        "",
    ]

    if m.warnings:
        L += ["> **Flags**", ""] + [f"> - {w}" for w in m.warnings] + [""]

    L += [
        "---", "",
        "## Step 1 -- Convert points priced to implied probability", "",
        "```",
        "Implied probability  =  Points priced  /  Size of the move",
        f"                     =  {m.points:g}  /  {m.size:g}",
        f"                     =  {_pct(s.implied)}",
        "```", "",
        f"Check it the other way: {_pct(s.implied)} x {m.size:g} = {s.implied * m.size:.2f}bp. "
        f"Market is at {m.points:g}bp.", "",
        "---", "",
        "## Step 2 -- Define the two terminal outcomes", "",
        "| Outcome | Contract settles at | Your P&L |",
        "|---|---|---|",
    ]
    for o in m.outcomes:
        L.append(f"| **{o.label}** | {o.settles_at:g}bp | **{o.pnl:+g}bp** |")
    L += [
        "",
        f"Max loss is capped at {abs(s.loss):g} because the RBA cannot move more than "
        f"{m.size:g}bp at this meeting.", "",
        "---", "",
        "## Step 3 -- Expected value", "",
        "```",
        f"EV  =  Points {'received' if m.side == 'fade' else 'paid'}  -  (Size x Your probability)",
        f"    =  {m.points:g} - {m.size:g}({s.q:.4g})",
        f"    =  {s.ev:+.2f}bp",
        "```", "",
        "### Breakeven", "",
        "```",
        f"q  =  {m.points:g} / {m.size:g}  =  {_pct(s.breakeven)}",
        "```", "",
        "Breakeven probability equals market-implied probability. Always -- that is the",
        "definition of fair value. The only source of edge is your probability estimate",
        "differing from the market's.", "",
        "### Edge, stated directly", "",
        "```",
        f"Edge  =  (Market probability - Your probability) x Size",
        f"      =  ({_pct(s.implied)} - {_pct(s.q)}) x {m.size:g}",
        f"      =  {s.ev_via_edge:+.2f}bp",
        "```", "",
        f"Which matches EV = {s.ev:+.2f}. Two routes, same answer -- that is your arithmetic check.",
        "",
        "---", "",
        "## Step 4 -- Sensitivity to your probability estimate", "",
        "| Your q | EV (bp) | |",
        "|---|---|---|",
    ]
    for r in m.sensitivity:
        tag = "**breakeven -- market fair value**" if r.is_breakeven else (
            "<-- your estimate" if abs(r.q - s.q) < 1e-9 else "")
        L.append(f"| {_pct(r.q)} | **{r.ev:+.2f}** | {tag} |")
    L += [
        "",
        f"You have {s.margin * 100:.1f} percentage points of room between your estimate "
        f"({_pct(s.q)}) and breakeven ({_pct(s.breakeven)}).", "",
        "---", "",
        f"## Step 5 -- Sizing (Kelly at {m.kelly_fraction:g}x)", "",
        "```",
        f"b  =  W / L  =  {s.gain:g} / {abs(s.loss):g}  =  {s.gain / abs(s.loss):.4f}",
        "f* =  (1-q) - q x (L / W)",
        "```", "",
        f"| Your q | Full Kelly | {m.kelly_fraction:g}x Kelly (practical) |",
        "|---|---|---|",
    ]
    for k in m.kelly_rows:
        L.append(f"| {_pct(k.q)} | {k.full:.1%} | **{k.fractional:.1%}** |")
    L += [
        "",
        "Do not use full Kelly. It assumes log utility, a repeatable independent bet and no",
        "mark-to-market constraint -- none of which describe a rates book facing a single",
        "event with a risk limit and a P&L stop.", "",
    ]

    lad = m.sizing_ladder
    if lad is not None and lad.ok:
        spec = sizing.contract_for(m.contract_key, m.as_of)
        L += [
            "### In DV01, lots and dollars", "",
            f"Kelly bankroll is the A${m.max_drawdown:,.0f} max drawdown; the "
            f"A${m.daily_limit:,.0f} daily limit vetoes rather than sizes. "
            f"**{spec.label}** at A${m.dv01:,.2f} per bp per lot"
            + (f" (A${m.quoted_dv01:,.2f} quoted, corrected for {m.capture:.0%} capture "
               f"of the move)" if m.capture < 1.0 - 1e-9 else "")
            + f" (A${lad.loss_per_contract:,.2f} lost per lot in the bad state, so the daily "
            f"limit allows at most {lad.daily_cap_contracts:,} lots). "
            f"Full Kelly is {lad.f_star:.0%} of the bankroll"
            + (f"; growth reaches zero at {lad.zero_growth_f:.0%}."
               if lad.zero_growth_f else "."),
            "",
            "| Kelly | f | Max loss | DV01 risk/bp | Lots | Expected A$ | Max gain | "
            "g/bet | % of full | Annualised | Daily | Growth / A$10k |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in lad.rungs:
            mark = " **<-**" if r.is_selected else ""
            L.append(
                f"| {r.label}{mark} | {r.f:.1%} | A${r.max_loss:,.0f} "
                f"| A${r.position_dv01:,.0f} | {r.contracts:,} | A${r.expected_pnl:,.0f} "
                f"| A${r.max_gain:,.0f} | {r.growth * 100:.2f}% | {r.growth_share:.1%} "
                f"| {r.annualised:.1%} | {'pass' if r.within_daily_limit else '**BREACH**'} "
                f"| {'--' if r.growth_per_10k is None else f'{r.growth_per_10k * 100:.1f}pp'} |")
        L += ["",
              "f is a share of the bankroll, so it multiplies dollars, never DV01: the",
              "dollars risked over the bp at risk gives the position's DV01, and that over",
              "the contract's DV01 gives the lots. A breaching rung reports its true size",
              "rather than being shrunk to fit -- the limit is a veto, not a sizing input.", ""]
        if lad.breaches:
            names = ", ".join(r.label for r in lad.breaches)
            L += [f"> **Breaches the A${m.daily_limit:,.0f} daily limit: {names}.** "
                  + (f"Largest rung that fits: {lad.largest_within_limit.label}."
                     if lad.largest_within_limit else ""), ""]

    L += [
        "---", "",
        "## Step 6 -- Path risk", "",
        "| Contract level | Implied probability | Your MTM |",
        "|---|---|---|",
    ]
    for r in m.ladder:
        L.append(f"| {r.level:g}bp{' (entry)' if r.is_entry else ''} | {_pct(r.implied, 0)} "
                 f"| {r.mtm:+g}bp |")

    if m.exit_levels:
        has_price = any(x.price is not None for x in m.exit_levels)
        has_pnl = any(x.pnl is not None for x in m.exit_levels)
        # Priced and Price sit together (level, then the instrument quote it
        # implies) ahead of the two P&L columns -- matches the on-screen table.
        head = "| Exit | Implied | Priced |"
        rule = "|---|---|---|"
        if has_price:
            head, rule = head + " Price |", rule + "---|"
        head, rule = head + " P&L |", rule + "---|"
        if has_pnl:
            head, rule = head + " P&L (A$) |", rule + "---|"
        L += ["", "### Exit map", "", head, rule]
        for x in m.exit_levels:
            cells = [x.label, _pct(x.implied, 0), f"{x.level:g}bp"]
            if has_price:
                cells.append("--" if x.price is None else f"{x.price:.4f}")
            cells.append("--" if x.is_entry else f"{x.mtm:+g}bp")
            if has_pnl:
                cells.append("--" if x.pnl is None or x.is_entry else f"{x.pnl:+,.0f}")
            L.append("| " + " | ".join(cells) + " |")
        L += ["", "A market bound is not an order you place: fading the event, the contract "
              "runs out of room at 0bp priced and at the full move, so those two are the "
              "terminal max gain and max loss. Only the reassess levels are choices.", ""]

    if m.stop:
        st = m.stop
        title = f"### Stop at {st.stop_level:g}bp ({st.stop_mtm:+g}bp MTM)"
        if st.has_target:
            title += f", target at {st.target_level:g}bp ({st.target_mtm:+g}bp MTM)"
        L += [
            "", title, "",
            f"| Path | Count | P&L each | Total |", "|---|---|---|---|",
        ]
        for p_ in st.paths:
            L.append(f"| {p_.label} | {p_.count:g} | {p_.pnl_each:+g} | {p_.total:+g} |")
        L += [
            f"| | | | **{sum(x.total for x in st.paths):+g}** |", "",
            "```",
            f"EV with exits      = {st.ev_with_stop:+.2f}bp",
            f"EV riding on       = {st.ev_without_stop:+.2f}bp",
            f"Cost of the exits  = {st.cost_of_stop:+.2f}bp  (~{st.cost_as_share_of_edge:.0%} of your edge)",
            f"  stop's effect    = {st.stop_effect:+.2f}bp",
            f"  target's effect  = {st.target_effect:+.2f}bp",
            "```", "",
        ]
        if not st.counts_consistent:
            if not st.counts_sum_correctly:
                L.append(f"> Your six counts sum to {st.counts_total:g}, not {st.trials:g} -- "
                         "EV above is still the correct weighted average of what you entered, "
                         "but the split isn't the out-of-100 read the labels promise.")
            L.append(f"> Counts imply P(move) = {st.implied_p_move:.0%}, not q = {_pct(s.q, 0)}.")
            L.append("")
        L.append("Because you win most of the time, false triggers dominate the arithmetic. "
                 "Size to sit through the drawdown rather than defending a larger position with a stop.")
    if m.state.get("path", {}).get("note"):
        L += ["", m.state["path"]["note"]]

    L += ["", "---", "", "## Step 7 -- Kill criteria", "",
          "| Criterion | Threshold | Current | Status |", "|---|---|---|---|"]
    for c in m.kill.criteria:
        thr = "manual" if c.threshold is None else f"{c.comparator} {c.threshold:g}"
        cur = "--" if c.current is None else f"{c.current:.2f}"
        L.append(f"| {c.label} | {thr} | {cur} | {'**TRIGGERED**' if c.triggered else c.status} |")
    L += ["", f"Any two together and the estimate is stale. "
          f"Currently {m.kill.count} live." + (" **STALE.**" if m.kill.is_stale else ""), ""]

    a = m.arithmetic
    L += [
        "---", "",
        f"## Step 8 -- Scenario: the bloc votes for the {move}", "",
        f"A rate decision needs **{a.votes_needed} of {a.total_voters}**. "
        f"The move candidates counted in P(bloc votes for the move) number {a.mover_count} "
        f"({', '.join(a.mover_names) if a.mover_names else 'none marked'}), so it must recruit "
        f"**{a.shortfall}** more from the remaining {a.recruitable}.", "",
        "A bloc of dissenters is not a decision: the trade is short the *policy outcome*, "
        "not the *vote split*.", "",
        "| Bloc | Voters | Count | Mean score |", "|---|---|---|---|",
    ]
    for b in a.blocs:
        L.append(f"| {b.bloc} | {', '.join(b.members)} | {b.count} | {b.mean_score:.1f} |")

    d = m.decomposition
    L += [
        "", "### Decomposition", "",
        "```",
        f"q  =  P(bloc moves) x P(centre joins | bloc moves)",
        f"   =  {_pct(d.p_bloc, 0)} x {_pct(d.p_centre, 0)}",
        f"   =  {_pct(d.q)}",
        "```", "",
        "Useful because it separates the observable from the decisive. The bloc's behaviour",
        "is broadly readable from speeches -- though the RBA never attributes a vote, so this",
        "is your read and cannot be scored against a published tally. The centre settles the",
        "contract.", "",
        "### Conditional EV -- the bloc already voted yes", "",
        "| P(centre joins) | EV given the bloc already voted yes |", "|---|---|",
    ]
    for r in m.conditional:
        tag = " -- breakeven" if r.is_breakeven else ""
        L.append(f"| {_pct(r.p_centre, 0)} | **{r.ev:+.2f}bp**{tag} |")
    L += ["", f"Breakeven is again {_pct(s.breakeven)} -- a property of the payoff, invariant "
          "to the scenario.", ""]
    # ------------------------------------------------------------- Part II
    struct = m.state.get("structure", {})
    L += ["---", "", "# Part II -- What Part I Doesn't Cover", "",
          "## 1. Does the trade match the view?", ""]
    L.append(struct.get("spread_note") or
             "An outright expresses only the near-meeting half of a two-meeting view.")
    far = struct.get("far_leg_points")
    if m.next_meeting and far is not None:
        L += ["", f"Far leg: {m.next_meeting.label} at {float(far):g} points "
              f"({_pct(float(far) / (float(struct.get('far_leg_size') or m.size)))} implied).", ""]

    L += ["", "## 2. Contract mechanics", "",
          f"Decision {m.meeting.end:%d %b %Y}, effective "
          f"{contracts.effective_date(m.meeting.end):%d %b %Y}.", "",
          "| Contract | Days at new rate | Captures |", "|---|---|---|"]
    for c in m.captures:
        L.append(f"| **{c.code}** ({c.month_label}) | {c.days_at_new_rate} of {c.days_in_month} "
                 f"| {c.days_at_new_rate}/{c.days_in_month} x {m.size:g} = **{c.captured_bp:.2f}bp** |")
    if m.near_trap:
        near = m.captures[0]
        L += ["", f"> **The near contract is a trap.** {near.code} captures only "
              f"{near.share:.0%} of the intended risk. A late-month meeting is expressed in "
              f"the following contract, {m.captures[1].code}.", ""]
    L += ["", "**Instrument ranking:**", ""]
    for i in contracts.INSTRUMENT_RANKING:
        L.append(f"{i.rank}. **{i.name}** -- {i.verdict}. {i.caveat}")

    opens, closes = m.blackout
    L += ["", "## 3. Blackout", "",
          f"Blackout runs {opens:%d %b} to {closes:%d %b}. "
          + ("**You are inside it** -- no further RBA signal is available before settlement; "
             "the remaining channel is a press trial balloon."
             if m.in_blackout else
             f"It opens in {(opens - m.as_of).days} days."), ""]

    cal = struct.get("calendar") or []
    if cal:
        L += ["## 4. Pre-decision calendar", "", "| Date | Release | Tier |", "|---|---|---|"]
        for row in cal:
            L.append(f"| {row.get('date', '')} | {row.get('release', '')} | {row.get('tier', '')} |")
        if struct.get("calendar_note"):
            L += ["", struct["calendar_note"]]
        L.append("")

    L += ["## 5. The statement, not the decision", "",
          f"{'A Statement on Monetary Policy lands with this decision' if m.meeting.has_smp else 'No SMP at this meeting'}"
          f" -- {'a full forecast round can anchor or disrupt the strip' if m.meeting.has_smp else 'so the statement wording and the press conference do all the work'}.",
          ""]
    scen = struct.get("scenarios") or []
    if scen:
        L += ["| Outcome | Near leg | Strip reaction |", "|---|---|---|"]
        for row in scen:
            L.append(f"| {row.get('outcome', '')} | {near_leg_pnl(row.get('near_leg', ''), m)} "
                     f"| {row.get('strip', '')} |")
        L += ["", f"The near leg pays {s.gain:+g} in every no-move scenario and cannot distinguish "
              "between them. A spread can.", ""]

    check = struct.get("checklist") or []
    if check:
        L += ["## 6-8. Housekeeping", ""]
        for c in check:
            mark = "x" if c.get("done") else " "
            L.append(f"- [{mark}] **{c.get('item', '')}**" + (f" -- {c['note']}" if c.get("note") else ""))
        L.append("")

    L += ["", "---", "",
          f"*Generated {datetime.now():%Y-%m-%d %H:%M} from the RBA event-pricing "
          "dashboard.*", ""]
    return "\n".join(L)
