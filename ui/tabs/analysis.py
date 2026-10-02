"""Analysis: studies that draw on the rest of the dashboard.

First item -- the next unemployment print: a bias for the direction and size
of the next monthly change in the Australian unemployment rate. All the work
is in the self-contained `nextprint` package, imported only when this tab
renders, so no other tab pays for it (see nextprint/__init__.py to remove it).

The US tab sets the model against Kalshi's odds on the same print. No market
trades the Australian rate, so the comparison here is against the street's
consensus, from the reaction card's sourced search on the Releases tab.
"""

from __future__ import annotations

import math
from datetime import date

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

from core import econ_calendar
from state import store
from ui import charts
from ui import components as C
from ui import sources as src
from ui.context import Ctx

#: The Labour Force number the street's consensus is read for.
LEAD = "LF_UR"


def render(ctx: Ctx) -> None:
    res = _result(ctx.as_of)
    if not res.ok:
        C.section("Next unemployment print")
        st.warning(res.message)
        return
    _headline(res, ctx)
    left, right = st.columns([1, 1.25], gap="large")
    with left:
        _probabilities(res, ctx)
    with right:
        _drivers(res, ctx)
    _street(res, ctx)
    _track(res, ctx)
    _inputs(res)
    _method(res)


#: How long a built result is reused before the model is rerun (which is also
#: when its data files are rechecked against their release calendars).
RESULT_TTL = 600.0


@st.cache_resource
def _results() -> dict:
    """Hand-rolled cache (ingestion caching belongs in ui/sources.py; this is a
    compute cache keyed on the as-of date and the model's own data files)."""
    return {}


def _result(as_of: date):
    import time

    from nextprint import fetch, pipeline

    cache = _results()
    with st.spinner("Reading the Labour Force and the surveys…"):
        data = fetch.load_all()
    key = (as_of, fetch.fingerprint(data))
    built_at, res = cache.get(key, (0.0, None))
    if res is None or time.monotonic() - built_at > RESULT_TTL:
        with st.spinner("Building the next-print model…"):
            res = pipeline.run(as_of, data, next_release=src.next_release(
                econ_calendar.LABOUR, as_of))
        cache.clear()                       # one live result at a time
        cache[key] = (time.monotonic(), res)
    return res


def result(as_of: date):
    """The next-print model for `as_of`, from the tab's own cache -- for any
    brief that should read the numbers this tab shows."""
    return _result(as_of)


# ------------------------------------------------------------------ helpers ---

def _pp(x: float, digits: int = 2) -> str:
    return "—" if not math.isfinite(x) else f"{x:+.{digits}f} pp".replace("-", "−")


def _pct(x: float, signed: bool = False) -> str:
    if not math.isfinite(x):
        return "—"
    return (f"{100 * x:+.2f}%" if signed else f"{100 * x:.2f}%").replace("-", "−")


def _move_colour(x: float, P) -> str | None:
    """Higher unemployment is the bad-news colour."""
    if not math.isfinite(x) or abs(x) < 0.005:          # rounds to +0.00: no colour
        return None
    return P.negative if x > 0 else P.positive


def _lean(live: dict) -> tuple[str, str | None]:
    up, down = live.get("p_up", float("nan")), live.get("p_down", float("nan"))
    if not (math.isfinite(up) and math.isfinite(down)):
        return "no probability yet", None
    gap = up - down
    if gap > 0.10:
        return "leans higher", "up"
    if gap < -0.10:
        return "leans lower", "down"
    return "two-sided, no clear lean", None


# ----------------------------------------------------------------- sections ---

def _headline(res, ctx: Ctx) -> None:
    L, P = res.live, ctx.P
    month, release = L.get("month"), L.get("release")
    when = f"{month:%B %Y} rate" if month else "next print"
    if release:
        when += f" · released {release:%a %d %b %Y}"
    C.heading("Next unemployment print", when, help=(
        "", "A bias for the next monthly unemployment-rate print, made only from data "
            "public before the release. The baseline is no change. Each of 5 "
            "pre-registered inputs pushes the forecast by (its slope) x (how far it sits "
            "from its own 15-year normal), and the forecast is the plain average of those "
            "pushes, so nothing is picked by looking at results. The probabilities add the "
            "model's own past errors, rescaled to today's level of unemployment, to the "
            "unrounded last rate and round to 0.1 as the ABS does.",
            "", "nextprint-help"))
    if L.get("masked"):
        st.info("This month sits inside the excluded COVID window; the model does not "
                "forecast it.")
    prior, prior_u = L.get("prior_print", float("nan")), L.get("prior_u", float("nan"))
    fc = L.get("forecast", float("nan"))
    lean, side = _lean(L)
    items = [
        ("Last print", f"{prior_u:.2f}%  (printed {prior:.2f}%)", None),
        ("Expected change", _pp(fc), _move_colour(fc, P)),
    ]
    if "likely_print" in L:
        items += [
            ("Most likely print", f"{L['likely_print']:.2f}%  ·  {_pct(L['likely_p'])}", None),
            ("P(higher)", _pct(L["p_up"]), P.negative if side == "up" else None),
            ("P(unchanged)", _pct(L["p_flat"]), None),
            ("P(lower)", _pct(L["p_down"]), P.positive if side == "down" else None),
        ]
    C.stat_row(items)
    drivers = [d for d in res.drivers if math.isfinite(d["driver"])]
    ups = sum(d["driver"] > 0.005 for d in drivers)
    downs = sum(d["driver"] < -0.005 for d in drivers)
    if side is not None:
        # Where the lean comes from: the inputs, or where the unrounded rate
        # sits against the rounding lines.
        agrees = math.isfinite(fc) and abs(fc) >= 0.02 and (fc > 0) == (side == "up")
        lean += " (from the inputs)" if agrees else " (mostly rounding)"
    chips = [(f"Print bias: {lean}", P.negative if side == "up" else P.positive if side == "down" else None),
             (f"{ups} inputs push higher · {downs} lower · {len(drivers) - ups - downs} neutral", None)]
    if math.isfinite(prior) and math.isfinite(prior_u):
        base = min(max(prior_u, prior - 0.05), prior + 0.0499)
        up_by, down_by = prior + 0.05 - base, base - (prior - 0.05)
        chips.append((f"Rounding: +{up_by:.2f} prints {prior + 0.1:.2f}% · "
                      f"−{down_by:.2f} prints {prior - 0.1:.2f}%", None))
    actual = L.get("actual")
    if actual and math.isfinite(actual.get("new_print", float("nan"))):
        chips.append((f"Actual: {actual['new_u']:.2f}% (printed {actual['new_print']:.2f}%) · "
                      f"change {actual['y']:+.2f}".replace("-", "−"),
                      P.select))
    C.chips(chips)


def _probabilities(res, ctx: Ctx) -> None:
    L, P = res.live, ctx.P
    C.sub("Where the print lands", "probability of each printed rate")
    buckets = L.get("buckets")
    if not buckets:
        C.note("Not enough out-of-sample history yet to price the outcomes.")
        return
    n = len(buckets)
    rows = []
    for j, (level, b, p) in enumerate(buckets):
        label = f"{level:.2f}%"
        if j == 0:
            label = f"≤{label}"
        elif j == n - 1:
            label = f"≥{label}"
        tone = "higher" if b > 0 else "lower" if b < 0 else "unchanged"
        rows.append({"print": label, "p": p, "tone": tone, "order": j, "txt": f"{100 * p:.2f}%"})
    df = pd.DataFrame(rows)
    colour = alt.Color("tone:N", legend=None, scale=alt.Scale(
        domain=["lower", "unchanged", "higher"], range=[P.positive, P.muted, P.negative]))
    x = alt.X("print:N", sort=[r["print"] for r in rows], title=None, axis=alt.Axis(labelAngle=0))
    bars = alt.Chart(df).mark_bar(cornerRadiusTopLeft=2, cornerRadiusTopRight=2).encode(
        x=x, y=alt.Y("p:Q", axis=alt.Axis(format=".2%", title=None, tickCount=4)),
        color=colour, tooltip=[alt.Tooltip("print:N", title="Print"),
                               alt.Tooltip("p:Q", title="Probability", format=".2%")])
    text = alt.Chart(df).mark_text(dy=-6).encode(x=x, y="p:Q", text="txt:N")
    st.altair_chart(charts._apply(bars + text, P, 230), width="stretch", theme=None)


def _drivers(res, ctx: Ctx) -> None:
    P = ctx.P
    C.sub("What's driving it", "each input's push vs no change, pp · the forecast is their average")
    rows = []
    for d in res.drivers:
        if not math.isfinite(d["driver"]):
            continue
        obs, rel = d.get("obs"), d.get("released")
        rows.append({
            "input": d["label"], "driver": d["driver"],
            "tone": "higher" if d["driver"] > 0 else "lower",
            "value": f"{d['x']:.2f} {d['unit']}" if math.isfinite(d["x"]) else "—",
            "normal": f"{d['normal']:.2f}" if math.isfinite(d.get("normal", float("nan"))) else "—",
            "data": f"{obs:%d %b %Y}" if obs else "—",
            "released": f"{rel:%d %b %Y}" if rel else "—",
        })
    if not rows:
        C.note("No input has released data for this print yet.")
        return
    df = pd.DataFrame(rows)
    bars = alt.Chart(df).mark_bar(height=12).encode(
        y=alt.Y("input:N", sort=alt.SortField("driver", order="descending"), title=None),
        x=alt.X("driver:Q", title=None, axis=alt.Axis(format="+.2f", tickCount=5, tickMinStep=0.01)),
        color=alt.Color("tone:N", legend=None, scale=alt.Scale(
            domain=["lower", "higher"], range=[P.positive, P.negative])),
        tooltip=[alt.Tooltip("input:N", title="Input"),
                 alt.Tooltip("driver:Q", title="Push (pp)", format="+.2f"),
                 alt.Tooltip("value:N", title="Value"),
                 alt.Tooltip("normal:N", title="15y normal"),
                 alt.Tooltip("data:N", title="Data for"),
                 alt.Tooltip("released:N", title="Released")])
    rule = alt.Chart(pd.DataFrame({"x": [0]})).mark_rule(color=P.baseline).encode(x="x:Q")
    st.altair_chart(charts._apply(bars + rule, P, 26 * len(rows) + 20), width="stretch", theme=None)
    missing = [d["label"] for d in res.drivers if not math.isfinite(d["driver"])]
    if missing:
        C.note("Not yet available for this print: " + ", ".join(missing) + ".")


def _print_labels(prior: float) -> list[str]:
    from nextprint.model import BUCKETS

    labels = [f"{prior + b / 10:.2f}%" for b in BUCKETS]
    labels[0], labels[-1] = "≤" + labels[0], "≥" + labels[-1]
    return labels


def _street_reads(res) -> tuple[dict | None, list[dict]]:
    """The street's unemployment-rate consensus from the reaction cards: the
    one for the live print, and every past one that can be scored against
    what printed. A card is matched to a print by its release date."""
    from core import release_research

    cards = dict(store.load_release_cards())
    cards.update(st.session_state.get("release_cards") or {})
    by_day: dict[date, float] = {}
    for key, card in cards.items():
        day_s, _, release = str(key).partition("|")
        if release != econ_calendar.LABOUR or not isinstance(card, dict):
            continue
        research = card.get("research")
        if not isinstance(research, dict):
            continue
        try:
            e = release_research.from_dict(research).for_series(LEAD)
            by_day[date.fromisoformat(day_s)] = float(e.consensus) if e else None
        except (ValueError, TypeError, AttributeError):
            continue
    by_day = {d: v for d, v in by_day.items() if v is not None}

    def near(day: date | None) -> float | None:
        if day is None:
            return None
        hits = [(abs((d - day).days), v) for d, v in by_day.items() if abs((d - day).days) <= 7]
        return min(hits)[1] if hits else None

    live = None
    L = res.live
    if L.get("month") and (c := near(L.get("release"))) is not None:
        live = {"consensus": c}
    past = []
    for h in res.history:
        if h["live"] or not math.isfinite(h.get("new_print", float("nan"))):
            continue
        c = near(h.get("release"))
        if c is not None and h.get("probs"):
            past.append({**h, "consensus": c})
    return live, past


def _street(res, ctx: Ctx) -> None:
    """The model's odds against the street's number for the same print -- the
    role Kalshi's odds play in the US tab. The consensus comes from the
    reaction card's sourced search (Releases tab); nothing here searches."""
    from nextprint.model import BUCKETS

    P, L = ctx.P, res.live
    C.sub("Model vs street", "the consensus unemployment rate for the same print")
    if not L.get("buckets"):
        C.note("No model odds for this print yet.")
        return
    live, past = _street_reads(res)
    prior = L["prior_print"]
    probs = {b: p for _, b, p in L["buckets"]}
    if live is None:
        C.note("No consensus on file for this print. Open its reaction card on the Releases "
               "tab and run “Search consensus”; the street's number then lands here beside "
               "the model's odds.")
    else:
        cons = live["consensus"]
        k = int(max(BUCKETS[0], min(BUCKETS[-1], round((cons - prior) * 10))))
        above = sum(p for b, p in probs.items() if b > k)
        below = sum(p for b, p in probs.items() if b < k)
        labels = _print_labels(prior)
        C.stat_row([
            ("Street consensus", f"{cons:.1f}%", None),
            ("Model's likeliest", f"{L['likely_print']:.1f}%  ·  {_pct(L['likely_p'])}", P.accent),
            (f"P(print = {labels[BUCKETS.index(k)].lstrip('≤≥')})", _pct(probs[k]), None),
            ("P(above · below consensus)", f"{_pct(above)} · {_pct(below)}",
             P.negative if above > below + 0.1 else P.positive if below > above + 0.1 else None),
        ])
        lean = ("a higher print than the street" if above > below + 0.1
                else "a lower print than the street" if below > above + 0.1
                else "no clear side against the street")
        C.note(f"The model leans to {lean}. A print above consensus is the hawkish surprise "
               "for the strip only if it is a rise in the rate — read with the reaction card's "
               "playbook for the move the IB strip would make.")
    if past:
        hit_model = np.mean([abs(h["new_print"] - (h["prior_print"] + max(h["probs"], key=h["probs"].get) / 10)) < 0.05
                             for h in past])
        hit_street = np.mean([abs(h["new_print"] - h["consensus"]) < 0.05 for h in past])
        C.table(["", "Model", "Street"], [
            ["Likeliest print came in", _pct(float(hit_model)), _pct(float(hit_street))],
            ["Prints scored", len(past), len(past)],
        ], numeric=(1, 2))
        C.note(f"{len(past)} past print{'' if len(past) == 1 else 's'} with a consensus on file, "
               "matched by release date. Too few to call an edge either way until the cards "
               "build a history.")


def _track(res, ctx: Ctx) -> None:
    T, P = res.track, ctx.P
    first = T.get("first")
    C.sub("Track record", f"out of sample, every print since {first:%Y} · {T.get('n', 0)} months"
          if first else "out of sample")
    ev = "p < 0.01" if T.get("p_cw", 1) < 0.005 else f"p = {T.get('p_cw', float('nan')):.2f}"
    C.stat_row([
        ("Error cut vs no change", _pct(T.get("er", float("nan")), signed=True),
         P.good if T.get("er", 0) > 0 else P.critical),
        ("Direction right when it moved", f"{_pct(T.get('dir', float('nan')))}  "
                                          f"(base rate {_pct(T.get('dir_base', float('nan')))})", None),
        ("Probability skill", _pct(T.get("brier_skill", float("nan")), signed=True), None),
        ("Beats no change (Clark–West)", ev, None),
    ])
    head, ctl = st.columns([2.4, 1], vertical_alignment="bottom")
    with head:
        C.sub("Forecast vs actual", "unemployment rate, % · forecast made the day before "
                                    "each print, with 70% and 90% error bands")
    with ctl:
        window = C.history_window("nextprint_window", options=("5Y", "10Y", "All"))
    start = C.window_start(window, ctx.as_of)
    _versus(res, ctx, start)
    rolling = [(m, er) for m, er in res.rolling if start is None or m >= start]
    if rolling:
        C.sub("Edge over time", "error cut vs no change, trailing 5 years")
        df = pd.DataFrame(rolling, columns=["month", "er"])
        df["month"] = pd.to_datetime(df["month"])
        line = alt.Chart(df).mark_line(color=P.accent, strokeWidth=1.6).encode(
            x=alt.X("month:T", title=None, scale=_x_domain(res, start) or alt.Undefined,
                    axis=_x_axis(start, ctx.as_of)),
            y=alt.Y("er:Q", title=None, axis=alt.Axis(format=".2%", tickCount=4)),
            tooltip=[alt.Tooltip("month:T", title="5 years to", format="%b %Y"),
                     alt.Tooltip("er:Q", title="Error cut vs no change", format=".2%")])
        zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color=P.baseline).encode(y="y:Q")
        st.altair_chart(charts._apply(zero + line, P, 170), width="stretch", theme=None)
        C.note("Error cut vs no change over the trailing 5 years. The edge is largest "
               "around turning points (1990-91, 2008-09) and small in calm "
               "stretches, so the bias matters most when the inputs agree strongly.")


def _x_axis(start: date | None, as_of: date) -> alt.Axis:
    step = 1 if start and as_of.year - start.year <= 10 else 5
    return alt.Axis(format="%Y", tickCount={"interval": "year", "step": step})


def _x_domain(res, start: date | None) -> alt.Scale | None:
    """The forecast-vs-print chart's span, so the edge chart below lines up with it."""
    months = [h["month"] for h in res.history if start is None or h["month"] >= start]
    if not months:
        return None
    lo, hi = min(months), max(months)
    return alt.Scale(domain=[alt.DateTime(year=lo.year, month=lo.month, date=1),
                             alt.DateTime(year=hi.year, month=hi.month, date=28)])


def _versus(res, ctx: Ctx, start: date | None) -> None:
    """Each month's forecast of the unemployment rate against the rate that
    printed, with the forecast's own error bands. The bands are the same
    past misses that price the print odds, so the chart and the odds agree;
    their coverage is scored below the chart."""
    P = ctx.P
    rows = [h for h in res.history if start is None or h["month"] >= start]
    if not rows:
        C.note("No scored months in this window.")
        return
    # The odds dictionaries and release days are for the market panel, not the chart.
    df = pd.DataFrame(rows).drop(columns=["probs", "release"], errors="ignore")
    df["month"] = pd.to_datetime(df["month"])
    df["end"] = df["month"] + pd.Timedelta(days=26)           # each month's hover column
    df["mid"] = df["month"] + pd.Timedelta(days=13)
    # Break lines and bands across excluded stretches (COVID) rather than bridge them.
    df["segment"] = (df["month"].diff() > pd.Timedelta(days=45)).cumsum()
    # The actual line is the unrounded rate (two valid decimals, from the
    # unemployed and labour-force levels) -- what the misses are scored on. The
    # printed rate only has one: the ABS publishes it to 0.1.
    printed = df["new_print"].notna() & df["new_u"].notna()
    banded = df["lo70"].notna()
    df["t_print"] = [f"{u:.2f}%  (printed {n:.2f}%)" if ok else "not out yet"
                     for u, n, ok in zip(df["new_u"], df["new_print"], printed)]
    df["t_fc"] = [f"{lv:.2f}%  ({_pp(f)})" for lv, f in zip(df["fc_level"], df["forecast"])]
    df["t_b70"] = [f"{lo:.2f}–{hi:.2f}%" if ok else "—"
                   for lo, hi, ok in zip(df["lo70"], df["hi70"], banded)]
    df["t_b90"] = [f"{lo:.2f}–{hi:.2f}%" if ok else "—"
                   for lo, hi, ok in zip(df["lo90"], df["hi90"], banded)]
    df["t_miss"] = [_pp(a - f) if ok else "—"
                    for a, f, ok in zip(df["new_u"], df["fc_level"], printed)]
    for k in ("p_up", "p_flat", "p_down"):
        if k not in df:
            df[k] = float("nan")
    df["t_odds"] = [f"{_pct(u)} higher · {_pct(f)} same · {_pct(d)} lower"
                    if math.isfinite(u) else "—"
                    for u, f, d in zip(df["p_up"], df["p_flat"], df["p_down"])]

    scale = _x_domain(res, start) or alt.Undefined
    x = alt.X("month:T", title=None, scale=scale, axis=_x_axis(start, ctx.as_of))
    x_mid = alt.X("mid:T", title=None, scale=scale, axis=_x_axis(start, ctx.as_of))
    y_axis = alt.Axis(format=".1f", tickCount=5)

    def band(c: int, opacity: float) -> alt.Chart:
        return alt.Chart(df[banded]).mark_area(color=P.accent, opacity=opacity).encode(
            x=x_mid, y=alt.Y(f"lo{c}:Q", title=None, axis=y_axis, scale=alt.Scale(zero=False)),
            y2=f"hi{c}:Q", detail="segment:N")

    actual = alt.Chart(df[printed]).mark_line(color=P.ink, strokeWidth=1.4).encode(
        x=x_mid, y="new_u:Q", detail="segment:N")
    line = alt.Chart(df).mark_line(color=P.accent, strokeWidth=1.6).encode(
        x=x_mid, y="fc_level:Q", detail="segment:N")
    live = alt.Chart(df[df["live"]]).mark_point(
        shape="circle", size=70, filled=True, fill=P.surface, stroke=P.accent, strokeWidth=1.8,
    ).encode(x="mid:T", y="fc_level:Q")
    tip = [alt.Tooltip("month:T", title="Month", format="%b %Y"),
           alt.Tooltip("t_print:N", title="Actual rate"),
           alt.Tooltip("t_fc:N", title="Model said"),
           alt.Tooltip("t_b70:N", title="70% band"),
           alt.Tooltip("t_b90:N", title="90% band"),
           alt.Tooltip("t_miss:N", title="Model miss"),
           alt.Tooltip("t_odds:N", title="Model odds")]
    # One invisible full-height column per month carries the tooltip.
    hover = alt.Chart(df).mark_rect(opacity=0).encode(x=x, x2="end:T", tooltip=tip)
    st.altair_chart(charts._apply(band(90, 0.14) + band(70, 0.26) + line + actual + live + hover,
                                  P, 260),
                    width="stretch", theme=None)
    C.legend([("Actual rate (unrounded)", P.ink, "━"), ("Model forecast", P.accent, "━"),
              ("70% band", P.accent + "73"), ("90% band", P.accent + "33"),
              ("Next print", P.accent, "○")])

    scored = df[printed & banded & ~df["live"]]
    if len(scored) >= 12:
        y, f = scored["actual_u"], scored["forecast"]
        rm, rb = math.sqrt(((y - f) ** 2).mean()), math.sqrt((y ** 2).mean())
        inside = {c: float(((scored[f"lo{c}"] <= scored["new_u"])
                            & (scored["new_u"] <= scored[f"hi{c}"])).mean()) for c in (70, 90)}
        whole = res.track.get("coverage") or {}
        overall = ""
        if whole:
            (c70, n), (c90, _) = whole[0.7], whole[0.9]
            overall = (f" Over all {n} prints since {res.track['first']:%Y}: {_pct(c70)} and "
                       f"{_pct(c90)}.")
        C.note(f"Over these {len(scored)} months the actual rate fell inside the 70% band in "
               f"{_pct(inside[70])} of them and inside the 90% band in {_pct(inside[90])}."
               f"{overall} Bands that fit should hold about 70% and 90%. The model's error was "
               f"{_pct(abs(1 - rm / rb))} {'smaller' if rm <= rb else 'larger'} than no "
               "change's. "
               "How the bands are built: the unrounded last rate plus the forecast, plus the "
               "model's own misses on the prints of the previous 15 years. Each miss is "
               "rescaled to today's rate for survey noise, re-centred on zero, and read at "
               "its 5/15/85/95th percentiles, so fat tails in past misses carry through.")


def _evidence(p: float) -> str:
    if not math.isfinite(p):
        return "—"
    return "strong" if p < 0.05 else "weak" if p < 0.10 else "none"


def _inputs(res) -> None:
    C.sub("Which inputs lead the print",
          "each input on its own vs no change · error cut, higher is better")
    rows = []
    for r in res.table:
        rows.append([
            r["label"],
            _pct(r["er_long"], signed=True), _pct(r["er_common"], signed=True),
            f"{_pct(r['dir'])} ({_pct(r['dir_base'])})",
            _evidence(r["p_cw"]), "✓" if r["tied"] else "",
            f"{r['first']:%Y}" if r["first"] else "—",
        ])
    C.table(["Input", "Since 1990", "Since 2016", "Direction", "Evidence", "Tied", "From"],
            rows, numeric=(1, 2, 3))
    C.note("Since 1990 / 2016: error cut vs no change (blank for inputs that start later; "
           "since 2016 compares every input on the same months). Direction: right when the "
           "rate moved, with the base rate in brackets. "
           "Evidence: whether the input carries real information at all "
           "(Clark–West test; strong p<0.05, weak p<0.10) — a small error cut can still be "
           "real, it is just small. "
           "Tied best: the best input does not beat it at the 10% level (one-sided Diebold–Mariano) — the data cannot tell these apart "
           "from the best one. The forecast averages ALL inputs regardless, so this table "
           "never feeds back into it.")


def _challenger(res) -> None:
    """The ridge challenger's scorecard. It never moves the forecast; it only
    earns a switch on prints it has not seen."""
    ch = res.challenger
    if not ch:
        return
    reg = ch["registered"]
    C.sub("Challenger", f"ridge on all inputs · registered {reg:%d %b %Y} · error cut, higher is better")

    def row(label: str, s: dict) -> list:
        return [label, s["n"], _pct(s["er"], signed=True), _pct(s["er_bench"], signed=True),
                "—" if not math.isfinite(s["p"]) else f"{s['p']:.2f}"]

    C.table(["Sample", "Prints", "vs average", "vs no change", "p"],
            [row("Backtest since 1990 (seen)", ch["backtest"]),
             row(f"Since {reg:%d %b %Y} (unseen)", ch["unseen"])], numeric=(1, 2, 3, 4))
    n, need = ch["unseen"]["n"], ch["min_prints"]
    verdict = {
        "collecting": f"Collecting unseen prints: {n} of {need}.",
        "switch": "The challenger has beaten the average on unseen prints: switch to it.",
        "keep": "The challenger has not beaten the average on unseen prints: keep the average.",
    }[ch["verdict"]]
    fc = res.live.get("challenger", float("nan"))
    C.note(f"{verdict} It would say {_pp(fc)} for this print. It regresses the change on "
           "every input at once (standardised, penalty fixed in advance, never tuned) "
           "rather than averaging one-input forecasts. Its backtest looked better, but that "
           "was seen when it was picked, so it replaces the average only after "
           f"{need} later prints with a lower error and a one-sided Diebold–Mariano "
           f"p below {ch['p_needed']:.2f}.")


def _method(res) -> None:
    from nextprint import inputs as I
    from nextprint import releases as R

    with st.expander("How it works, data timing and caveats"):
        st.markdown(
            "- **Forecast day.** The day before each Labour Force release. An input counts "
            "only if it was released on an earlier calendar day. Release days are exact "
            "from the ABS release pages from December 2019; before that the release is "
            "dated the second Thursday of the following month — the earliest it has come — "
            "and every input's own rule errs late, so nothing can leak.\n"
            "- **Target.** The change in the unemployment rate, unrounded (unemployed over "
            "the labour force, seasonally adjusted). Probabilities round to 0.1 as the ABS "
            "does, from the unrounded last rate.\n"
            "- **Models.** Baseline = no change. Each input's push = its slope x (its value "
            f"minus its own {I.WINDOW_YEARS}-year normal); slopes refit every month on the "
            f"last {I.WINDOW_YEARS} years. Forecast = the plain average of the pushes.\n"
            "- **Direction** is scored only on months the rate moved; the base rate is what "
            "always calling the more common direction would have scored.\n"
            "- **Excluded.** COVID (2020-03 to 2022-03): JobKeeper held people in jobs "
            "while hours collapsed, so the rate's moves were not the labour market's.\n"
            "- **Vintages.** Australia has no ALFRED, and the ABS keeps past releases online "
            "only from December 2019 — too short for a 15-year window. So the target and "
            "the inputs are today's vintage. The surveys revise only with seasonal factors; "
            "the Labour Force's seasonally adjusted rate revises by 0.0–0.1pp as its "
            "factors update, which can move a published-change bucket at the margin.\n"
            "- **Probability spread.** Most of a month's miss is Labour Force sampling error "
            "(one-eighth of the sample rotates each month), whose size runs with "
            "√(u(1−u)). Past misses are rescaled to today's rate before they price the "
            "print.\n"
            "- **The Australian inputs** are the closest free equivalent of each US channel: "
            "NAB business conditions (ISM / regional Fed surveys), Westpac-MI sentiment "
            "(UMich), job vacancies (JOLTS hires), employment growth (payrolls), and the "
            "underemployment rate as the slack channel.\n"
            "- **Not available.** Weekly jobless claims (Australia has none; JobSeeker "
            "counts start in 2020); the flow gap (the gross flows are unadjusted and cover "
            "the matched sample only, and adjusting them needs future data); job ads "
            "(ANZ-Indeed is commercial); a prediction market on the print — so the model is "
            "set against the street's consensus instead.")
        timing = []
        for d in res.drivers:
            inp = I.BY_KEY[d["key"]]
            obs, rel = d.get("obs"), d.get("released")
            timing.append([inp.label, f"{obs:%d %b %Y}" if obs else "—",
                           f"{rel:%d %b %Y}" if rel else "—",
                           f"{d.get('raw', float('nan')):.2f}" if d.get("raw") is not None else "—",
                           R.RULE_TEXT[inp.timing]])
        C.sub("Data used for this forecast")
        C.table(["Input", "Latest data", "Released", "Value", "How the date is known"], timing)
        rel_rows = res.track.get("reliability") or []
        if rel_rows:
            C.sub("Are the probabilities honest?", "stated P(higher) vs what happened, by quintile")
            C.table(["Model said P(higher)", "Went higher", "Months"],
                    [[_pct(a), _pct(b), n] for a, b, n in rel_rows], numeric=(0, 1, 2))
        C.sub("Robustness of the edge", "error cut vs no change")
        C.table(["Variant", "Error cut"], [[r["label"], _pct(r["er"], signed=True)]
                                           for r in res.robustness], numeric=(1,))
        _challenger(res)
        if st.button("Refresh next-print data"):
            from nextprint import fetch

            fetch.clear()
            _results().clear()
            st.rerun()
