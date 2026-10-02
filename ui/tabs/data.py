"""Data: health of every external source, in one place."""

from __future__ import annotations

import streamlit as st

from core import employment as empmod
from core import macro as macro_mod
from data import abs_sdmx, asx, rba
from ui import charts
from ui import components as C
from ui import sources as src
from ui.context import Ctx


def _status(ok: bool, P) -> str:
    return P.good if ok else P.critical


def render(ctx: Ctx) -> None:
    head, btn = st.columns([3, 1], vertical_alignment="bottom")
    with head:
        C.section("Data sources", "every fetch degrades to blank on failure; any value can be typed")
    with btn:
        if st.button("Refresh all sources", width="stretch"):
            src.refresh_curve_quotes()
            src.refresh_all_sources()
            st.rerun()

    left, right = st.columns(2, gap="large")
    with left:
        _market(ctx)
        _rba(ctx)
        _calendar(ctx)
    with right:
        _labour(ctx)
        _cpi(ctx)
        _macro(ctx)
    for extra in _EXTRA_PANELS:
        extra(ctx)
    C.note("No API key is needed for any source, a real difference from the US version, which "
           "needs a FRED key. The trade-off is that the ASX endpoint is undocumented — the JSON "
           "behind the exchange's own price pages — so the last good strip is cached to disk "
           "and served if it ever disappears.")


#: Panels later ports add (growth, SMP, expectations ...), drawn full width.
_EXTRA_PANELS: list = []


def register(panel) -> None:
    if panel not in _EXTRA_PANELS:
        _EXTRA_PANELS.append(panel)


def _market(ctx: Ctx) -> None:
    P, mk = ctx.P, ctx.market
    C.sub("ASX interest rate futures")
    for label, quotes, ref in (("IB — 30 day interbank cash", mk.ib_quotes, mk.ib_ref),
                               ("IR — 90 day bank bills", mk.ir_quotes, mk.ir_ref)):
        live = [q for q in quotes if q.ok]
        C.data_source_header(label, bool(live), f"{len(live)} of {len(quotes)} contracts live"
                             + (f" · settled {ref:%d %b %Y}" if ref else ""), P)
    with st.expander("Both strips, contract by contract"):
        C.table(["Contract", "Settlement", "Implied", "Chg", "Bid/ask", "Volume", "Flag"],
                [[q.code, "—" if q.price is None else f"{q.price:.3f}",
                  "—" if q.implied_rate is None else f"{q.implied_rate:.3f}%",
                  "—" if q.change_bp is None else f"{q.change_bp:+.1f}bp",
                  "—" if q.spread_bp is None else f"{q.spread_bp:.1f}bp",
                  f"{q.volume:,.0f}" if q.volume else "0",
                  "stale" if asx.is_stale(q, mk.ib_ref if q.code.startswith("IB") else mk.ir_ref)
                  else ""] for q in mk.ib_quotes + mk.ir_quotes],
                numeric=(1, 2, 3, 4, 5), nowrap=True)
    C.note("Settlement is ASX's official daily mark, used in preference to the last trade, which "
           "on the back of the IB strip can be weeks old and up to 17bp away. The feed's "
           "dateExpiry is two days wrong on every contract, so delivery months come off the "
           "contract code instead.")


def _rba(ctx: Ctx) -> None:
    P = ctx.P
    spot = ctx.market.spot
    C.sub("RBA statistical tables")
    ok = spot["cash"] is not None
    bbsw = "unknown" if spot["bbsw"] is None else f"{spot['bbsw']}%"
    gap = "unknown" if spot["gap_bp"] is None else f"{spot['gap_bp']:+.1f}bp"
    C.data_source_header("F1 — money market rates", ok,
                         f"cash rate {spot['cash']}%, 3-month BBSW {bbsw}, AONIA gap {gap}"
                         if ok else "unavailable", P)
    hist = src.kill_series_history()
    labels = {"BBSW_CASH_SPREAD": "BBSW − cash target (bp)", "CASH_GAP_ABS": "|AONIA − target| (bp)",
              "BBSW_14D_MOVE": "BBSW 14-day move (bp)"}
    cols = st.columns(3, gap="small")
    for col, (k, label) in zip(cols, labels.items()):
        with col:
            obs = hist.get(k) or []
            C.note(f"{label} · {obs[-1].value:.1f}" if obs else f"{label} · —")
            if obs:
                st.altair_chart(charts.sparkline(obs, None, False, P), width="stretch", theme=None)
    for name, code in (("H4 — wages and prices", "h4"), ("H5 — labour force", "h5")):
        t = src.rba_table(code)
        last = max((s[-1][0] for s in t.values() if s), default=None)
        C.data_source_header(name, bool(t), f"{len(t)} series · latest {last:%b %Y}"
                             if last else "unavailable", P)
    with st.expander("Discontinued series"):
        C.table(["Series", "Label", "Last published"],
                [[s.code, s.label, s.discontinued.isoformat()] for s in rba.CATALOGUE if not s.live])
        C.note("The RBA retired its OIS series in December 2022 — why the bill basis on the Curve "
               "tab is measured against the IB strip, which is an OIS curve, and a tradeable one.")


def _calendar(ctx: Ctx) -> None:
    M = ctx.M
    C.sub("Releases between now and the decision")
    events = [e for e in M.calendar_events if ctx.as_of <= e.when <= M.meeting.end]
    if events:
        C.table(["Date", "Event", "Tier", "Source"],
                [[f"{e.when:%a %d %b}", e.label, e.weight, e.source] for e in events],
                numeric=(2,))
    else:
        st.caption("Nothing scheduled between the as-of and the decision.")


def _labour(ctx: Ctx) -> None:
    P = ctx.P
    C.sub("ABS Labour Force")
    lab = src.abs_series()
    lf, lfd = (lab.get("_periods") or ["", ""])[:2]
    ok = bool(lab.get("unemployment"))
    C.data_source_header("Labour Force (Excel time series)", ok,
                         f"headline {lf or 'unknown'} · detailed {lfd or 'unknown'}"
                         if ok else "unavailable", P)
    flows = src.flow_series()
    C.data_source_header("Gross flows (LMS1)", bool(flows.get("finding")),
                         f"release {flows.get('period') or 'unknown'} · job-finding rate over "
                         f"{len(flows.get('finding') or [])} quarters"
                         if flows.get("finding") else "unavailable", P)
    cap = src.manual_series(src.NAB_CAPACITY)
    C.data_source_header("NAB capacity utilisation (pasted)", bool(cap),
                         f"{len(cap)} months to {cap[-1][0]:%b %Y}" if cap else
                         "commercial, no feed — paste into meetings/_manual_series.json", P)
    C.table(["Series", "Obs", "From", "To"],
            [[empmod.BY_KEY[k].label, f"{len(v):,}", f"{v[0][0]:%b %Y}", f"{v[-1][0]:%b %Y}"]
             for k, v in lab.items() if not k.startswith("_") and v and k in empmod.BY_KEY],
            numeric=(1,), nowrap=True)
    C.note("Labour Force and Labour Force Detailed are not on the same month: `data.abs` reads "
           "both periods off the ABS landing pages, because a hardcoded path rots monthly.")


def _cpi(ctx: Ctx) -> None:
    P = ctx.P
    C.sub("ABS Consumer Price Index")
    cpi = src.cpi_series()
    ok = bool(cpi.get("leaves"))
    C.data_source_header("CPI (Excel time series)", ok,
                         f"release {cpi['period'] or 'unknown'} · {len(cpi['leaves'])} expenditure "
                         f"classes at {cpi['as_at']:%b %Y}" if ok else "unavailable", P)
    if ok:
        root = cpi["contribution"].get("All groups CPI") or 0.0
        C.note(f"{len(cpi['order'])} workbook series resolve to {len(cpi['leaves'])} classes; leaf "
               f"contributions reconcile to the published All groups contribution of {root:.2f} "
               f"with a residual of {cpi['residual']:+.2f} index points (ABS rounding).")


def _macro(ctx: Ctx) -> None:
    P = ctx.P
    C.sub("Macro indicators")
    mb = src.fetch_macro_bundle()
    wired = abs_sdmx.wired_keys()
    C.data_source_header("ABS SDMX data API", any(k in mb for k in wired),
                         f"{sum(1 for k in wired if k in mb)} of {len(wired)} dataflows returned", P)
    reads = macro_mod.dashboard(mb)
    with st.expander(f"The {len(reads)} macro rows"):
        C.table(["Indicator", "Group", "Obs", "From", "To", "State"],
                [[r.indicator.label, r.indicator.group,
                  f"{len(mb.get(r.indicator.key, [])):,}" if r.live else "—",
                  f"{mb[r.indicator.key][0][0]:%b %Y}" if r.indicator.key in mb else "—",
                  f"{mb[r.indicator.key][-1][0]:%b %Y}" if r.indicator.key in mb else "—",
                  "live" if r.live else ("commercial" if r.indicator.commercial else "seed")]
                 for r in reads], numeric=(2,), nowrap=True,
                colours={(i, 5): _status(r.live, P) if not r.indicator.commercial else P.muted
                         for i, r in enumerate(reads)})
