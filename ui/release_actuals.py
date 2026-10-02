"""The Actual column of the release calendar: what each realised print said.

The US version reads the latest FRED revision for the month a report covers.
Here each release maps to the ABS or RBA series that carries its headline, and
the reference period is the one the report covers -- the month before for
Labour Force and the monthly CPI, the quarter before for the quarterly CPI,
the WPI and the National Accounts. The three sources date a quarter three
different ways (ABS workbooks on the quarter's last month, RBA tables on its
last day, the ABS data API on its first month), so periods are matched on
(year, quarter), never on a date.
"""

from __future__ import annotations

from datetime import date

from core import econ_calendar
from data import abs_sdmx
from ui import sources as src


def _quarter(d: date) -> tuple[int, int]:
    return d.year, (d.month - 1) // 3 + 1


def _prev_month(d: date) -> tuple[int, int]:
    return (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)


def _prev_quarter(d: date) -> tuple[int, int]:
    y, q = _quarter(d)
    return (y, q - 1) if q > 1 else (y - 1, 4)


def _by_month(series) -> dict[tuple[int, int], float]:
    return {(d.year, d.month): v for d, v in series or []}


def _by_quarter(series) -> dict[tuple[int, int], float]:
    return {_quarter(d): v for d, v in series or []}


def _series() -> dict:
    """Everything the column reads, fetched through the cached sources."""
    out: dict = {}
    try:
        lab = src.abs_series()
        out["u"] = _by_month(lab.get("unemployment"))
    except Exception:
        out["u"] = {}
    try:
        cpi = src.cpi_series()
        out["tm_q"] = _by_quarter(cpi.get("trimmed_q"))
        out["cpi_q"] = _by_quarter(cpi.get("headline_q"))
        out["tm_m"] = _by_month(cpi.get("monthly_trimmed_yr"))
    except Exception:
        out.update(tm_q={}, cpi_q={}, tm_m={})
    out["wpi"] = _by_quarter(src.rba_table("h4").get("GWPIQP"))
    try:
        out["gdp"] = _by_quarter(abs_sdmx.series_pairs("gdp"))
    except Exception:
        out["gdp"] = {}
    out["cash"] = src.cash_target_history()
    return out


def _cash_after(cash: list[tuple[date, float]], decision: date) -> str:
    """The target set at a decision: the first print on or after the day it
    takes effect, against the last one before."""
    after = next((v for d, v in cash if d > decision), None)
    before = next((v for d, v in reversed(cash) if d <= decision), None)
    if after is None:
        return ""
    if before is None or abs(after - before) < 1e-9:
        return f"hold {after:.2f}%"
    return f"{(after - before) * 100:+.0f}bp → {after:.2f}%"


def for_events(events, as_of: date) -> dict[tuple[date, str], str]:
    """{(date, release): cell text} for every event that has printed by the as-of."""
    done = [e for e in events if e.when < as_of]
    if not done:
        return {}
    s = _series()
    out: dict[tuple[date, str], str] = {}
    for e in done:
        text = ""
        if e.release == econ_calendar.LABOUR:
            v = s["u"].get(_prev_month(e.when))
            text = f"U {v:.1f}%" if v is not None else "no data"
        elif e.release == econ_calendar.CPI_Q:
            tm, hd = s["tm_q"].get(_prev_quarter(e.when)), s["cpi_q"].get(_prev_quarter(e.when))
            text = (f"TM {tm:.1f}% q/q" + (f" · CPI {hd:.1f}%" if hd is not None else "")
                    if tm is not None else "no data")
        elif e.release == econ_calendar.CPI_M:
            v = s["tm_m"].get(_prev_month(e.when))
            text = f"TM {v:.1f}% y/y" if v is not None else "no data"
        elif e.release == econ_calendar.WPI:
            v = s["wpi"].get(_prev_quarter(e.when))
            text = f"{v:.1f}% q/q" if v is not None else "no data"
        elif e.release == econ_calendar.GDP:
            v = s["gdp"].get(_prev_quarter(e.when))
            text = f"{v:+.1f}% q/q" if v is not None else "no data"
        elif e.release in (econ_calendar.DECISION, econ_calendar.SMP):
            text = _cash_after(s["cash"], e.when)
        out[(e.when, e.release)] = text
    return out
