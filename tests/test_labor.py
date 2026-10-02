"""The AU labour dashboard: indicators, channels, flows and the employment split."""

from datetime import date

from core import jobs_wages, labor, labor_flows


def _monthly(start: date, values):
    out = []
    y, m = start.year, start.month
    for v in values:
        out.append((date(y, m, 1), v))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def test_channels_partition_the_indicators():
    members = [k for c in labor.CHANNELS for k in c.members]
    assert sorted(members) == sorted(labor.BY_KEY)
    assert len(members) == len(set(members))


def test_stalled_rows_leave_the_month_gate():
    rows = {code: _monthly(date(2000, 1, 1), [float(i % 7) for i in range(320)])
            for ind in labor.INDICATORS for code in ind.codes}
    # One row stops five months before the rest.
    rows["MTU"] = rows["MTU"][:-5]
    month = labor.complete_month(rows)
    assert month == rows["UR"][-1][0]
    assert [i.key for i in labor.stalled(rows)] == ["mtu"]


def test_decomposition_closes_exactly():
    stocks = {"L": _monthly(date(2020, 1, 1), [1000, 1002, 1005, 1004]),
              "U": _monthly(date(2020, 1, 1), [50, 52, 51, 53]),
              "E": _monthly(date(2020, 1, 1), [950, 950, 954, 951]),
              "N": _monthly(date(2020, 1, 1), [500, 499, 497, 498])}
    rates = {k: _monthly(date(2020, 1, 1), [v] * 4) for k, v in
             {"UE": 0.2, "EU": 0.01, "UN": 0.15, "NU": 0.02}.items()}
    d = labor_flows.decompose(rates, stocks)
    assert d.ok and len(d.change) == 3
    for i, (when, du) in enumerate(d.change):
        parts = sum(c.monthly[i][1] for c in d.components)
        assert abs(parts - du) < 1e-9, when


def test_components_sum_to_total():
    q = [date(2025, m, 1) for m in (1, 4, 7, 10)]
    jobs = {"TOTAL": list(zip(q, [100.0, 103.0, 104.0, 108.0])),
            "Q": list(zip(q, [20.0, 21.0, 21.5, 22.0])),
            "P": list(zip(q, [10.0, 10.0, 10.5, 10.5])),
            "O": list(zip(q, [8.0, 8.5, 8.5, 8.0]))}
    comp = jobs_wages.components(jobs)
    for i in range(3):
        parts = sum(comp[k][i][1] for k in ("market", "health_ed", "public_admin"))
        assert abs(parts - comp["total"][i][1]) < 1e-9


def test_quarter_fill_stops_at_the_next_print():
    from ui.sources import _quarter_fill
    filled = _quarter_fill([(date(2026, 2, 1), 1.0), (date(2026, 5, 1), 2.0)])
    assert filled == [(date(2026, 2, 1), 1.0), (date(2026, 3, 1), 1.0), (date(2026, 4, 1), 1.0),
                      (date(2026, 5, 1), 2.0), (date(2026, 6, 1), 2.0), (date(2026, 7, 1), 2.0)]


def test_technical_recession_needs_two_falling_quarters(monkeypatch):
    from ui import sources
    gdp = [(date(2019, 9, 30), 100.0), (date(2019, 12, 31), 101.0), (date(2020, 3, 31), 100.5),
           (date(2020, 6, 30), 93.0), (date(2020, 9, 30), 96.0), (date(2020, 12, 31), 95.5)]
    monkeypatch.setattr(sources, "rba_table", lambda name: {"GGDPCVGDP": gdp})
    rec = sources.au_recessions.__wrapped__() if hasattr(sources.au_recessions, "__wrapped__") \
        else sources.au_recessions()
    marked = sorted({d for d, v in rec if v})
    # Mar and Jun 2020 fell back to back; Dec 2020's single fall is not a recession.
    assert marked[0] == date(2020, 1, 1) and marked[-1] == date(2020, 6, 1)
