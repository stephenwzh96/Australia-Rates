"""Headless smoke run of every section, unpriced and priced.

    .venv/Scripts/python scripts/smoke_tabs.py [Section ...]

The priced pass types 10bp on the trade and sizes it in a contract that spans
two undecided meetings, so the moves view and every chart behind a priced
trade are exercised too. Prints one line per section and any exception text.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

from streamlit.testing.v1 import AppTest

warnings.filterwarnings("ignore")
APP = str(Path(__file__).resolve().parent.parent / "app.py")
DEFAULT = ["Curve", "Sizing", "Path", "Releases", "Vote count", "Macro", "Labour",
           "Inflation", "Data"]


def run(section: str, priced: bool) -> list[str]:
    at = AppTest.from_file(APP, default_timeout=300)
    at.query_params["section"] = section
    at.run()
    if priced and not at.exception:
        S = at.session_state["state"]
        S["trade"]["points"] = 10.0
        S.setdefault("sizing", {})["contract"] = S["sizing"].get("contract") or "ib"
        at.run()
    return [str(e.value)[:3000] for e in at.exception]


def main(sections: list[str]) -> int:
    bad = 0
    for priced in (False, True):
        for sec in sections:
            errs = run(sec, priced)
            bad += bool(errs)
            print(f"== {sec} ({'priced' if priced else 'unpriced'}): {'OK' if not errs else 'ERR'}")
            for e in errs:
                print("   ", e)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or DEFAULT))
