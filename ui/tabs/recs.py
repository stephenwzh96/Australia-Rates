"""Trade recs: three trades from the whole desk, one per horizon.

A research agent (Claude Opus 5.5 through the Claude Agent SDK, with web
search) reads a brief built from every tab's live numbers (`ui.recs_brief`)
plus what it finds on the web, and returns one trade for the next tier-1
print, one to the next RBA decision, and one for two to three months -- each with
levels and its reasons in two kinds, fundamental and quantitative.

Runs only on the button: a run is billed and takes minutes. Every run is saved
(`state.store.save_recs_run`) with the brief it was given, so an old
recommendation can always be read against the numbers it was made on.
"""

from __future__ import annotations

import hashlib
import html
import time
from datetime import date, datetime

import streamlit as st

from core import clock, trade_recs as tr
from data import agent as claude_agent
from state import store
from ui import components as C
from ui import recs_brief
from ui.context import Ctx

MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5")
EFFORTS = ("high", "xhigh", "medium")
TIMEOUTS = (15, 20, 30, 45)
CACHE_TTL = 3600
STALE_HOURS = 24

_DIRECTION_WORD = {"receive": "Receive", "pay": "Pay", "steepener": "Steepener",
                   "flattener": "Flattener", "other": "Trade"}


@st.cache_resource
def _run_cache() -> dict:
    """Hand-rolled, like the speech roundup's: keyed on the exact brief, so a
    rerun a minute later is free and a changed number asks again."""
    return {}


def _esc(s: object) -> str:
    return html.escape(str(s or ""))


# -------------------------------------------------------------------- render

def render(ctx: Ctx) -> None:
    C.heading("Trade recs", "three trades from every tab's numbers · Claude Opus 5.5 with "
                            "live web research", help=(
        "", "The strategist is given a brief built from this app's own live figures -- the "
            "IB and IR strips, the calendar, the vote count, the SMP, the street calls, "
            "macro, labour, inflation, growth and the next-print model -- then searches the "
            "web for what the app cannot see: RBA speakers, previews and consensus, the "
            "newest bank notes. It returns one trade per horizon, each with levels, fundamental and "
            "quantitative reasons, and the observable that would prove it wrong.",
        "", "recs"))

    history = store.load_recs_history()
    ok, why = claude_agent.available()
    run = _controls(ctx, history, ok)
    if not ok:
        st.info(why)

    if run is None:
        spans = recs_brief.spans(ctx)
        st.markdown(_ruler(spans, None, ctx.as_of), unsafe_allow_html=True)
        st.markdown(_empty(spans), unsafe_allow_html=True)
        return
    _show(ctx, run)


def _label(entry: dict) -> str:
    try:
        when = datetime.fromisoformat(entry["stamp"]).strftime("%d %b %H:%M")
    except (KeyError, ValueError):
        when = "an earlier run"
    return f"{when} · {entry.get('model', '')}"


def _controls(ctx: Ctx, history: list[dict], agent_ok: bool) -> dict | None:
    """The run picker and the generate controls. Returns the run to show."""
    # A fresh run lands at the top of the history; point the picker there
    # before it is drawn (its key cannot be written once it exists).
    if st.session_state.pop("_recs_new", False):
        st.session_state.pop("recs_view", None)
    c_hist, c_model, c_eff, c_time, c_go = st.columns([1.5, 1.15, 0.8, 0.8, 1.2],
                                                      vertical_alignment="bottom")
    with c_hist:
        options = list(range(len(history)))
        pick = st.selectbox("Run", options or [None], key="recs_view",
                            format_func=lambda i: "No saved runs" if i is None
                            else ("Latest · " if i == 0 else "") + _label(history[i]),
                            disabled=not history,
                            help="Every run is kept with the brief it was given.")
    with c_model:
        model = st.selectbox("Model", MODELS, key="recs_model",
                             help="Claude Opus 5.5 by default, through the Claude Agent SDK.")
    with c_eff:
        effort = st.selectbox("Effort", EFFORTS, key="recs_effort",
                              help="How hard the model thinks. Higher is slower and costs more.")
    with c_time:
        st.session_state.setdefault("recs_timeout", 20)
        minutes = st.selectbox("Timeout", TIMEOUTS, key="recs_timeout",
                               format_func=lambda m: f"{m} min")
    with c_go:
        go = st.button("Generate recommendations", type="primary", width="stretch",
                       disabled=not agent_ok, key="recs_go")

    if go:
        fresh = _generate(ctx, model, effort, int(minutes) * 60)
        if fresh is not None:
            st.session_state["_recs_new"] = True
            return fresh
    if pick is None or not history:
        return None
    return history[pick]


def _generate(ctx: Ctx, model: str, effort: str, timeout: int) -> dict | None:
    with st.status("Assembling the desk brief…", expanded=False) as status:
        brief = recs_brief.build(ctx, on_section=lambda t: status.update(
            label=f"Assembling the desk brief · {t}…"))
        prompt = tr.prompt(brief.text, ctx.as_of, brief.spans)
        key = (hashlib.sha256(prompt.encode("utf-8")).hexdigest(), model, effort,
               tr.PROMPT_VERSION)
        cache = _run_cache()
        answered, cached = cache.get(key, (0.0, None))
        if cached is not None and time.monotonic() - answered < CACHE_TTL:
            status.update(label="Same brief as a run in the last hour — showing it.",
                          state="complete")
            return cached
        status.update(label=f"The strategist is reading the brief and researching the web "
                            f"({model}, {effort} effort) — this takes several minutes…")
        t0 = time.monotonic()
        raw, err, meta = claude_agent.research_structured(
            prompt, tr.SYSTEM, tr.SCHEMA, model=model, effort=effort, timeout=timeout)
        recs = tr.Recs(ctx.as_of, error=err) if err else tr.parse(raw, ctx.as_of)
        if not recs.ok:
            status.update(label="The run did not produce recommendations.", state="error")
            if claude_agent.needs_sign_in(recs.error):
                st.error(recs.error)
                st.code("claude", language="bash")
            else:
                st.warning(recs.error or "No usable recommendation came back.")
            return None
        record = {
            "stamp": clock.stamp(), "as_of": ctx.as_of.isoformat(), "meeting": ctx.key,
            "model": model, "effort": effort, "prompt_version": tr.PROMPT_VERSION,
            "seconds": meta.get("seconds") or round(time.monotonic() - t0),
            "cost_usd": meta.get("cost_usd"), "turns": meta.get("turns"),
            "brief": brief.text, "missing": brief.missing,
            "spans": [{"key": h.key, "label": h.label, "ends": h.ends.isoformat(),
                       "anchor": h.anchor} for h in brief.spans],
            "recs": tr.to_dict(recs),
        }
        store.save_recs_run(record)
        cache[key] = (time.monotonic(), record)
        status.update(label=f"Done in {C.fmt_secs(time.monotonic() - t0)}.", state="complete")
    return record


def _spans_from(run: dict) -> list[tr.Horizon]:
    out = []
    for s in run.get("spans") or []:
        try:
            out.append(tr.Horizon(s["key"], s["label"], date.fromisoformat(s["ends"]),
                                  s.get("anchor", "")))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _show(ctx: Ctx, run: dict) -> None:
    recs = tr.from_dict(run.get("recs") or {})
    spans = _spans_from(run)
    run_as_of = recs.as_of
    st.markdown(_meta_line(run, recs, ctx.as_of), unsafe_allow_html=True)
    if recs.regime or recs.desk_view:
        st.markdown(_view(recs), unsafe_allow_html=True)
    st.markdown(_ruler(spans, recs, run_as_of), unsafe_allow_html=True)
    for rec in recs.recs:
        span = next((s for s in spans if s.key == rec.horizon), None)
        st.markdown(_ticket(rec, span), unsafe_allow_html=True)
    if recs.dropped:
        C.note("Left out of this run: " + "; ".join(recs.dropped) + ".")
    with st.expander("What the strategist was given"):
        missing = run.get("missing") or []
        C.note("The brief, exactly as sent. Every figure is the dashboard's own, capped at "
               f"{run_as_of:%d %b %Y}."
               + (f" Sections that failed to load: {', '.join(missing)}." if missing else ""))
        st.code(run.get("brief") or "(not saved with this run)", language=None,
                wrap_lines=True, height=420)
    C.note("Model-generated trade ideas for the desk to test, not advice. Check every level "
           "against the Curve tab before acting on it.")


# ------------------------------------------------------------------- pieces

def _meta_line(run: dict, recs: tr.Recs, as_of: date) -> str:
    try:
        made = datetime.fromisoformat(run["stamp"])
    except (KeyError, ValueError):
        made = None
    bits = []
    if made:
        bits.append(f"Generated {made:%a %d %b, %H:%M}")
    bits.append(f"numbers as of {recs.as_of:%d %b %Y}")
    bits.append(str(run.get("model") or ""))
    if run.get("effort"):
        bits.append(f"{run['effort']} effort")
    if run.get("seconds"):
        bits.append(C.fmt_secs(float(run["seconds"])))
    if run.get("cost_usd") is not None:
        bits.append(f"${float(run['cost_usd']):,.2f}")
    stale = ""
    if made is not None:
        hours = (clock.now().replace(tzinfo=None) - made).total_seconds() / 3600
        if recs.as_of != as_of or hours >= STALE_HOURS:
            age = f"{hours / 24:.0f} days" if hours >= 48 else f"{hours:.0f} hours"
            stale = (f'<span class="rx-stale">{age} old — prices have moved since; '
                     f'generate again before trading it</span>')
    return (f'<div class="rx-meta">{" · ".join(_esc(b) for b in bits if b)}{stale}</div>')


def _view(recs: tr.Recs) -> str:
    return ('<div class="rx-view">'
            + (f'<div class="rx-eyebrow">Regime call</div>'
               f'<div class="rx-regime">{_esc(recs.regime)}</div>' if recs.regime else "")
            + (f'<p class="rx-desk">{_esc(recs.desk_view)}</p>' if recs.desk_view else "")
            + "</div>")


def _dir_class(direction: str) -> str:
    return {"receive": "rx-rec", "pay": "rx-pay"}.get(direction, "rx-curve")


def _ruler(spans: list[tr.Horizon], recs: tr.Recs | None, start: date) -> str:
    """Today to the medium horizon's end on one hairline, each horizon pinned
    at its real exit date -- the spacing is the calendar's, not a layout's."""
    if not spans:
        return ""
    end = max(s.ends for s in spans)
    total = max((end - start).days, 1)
    pins = []
    for s in spans:
        pos = min(max((s.ends - start).days / total, 0.0), 1.0) * 100
        rec = recs.for_horizon(s.key) if recs else None
        cls = _dir_class(rec.direction) if rec else "rx-open"
        what = (f'{_esc(_DIRECTION_WORD.get(rec.direction, "Trade"))} · '
                f'{_meter(rec.conviction, small=True)}' if rec else "to come")
        edge = " near-start" if pos < 12 else " near-end" if pos > 88 else ""
        pins.append(
            f'<div class="rx-pin {cls}{edge}" style="left:{pos:.2f}%">'
            f'<span class="rx-pin-mark"></span>'
            f'<span class="rx-pin-date">{s.ends:%a %d %b}</span>'
            f'<span class="rx-pin-name">{_esc(s.label)}</span>'
            f'<span class="rx-pin-what">{what}</span></div>')
    return (f'<div class="rx-ruler" role="img" aria-label="Horizons from {start:%d %b} '
            f'to {end:%d %b}"><div class="rx-rule"></div>'
            f'<div class="rx-today"><span>{"today" if start == clock.today() else "as of"} · '
            f'{start:%d %b}</span></div>{"".join(pins)}</div>')


def _meter(conviction: int, small: bool = False) -> str:
    bars = "".join(f'<i class="{"on" if i < conviction else ""}"></i>' for i in range(5))
    return (f'<span class="rx-meter{" sm" if small else ""}" '
            f'title="Conviction {conviction} of 5">{bars}</span>')


def _levels(rec: tr.Rec) -> str:
    """Stop, entry and target on one scale, so reward and risk read as
    distances before they read as numbers."""
    e, t, s = rec.entry_bp, rec.target_bp, rec.stop_bp
    if e is None or t is None or s is None or not rec.levels_coherent:
        return ""
    lo, hi = min(e, t, s), max(e, t, s)
    span = (hi - lo) or 1.0

    def at(x: float) -> float:
        return 4 + 92 * (x - lo) / span

    reward = f'left:{min(at(e), at(t)):.1f}%;width:{abs(at(t) - at(e)):.1f}%'
    risk = f'left:{min(at(e), at(s)):.1f}%;width:{abs(at(s) - at(e)):.1f}%'
    return (f'<div class="rx-scale">'
            f'<span class="rx-seg reward" style="{reward}"></span>'
            f'<span class="rx-seg risk" style="{risk}"></span>'
            f'<span class="rx-tick stop" style="left:{at(s):.1f}%"><b>{s:g}</b>stop</span>'
            f'<span class="rx-tick entry" style="left:{at(e):.1f}%"><b>{e:g}</b>entry</span>'
            f'<span class="rx-tick target" style="left:{at(t):.1f}%"><b>{t:g}</b>target</span>'
            f'</div>')


def _ticket(rec: tr.Rec, span: tr.Horizon | None) -> str:
    """One ticket, flattened to a single line: Markdown reads an indented line
    inside an HTML block as a code block."""
    return "".join(line.strip() for line in _ticket_html(rec, span).splitlines())


def _ticket_html(rec: tr.Rec, span: tr.Horizon | None) -> str:
    when = (f"{span.ends:%a %d %b}" if span else
            f"{rec.exit_by:%a %d %b}" if rec.exit_by else "")
    anchor = _esc(span.anchor) if span else _esc(rec.catalyst)
    rr = rec.reward_risk
    stats = []
    if rec.reward_bp is not None:
        stats.append(f'<span><b>{rec.reward_bp:.1f}</b>bp to target</span>')
    if rec.risk_bp is not None:
        stats.append(f'<span><b>{rec.risk_bp:.1f}</b>bp to stop</span>')
    if rr is not None:
        stats.append(f'<span><b>{rr:.1f}</b>: 1 reward : risk</span>')
    warn = ("" if rec.levels_coherent else
            '<div class="rx-warn">Target and stop sit on the same side of entry — '
            'the levels do not describe one trade.</div>')
    fund = "".join(f"<li>{_esc(p.text)}</li>" for p in rec.fundamental)
    quant = "".join(f'<li><span class="rx-fig">{_esc(p.figure)}</span>'
                    f'<span class="rx-read">{_esc(p.text)}</span></li>'
                    for p in rec.quantitative)
    risks = "".join(f"<li>{_esc(p.text)}</li>" for p in rec.risks)
    return f"""
<article class="rx-ticket {_dir_class(rec.direction)}">
  <section class="rx-stub">
    <div class="rx-eyebrow">{_esc(rec.horizon_label)}</div>
    <div class="rx-when">{when}<span> · {anchor}</span></div>
    <div class="rx-side">{_esc(_DIRECTION_WORD.get(rec.direction, "Trade"))}</div>
    <h3 class="rx-title">{_esc(rec.title)}</h3>
    <div class="rx-expr">{_esc(rec.expression)}</div>
    <dl class="rx-levels">
      <div><dt>Entry</dt><dd>{_esc(rec.entry)}</dd></div>
      <div><dt>Target</dt><dd>{_esc(rec.target)}</dd></div>
      <div><dt>Stop</dt><dd>{_esc(rec.stop)}</dd></div>
    </dl>
    {_levels(rec)}{warn}
    <div class="rx-stats">{"".join(stats)}</div>
    <div class="rx-conv">{_meter(rec.conviction)}<span>conviction {rec.conviction}/5</span></div>
    <div class="rx-size">{_esc(rec.sizing)}</div>
  </section>
  <section class="rx-case">
    <p class="rx-thesis">{_esc(rec.thesis)}</p>
    <div class="rx-cols">
      <div><div class="rx-eyebrow">Fundamental case</div><ul class="rx-fund">{fund}</ul></div>
      <div><div class="rx-eyebrow">Quantitative case</div><ul class="rx-quant">{quant}</ul></div>
    </div>
    <div class="rx-foot">
      <div><div class="rx-eyebrow">What hurts it</div><ul class="rx-risks">{risks or "<li>—</li>"}</ul></div>
      <div class="rx-wrong"><div class="rx-eyebrow">Wrong if</div><p>{_esc(rec.wrong_if) or "—"}</p></div>
    </div>
  </section>
</article>"""


def _empty(spans: list[tr.Horizon]) -> str:
    items = "".join(f"<li><b>{_esc(s.label)}</b> — exit by {s.ends:%a %d %b} "
                    f"<span>({_esc(s.anchor)})</span></li>" for s in spans)
    return (f'<div class="rx-empty"><p>No recommendations yet. Generate them and the '
            f'strategist reads every tab\'s numbers, searches the web for what the app '
            f'cannot see, and comes back with three trades:</p><ul>{items}</ul>'
            f'<p class="rx-empty-note">A run takes several minutes and is billed. Each one '
            f'is saved with the brief it was given.</p></div>')
