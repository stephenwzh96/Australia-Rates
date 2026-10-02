"""The release-prep reaction card: a playbook written before the print.

The FOMC version's card on the Australian calendar: the three numbers per
report, the street's consensus (research agent), the move and q each scenario
implies, and the playbook costed at Kelly size on the sizing contract. The
market level is read off the IB strip, measured from the Curve tab's spot.
"""

from __future__ import annotations

from dataclasses import replace as dcreplace

import streamlit as st

from core import clock, release_prep, release_research, sizing
from data import agent as claude_agent
from state import store
from ui import charts
from ui import components as C
from ui import research
from ui import sources as src
from ui.context import Ctx
from ui.theme import pnl_colour


def prep_card(ctx: Ctx) -> None:
    M = ctx.M
    C.heading("The reaction card", "written before the print, not after it", help=(
        r"L' = \text{clamp}\big(L + |m| \cdot s_{\text{scen}} \cdot s_{\text{dir}},\ 0,\ S\big)"
        r"\qquad \text{edge} = \big|L' - S q\big|",
        "L is the market's priced level for this meeting, read off the strip. Each scenario "
        "moves it by the bp you enter (up for hawkish, down for dovish, flipped for a cut). "
        "The new level is measured against your own q; below the minimum edge a scenario "
        "reads stand down.",
        "L = market priced, S = move size, q = your probability, m = your bp move", "relprep"))

    saved = store.load_release_cards()
    days = release_prep.upcoming_tier1(M.calendar_events, M.as_of)
    have = {d.key for d in days}
    days += [d for d in release_prep.all_release_days(M.calendar_events)
             if d.key in saved and d.key not in have]
    days.sort(key=lambda d: (d.when < M.as_of, d.when, d.release))
    if not days:
        st.info("No carded release (Labour Force, CPI, WPI, GDP) falls between the as-of "
                "date and this meeting.")
        return

    market_bp = src.playbook_market_level(ctx)
    base_ctx = release_prep.context_from_model(M, market_level_bp=market_bp)
    st.session_state.setdefault("release_cards", dict(saved))
    labels = {d.key: d.label for d in days}
    with C.bar("release_pick"):
        top, chips_col = st.columns([0.85, 1.45], vertical_alignment="bottom")
    with top:
        chosen = st.selectbox("Release", [d.key for d in days], width=300,
                              format_func=lambda k: labels[k] + ("  · saved" if k in saved else ""),
                              help="Labour Force, CPI, WPI and GDP prints between the as-of date "
                                   "and this meeting, plus saved cards for releases that have "
                                   "printed.")
    day = next(d for d in days if d.key == chosen)
    cards = st.session_state["release_cards"]
    cards[day.key] = release_prep.normalise(cards.get(day.key), day)
    card = cards[day.key]
    opens, closes = M.blackout
    in_blackout = opens <= day.when <= closes
    with chips_col:
        C.chips([(f"{day.when:%a %d %b %Y}", None), (f"{day.days_out(M.as_of)}d out", None),
                 (f"{len(day.specs)} numbers", None)]
                + ([("in blackout", ctx.P.serious)] if in_blackout else []))
    if in_blackout:
        C.note("This print lands inside the blackout: no Board member can walk the reaction "
               "back.")
    if day.skipped:
        C.note("Not priced here (no input spec): " + ", ".join(day.skipped) + ".")
    if base_ctx is None:
        st.warning("Enter the points priced in the sidebar to cost these scenarios. The numbers "
                   "below still save without it.")

    _consensus(ctx, day, card)
    _scenarios(day, card)
    _moves(ctx, day, card, base_ctx)
    if base_ctx is None:
        st.info("Priced levels, actions and lots appear once the meeting has points priced.")
    else:
        _playbook(ctx, day, card, base_ctx, market_bp)
    _save_card(M, day, card)


def _consensus(ctx: Ctx, day, card: dict) -> None:
    P = ctx.P
    C.sub("What the street expects")
    with C.bar("consensus"):
        a1, a2, b, _ = st.columns([1, 1, 1, 1], vertical_alignment="bottom")
    backend, model = research.agent_picker((a1, a2))
    agent_ok, why = src.RESEARCH_AGENTS[backend].available()
    with b:
        go = st.button("Search consensus", width="content", disabled=not agent_ok,
                       key=f"rp_go:{day.key}")
    if not agent_ok:
        research.agent_blocked(backend, why)
    if go:
        st.session_state["rp_asked"] = (day.key, backend, model)

    result = (release_research.from_dict(card["research"])
              if isinstance(card.get("research"), dict) else None)
    if st.session_state.get("rp_asked") == (day.key, backend, model):
        with st.spinner(f"Searching consensus for {day.release} "
                        f"({src.RESEARCH_AGENT_LABELS[backend]} · {model})"):
            result = src.fetch_release_research(day.release, day.when,
                                                tuple(s.series_key for s in day.specs),
                                                backend, model)
        card["research"] = release_research.to_dict(result)

    if result is None:
        C.note("One live web search, cached an hour per release, agent and model. Every figure "
               "carries a source link; nothing lands in an input until you accept it.")
        return
    if not result.ok:
        if research.needs_sign_in(backend, result.error):
            research.agent_blocked(backend, claude_agent.SIGN_IN_MESSAGE)
        else:
            st.warning(result.error)
        return
    if result.empty:
        st.info("Nothing sourced came back — normal for a release more than a week out.")
        return

    rows, colours = [], {}
    for i, spec in enumerate(day.specs):
        e = result.for_series(spec.series_key)
        if e is None:
            rows.append([spec.label] + ["—"] * 5)
            colours.update({(i, c): P.muted for c in range(6)})
            continue
        rng = f"{spec.fmt(e.range_low)} – {spec.fmt(e.range_high)}" if e.has_range else "—"
        rng += f"  (n={e.forecasters})" if e.forecasters else ""
        above, below = e.surprise_run()
        rows.append([spec.label, spec.fmt(e.consensus), rng,
                     spec.fmt(e.prior) + (" (revisable)" if e.prior_revisable else ""),
                     f"{above}↑ / {below}↓ of {len(e.recent)}" if e.recent else "—",
                     f'<a href="{e.source}" target="_blank">source</a>'])
    C.table(["Sub-release", "Consensus", "Range", "Prior", "Last prints vs consensus", ""],
            rows, numeric=(1, 2, 3, 4), colours=colours)
    for f in result.swing_factors:
        st.markdown(f'<div class="fomc-note fomc-quote" style="border-left-color:{P.serious}">'
                    f'{f.text} <a href="{f.source}" target="_blank">source</a></div>',
                    unsafe_allow_html=True)
    if result.expectations and st.button("Use these as the expectation", key=f"rp_accept:{day.key}"):
        for spec in day.specs:
            e = result.for_series(spec.series_key)
            if e is None:
                continue
            st.session_state[f"rp:{day.key}:{spec.series_key}:expectation"] = e.consensus
            base_k = f"rp:{day.key}:{spec.series_key}:base"
            if st.session_state.get(base_k) is None:
                st.session_state[base_k] = e.consensus
        st.rerun()


def _keyed_input(wkey: str, seed, **kw):
    if wkey not in st.session_state:
        st.session_state[wkey] = seed
    return st.number_input(key=wkey, **kw)


def _scenarios(day, card: dict) -> None:
    C.sub("The three numbers")
    C.note("Expectation is the reference; base seeds from it and stays editable. Hawkish and "
           "dovish are ordered by rates impact, so for unemployment the hawkish box holds the "
           "lower number.")
    for spec in day.specs:
        row = card["sub_releases"][spec.series_key]
        lbl, *cols = st.columns([1.15, 1, 1, 1, 1], vertical_alignment="bottom")
        with lbl:
            st.markdown(f"**{spec.label}**")
            C.note(f"{spec.basis} · {spec.polarity_note}")
        step = 5.0 if spec.unit == "k" else 10.0 ** -spec.decimals
        fmt = "%.0f" if spec.unit == "k" else f"%.{spec.decimals}f"
        for col, field in zip(cols, ("expectation", "dovish", "base", "hawkish")):
            with col:
                row[field] = _keyed_input(f"rp:{day.key}:{spec.series_key}:{field}", row[field],
                                          label=field.title(), step=step, format=fmt,
                                          placeholder="—")
    for problem in release_prep.order_problems(day, card):
        st.warning(problem)


def _moves(ctx: Ctx, day, card: dict, base_ctx) -> None:
    M = ctx.M
    C.heading("What each one is worth", level="sub", help=(
        r"\sigma_{\text{event}} = \sigma_{\text{quiet}} \cdot \sqrt{k}\ /\ \text{capture}",
        "The hint is this release's own measured day size on the contract you are sized in. "
        "Below six observations no ratio is applied and no size is suggested.",
        "see core.volatility and core.release_prep.sigma_hint", "relsigma"))
    mult = M.vol_multipliers.get(day.release)
    # A rate-calibrated sigma is already in event terms: no capture division
    # (core.model calibrates on 3-month BBSW, not on a contract price).
    hint = release_prep.sigma_hint(mult, M.quiet_sigma_bp, 1.0)
    if hint is not None and mult is not None:
        C.note(f"{day.release} days measure {mult.applied:.2f}x a quiet day's variance on "
               f"3-month BBSW (n={mult.n_event}, 95% CI {mult.ci_low:.1f}–{mult.ci_high:.1f}"
               f"{'' if mult.significant else ', does not exclude no effect'}) — about "
               f"{hint:.1f}bp of event on the day.")
    elif mult is not None:
        C.note(f"{day.release} has only {mult.n_event} observations in the BBSW history — "
               "too few to suggest a size.")
    else:
        C.note("No measured day size for this release in the BBSW history.")

    panel_q = (base_ctx.q if base_ctx is not None
               else (M.summary.q if M.summary is not None else None))
    scen_q = card.setdefault("scenario_q", {"dovish": None, "base": None, "hawkish": None})
    mv1, mv2, mv3, mv4 = st.columns([1, 1, 1, 1.1], vertical_alignment="bottom")
    for col, name in ((mv1, "dovish"), (mv3, "hawkish")):
        with col:
            card["moves_bp"][name] = _keyed_input(
                f"rp:{day.key}:move:{name}", card["moves_bp"][name], label=f"{name.title()} move (bp)",
                min_value=0.0, step=0.5, format="%.1f", placeholder="—",
                help="Magnitude only; direction comes from the scenario and hike/cut pricing.")
    with mv2:
        st.number_input("Base move (bp)", value=0.0, format="%.1f", disabled=True,
                        help="Pinned at 0: in line means the market learned nothing.")
    with mv4:
        card["min_edge_bp"] = _keyed_input(
            f"rp:{day.key}:minedge", float(card["min_edge_bp"]), label="Min edge (bp)",
            min_value=0.0, step=0.5, format="%.1f",
            help="Below this edge a scenario reads stand down.")

    q1, q2, q3, q4 = st.columns([1, 1, 1, 1.1], vertical_alignment="bottom")
    for col, name in ((q1, "dovish"), (q3, "hawkish")):
        stored = scen_q.get(name)
        with col:
            typed = _keyed_input(
                f"rp:{day.key}:q:{name}", None if stored is None else round(stored * 100.0, 1),
                label=f"{name.title()} q (%)", min_value=0.0, max_value=100.0, step=1.0,
                format="%.1f", placeholder="—" if panel_q is None else f"{panel_q:.0%} unchanged",
                help="Your probability of the move if this prints. Blank = the print does not "
                     "move your view.")
            scen_q[name] = None if typed is None else float(typed) / 100.0
    with q2:
        st.number_input("Base q (%)", value=None if panel_q is None else round(panel_q * 100, 1),
                        format="%.1f", disabled=True, placeholder="—",
                        help="The sidebar's q: in line means nobody learned anything.")
    with q4:
        C.note("Each column sizes on its own q, so a print that moves you with the market "
               "shows the smaller position it deserves.")
    if base_ctx is not None:
        for problem in release_prep.q_problems(card, base_ctx):
            st.warning(problem)

    lead_keys = [s.series_key for s in day.specs]
    ld1, ld2 = st.columns([1, 2])
    with ld1:
        lkey = f"rp:{day.key}:lead"
        st.session_state.setdefault(lkey, card["lead"] if card["lead"] in lead_keys
                                    else (lead_keys[0] if lead_keys else None))
        card["lead"] = st.selectbox("Lead number", lead_keys, key=lkey,
                                    format_func=lambda k: release_prep.SPECS[k].label,
                                    help="What drives the first thirty seconds.")
    with ld2:
        tkey = f"rp:{day.key}:tiebreak"
        st.session_state.setdefault(tkey, card["tiebreak"])
        card["tiebreak"] = st.text_input(
            "Mixed-print rule", key=tkey,
            placeholder="e.g. the rate leads, but a participation-led rise fades by noon",
            help="The judgement for a hot headline with a soft detail — write it now.")


def _playbook(ctx: Ctx, day, card: dict, base_ctx, market_bp) -> None:
    M, P = ctx.M, ctx.P
    C.sub("The playbook")
    entry = float(M.points)
    mk = f"rp:{day.key}:mode"
    st.session_state.setdefault(mk, card["mode"])
    mode = C.choice(
        "Playbook mode", ["position", "flat"], key=mk, fallback=card["mode"],
        label_visibility="collapsed",
        format_func=lambda m: "Position (I own it)" if m == "position" else "Flat (looking to enter)",
        help="Position: P&L from your entry at the lots you own. Flat: P&L from a prospective "
             "entry, sized at Kelly.")
    card["mode"] = mode
    is_position = card["mode"] == "position"

    pk, rk, lk, mbk = (f"rp:{day.key}:{s}" for s in ("paylevel", "recvlevel", "lotsowned", "marketbp"))
    st.session_state.setdefault(pk, card["pay_level_bp"] if card["pay_level_bp"] is not None else entry)
    st.session_state.setdefault(mbk, market_bp if market_bp is not None else entry)
    if not is_position:
        # Blank unless the card set one: a blank receive level is the plain
        # single-leg read. A fixed 20bp default costed a receive leg nobody had
        # chosen -- in a 15bp market, a non-executable one with a large "edge".
        st.session_state.setdefault(rk, card.get("receive_level_bp"))
    else:
        st.session_state.setdefault(lk, card.get("lots_owned") or 0)

    i1, i2, i3 = st.columns(3, vertical_alignment="bottom")
    owned_lots: int | None = None
    with i1:
        st.number_input("Market priced (bp)", key=mbk, min_value=0.0, step=0.5, format="%.1f",
                        help="Defaults to the IB strip read; overwrite to reprice the whole "
                             "playbook from your own level.")
    with i2:
        card["pay_level_bp"] = st.number_input(
            "Your entry (bp)" if is_position else "Pay level (bp)", key=pk, min_value=0.0,
            step=0.5, format="%.1f", placeholder=str(entry),
            help="P&L is measured from here" + ("." if is_position else "; Kelly sizes at this level."))
    with i3:
        if is_position:
            owned_lots = st.number_input("Lots owned", key=lk, min_value=0, step=1, format="%d",
                                         help="Your actual position; P&L is sized at these lots.")
            card["lots_owned"] = int(owned_lots or 0)
        else:
            card["receive_level_bp"] = st.number_input(
                "Receive level (bp)", key=rk, min_value=0.0, step=0.5, format="%.1f",
                placeholder="optional",
                help="Where you flip from paying to receiving; rec columns measure P&L and "
                     "Kelly size from here.")

    raw = st.session_state.get(mbk)
    mbp_eff = float(raw if raw is not None else (market_bp if market_bp is not None else entry))
    implied_note = f"{mbp_eff / M.move_size:.0%} implied"
    if market_bp is None:
        implied_note += " · no strip, from Step 1"
    elif abs(mbp_eff - market_bp) > 1e-9:
        implied_note += f" · strip says {market_bp:.1f}bp"
    n1, n2, n3 = st.columns(3)
    n1.caption(implied_note)
    n2.caption("P&L from the owned entry" if is_position else "P&L measured from here")
    n3.caption("P&L sized at these lots" if is_position else "Rec columns reprice from here")

    pb = base_ctx
    if abs(mbp_eff - (market_bp if market_bp is not None else entry)) > 1e-9:
        pb = dcreplace(pb, market_level_bp=mbp_eff)
    if pb.anchor_price is None:
        price = src.playbook_anchor_price(ctx, pb.market_level_bp)
        if price is not None:
            pb = dcreplace(pb, anchor_price=price)

    pay = float(st.session_state[pk] or entry)
    outcomes = release_prep.playbook(card, pb)

    def pnl_usd(o) -> float | None:
        if o.action == release_prep.ACTION_UNSET or o.pnl_bp == 0:
            return o.pnl_usd
        return o.pnl_bp * owned_lots * pb.dv01 if is_position and owned_lots else o.pnl_usd

    def lots(o) -> int:
        if is_position and owned_lots:
            return owned_lots if o.action != release_prep.ACTION_UNSET else 0
        return o.lots

    def executable(o) -> bool:
        level = o.pay_level if o.pay_level is not None else pay
        return o.new_points <= level if o.side == "back" else o.new_points >= level

    kelly = sizing.fraction_label(sizing.snap_fraction(M.kelly_fraction))
    capture = f", {pb.capture:.0%} capture" if pb.capture < 1.0 - 1e-9 else ""
    worst = min((pnl_usd(o) or 0.0) for o in outcomes)
    if is_position:
        caption = (f"{pb.contract_label} · DV01 A${pb.dv01:,.2f}/lot/bp{capture} · sized at the "
                   f"lots you own" + (f" · A${owned_lots * pb.dv01:,.0f}/bp · worst scenario "
                                      f"A${worst:,.0f} vs A${M.daily_limit:,.0f} limit"
                                      if owned_lots else ""))
    else:
        own_q = any(abs(o.q - pb.q) > 1e-9 for o in outcomes)
        caption = (f"{pb.contract_label} · DV01 A${pb.dv01:,.2f}/lot/bp{capture} · {kelly} Kelly at "
                   f"your pay level" + (", each column at its own q" if own_q else ""))
        rec = card.get("receive_level_bp")
        if rec is not None:
            caption += f" · receive at {rec:.1f}bp is {rec - pb.fair:+.1f}bp vs fair ({pb.fair:.1f}bp)"
    st.caption(caption)

    spec = next((s for s in day.specs if s.series_key == card.get("lead")), None)
    lead_row = (card.get("sub_releases") or {}).get(card.get("lead")) or {}

    def header(name: str) -> str:
        try:
            val = None if lead_row.get(name) is None else float(lead_row.get(name))
        except (TypeError, ValueError):
            val = None
        if spec is None or val is None:
            return name.title()
        marker = "" if name == "base" else ("≤" if name == "dovish" else "≥")
        return f"{name.title()} ({marker}{spec.fmt(val)})"

    triggers = release_prep.trigger_lines(day, card, outcomes)
    for line in triggers:
        st.markdown(f'<div class="fomc-trigger" style="border-left-color:{P.accent}">{line}</div>',
                    unsafe_allow_html=True)
    if not triggers:
        C.note("No scenario clears the minimum edge yet — enter hawkish and dovish moves, or "
               "lower the minimum edge.")

    unset = release_prep.ACTION_UNSET

    def cell(o, render) -> str:
        return "—" if o.action == unset else render(o)

    body = [
        ["DV01 (A$/bp/lot)"] + [f"A${pb.dv01:,.2f}"] * len(outcomes),
        ["Your level"] + [cell(o, lambda x: f"{x.pay_level:.1f}bp") for o in outcomes],
        ["Market after print"] + [cell(o, lambda x: f"{x.new_points:.1f}bp") for o in outcomes],
        ["Actual price"] + [cell(o, lambda x: f"{x.price:.4f}" if x.price is not None else "—")
                            for o in outcomes],
        ["Market implied q"] + [cell(o, lambda x: f"{x.new_implied:.0%}") for o in outcomes],
        ["My q"] + [cell(o, lambda x: f"{x.q:.0%}") for o in outcomes],
        ["Fair (my q)"] + [cell(o, lambda x: f"{x.fair_bp:.1f}bp") for o in outcomes],
        ["Market move (bp)"] + [cell(o, lambda x: f"{x.delta_rate_bp:+.1f}bp") for o in outcomes],
        ["Your trade (bp)"] + [cell(o, lambda x: f"{x.pnl_bp:+.1f}bp") for o in outcomes],
        ["Your trade (A$)"] + ["—" if o.action == unset or pnl_usd(o) is None
                              else f"{pnl_usd(o):+,.0f}" for o in outcomes],
        ["Edge vs your q"] + [cell(o, lambda x: f"{x.edge_bp:.1f}bp") for o in outcomes],
        ["Edge at your level"] + [cell(o, lambda x: f"{x.own_edge_bp:+.1f}bp") for o in outcomes],
        ["Expected at size (A$)"] + ["—" if o.action == unset or not lots(o)
                                    else f"{o.own_edge_bp * lots(o) * pb.dv01:+,.0f}"
                                    for o in outcomes],
        ["Fresh entry"] + [{"stand down": "pass", "no move set": "—"}.get(o.action, o.action)
                           if o.action != unset else "—" for o in outcomes],
    ]
    if not is_position:
        body.append(["Executable"] + [cell(o, lambda x: "Yes" if executable(x) else "No")
                                      for o in outcomes])
    body.append(["Lots"] + [str(lots(o)) if lots(o) else "—" for o in outcomes])
    idx = {r[0]: i for i, r in enumerate(body)}

    action_colour = {release_prep.ACTION_REC: P.positive, release_prep.ACTION_PAY: P.negative,
                     release_prep.ACTION_STAND: P.muted, unset: P.muted}
    colours: dict[tuple[int, int], str] = {}
    for i, o in enumerate(outcomes, start=1):
        colours[(idx["Your level"], i)] = P.accent
        colours[(idx["Fresh entry"], i)] = action_colour.get(o.action, P.muted)
        if not lots(o):
            colours[(idx["Lots"], i)] = P.muted
        if not is_position:
            colours[(idx["Executable"], i)] = P.positive if executable(o) else P.warning
        if o.action == unset:
            continue
        unreachable = not is_position and not executable(o)
        colours[(idx["Your trade (bp)"], i)] = P.muted if unreachable else pnl_colour(o.pnl_bp, P)
        colours[(idx["Your trade (A$)"], i)] = (P.muted if unreachable
                                              else pnl_colour(pnl_usd(o) or 0.0, P))
        level = o.pay_level if o.pay_level is not None else pay
        winning = o.new_points >= level if o.side == "back" else o.new_points <= level
        colours[(idx["Market after print"], i)] = P.positive if winning else P.negative
        if abs(o.q - pb.q) > 1e-9:
            colours[(idx["My q"], i)] = P.accent
            colours[(idx["Fair (my q)"], i)] = P.accent
        tone = (P.muted if not executable(o)
                else P.warning if o.tradeable and o.own_edge_bp < card["min_edge_bp"]
                else pnl_colour(o.own_edge_bp, P))
        colours[(idx["Edge at your level"], i)] = tone
        colours[(idx["Expected at size (A$)"], i)] = tone

    fresh = idx["Fresh entry"]
    C.table([f"{day.release} prints"] + [header(o.name) for o in outcomes], body,
            numeric=tuple(range(1, len(outcomes) + 1)), colours=colours, mark_rows=[fresh],
            row_bg={fresh: "rgba(127,127,127,0.10)"})
    C.legend([("Receive", P.positive), ("Pay", P.negative), ("Pass", P.muted)])

    if any(o.clamped for o in outcomes):
        C.note("A scenario prices the move fully — the level pins at 0 or the full move size.")
    if is_position and owned_lots and abs(worst) > M.daily_limit:
        st.warning(f"Your {owned_lots} lots lose A${worst:,.0f} in the worst scenario, breaching "
                   f"the A${M.daily_limit:,.0f} daily limit.")
    if not is_position:
        for o in outcomes:
            if o.breach:
                st.warning(f"The {o.name} column sizes to {o.lots} lots, breaching the "
                           f"A${M.daily_limit:,.0f} daily limit at {kelly} Kelly.")
        sized = [o for o in outcomes if o.lots > 0]
        if sized:
            summary = f"Sized {sized[0].lots} lots at {pay:.1f}bp · A${sized[0].lots * pb.dv01:,.0f}/bp total DV01"
            rec = [o for o in outcomes if o.action == release_prep.ACTION_REC and o.lots > 0]
            if card.get("receive_level_bp") is not None and rec:
                summary += f" · receive {rec[0].lots} lots at {rec[0].pay_level:.1f}bp"
            C.note(summary)

    g1, g2 = st.columns(2)
    with g1:
        st.altair_chart(charts.playbook_levels(
            list(outcomes), pay, pb.market_level_bp, pb.fair, M.size, P,
            receive_level=None if is_position else card.get("receive_level_bp")),
            width="stretch", theme=None)
    with g2:
        st.altair_chart(charts.playbook_pnl([dcreplace(o, pnl_usd=pnl_usd(o)) for o in outcomes], P),
                        width="stretch", theme=None)
    C.note("Absolute levels not shown — enter the contract price on the Path tab to anchor them."
           if pb.anchor_price is None else
           f"Absolute levels anchored on {pb.anchor_price:.4f} for {pb.contract_label}, "
           f"{pb.capture:.0%} capture. Trade on the bp offsets.")


def _save_card(M, day, card: dict) -> None:
    st.divider()
    saved = store.load_release_cards()
    dirty = release_prep.normalise(saved.get(day.key), day) != card
    s1, s2 = st.columns([1, 3], vertical_alignment="center")
    with s1:
        if st.button("Save card" + (" ●" if dirty else ""), type="primary" if dirty else "secondary",
                     width="stretch", key=f"rp_save:{day.key}"):
            store.save_release_card(day.key, {**card, "stamp": clock.stamp(),
                                              "meeting": M.meeting.key})
            st.rerun()
    with s2:
        prior = saved.get(day.key) or {}
        if prior.get("stamp"):
            was = prior.get("meeting")
            C.note(f"Last saved {prior['stamp'][:16].replace('T', ' ')}"
                   + (f", prepped against {was}" if was and was != M.meeting.key else "")
                   + ". Cards are kept per release.")
        else:
            C.note("Not saved yet. Cards live in meetings/_release_cards.json, one per print.")
