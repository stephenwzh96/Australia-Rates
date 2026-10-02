"""Who votes at a Monetary Policy Board meeting: everyone.

The FOMC rotates four of its eleven regional seats each year, and the US app's
roster, dots and bloc means all ask `votes_in(role, year)`. The RBA's
Monetary Policy Board has no rotation -- the Governor, the Deputy Governor,
the Secretary to the Treasury and six external members all vote at every
meeting (Reserve Bank Act 1959 s.10A) -- so the same questions have one answer.
Kept as a module so the ported roster logic reads identically.
"""

from __future__ import annotations


def votes_in(role: str, year: int) -> bool:
    return True


def normalise_bank(role: str) -> str | None:
    """No regional seats on the Board."""
    return None


def rotation_note(year: int) -> str:
    return "Every member of the Monetary Policy Board votes at every meeting."
