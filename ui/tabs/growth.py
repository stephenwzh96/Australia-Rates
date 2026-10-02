"""Growth: the activity tracker against the accounts, then the consumer --
the US Growth tab's layout, in Australian series."""

from __future__ import annotations

import streamlit as st

from core import consumer, econ_calendar
from core import growth as growth_mod
from core import publication as pub
from ui import charts
from ui import components as C
from ui import sources as src
from ui.context import Ctx

#: The size charts take on every other tab: a pair of half-width panels.
CHART_H = 420
HALF_PX = 390
BUBBLE_H = 480

_SHORT = {"1m": "1M AR", "3m": "3M AR", "12m": "12M", "spending": "Consumption",
          "income": "Disp. income"}


def _latest_figs(lines, fmt: str, tone: dict | None = None, quarterly: bool = False) -> list:
    live = [ln for ln in lines if ln.ok]
    if not live:
        return []
    latest = max(ln.observations[-1].date for ln in live)
    when = (f"{['Mar', 'Jun', 'Sep', 'Dec'][(latest.month - 1) // 3]} qtr {latest:%Y}"
            if quarterly else f"{latest:%b %Y}")
    return [C.Fig("Latest", when)] + [
        C.Fig(_SHORT.get(ln.key, ln.label), fmt.format(ln.observations[-1].value),
              colour=(tone or {}).get(ln.key))
        for ln in live if ln.observations[-1].date == latest]


def _period(d, cadence: str) -> str:
    """A quarterly reading names its quarter, not the month it is dated on."""
    if cadence == "quarterly":
        return f"{['Mar', 'Jun', 'Sep', 'Dec'][(d.month - 1) // 3]} qtr {d:%Y}"
    return f"{d:%b %Y}"


def _capped(obs, as_of, family=pub.NATIONAL_ACCOUNTS):
    """What had printed by the as-of (`core.publication`): the accounts in the
    third month after their quarter, hours with the Labour Force."""
    return src.published(obs, family, as_of)


def render(ctx: Ctx) -> None:
    _growth(ctx)
    _consumer(ctx)


def _growth(ctx: Ctx) -> None:
    head, ctl = st.columns([2, 1.2], vertical_alignment="bottom")
    with head:
        C.heading("Australian economic growth",
                  "year-over-year % change · monthly tracker against the accounts",
                  help=("", "Australia publishes no weekly activity index, so the monthly read "
                            "on output is labour input: total hours worked, from every Labour "
                            "Force survey, year-ended, and its 3-month average (one quarter, "
                            "which is what makes it comparable to the quarterly steps). Output "
                            "is hours times productivity, so hours running above GDP is weak "
                            "productivity — the gap the RBA has been forecasting to close.",
                        "", "au-growth"))
    with ctl:
        window = C.history_window("growth_window")
    start = C.window_start(window, ctx.as_of)

    hours, h1 = src.fetch_growth_inputs()
    series = growth_mod.build(_capped(hours, ctx.as_of, pub.LABOUR_FORCE),
                              {k: _capped(v, ctx.as_of) for k, v in h1.items()})
    live = [s for s in series if s.ok]
    if not live:
        st.warning("No growth series could be loaded — the Data tab has each source's status.")
        return
    vintage = charts.Vintage(src.next_release(econ_calendar.GDP, ctx.today), None)
    chart_col, side = st.columns(2)
    with chart_col:
        st.plotly_chart(charts.au_growth(live, ctx.P, height=CHART_H, window_start=start,
                                         vintage=vintage, width_px=HALF_PX), width="stretch")
    with side:
        spread = growth_mod.gap(live)
        if spread is not None:
            C.board([C.Fig(f"Hours {growth_mod.SMOOTH_MONTHS}-month avg less GDP",
                           f"{spread:+.2f} pp",
                           "labour input above output" if spread > 0
                           else "labour input below output",
                           ctx.P.negative if spread > 0 else ctx.P.positive,
                           help="Positive is hours growing faster than output: falling "
                                "productivity, the RBA's recent forecast story.")])
        readings = growth_mod.latest_readings(live)
        if readings:
            C.sub("Latest readings")
            C.table(["Indicator", "Cadence", "As of", "y-o-y"],
                    [[r["indicator"], r["cadence"].title(), _period(r["date"], r["cadence"]),
                      f"{r['value']:+.2f}%"] for r in readings], numeric=(3,))
        C.note("Sources: ABS national accounts and Labour Force, via RBA tables H1 and H5. "
               "Quarterly steps are dated on the quarter's first month.")


def _consumer(ctx: Ctx) -> None:
    P = ctx.P
    head, ctl = st.columns([2, 1.2], vertical_alignment="bottom")
    with head:
        C.heading("The Australian consumer",
                  "household spending monthly · income quarterly",
                  help=("", "The ABS Household Spending Indicator replaced Retail Trade in July "
                            "2025: monthly, from bank transaction data, and it covers services, "
                            "which retail never did — so it stands in for the US retail control "
                            "group. One- and three-month rates are compounded to an annual rate. "
                            "Household income exists only in the quarterly accounts, so the "
                            "income panel is quarterly, year-ended.", "", "consumer"))
    with ctl:
        window = C.history_window("consumer_window",
                                  help="Scopes the spending and household charts.")
    start = C.window_start(window, ctx.as_of)

    hsi = {k: _capped(v, ctx.as_of, pub.HSI) for k, v in src.fetch_hsi().items()}
    spend_lines = consumer.spending_lines(hsi.get(consumer.TOTAL, []))
    house = {k: _capped(v, ctx.as_of) for k, v in src.fetch_household_income().items()}
    house_lines = consumer.household_lines(house)
    spend_vintage = charts.Vintage(None, hsi[consumer.TOTAL][-1].date
                                   if hsi.get(consumer.TOTAL) else None)
    house_vintage = charts.Vintage(src.next_release(econ_calendar.GDP, ctx.today), None)
    tone = charts.consumer_tones(P)
    left, right = st.columns(2)
    with left:
        C.board(_latest_figs(spend_lines, "{:.1f}%"))
        st.plotly_chart(charts.household_spending(spend_lines, P, height=CHART_H,
                                                  window_start=start, vintage=spend_vintage,
                                                  readout=False), width="stretch")
    with right:
        C.board(_latest_figs(house_lines, "{:+.1f}%", tone, quarterly=True))
        st.plotly_chart(charts.household_finances(house_lines, P, height=CHART_H,
                                                  window_start=start, vintage=house_vintage,
                                                  readout=False), width="stretch")

    snap = consumer.snapshot(hsi)
    st.plotly_chart(charts.spending_bubbles(snap, P, height=BUBBLE_H, vintage=spend_vintage),
                    width="stretch")
    if snap is not None:
        with st.expander(f"Spending categories · {snap.month:%B %Y}"):
            rows = sorted(snap.categories, key=lambda c: -c.sales)
            C.table(["Category", "Share of spending", "m-o-m", "y-o-y"],
                    [[snap.overall.label, "100.0%", f"{snap.overall.mom:+.1f}%",
                      f"{snap.overall.yoy:+.1f}%"]]
                    + [[c.label, f"{c.share:.1f}%", f"{c.mom:+.1f}%", f"{c.yoy:+.1f}%"]
                       for c in rows], numeric=(1, 2, 3), mark_rows=(0,))
    sav = house.get("GGDPICHDISR") or []
    C.note("Sources: ABS Household Spending Indicator; ABS national accounts via RBA table H2."
           + (f" Household saving ratio {sav[-1].value:.1f}% in the latest quarter." if sav
              else ""))
