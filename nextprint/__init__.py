"""Next print: a bias for the next monthly Australian unemployment-rate print.

The US app's `nextprint` package, on the ABS Labour Force. Self-contained, and
only `ui/tabs/analysis.py` imports it (lazily). Nothing here runs unless the
Analysis tab is open.

The forecast for month m is made the day before its Labour Force release,
R_m, from inputs released strictly before that day (`releases.py`). Each
pre-registered input (`inputs.py`) moves a no-change benchmark through one
slope (`model.py`). The bias traders see is the equal-weight average of those
input models, so nothing is chosen by searching over results; a ridge
challenger is tracked on prints it has not seen.

What differs from the US build, and why:
  * no ALFRED. The ABS keeps past releases online only from December 2019,
    so the target and the inputs are today's vintage (seasonally adjusted
    monthly revisions to the rate are typically 0.0-0.1pp) -- disclosed on
    the tab. Release DATES are exact from the ABS release pages where those
    exist, and a conservative rule before.
  * no prediction market on the Australian rate. The tab compares the model
    with the street's consensus instead, for the live print.
  * numpy only: the Clark-West and Diebold-Mariano tests are written out
    (Newey-West variance, normal tail), and the tied-best set is a pairwise
    test against the best input rather than the arch package's MCS.

To remove it:
  1. delete this folder, tests/test_nextprint.py and data/cache/nextprint*;
  2. drop "Analysis" from SECTIONS in app.py and delete ui/tabs/analysis.py.
"""
