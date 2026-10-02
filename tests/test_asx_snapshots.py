"""ASX settlements kept day by day: only true settlements, each day once."""

from datetime import date

from data import asx, asx_snapshots as S


def _q(symbol, price, day, source="settlement"):
    return asx.Quote(month=asx.month_from_symbol(symbol), symbol=symbol, price=price,
                     asof=day, source=source, kind="ib")


def test_record_keeps_settlements_once(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "DIR", tmp_path)
    d1, d2 = date(2026, 10, 1), date(2026, 10, 2)
    assert S.record("ib", [_q("IBV2026", 95.40, d1), _q("IBX2026", 95.30, d1, "midpoint")]) == 1
    assert S.record("ib", [_q("IBV2026", 95.41, d1)]) == 0          # a day is written once
    S.record("ib", [_q("IBV2026", 95.42, d2)])
    assert S.history("ib", "IBV2026") == [(d1, 95.40), (d2, 95.42)]
    assert S.history("ib", "IBX2026") == []                          # midpoints never kept
    assert S.coverage("ib") == (d1, 2)


def test_contract_history_chart_renders():
    from ui import charts
    from ui.theme import active
    c = charts.contract_history_chart([(date(2026, 10, 1), 95.40), (date(2026, 10, 2), 95.42)],
                                      "IBV6", active(), moves_bp=[(date(2026, 10, 1), 0.0),
                                                                  (date(2026, 10, 2), -2.0)])
    assert c.to_dict()
