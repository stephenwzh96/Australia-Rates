"""One computation of the whole framework from a meeting's saved state.

Both the dashboard and the Markdown export read from this, so the screen and the
exported report can never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

from . import (contracts, econ_calendar, kills, montecarlo, path,
               pricing, rba_calendar, sizing, volatility, votes)
from .rba_calendar import Meeting


@dataclass
class Model:
    state: dict[str, Any]
    meeting: Meeting
    as_of: date
    label: str

    # inputs
    points: float | None
    size: float
    side: pricing.Side
    direction: pricing.Direction
    instrument: str
    kelly_fraction: float

    # probability
    q: float | None
    q_mode: str
    decomposition: votes.Decomposition

    # steps 1-5
    summary: pricing.PricingSummary | None
    outcomes: list[pricing.Outcome]
    sensitivity: list[pricing.SensitivityRow]
    kelly_rows: list[pricing.KellyRow]

    # step 5 -- position sizing in contracts and dollars
    max_drawdown: float          # the Kelly bankroll
    daily_limit: float           # a hard cap, not a sizing input
    bets_per_year: int
    dv01: float                  # per bp of the EVENT, capture already applied
    quoted_dv01: float           # the contract's own quoted DV01, before capture
    capture: float               # share of the event this contract expresses
    contract_key: str
    contract_label: str
    sizing_ladder: sizing.SizingLadder | None

    # step 6
    ladder: list[path.LadderRow]
    exit_levels: list[path.ExitLevel]
    stop: path.StopAnalysis | None
    monte_carlo: montecarlo.MonteCarloResult | None
    vol_multipliers: dict[str, volatility.Multiplier]
    # Quiet-day sigma in RATE bp -- the level the multipliers scale. Rate, not
    # contract price: this app calibrates on 3-month BBSW, which is why no
    # capture rescale is applied to it (see `build`). Kept alongside the
    # multipliers because a ratio on its own sizes nothing -- reading "a CPI
    # day is 5x" needs the level to multiply -- and deriving it from
    # `monte_carlo` would only work when the simulation actually ran.
    quiet_sigma_bp: float | None

    # step 7
    kill: kills.KillAssessment

    # step 8
    arithmetic: votes.VoteArithmetic
    conditional: list[votes.ConditionalRow]
    roster: list[votes.Voter]

    # part II
    captures: list[contracts.ContractCapture]
    near_trap: bool
    blackout: tuple[date, date]
    in_blackout: bool
    days_to_meeting: int
    next_meeting: Meeting | None
    previous_meeting: Meeting | None
    calendar_events: list[econ_calendar.Event]

    warnings: list[str] = field(default_factory=list)

    @property
    def priced(self) -> bool:
        return self.summary is not None

    @property
    def conditional_ev_at(self) -> float | None:
        """EV assuming the bloc already voted yes, at the given P(centre joins)."""
        if self.points is None:
            return None
        return pricing.expected_value(self.points, self.size, self.decomposition.p_centre, self.side)


def _f(x: Any) -> float | None:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def build(state: dict[str, Any],
          as_of: date | None = None,
          rba_values: dict[str, float | None] | None = None,
          rba_units: dict[str, str] | None = None,
          all_meetings: list[Meeting] | None = None,
          rate_history: list[tuple[date, float]] | None = None,
          rate_symbol: str | None = None,
          policy_changes: set[date] | None = None) -> Model:
    trade = state.get("trade", {})
    prob = state.get("probability", {})
    pth = state.get("path", {})

    meeting = rba_calendar.meeting_from_dict(state["meeting"])
    when = as_of or date.fromisoformat(state.get("as_of") or date.today().isoformat())
    meetings = all_meetings or list(rba_calendar.DEFAULT_MEETINGS)

    # The economic calendar is built up front, not with the rest of Part II,
    # because the volatility schedule below is indexed against it: which
    # releases land in the forward window is what makes one day louder than
    # another. Cheap (pure date arithmetic) and used twice.
    calendar_start = when - timedelta(days=econ_calendar.LOOKBACK_DAYS)
    calendar_events = econ_calendar.merged_calendar(
        calendar_start, meeting.end, state.get("structure", {}).get("calendar") or [],
        meetings)

    # Volatility shape is estimated over the CONTRACT's own history, which
    # reaches much further back than the calendar shown on screen -- see
    # `core.volatility` for why level and shape take different windows.
    quiet_sigma_price: float | None = None
    multipliers: dict[str, volatility.Multiplier] = {}
    if rate_history:
        h0, h1 = rate_history[0][0], rate_history[-1][0]
        # Keyed on the REPORT, not the series: three Employment Situation
        # lines on one Friday are one shock, and summing their excess
        # variance would treat a single report as three independent ones.
        hist_events = [(e.when, e.release)
                       for e in econ_calendar.recurring_events(h0, h1, meetings)
                       if e.release]
        # DECISION DAYS MUST COVER THE WHOLE HISTORY, NOT JUST THE CALENDAR.
        # The meeting calendar spans 2026-27; the BBSW history behind it runs
        # from 2011. Excluding only calendar meetings left ~145 real decision
        # days sitting in the "quiet" bucket, which inflates the baseline sigma
        # and deflates every multiplier measured against it. `policy_changes`
        # carries the RBA's own dated record of every cash rate move (see
        # `data.rba.policy_change_dates`) and closes that gap.
        decision_days = ({m.end for m in meetings}
                         | {m.end + timedelta(days=1) for m in meetings}
                         | set(policy_changes or ()))
        quiet_sigma_price, multipliers, quiet_observations = volatility.estimate_from_history(
            rate_history, hist_events, decision_days)

    points = _f(trade.get("points"))
    # `or 25.0` would silently turn a typed 0 into 25, and a NEGATIVE size
    # reaches `pricing.implied_probability`, which raises straight out of this
    # function into a red Streamlit traceback. Neither is acceptable from a
    # number the user types, so it is clamped here and flagged below.
    raw_size = _f(trade.get("size"))
    size = raw_size if (raw_size or 0) > 0 else 25.0
    side: pricing.Side = trade.get("side", "fade")
    direction: pricing.Direction = trade.get("direction", "hike")
    kf = _f(trade.get("kelly_fraction")) or pricing.DEFAULT_KELLY_FRACTION

    override = _f(prob.get("override_q"))
    p_bloc = _f(prob.get("p_bloc")) or 0.0
    p_centre = _f(prob.get("p_centre")) or 0.0
    decomposition = votes.decompose(p_bloc, p_centre, override)
    q = override if prob.get("mode") == "manual" and override is not None else decomposition.q

    warnings: list[str] = []
    if raw_size is not None and raw_size <= 0:
        warnings.append(
            f"Size of the move was {raw_size:g}bp, which is not a move. Using "
            f"{size:g}bp instead - the implied probability is points/size, so a "
            "zero or negative size has no meaning.")
    if not meeting.verified:
        warnings.append("Meeting date is unverified - confirm against rba.gov.au.")
    if prob.get("mode") == "decomposition" and override is not None and not decomposition.reconciles:
        warnings.append(
            f"Decomposition gives {decomposition.q:.1%} but the recorded estimate is {override:.1%}. "
            "The number you are sizing on is not the number your vote read supports."
        )

    sizing_state = state.get("sizing", {})
    max_drawdown = _f(sizing_state.get("max_drawdown"))
    if max_drawdown is None:
        max_drawdown = sizing.DEFAULT_MAX_DRAWDOWN
    # Meetings saved before the drawdown/limit split carry a single
    # "risk_budget"; it was the daily limit, so that is where it lands.
    daily_limit = _f(sizing_state.get("daily_limit"))
    if daily_limit is None:
        daily_limit = _f(sizing_state.get("risk_budget")) or sizing.DEFAULT_DAILY_LIMIT
    bets_per_year = int(_f(sizing_state.get("bets_per_year"))
                        or sizing.DEFAULT_BETS_PER_YEAR)
    contract_key = sizing_state.get("contract") or "ib"
    contract_spec = sizing.contract_for(contract_key, when)
    # The live yield only matters for IR, whose bp value moves with the level;
    # `dv01_for` falls back to a reference yield when the curve is unavailable.
    quoted_dv01 = sizing.dv01_for(contract_key, _f(sizing_state.get("custom_dv01")),
                                  yield_pct=_f(sizing_state.get("contract_yield")))
    contract_label = contract_spec.label

    # CAPTURE. The quoted DV01 is dollars per bp of the CONTRACT's own price;
    # everything downstream is denominated in bp of the EVENT. A contract whose
    # delivery month only partly covers the post-decision period moves less
    # than one-for-one with the event, so its effective DV01 is scaled -- and
    # sizing off the unscaled figure under-sizes by exactly 1/capture. See
    # `core.contracts` for the exact ZQ (simple average) and SR3 (compounded)
    # derivations. Below the existing trap threshold the contract is not
    # expressing the view at all and no rescale can rescue it, so capture is
    # left at 1 and a warning is raised instead of quoting a wild number.
    capture = contracts.contract_capture_share(
        contract_spec.kind, contract_spec.month, meeting.end)
    # IR capture is binary by construction -- one fix, so the contract either
    # sees the decision or it does not (see `core.contracts`). A 0.0 there is
    # not "a bit of the move", it is the wrong contract entirely.
    capture_usable = capture >= contracts.CAPTURE_WARNING_THRESHOLD
    dv01 = quoted_dv01 * capture if capture_usable else quoted_dv01
    if capture_usable and capture < 1.0 - 1e-9:
        warnings.append(
            f"{contract_label} captures {capture:.1%} of a {meeting.label} move, so its "
            f"DV01 per bp of the event is corrected from A${quoted_dv01:,.2f} to "
            f"A${dv01:,.2f} - lot counts and the exit-map price column below use the "
            "corrected figure."
        )
    elif not capture_usable:
        warnings.append(
            f"{contract_label} captures only {capture:.1%} of a {meeting.label} move - "
            f"below the {contracts.CAPTURE_WARNING_THRESHOLD:.0%} threshold at which a "
            "contract stops expressing the view. Sizing is left uncorrected and the "
            "simulation is skipped; choose a contract that actually spans the decision."
        )

    summary = outcomes = None
    sens: list[pricing.SensitivityRow] = []
    kelly_rows: list[pricing.KellyRow] = []
    ladder_rows: list[path.LadderRow] = []
    exit_levels: list[path.ExitLevel] = []
    sizing_ladder = None
    stop = None
    monte_carlo: montecarlo.MonteCarloResult | None = None
    conditional: list[votes.ConditionalRow] = []

    if points is not None:
        summary = pricing.summarise(points, size, q, side, direction, kf)
        outcomes = pricing.terminal_outcomes(points, size, side, direction)
        sens = pricing.sensitivity(points, size, side, state.get("sensitivity_grid"))
        kelly_rows = pricing.kelly_table(points, size, side,
                                         state.get("kelly_grid", (0.10, 0.15, 0.20, 0.25)), kf)
        ladder_rows = path.ladder(points, size, pth.get("ladder_levels") or None, side)
        sizing_ladder = sizing.build_ladder(
            points, size, q, side, max_drawdown, dv01, daily_limit, bets_per_year,
            selected=sizing.snap_fraction(kf))
        if sizing_ladder.ok and not sizing_ladder.rung_for(
                sizing.snap_fraction(kf)).within_daily_limit:
            warnings.append(
                f"{sizing.fraction_label(sizing.snap_fraction(kf))} Kelly risks "
                f"A${sizing_ladder.selected.max_loss:,.0f} against a "
                f"A${daily_limit:,.0f} daily limit - that is a breach, not a size."
            )
        conditional = votes.conditional_ev(points, size, side,
                                           state.get("centre_grid", (0.20, 0.30, 0.40, 0.50)))

        # The exit map prices every level against the size actually being run,
        # so the dollar column moves with the Kelly rung selected in Step 5
        # rather than quoting a notional nobody is trading.
        selected_rung = (sizing_ladder.selected
                         if sizing_ladder is not None and sizing_ladder.ok else None)
        target_level = _f(pth.get("target_level"))
        stop_level = _f(pth.get("stop_level"))
        # A partial of exactly 0 is "blank", not a level: a scale-out at 0bp
        # is never a real barrier (it marks the entry -- zero MTM), and the
        # UI's optional input stores whatever was typed, including a 0 meant
        # as "single take-profit". Treated as a barrier it sits on the wrong
        # side of entry, flags itself misplaced, and blocks the whole Monte
        # Carlo over a value that means nothing.
        partial_target_level = _f(pth.get("partial_target_level")) or None
        partial_share = _f(pth.get("partial_share")) or 0.5
        exit_levels = path.exit_map(
            points, size, side, direction,
            reassess_target=target_level, reassess_stop=stop_level,
            position_dv01=selected_rung.position_dv01 if selected_rung else None,
            current_price=_f(pth.get("current_price")),
            capture=capture if capture_usable else 1.0,
            hard_stop=_f(pth.get("hard_stop_level")),
            partial_target=partial_target_level,
        )

        for x in exit_levels:
            if x.misplaced:
                warnings.append(
                    f"{x.label} sits at {x.level:g}bp, which is {x.mtm:+g}bp against a "
                    f"{points:g}bp entry - a {x.role} cannot sit there. It is most likely "
                    "carried over from a meeting with a different entry."
                )

        # A user-typed vol always wins; failing that, the realised volatility
        # of 3-month BBSW (see `data.rba.realised_daily_vol_bp`), excluding
        # known decision dates whose jump this model simulates separately from
        # the pre-announcement drift being estimated here. No fabricated
        # default: without one of these two, there is simply no Monte Carlo.
        #
        # NO CAPTURE DIVISION HERE, and that is the correction. The US original
        # divides by capture because it calibrates on a CONTRACT PRICE, and a
        # contract whose delivery month only partly spans the decision moves
        # correspondingly less per bp of event -- so dividing recovers the
        # event's own scale. This app calibrates on a RATE. Three-month BBSW
        # moves one-for-one with rate expectations no matter which contract you
        # happen to be sizing, so the same division would inflate sigma by
        # 1/capture -- fivefold at the 0.20 floor -- for no reason at all.
        vol_override = _f(pth.get("vol_override_bp"))
        vol_estimate: montecarlo.VolEstimate | None = None
        if vol_override is not None and vol_override > 0:
            # Typed in event terms already -- the user is stating what they
            # think the priced level does per day, not what a contract does.
            vol_estimate = montecarlo.VolEstimate(vol_override, "override",
                                                  capture=1.0)
        elif quiet_sigma_price is not None:
            vol_estimate = montecarlo.VolEstimate(
                quiet_sigma_price, "historical", symbol=rate_symbol,
                observations=quiet_observations,
                capture=1.0)

        # A misplaced level makes the simulation meaningless, not just
        # imprecise: `touch_shares` classifies by which side of entry a level
        # sits on, so a "stop" below entry is treated as a second target and
        # the six counts come back describing a trade nobody is running. The
        # exit map already flags the level; this refuses to compute on it.
        # The hard floor is exempt: it is a discipline line, not a
        # simulation barrier, so a misplaced floor warns but never blocks.
        levels_placed = not any(
            x.misplaced for x in exit_levels if x.label != "Stop -- hard floor")
        if not levels_placed:
            warnings.append(
                "Monte Carlo skipped: an exit level sits on the wrong side of entry, "
                "so touch probabilities would describe a different trade. Fix the "
                "level flagged above and it will run."
            )

        monte_carlo: montecarlo.MonteCarloResult | None = None
        if (vol_estimate is not None and levels_placed
                and target_level is not None and stop_level is not None):
            trading_days = montecarlo.trading_days_between(when, meeting.end)
            sim_days = montecarlo.trading_day_list(when, meeting.end)
            if multipliers and vol_estimate.source == "historical":
                day_events = {d: [] for d in sim_days}
                for e in calendar_events:
                    if e.when in day_events and e.release:
                        if e.release not in day_events[e.when]:
                            day_events[e.when].append(e.release)
                vol_schedule = volatility.build_schedule(
                    sim_days, vol_estimate.daily_vol_bp, day_events, multipliers)
                vol_estimate = replace(
                    vol_estimate, schedule=tuple(vol_schedule.sigmas))
            monte_carlo = montecarlo.simulate_exit_paths(
                points, size, q, target_level, stop_level, vol_estimate, trading_days,
                vol_uncertainty=bool(pth.get("vol_uncertainty")), days=sim_days, when=when,
                partial_target_level=partial_target_level, partial_share=partial_share)

        # The stop-cost decomposition runs AFTER the simulation so a partial
        # take-profit blend can use the simulated share of target-touches that
        # reached the full target (manual fallback: assume they all did).
        #
        # Only once at least one count is entered, though. All six at zero is
        # "not filled in yet", not "a hundred runs that went nowhere", and
        # analysing it produces two confident warnings -- counts sum to 0, and
        # they imply P(move) = 0% -- about a table the user has not touched.
        # Nagging about an empty form teaches people to ignore the warnings
        # that matter.
        _count_keys = ("no_move_target_hit", "no_move_never_breached",
                       "no_move_falsely_stopped", "move_target_hit",
                       "move_stopped_early", "move_gapped_through")
        any_counts = any((_f(pth.get(k)) or 0.0) > 0 for k in _count_keys)
        if stop_level is not None and any_counts:
            stop = path.stop_analysis(
                points, size, q, stop_level,
                _f(pth.get("no_move_never_breached")) or 0.0,
                _f(pth.get("no_move_falsely_stopped")) or 0.0,
                _f(pth.get("move_stopped_early")) or 0.0,
                _f(pth.get("move_gapped_through")) or 0.0,
                side,
                target_level=target_level,
                no_move_target_hit=_f(pth.get("no_move_target_hit")) or 0.0,
                move_target_hit=_f(pth.get("move_target_hit")) or 0.0,
                partial_target_level=partial_target_level,
                partial_share=partial_share,
                partial_reach_full=(monte_carlo.frac_reached_full if monte_carlo else 1.0),
            )
            if not stop.counts_consistent:
                if not stop.counts_sum_correctly:
                    warnings.append(
                        f"Stop-path counts sum to {stop.counts_total:g}, not {stop.trials:g}."
                    )
                warnings.append(
                    f"Stop-path counts imply P(move) = {stop.implied_p_move:.1%} "
                    f"({stop.actual_move_count:g} of {stop.counts_total:g}), "
                    f"not your q of {q:.1%}."
                )

    roster = votes.voters_from_dicts(state.get("roster") or [])
    arithmetic = votes.tally(roster)
    if not roster:
        warnings.append("No roster on this meeting - the vote arithmetic is empty.")

    criteria = kills.from_dicts(state.get("kill_criteria", []), rba_values, rba_units)
    assessment = kills.assess(criteria)
    if assessment.is_stale:
        warnings.append(
            f"{assessment.count} kill criteria live - the probability estimate is stale."
        )

    caps = contracts.capture(meeting.end, size)
    blackout = rba_calendar.blackout_window(meeting)
    previous_meeting = rba_calendar.previous_meeting_before(meetings, meeting)

    return Model(
        state=state, meeting=meeting, as_of=when,
        label=state.get("label") or meeting.label,
        points=points, size=size, side=side, direction=direction,
        instrument=trade.get("instrument", ""), kelly_fraction=kf,
        q=q, q_mode=prob.get("mode", "decomposition"), decomposition=decomposition,
        summary=summary, outcomes=outcomes or [], sensitivity=sens, kelly_rows=kelly_rows,
        max_drawdown=max_drawdown, daily_limit=daily_limit,
        bets_per_year=bets_per_year, dv01=dv01, quoted_dv01=quoted_dv01,
        capture=capture, contract_key=contract_key,
        contract_label=contract_label, sizing_ladder=sizing_ladder,
        ladder=ladder_rows, exit_levels=exit_levels, stop=stop, monte_carlo=monte_carlo,
        vol_multipliers=multipliers, quiet_sigma_bp=quiet_sigma_price,
        kill=assessment,
        arithmetic=arithmetic, conditional=conditional, roster=roster,
        captures=caps, near_trap=contracts.near_contract_is_a_trap(caps),
        blackout=blackout, in_blackout=rba_calendar.in_blackout(meeting, when),
        days_to_meeting=rba_calendar.days_until(meeting, when),
        next_meeting=rba_calendar.next_meeting_after(meetings, meeting),
        previous_meeting=previous_meeting,
        calendar_events=calendar_events,
        warnings=warnings,
    )
