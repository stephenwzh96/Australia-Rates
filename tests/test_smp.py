"""The SMP forecast table, the cash-rate path, the heatmap and the named dots."""

from datetime import date

from core import dots, smp, taylor_heatmap as th
from data import rba_smp

PAGE = """<table><caption>Table 3.1: Detailed Forecast Table (a)</caption>
<tr><th></th><th>Jun 2026</th><th>Dec 2026</th></tr>
<tr><td>Unemployment rate (quarterly, %) (c)</td><td>4.4</td><td>4.5</td></tr>
<tr><td>Trimmed mean inflation</td><td>3.6</td><td>3.3</td></tr>
<tr><td>Cash rate (%) (d)</td><td>4.3</td><td>4.4</td></tr>
<tr><td>Terms of trade</td><td>&minus;3.1</td><td>&minus;2.8</td></tr>
</table>"""


def test_parse_reads_the_forecast_table():
    r = rba_smp.parse(PAGE, 2026, 8)
    assert r.horizons == ["Jun 2026", "Dec 2026"]
    assert r.rows["Cash rate (%)"] == [4.3, 4.4]
    assert r.rows["Terms of trade"] == [-3.1, -2.8]
    assert r.key == "2026-aug" and r.label == "Aug 2026"


def test_revisions_line_up_on_the_horizon():
    latest = rba_smp.Round(2026, 8, ["Jun 2026", "Dec 2026"],
                           {"Cash rate (%)": [4.3, 4.4], "Trimmed mean inflation": [3.6, 3.3]})
    prior = rba_smp.Round(2026, 5, ["Dec 2025", "Jun 2026", "Dec 2026"],
                          {"Cash rate (%)": [3.6, 4.2, 4.7], "Trimmed mean inflation": [3.4, 3.8, 3.5]})
    table = smp.build(latest, prior)
    cash = next(r for r in table.rows if r.metric.label == smp.CASH_RATE)
    assert [c.change for c in cash.cells] == [0.1, -0.3]
    path = smp.cash_path(latest, prior,
                         {date(2026, 10, 1): 4.6, date(2026, 11, 1): 4.7, date(2026, 12, 1): 4.8},
                         date(2026, 10, 2))
    dec = path[-1]
    assert dec.latest == 4.4 and dec.prior == 4.7 and abs(dec.market - 4.7) < 1e-9
    assert path[0].market is None                 # Jun 2026 is history at the as-of


def test_heatmap_prescription_and_counts():
    heat = th.build(inflation=3.0, unemployment=4.5, funds_rate=4.6)
    centre = heat.rows[2][2]                      # r* 1.0, u* 4.5
    assert abs(centre.rate - (1.0 + 3.0 + 0.5 * 0.5)) < 1e-12
    assert heat.n == 25 and heat.above + heat.below <= heat.n
    assert th.build(None, 4.5, 4.6).ok is False


def test_dots_snap_to_five_basis_points_and_group_seats():
    assert dots.snap("4.37") == 4.35 and dots.snap("x") is None
    roster = [{"name": "Michele Bullock", "role": "Governor, Chair", "bloc": "Executive", "score": 3},
              {"name": "", "role": "External member", "bloc": "External", "score": 2.5}]
    seat = dots.seat_name(roster[1], 1)
    panel = dots.build(roster, "Dec 2026", {"Michele Bullock": 4.6, seat: 4.35})
    assert panel.count == 2 and panel.median == (4.6 + 4.35) / 2
    assert dots.reconcile_split(panel, 4.6) == (0, 1, 1)
