"""Path: exit levels, the stop-cost decomposition and the Monte Carlo behind it."""

from __future__ import annotations

import streamlit as st

from core import montecarlo, path as pathmod
from ui import charts
from ui import components as C
from ui.context import Ctx, mark_dirty
from ui.theme import FAMILY_NAME, FAMILY_SOLID, RELEASE_FAMILIES, RELEASE_FAMILY, RELEASE_FAMILY_COLOUR, pnl_colour

#: The six out-of-100 buckets of the stop-cost decomposition, no-move first.
COUNT_FIELDS = ("no_move_target_hit", "no_move_never_breached", "no_move_falsely_stopped",
                "move_target_hit", "move_stopped_early", "move_gapped_through")


@st.cache_data(max_entries=32, show_spinner="Sweeping exit levels…")
def _sweeps(points, size, side, q, target, stop, vol, days, partial, share):
    target_bound, stop_bound = pathmod.bound_levels(size, side)
    kw = dict(n_seeds=montecarlo.DEFAULT_SWEEP_SEEDS, partial_target_level=partial,
              partial_share=share)
    stops = montecarlo.stop_sweep(points, size, side, q, target,
                                  montecarlo.suggested_sweep_levels(
                                      points, stop_bound, n=montecarlo.DEFAULT_SWEEP_LEVELS),
                                  vol, days, **kw)
    targets = montecarlo.target_sweep(points, size, side, q, stop,
                                      montecarlo.suggested_sweep_levels(
                                          points, target_bound, n=montecarlo.DEFAULT_SWEEP_LEVELS),
                                      vol, days, **kw)
    return stops, targets


def render(ctx: Ctx) -> None:
    S, M = ctx.S, ctx.M
    pth = S.setdefault("path", {})
    if not M.priced:
        st.info("Price the trade first.")
        return
    C.section("Path risk", "levels are the quoted bp priced, not the collapsed payoff")
    if M.multi_move:
        C.note(f"Levels run 0 to {M.level_size:g}bp ({M.moves.n} × {M.moves.size:g}bp). A "
               f"'move' is any one meeting moving, {M.level_q:.0%} on your view of "
               f"{M.moves.moves_you:g}; the two-point terminal is exact in expectation but "
               "overstates how often the level ends at an extreme.")
    if M.ladder:
        st.altair_chart(charts.mtm_ladder(M.ladder, ctx.P), width="stretch", theme=None)

    _level_inputs(ctx, pth)
    M = ctx.rebuild()
    _exit_table(ctx)
    _decomposition(ctx, pth)
    _monte_carlo(ctx, pth)
    pth["note"] = st.text_area("Path note", pth.get("note", ""), height=80, on_change=mark_dirty)


def _level_inputs(ctx: Ctx, pth: dict) -> None:
    M, S = ctx.M, ctx.S
    C.heading("Exit levels", level="sub", help=(
        "", "Target and stop are reassess levels you choose inside the market bounds; "
        "the partial target banks a share on the way; the hard floor is a discipline line, "
        "not a simulation barrier.", "", "exits"))
    default_target, default_stop = pathmod.reassess_defaults(M.level_points, M.level_size, M.trade_side)
    x1, x2, x3, x4 = st.columns(4)
    with x1:
        pth["target_level"] = st.number_input(
            "Target — reassess (bp)",
            value=float(pth["target_level"] if pth.get("target_level") is not None else default_target),
            step=0.5, format="%.2f", on_change=mark_dirty,
            help="Take profit here rather than ride to the bound. Defaults halfway.")
    with x2:
        pth["stop_level"] = st.number_input(
            "Stop — thesis (bp)",
            value=float(pth["stop_level"] if pth.get("stop_level") is not None else default_stop),
            step=0.5, format="%.2f", on_change=mark_dirty,
            help="The thesis-broken level the Monte Carlo treats as the stop.")
    pt_key = f"pt_partial_{S.get('meeting')}"
    with x3:
        pth["partial_target_level"] = st.number_input(
            "Partial target (bp)",
            value=float(pth["partial_target_level"]) if pth.get("partial_target_level") is not None else None,
            step=0.5, format="%.2f", placeholder="optional", on_change=mark_dirty, key=pt_key,
            help="Scale-out level between entry and the target. Clear it for a single take-profit.")
    with x4:
        pth["partial_share"] = st.number_input(
            "Partial share", value=float(pth.get("partial_share", 0.5)), min_value=0.0,
            max_value=1.0, step=0.1, format="%.2f", on_change=mark_dirty,
            help="Fraction of the position banked at the partial target.")
    y1, y2, y3 = st.columns(4)[:3]
    with y1:
        pth["hard_stop_level"] = st.number_input(
            "Hard floor (bp)",
            value=float(pth["hard_stop_level"]) if pth.get("hard_stop_level") is not None else None,
            step=0.5, format="%.2f", placeholder="optional", on_change=mark_dirty,
            help="A non-negotiable risk floor beyond the thesis stop.")
    with y2:
        cp = pth.get("current_price")
        pth["current_price"] = st.number_input(
            "Contract price now", value=float(cp) if cp is not None else None, step=0.005,
            format="%.4f", placeholder="optional, e.g. 96.19", on_change=mark_dirty,
            help="Prices every exit: 1bp of rate is 0.01 of price on both IB and IR. Blank "
                 "hides the price column.")
    with y3:
        st.markdown('<div style="height:1.75rem"></div>', unsafe_allow_html=True)
        if st.button("Clear partial target", width="stretch", key=pt_key + "_clear",
                     disabled=pth.get("partial_target_level") is None):
            st.session_state.pop(pt_key, None)
            pth["partial_target_level"] = None
            mark_dirty()
            st.rerun()


def _exit_table(ctx: Ctx) -> None:
    M, P = ctx.M, ctx.P
    if not M.exit_levels:
        return
    has_price = any(x.price is not None for x in M.exit_levels)
    has_pnl = any(x.pnl is not None for x in M.exit_levels)
    headers = ["Exit", "Moves" if M.multi_move else "Implied", "Priced"]
    headers += ["Price"] if has_price else []
    headers += ["P&L"] + (["P&L (A$)"] if has_pnl else [])
    pnl_col = headers.index("P&L")
    rows, colours, marks = [], {}, []
    for i, x in enumerate(M.exit_levels):
        if x.is_entry:
            marks.append(i)
        row = [x.label, f"{x.implied * M.moves.n:.2f}" if M.multi_move else f"{x.implied:.0%}",
               f"{x.level:g}bp"]
        if has_price:
            row.append("—" if x.price is None else f"{x.price:.4f}")
        row.append("—" if x.is_entry else f"{x.mtm:+g}bp")
        if has_pnl:
            row.append("—" if x.pnl is None or x.is_entry else f"{x.pnl:+,.0f}")
        rows.append(row)
        if not x.is_entry:
            colours[(i, pnl_col)] = pnl_colour(x.mtm, P)
            if has_pnl:
                colours[(i, len(headers) - 1)] = pnl_colour(x.mtm, P)
        if x.structural:
            colours[(i, 0)] = P.muted
        if x.misplaced:
            colours[(i, 0)] = P.critical
    C.table(headers, rows, numeric=tuple(range(1, len(headers))), mark_rows=marks, colours=colours)


def _decomposition(ctx: Ctx, pth: dict) -> None:
    M = ctx.M
    C.heading("Stop-cost decomposition", level="sub", help=(
        r"EV_{\text{exits}} = \frac{\sum_i n_i \cdot \text{P\&L}_i}{N}",
        "Per-100-trade decomposition, each trial classified by the level the path reaches "
        "first. Each barrier's effect compares the paths that exited there with what they "
        "would have made riding to expiry.", "see the table", "stop"))
    counts = M.monte_carlo.counts if M.monte_carlo else {}
    locked = bool(counts)
    for f in COUNT_FIELDS:
        pth.setdefault(f, 0.0)
        if locked:
            pth[f] = counts[f]

    left, right = st.columns([1, 1.3], gap="large")
    with left:
        if not locked:
            st.caption("No simulation available (see the warning below) — entered by hand.")
        labels = {"no_move_target_hit": ("Target hit", "no move; target reached first"),
                  "no_move_never_breached": ("Rode out", "no move; neither level touched"),
                  "no_move_falsely_stopped": ("False stop", "no move; noise touched the stop"),
                  "move_target_hit": ("Target first", "target banked before the move"),
                  "move_stopped_early": ("Stopped", "move; the stop caught it early"),
                  "move_gapped_through": ("Gapped", "move; the price jumped past the stop")}
        g1, g2 = st.columns(2)
        for col, heading, fields in ((g1, "No move", COUNT_FIELDS[:3]), (g2, "Move", COUNT_FIELDS[3:])):
            with col:
                st.caption(heading)
                for f in fields:
                    label, tail = labels[f]
                    val = st.number_input(label, value=float(pth[f]), step=1.0, format="%.0f",
                                          disabled=locked, on_change=mark_dirty,
                                          key=f"count_{ctx.key}_{f}",
                                          help=f"Out of 100 trials: {tail}."
                                               + (" Tracks the simulation." if locked else ""))
                    if not locked:
                        pth[f] = val
    M = ctx.rebuild()
    with right:
        if not M.stop:
            st.info("Set a stop level to run the decomposition.")
            return
        stp = M.stop
        C.table(["Path", "Count", "P&L each", "Total"],
                [[p.label, f"{p.count:.0f}", f"{p.pnl_each:+.2f}", f"{p.total:+.1f}"] for p in stp.paths],
                numeric=(1, 2, 3),
                colours={(i, 3): pnl_colour(p.total, ctx.P) for i, p in enumerate(stp.paths)})
        P = ctx.P
        C.board([
            C.Fig("EV with exits", f"{stp.ev_with_stop:+.2f}bp", "",
                  pnl_colour(stp.ev_with_stop, P)),
            C.Fig("EV riding on", f"{stp.ev_without_stop:+.2f}bp", "",
                  pnl_colour(stp.ev_without_stop, P)),
            C.Fig("Cost of the exits", f"{stp.cost_of_stop:+.2f}bp",
                  f"{stp.cost_as_share_of_edge:.0%} of edge"),
            C.Fig("Stop's own effect", f"{stp.stop_effect:+.2f}bp"),
            C.Fig("Target's own effect", f"{stp.target_effect:+.2f}bp"),
        ])
        if not stp.counts_consistent:
            msg = (f"Counts sum to {stp.counts_total:g}, not {stp.trials:g}. "
                   if not stp.counts_sum_correctly else "")
            st.warning(msg + f"They imply P(move) = {stp.implied_p_move:.1%}, not the "
                       f"{M.level_q:.1%} these levels are scored against. Flagged, not rescaled.")


def _monte_carlo(ctx: Ctx, pth: dict) -> None:
    M, P = ctx.M, ctx.P
    C.heading("Monte Carlo calibration", level="sub", help=(
        r"\text{level}_{t+1} = \text{level}_t + \mathcal{N}(0,\ \sigma_t^2)"
        r"\ \text{, then a jump to 0 or size at the decision}",
        "A driftless daily walk calibrated to 3-month BBSW's quiet-day volatility (the ASX "
        "feed has no contract price history, and a bp of BBSW is a bp of bill price), "
        "scaled up on release days, then a discrete jump at the announcement. Touch "
        "probabilities are applied to a 100-trial population split exactly by q.",
        "see core.montecarlo", "mc"))
    v1, v2 = st.columns(2, gap="large")
    with v2:
        raw = pth.get("vol_override_bp")
        pth["vol_override_bp"] = st.number_input(
            "Vol override (bp/day)", value=float(raw) if raw is not None else None,
            min_value=0.0, step=0.1, format="%.2f", placeholder="use historical",
            on_change=mark_dirty, help="Replaces the whole per-day schedule with one flat number.")
        pth["vol_uncertainty"] = st.checkbox(
            "Add vol uncertainty", value=bool(pth.get("vol_uncertainty")), on_change=mark_dirty,
            help="Each path draws its own volatility from the estimate's sampling distribution.")
    M = ctx.rebuild()
    with v1:
        vol = M.monte_carlo.vol if M.monte_carlo else None
        if vol and vol.source == "historical":
            st.caption(f"Quiet-day vol {vol.daily_vol_bp:.2f}bp/day from "
                       f"{vol.symbol or '3-month BBSW'}'s last {vol.observations} quiet sessions "
                       "(releases and decision days excluded) · a rate, so no capture rescale")
        elif vol and vol.source == "override":
            st.caption(f"Vol {vol.daily_vol_bp:.2f}bp/day, typed override (flat)")
        elif M.quiet_sigma_bp is not None:
            st.caption(f"Quiet-day vol {M.quiet_sigma_bp:.2f}bp/day is available — the "
                       "simulation is skipped for the reason flagged in the verdict.")
        else:
            st.caption("No BBSW history available and no override set.")

    mc = M.monte_carlo
    if mc is None:
        st.info("No volatility estimate — type an override above to run the simulation."
                if M.quiet_sigma_bp is None else
                "The simulation did not run — fix the level flagged in the verdict.")
        return

    sched = mc.vol.schedule
    sched_note = ""
    if sched:
        loud = sum(1 for s in sched if s > min(sched) + 1e-9)
        flat = (sum(s * s for s in sched) / len(sched)) ** 0.5
        sched_note = f" · {loud} of {len(sched)} days carry a release ({flat:.2f}bp/day flat-equivalent)"
    scale = (f" · scale-out {mc.partial_share:.0%} at {mc.partial_target_level:g}bp"
             if mc.partial_target_level is not None else "")
    st.caption(f"{mc.trading_days} trading days · {mc.mc_trials:,} paths · target first "
               f"{mc.p_target_first:.0%}, stop first {mc.p_stop_first:.0%}, rides out "
               f"{mc.p_rode_out:.0%}{scale}{sched_note}")

    with C.bar("tier_checks"):
        cols = st.columns([1.6, 1, 1, 6], gap="small", vertical_alignment="center")
        cols[0].caption("Mark releases")
        tiers = {}
        for i, (tier, default, tip) in enumerate(((1, True, "Labour Force, quarterly CPI, RBA "
                                                                "decisions"),
                                                  (2, False, "Monthly CPI, WPI, National "
                                                             "Accounts, minutes"))):
            pth[f"mark_tier{tier}"] = cols[i + 1].checkbox(
                f"Tier {tier}", value=bool(pth.get(f"mark_tier{tier}", default)),
                on_change=mark_dirty, help=tip)
            tiers[tier] = pth[f"mark_tier{tier}"]
    selected = {t for t, on in tiers.items() if on}
    index = {d: i for i, d in enumerate(mc.days)}
    release_days: dict[int, list[str]] = {}
    for e in M.calendar_events if selected else ():
        i = index.get(e.when)
        if i is not None and e.release and e.weight in selected and e.release not in release_days.get(i, []):
            release_days.setdefault(i, []).append(e.release)
    st.altair_chart(charts.mc_fan_chart(mc, M.level_points, M.level_size, M.trade_side, P,
                                        height=420, release_days=release_days or None,
                                        family_colour=RELEASE_FAMILY_COLOUR),
                    width="stretch", theme=None)
    if release_days:
        shown = {n for names in release_days.values() for n in names}
        C.legend([(FAMILY_NAME[f], FAMILY_SOLID[f]) for f in RELEASE_FAMILIES
                  if any(RELEASE_FAMILY.get(n) == f for n in shown)])

    stops, targets = _sweeps(M.level_points, M.level_size, M.trade_side, M.level_q,
                             pth["target_level"], pth["stop_level"], mc.vol, mc.trading_days,
                             pth.get("partial_target_level"), float(pth.get("partial_share", 0.5)))
    s1, s2 = st.columns(2, gap="medium")
    with s1:
        st.altair_chart(charts.mc_sensitivity(stops, pth["stop_level"], "stop level (bp)", P),
                        width="stretch", theme=None)
        C.note(f"EV as the stop moves, target fixed; averaged over "
               f"{montecarlo.DEFAULT_SWEEP_SEEDS} batches.")
    with s2:
        st.altair_chart(charts.mc_sensitivity(targets, pth["target_level"], "target level (bp)", P),
                        width="stretch", theme=None)
        C.note("EV as the target moves, stop fixed. Each curve moves one barrier at a time.")

    if M.vol_multipliers:
        with st.expander("How each release was weighted"):
            rows, colours = [], {}
            for i, (name, m) in enumerate(sorted(M.vol_multipliers.items(),
                                                 key=lambda kv: -kv[1].applied)):
                rows.append([name, f"{m.n_event}", f"{m.raw:.2f}x", f"{m.applied:.2f}x",
                             f"{m.ci_low:.2f} – {m.ci_high:.2f}",
                             "applied" if m.usable else "too few to apply"])
                colours[(i, 5)] = P.good if m.significant else (P.muted if m.usable else P.serious)
            C.table(["Release", "n", "Raw", "Applied", "95% CI", "Status"], rows,
                    numeric=(1, 2, 3), colours=colours)
            C.note("Variance on each release's own days against quiet days, from 3-month "
                   "BBSW's own history. Applied is shrunk toward 1.0 by sample size. Quarterly "
                   "CPI is the loudest release on the Australian calendar by a wide margin.")
