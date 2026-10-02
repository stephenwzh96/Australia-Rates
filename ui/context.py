"""Per-rerun application context handed to every tab renderer.

The US version's `Ctx` carries the meeting state and the model; this one also
carries the two ASX strips and the curve read off them, because on this side
the strip is not a tab's private business. The priced path anchors the Verdict
rail's "strip says" figures, the IR DV01 moves with the live bill yield, and
the bank-bill basis is measured against the interbank path -- so the strips are
fetched once per run, here, and every tab reads the same quotes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import streamlit as st

from core import bbsw as bbswmod
from core import model, rba_calendar
from core import strip as stripmod
from data import asx
from ui import sources as src
from ui.theme import Palette


def mark_dirty() -> None:
    st.session_state["dirty"] = True


@dataclass
class Market:
    """Both ASX strips and the RBA anchors, as fetched this run."""

    ib_quotes: list
    ir_quotes: list
    spot: dict
    ib_ref: date | None
    ir_ref: date | None
    ib_stale: set
    ir_stale: set
    horizon: date | None

    @classmethod
    def fetch(cls) -> "Market":
        ib, ir = src.fetch_ib(), src.fetch_ir()
        ib_ref, ir_ref = asx.latest_asof(ib), asx.latest_asof(ir)
        return cls(ib_quotes=ib, ir_quotes=ir, spot=src.rba_spot(),
                   ib_ref=ib_ref, ir_ref=ir_ref,
                   ib_stale=asx.stale_codes(ib, ib_ref), ir_stale=asx.stale_codes(ir, ir_ref),
                   horizon=asx.coverage_end(ib, ib_ref))

    @property
    def ib_live(self) -> list:
        return [q for q in self.ib_quotes if q.ok]

    @property
    def ir_live(self) -> list:
        return [q for q in self.ir_quotes if q.ok]

    def ir_yield(self, month_key: str) -> float | None:
        """The live implied BBSW of the IR contract delivering in `YYYY-MM`."""
        q = next((q for q in self.ir_quotes if q.ok and f"{q.month:%Y-%m}" == month_key), None)
        return q.implied_rate if q is not None else None


@dataclass
class Ctx:
    key: str
    S: dict
    as_of: date
    today: date
    P: Palette
    meetings: list[rba_calendar.Meeting]
    market: Market
    M: model.Model | None = field(default=None)
    path: stripmod.StripPath | None = field(default=None)
    bills: object | None = field(default=None)

    @property
    def trade(self) -> dict:
        return self.S.setdefault("trade", {})

    # ---------------------------------------------------------------- curve

    @property
    def spot_key(self) -> str:
        """The Curve tab's spot override, namespaced by meeting so an anchor typed
        for one meeting can never skew another's ladder."""
        return f"curve_spot_{self.S.get('meeting')}"

    @property
    def spot_derived(self) -> float | None:
        """Spot cash backed out of the strip, else the RBA's published target."""
        strip_spot = stripmod.implied_spot(self.market.ib_quotes, self.meetings, as_of=self.as_of)
        return strip_spot if strip_spot is not None else self.market.spot["cash"]

    @property
    def spot(self) -> float | None:
        return st.session_state.get(self.spot_key, self.spot_derived)

    def _model_state(self) -> dict:
        """`S` with the IR contract's live yield overlaid for its DV01.

        A throwaway overlay, never written back: `S` is what Save persists, and
        a market reading is not a user decision -- persisting it would make a
        reopened meeting size off whatever the curve was on the day it was
        last viewed."""
        sz = self.S.get("sizing") or {}
        sel = sz.get("contract") or "ib"
        if sel.startswith("ir:"):
            y = self.market.ir_yield(sel.partition(":")[2])
            if y is not None:
                return {**self.S, "sizing": {**sz, "contract_yield": y}}
        return self.S

    def rebuild(self) -> model.Model:
        """Recompute the model and the curve from state -- call after any widget
        writes into S, so nothing downstream is one interaction behind."""
        values, units = src.kill_series_values()
        self.M = model.build(
            self._model_state(), as_of=self.as_of,
            rba_values=values, rba_units=units,
            all_meetings=self.meetings,
            rate_history=src.bbsw_history(),
            rate_symbol="3-month BBSW (RBA F1)",
            policy_changes=src.policy_changes(),
            release_dates=src.release_dates(self.today),
        )
        anchor = self.spot
        mk = self.market
        self.path = (stripmod.decompose(self.meetings, mk.ib_quotes, float(anchor), self.M.size,
                                        stale_codes=mk.ib_stale, as_of=self.as_of)
                     if anchor is not None
                     else stripmod.StripPath(0.0, self.M.size, error="no cash rate anchor"))
        self.bills = bbswmod.analyse(mk.ir_quotes, self.path, self.meetings, self.M.size,
                                     mk.ir_stale, mk.horizon, as_of=self.as_of,
                                     spot_basis_bp=mk.spot["spot_basis_bp"])
        return self.M
