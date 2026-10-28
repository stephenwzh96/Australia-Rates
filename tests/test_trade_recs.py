"""Trade recs: the answer is read defensively, and the tickets draw what it says."""

from __future__ import annotations

import json
from datetime import date

import pytest

from core import trade_recs as tr

AS_OF = date(2026, 9, 28)


def _rec(horizon="event", **over):
    base = {
        "horizon": horizon, "title": "Fade the October hike",
        "expression": "Receive FFX6", "instrument": "Fed funds futures, Nov 2026",
        "direction": "receive", "entry": "16.5bp priced", "target": "9bp", "stop": "20bp",
        "entry_bp": 16.5, "target_bp": 9.0, "stop_bp": 20.0, "conviction": 3,
        "sizing": "half normal risk", "exit_by": "2026-09-30",
        "catalyst": "Core PCE", "thesis": "The strip overprices October.",
        "fundamental": [{"point": "Core PCE is cooling on a 3-month basis.", "source": "app:Inflation"}],
        "quantitative": [{"figure": "+16.5bp", "reading": "priced for October", "source": "app:Pricing"}],
        "risks": [{"risk": "A hot print.", "source": "https://www.bea.gov/x"}],
        "wrong_if": "Core PCE prints 0.4% or more.",
    }
    base.update(over)
    return base


def _answer(*recs, **top):
    return json.dumps({"regime": "Hawkish hold", "desk_view": "The price leads the data.",
                       "recommendations": list(recs),
                       "sources": [{"title": "BEA", "url": "https://www.bea.gov/x"},
                                   {"title": "no url", "url": "not a link"}], **top})


def test_three_horizons_come_back_in_desk_order():
    got = tr.parse(_answer(_rec("medium"), _rec("event"), _rec("meeting")), AS_OF)
    assert got.ok
    assert [r.horizon for r in got.recs] == ["event", "meeting", "medium"]
    assert got.regime == "Hawkish hold"
    assert [s.url for s in got.sources] == ["https://www.bea.gov/x"]   # the non-URL is dropped


def test_a_trade_without_levels_or_sourced_reasons_is_dropped_and_said_so():
    no_levels = _rec("event", stop="")
    no_quant = _rec("meeting", quantitative=[{"figure": "", "reading": "x", "source": "app:Pricing"}])
    unsourced = _rec("medium", fundamental=[{"point": "Trust me.", "source": ""}])
    got = tr.parse(_answer(no_levels, no_quant, unsourced), AS_OF)
    assert not got.recs and not got.ok
    assert len(got.dropped) == 3
    assert any("without levels" in d for d in got.dropped)


def test_a_duplicate_horizon_keeps_the_first():
    got = tr.parse(_answer(_rec("event", title="first"), _rec("event", title="second")), AS_OF)
    assert [r.title for r in got.recs] == ["first"]
    assert got.dropped


def test_garbage_is_an_error_not_an_exception():
    assert tr.parse("no json here", AS_OF).error
    assert tr.parse("", AS_OF).error


def test_conviction_and_direction_are_clamped_to_their_scales():
    got = tr.parse(_answer(_rec(conviction=9, direction="YOLO")), AS_OF).recs[0]
    assert got.conviction == 5 and got.direction == "other"


def test_reward_risk_and_incoherent_levels():
    r = tr.parse(_answer(_rec()), AS_OF).recs[0]
    assert r.reward_bp == pytest.approx(7.5) and r.risk_bp == pytest.approx(3.5)
    assert r.reward_risk == pytest.approx(7.5 / 3.5)
    assert r.levels_coherent
    bad = tr.parse(_answer(_rec(stop_bp=5.0)), AS_OF).recs[0]    # stop on the target's side
    assert not bad.levels_coherent


def test_round_trip_through_the_saved_record():
    got = tr.parse(_answer(_rec("event"), _rec("meeting"), _rec("medium")), AS_OF)
    back = tr.from_dict(json.loads(json.dumps(tr.to_dict(got))))
    assert back == got


def test_source_tags():
    assert tr.source_tag("app:SEP & dots") == "SEP & dots"
    assert tr.source_tag("https://www.federalreserve.gov/a/b") == "federalreserve.gov"
    assert tr.source_tag("") == ""


def test_horizons_carry_real_dates():
    spans = tr.horizons(AS_OF, date(2026, 9, 30), "Core PCE", date(2026, 10, 28), "Oct 27-28, 2026")
    assert [s.key for s in spans] == ["event", "meeting", "medium"]
    assert spans[-1].ends == date(2026, 12, 27)          # 90 days out


def test_the_prompt_names_every_horizon_and_carries_the_brief():
    spans = tr.horizons(AS_OF, date(2026, 9, 30), "Core PCE", date(2026, 10, 28), "Oct")
    text = tr.prompt("## Pricing\nSpot EFFR 3.880%", AS_OF, spans)
    assert all(f'"{k}"' in text for k in tr.HORIZON_ORDER)
    assert "Spot EFFR 3.880%" in text


def test_the_schema_requires_every_field_it_describes():
    item = tr.SCHEMA["properties"]["recommendations"]["items"]
    assert set(item["required"]) == set(item["properties"])


# ----------------------------------------------------------------- render

def test_the_ticket_escapes_model_text_and_draws_the_levels():
    from ui.tabs import recs as tab

    r = tr.parse(_answer(_rec(title="<script>x</script>")), AS_OF).recs[0]
    out = tab._ticket(r, None)
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "rx-scale" in out and "rx-rec" in out
    assert "\n" not in out                               # one line: never a code block


def test_the_ruler_pins_each_horizon_by_date():
    from ui.tabs import recs as tab

    spans = tr.horizons(AS_OF, date(2026, 9, 30), "Core PCE", date(2026, 10, 28), "Oct")
    out = tab._ruler(spans, None, AS_OF)
    assert 'left:2.22%' in out and 'left:100.00%' in out     # 2 and 90 days of 90


def test_runs_are_saved_and_read_back_newest_first(tmp_path, monkeypatch):
    from state import store

    monkeypatch.setattr(store, "RECS_HISTORY_FILE", tmp_path / "_recs_history.json")
    monkeypatch.setattr(store, "_RESEARCH_BACKUP_DIR", tmp_path / "backups")
    store.save_recs_run({"stamp": "2026-09-27T10:00:00", "recs": {}})
    store.save_recs_run({"stamp": "2026-09-28T10:00:00", "recs": {}})
    assert [r["stamp"][:10] for r in store.load_recs_history()] == ["2026-09-28", "2026-09-27"]


def test_the_agent_runs_the_newest_desktop_cli_unless_one_is_named(tmp_path, monkeypatch):
    from data import agent

    for v in ("2.1.99", "2.1.281", "2.1.280", "notaversion"):
        d = tmp_path / "Claude" / "claude-code" / v
        d.mkdir(parents=True)
        (d / "claude.exe").write_bytes(b"")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))   # not this machine's
    monkeypatch.delenv("CLAUDE_CODE_CLI", raising=False)
    assert agent.cli_path() == str(tmp_path / "Claude" / "claude-code" / "2.1.281" / "claude.exe")
    named = tmp_path / "mine.exe"
    named.write_bytes(b"")
    monkeypatch.setenv("CLAUDE_CODE_CLI", str(named))
    assert agent.cli_path() == str(named)


def _no_path_claude(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)


def test_the_desktop_cli_is_found_one_folder_down(tmp_path, monkeypatch):
    """Desktop 2.1.289+ keeps <version>/<hash>/claude.exe; the version is still
    the folder under claude-code, not the hash."""
    from data import agent

    for v, sub in (("2.1.281", None), ("2.1.293", "83cb0bd7fed4"), ("2.1.289", "e1f0154146bb")):
        d = tmp_path / "Claude" / "claude-code" / v
        d = d / sub if sub else d
        d.mkdir(parents=True)
        (d / "claude.exe").write_bytes(b"")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.delenv("CLAUDE_CODE_CLI", raising=False)
    assert agent.cli_path() == str(tmp_path / "Claude" / "claude-code" / "2.1.293"
                                   / "83cb0bd7fed4" / "claude.exe")


def test_the_store_apps_cli_is_found_from_outside_its_container(tmp_path, monkeypatch):
    """The MSIX desktop app's %APPDATA% writes land under its package's
    LocalCache; a server the launcher starts sees nothing at %APPDATA%."""
    from data import agent

    appdata, local = tmp_path / "Roaming", tmp_path / "Local"
    (appdata / "Claude").mkdir(parents=True)
    pkg = local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
    d = pkg / "claude-code" / "2.1.293" / "83cb0bd7fed4"
    d.mkdir(parents=True)
    (d / "claude.exe").write_bytes(b"")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv("CLAUDE_CODE_CLI", raising=False)
    _no_path_claude(monkeypatch)
    assert agent.cli_path() == str(d / "claude.exe")


def test_the_desktop_apps_claude_data_folder_is_searched(tmp_path, monkeypatch):
    """Since late Oct 2026 the desktop app keeps its copies under
    %LOCALAPPDATA%\\Claude-Data\\claude_<id>\\claude-code."""
    from data import agent

    appdata, local = tmp_path / "Roaming", tmp_path / "Local"
    appdata.mkdir()
    d = local / "Claude-Data" / "claude_pzs8sxrjxfjjc" / "claude-code" / "2.1.308" / "c17a819afb40"
    d.mkdir(parents=True)
    (d / "claude.exe").write_bytes(b"")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv("CLAUDE_CODE_CLI", raising=False)
    _no_path_claude(monkeypatch)
    assert agent.cli_choice() == (str(d / "claude.exe"), (2, 1, 308))


def test_a_claude_code_folder_in_a_place_never_seen_is_still_found(tmp_path, monkeypatch):
    from data import agent

    appdata, local = tmp_path / "Roaming", tmp_path / "Local"
    appdata.mkdir()
    d = local / "AnthropicSomethingNew" / "claude-code" / "2.1.400" / "abc123"
    d.mkdir(parents=True)
    (d / "claude.exe").write_bytes(b"")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv("CLAUDE_CODE_CLI", raising=False)
    _no_path_claude(monkeypatch)
    assert agent.cli_choice() == (str(d / "claude.exe"), (2, 1, 400))


def test_the_path_claude_competes_on_version(tmp_path, monkeypatch):
    import shutil
    from data import agent

    d = tmp_path / "Claude" / "claude-code" / "2.1.300"
    d.mkdir(parents=True)
    (d / "claude.exe").write_bytes(b"")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.delenv("CLAUDE_CODE_CLI", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "C:/bin/claude.exe")
    monkeypatch.setattr(agent, "_reported_version", lambda exe: (2, 1, 63))
    assert agent.cli_path() == str(d / "claude.exe")
    monkeypatch.setattr(agent, "_reported_version", lambda exe: (2, 1, 350))
    assert agent.cli_path() == "C:/bin/claude.exe"


def test_a_too_old_cli_stops_the_run_before_it_starts(monkeypatch):
    from data import agent

    monkeypatch.setattr(agent, "cli_choice", lambda: ("C:/bin/claude.exe", (2, 1, 63)))
    ok, why = agent.available()
    assert not ok and "2.1.63" in why and "C:/bin/claude.exe" in why and "claude update" in why
    monkeypatch.setattr(agent, "cli_choice", lambda: ("C:/x/claude.exe", agent.MIN_VERSION))
    assert agent.available() == (True, "")
    monkeypatch.setattr(agent, "cli_choice", lambda: (None, ()))
    assert agent.available() == (True, "")


def test_a_too_old_cli_is_explained_not_reported_as_unknown():
    from data import agent

    said = 'API Error: 400 {"error":{"details":{"error_code":"claude_code_version_too_old"}}}'
    assert "newer Claude Code" in agent._explain(said)
    assert agent._explain("") == ""
