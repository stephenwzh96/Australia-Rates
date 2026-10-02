"""The Vote-count hawk-dove transition chart and its avatar sources.

Pure, no network: `rba_board.photos` is left alone (it is disk-cached and
degrades to nothing offline), and the chart is checked for shape only.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data import rba_board
from ui import charts
from ui.theme import DARK, LIGHT


def _recs():
    return [
        {"name": "Lorie Logan", "role": "Dallas", "voter": True,
         "s1": 4.6, "s2": 4.6, "avatar": ""},
        {"name": "Christopher Waller", "role": "Governor", "voter": True,
         "s1": 3.8, "s2": 4.1, "avatar": ""},
        {"name": "Neel Kashkari", "role": "Minneapolis", "voter": True,
         "s1": 4.1, "s2": 3.8, "avatar": ""},
        {"name": "Jerome Powell", "role": "Governor", "voter": True,
         "s1": 2.7, "s2": 2.7, "avatar": ""},
        {"name": "Alberto Musalem", "role": "St. Louis", "voter": False,
         "s1": 3.4, "s2": 3.6, "avatar": ""},
        {"name": "Cheryl Venable", "role": "Atlanta", "voter": False,
         "s1": None, "s2": None, "avatar": ""},        # unscored -> dropped
    ]


def _layers(spec):
    """Every mark layer in the (single, layered) chart."""
    return spec.get("layer", [])


def _scale_by_domain(spec, domain):
    """The first colour scale in the spec whose domain matches exactly."""
    for layer in _layers(spec) or [spec]:
        colour = layer.get("encoding", {}).get("color", {})
        sc = colour.get("scale", {})
        if sc.get("domain") == domain:
            return sc
    return None


def test_the_chart_builds_and_skips_the_unscored():
    spec = charts.hawk_dove_transition(_recs(), LIGHT).to_dict()
    # One layered chart -- names are a text layer, not a separate sub-chart.
    assert "hconcat" not in spec, "should be one layered chart, not a concat"
    assert _layers(spec), "expected a layered dumbbell"
    text = str(spec)
    assert "Lorie Logan" in text and "Alberto Musalem" in text
    assert "Cheryl Venable" not in text          # no score2 -> not a dumbbell


def test_an_all_monogram_roster_still_builds():
    recs = [{**r, "avatar": ""} for r in _recs()[:4]]
    assert charts.hawk_dove_transition(recs, DARK).to_dict()["layer"]


def test_the_chart_is_responsive_and_not_a_fixed_pixel_width():
    """It was an hconcat with a hard 1280px plot width, which overflowed the
    ~1030px left column it lives in. Back to one layered chart: `width` is
    "container" so it fits whatever it is dropped in, and the names sit in a
    left padding gutter rather than their own fixed column."""
    spec = charts.hawk_dove_transition(_recs(), LIGHT).to_dict()
    assert spec.get("width") == "container", (
        f"width = {spec.get('width')!r} -- not responsive")
    pad = spec.get("padding") or {}
    assert isinstance(pad, dict) and pad.get("left", 0) >= 90, (
        f"left padding = {pad!r} -- no room for the name gutter")
    # nothing carries a hard pixel width that would break the container fit
    for layer in _layers(spec):
        assert "width" not in layer, "a layer pins its own pixel width"


def test_no_position_mark_carries_a_pixel_literal_x2():
    """The first "All participants" bug: `x2=alt.value(10_000)` on the
    non-voter band -- a raw pixel literal -- pushed the plot ~10000px wide and
    squashed every dumbbell into a sliver. An x2 must be a data field."""
    recs = [
        {"name": "V", "role": "NY", "voter": True, "s1": 4.0, "s2": 4.2, "avatar": ""},
        {"name": "N1", "role": "KC", "voter": False, "s1": 3.0, "s2": 3.3, "avatar": ""},
        {"name": "N2", "role": "SF", "voter": False, "s1": 2.0, "s2": 1.8, "avatar": ""},
    ]
    spec = charts.hawk_dove_transition(recs, LIGHT).to_dict()
    for layer in _layers(spec):
        enc = layer.get("encoding", {}).get("x2", {})
        assert not isinstance(enc.get("value"), (int, float)), (
            f"x2 is a pixel literal {enc!r} -- use a data field")


def test_all_participants_renders_every_row_not_just_voters():
    """The "All participants" regression: a `mark_bar` background band forced
    the shared categorical y onto a band scale, clashed with the point/rule
    marks, and Vega-Lite silently dropped the non-voter rows. Every record
    with a score must survive into the spec, voter or not."""
    recs = [
        {"name": f"V{i}", "role": "Gov", "voter": True,
         "s1": 3.0, "s2": 3.0 + i * 0.1, "avatar": ""} for i in range(6)
    ] + [
        {"name": f"N{i}", "role": "KC", "voter": False,
         "s1": 2.5, "s2": 2.5 - i * 0.1, "avatar": ""} for i in range(5)
    ]
    spec = charts.hawk_dove_transition(recs, LIGHT).to_dict()
    expected = ("V0", "V1", "V2", "V3", "V4", "V5",
                "N0", "N1", "N2", "N3", "N4")
    sorts = [tuple(l["encoding"]["y"]["sort"]) for l in _layers(spec)
             if "sort" in l.get("encoding", {}).get("y", {})]
    assert sorts, "no y sort found"
    for s in sorts:
        assert s == expected, f"y sort mismatch: {s}"
    # no bar/rect mark on the shared y -- that is what caused the drop
    for l in _layers(spec):
        m = l.get("mark")
        mt = m.get("type") if isinstance(m, dict) else m
        if "y" in l.get("encoding", {}):
            assert mt not in ("bar", "rect"), (
                f"{mt} mark on the categorical y forces a band scale")


def test_move_candidates_get_a_tick_right_of_the_now_dot():
    """A member flagged `mover` (the roster's Move-candidate column) gets a
    tick immediately right of the Score-now dot. The score label shifts
    further right so it never sits under the tick. No mover -> no tick layer."""
    with_mover = [
        {"name": "A", "role": "NY", "voter": True, "s1": 4.0, "s2": 4.2,
         "avatar": "", "mover": True},
        {"name": "B", "role": "SF", "voter": True, "s1": 3.0, "s2": 2.8,
         "avatar": "", "mover": False},
    ]
    spec = charts.hawk_dove_transition(with_mover, LIGHT).to_dict()
    ticks = [l for l in _layers(spec)
             if isinstance(l.get("mark"), dict)
             and l["mark"].get("text") == "✓"]
    assert len(ticks) == 1, "expected exactly one tick layer"
    t = ticks[0]
    assert t["transform"] == [{"filter": "datum.mover"}]
    assert t["mark"]["dx"] > 0, "tick must sit to the RIGHT of the dot"
    # the s2 score label is pushed past the tick
    score = next(l for l in _layers(spec)
                 if isinstance(l.get("mark"), dict)
                 and l["mark"].get("type") == "text"
                 and l.get("encoding", {}).get("text", {}).get("field") == "s2")
    assert score["mark"]["dx"] > t["mark"]["dx"]

    # nobody flagged -> the tick layer is gone entirely
    none_flagged = [{**r, "mover": False} for r in with_mover]
    spec2 = charts.hawk_dove_transition(none_flagged, LIGHT).to_dict()
    assert not [l for l in _layers(spec2)
                if isinstance(l.get("mark"), dict)
                and l["mark"].get("text") == "✓"]


def test_empty_records_do_not_raise():
    charts.hawk_dove_transition([], LIGHT)
    charts.hawk_dove_transition([{"name": "X", "voter": True, "s1": 1, "s2": None}],
                                LIGHT)


def test_the_no_vote_gutter_tag_is_gone():
    """The little "no vote" text used to live in the left gutter, stamped over
    the very names it was meant to qualify. A green highlight on the voting
    members' names now says who counts without annotating who doesn't."""
    spec = charts.hawk_dove_transition(_recs(), LIGHT).to_dict()
    assert '"no vote"' not in str(spec), \
        "the lowercase 'no vote' gutter tag is still in the spec"


def test_member_names_take_the_ink_since_every_member_votes():
    """The US chart greens voters against greyed non-voters. Every Board
    member votes, so the green would mark nothing: names take the ink, and
    a (hypothetical) non-voter row would still read in `ink_secondary`."""
    spec = charts.hawk_dove_transition(_recs(), DARK).to_dict()
    scale = _scale_by_domain(spec, ["voter", "non-voter"])
    assert scale, "no voter/non-voter colour scale found"
    rng = scale["range"]
    assert rng[0] == DARK.ink, rng
    assert rng[1] == DARK.ink_secondary, rng
    assert rng[0] != DARK.muted, "name ink collapsed to muted grey"


def test_change_of_scale_is_red_and_blue_only():
    """Hawkish is red (`negative`), dovish is blue (`positive`). A member who
    did not move is drawn in the body ink (`ink_secondary`), NOT the faint
    muted grey -- so no third colour competes with the red/blue read."""
    spec = charts.hawk_dove_transition(_recs(), DARK).to_dict()
    scale = _scale_by_domain(spec, ["hawkish", "dovish", "flat"])
    assert scale, "no hawkish/dovish/flat colour scale found"
    rng = scale["range"]
    assert rng == [DARK.negative, DARK.positive, DARK.ink_secondary], rng
    assert DARK.muted not in rng, (
        "muted grey is still used as a change-of-scale colour")
    assert rng[0] == DARK.negative and rng[1] == DARK.positive


def test_photos_sit_just_inside_the_left_edge_not_over_the_names():
    """The headshot is pinned to x = 0 (the plot's left edge) with a small
    positive pixel offset, so it sits in the empty left margin of the plot,
    clear of both the y axis and the name gutter."""
    recs = [{**r, "avatar": "data:image/png;base64,AAAA"} for r in _recs()[:3]]
    spec = charts.hawk_dove_transition(recs, DARK).to_dict()
    img_layers = [l for l in _layers(spec)
                  if (isinstance(l.get("mark"), dict)
                      and l["mark"].get("type") == "image")]
    assert img_layers, "no image layer"
    for layer in img_layers:
        x = layer["encoding"]["x"]
        assert x.get("value") == 0, f"image x = {x!r} -- not pinned to the edge"
        xo = layer["encoding"].get("xOffset", {})
        assert xo.get("value", 0) > 0, "image not offset into the margin"


def test_photos_offline_returns_a_dict_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(rba_board, "_CACHE", tmp_path)
    monkeypatch.setattr(rba_board, "members", lambda *a, **k: [])
    got = rba_board.photos(["Nobody At All", "Also Nobody"])
    assert got == {}


def test_the_photo_cache_key_is_filesystem_safe():
    assert rba_board._safe("Renée Fry-McKibbin") == "renée_fry_mckibbin"
    messy = "a/b\c:d"
    assert "/" not in rba_board._safe(messy)
    assert chr(92) not in rba_board._safe(messy)


def test_each_bloc_gets_its_pictogram_in_the_name_gutter():
    from ui import bloc_icons as B
    import base64
    def body(bloc):
        uri = B.icon_uri(bloc, "#123456")
        return base64.b64decode(uri.split(",", 1)[1]).decode()
    assert B.HAWK in body("Hawks") and B.DOVE in body(" doves ")
    assert B.DATA in body("Data Dependent") and B.BALANCE in body("Board centre")
    assert B.OTHER in body("Hawk-leaning") and "#123456" in body("Hawks")
    assert B.icon_uri("", "#000") is None and B.icon_uri("Unscored", "#000") is None
    recs = [{**r, "bloc": "Hawks"} for r in _recs()[:3]]
    spec = charts.hawk_dove_transition(recs, DARK).to_dict()
    icons = [l for l in _layers(spec) if isinstance(l.get("mark"), dict)
             and l["mark"].get("type") == "image" and l["encoding"]["url"]["field"] == "icon"]
    assert icons and icons[0]["encoding"]["xOffset"]["value"] < 0, "icon not in the gutter"
