"""Tests for the agent-facing layers that do not need EnergyPlus.

Everything here runs without an EnergyPlus install or an LLM server, so it works
in CI and on a teammate's laptop before they finish installing E+.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import Config
from src.llm_client import LLMError, extract_json
from src.runtime_store import RuntimeStore
from src.schemas import Policy, parse_policy
from src.tools import ToolBox, parse_idf_objects, serialize_idf_objects


@pytest.fixture()
def cfg() -> Config:
    return Config({
        "zones": ["CORE_ZN", "PERIMETER_ZN_1"],
        "limits": {
            "cooling_sp_min_c": 22.0, "cooling_sp_max_c": 28.0,
            "heating_sp_min_c": 16.0, "heating_sp_max_c": 22.0,
            "min_deadband_c": 2.0,
        },
        "carbon": {"base_g_per_kwh": 380.0, "peak_g_per_kwh": 620.0,
                   "peak_hours": [17, 21], "trough_hours": [1, 5],
                   "trough_g_per_kwh": 250.0},
        "energyplus": {"idf": "models/ai_instrumented.idf", "output_dir_ai": "out_ai"},
    })


@pytest.fixture()
def box(cfg, tmp_path) -> ToolBox:
    return ToolBox(cfg, RuntimeStore(tmp_path / "store"))


# ------------------------------------------------------------------ JSON parsing

def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_markdown_fence():
    text = 'Here you go:\n```json\n{"valid_from_hour": 5}\n```\nHope that helps!'
    assert extract_json(text) == {"valid_from_hour": 5}


def test_extract_json_prose_wrapped():
    text = 'Sure. {"night_setback_c": 3.0} That should save energy.'
    assert extract_json(text) == {"night_setback_c": 3.0}


def test_extract_json_nested_braces_and_strings():
    payload = {"zones": [{"zone": "CORE_ZN"}], "rationale": "uses } and { in text"}
    text = f"Reasoning...\n{json.dumps(payload)}\nDone."
    assert extract_json(text) == payload


def test_extract_json_raises_on_garbage():
    with pytest.raises(LLMError):
        extract_json("I am afraid I cannot do that.")


# ------------------------------------------------------------- schema validation

def valid_policy_dict() -> dict:
    return {
        "valid_from_hour": 24,
        "zones": [{"zone": "CORE_ZN", "cooling_sp_c": 25.0, "heating_sp_c": 20.0}],
        "night_setback_c": 3.0,
        "precool_hours": 2,
        "rationale": "Widen deadband overnight.",
    }


def test_valid_policy_parses():
    policy, rejection = parse_policy(valid_policy_dict())
    assert rejection is None
    assert policy.zones[0].zone == "CORE_ZN"


def test_zone_name_is_uppercased():
    d = valid_policy_dict()
    d["zones"][0]["zone"] = "core_zn"
    policy, _ = parse_policy(d)
    assert policy.zones[0].zone == "CORE_ZN"


def test_out_of_range_setpoint_is_rejected_with_field_name():
    d = valid_policy_dict()
    d["zones"][0]["cooling_sp_c"] = 31.0
    policy, rejection = parse_policy(d)
    assert policy is None
    assert any("cooling_sp_c" in f for f in rejection.rejected_fields)
    assert "cooling_sp_c" in rejection.as_feedback()


def test_narrow_deadband_is_rejected():
    d = valid_policy_dict()
    d["zones"][0].update(cooling_sp_c=22.0, heating_sp_c=21.5)
    policy, rejection = parse_policy(d)
    assert policy is None
    assert "deadband" in rejection.reason.lower()


def test_parse_policy_never_raises_on_junk():
    for junk in (None, [], "nope", {"zones": "not a list"}, {}):
        policy, rejection = parse_policy(junk)
        assert policy is None
        assert rejection is not None and rejection.accepted is False


def test_prompt_schema_is_small():
    schema = Policy.json_schema_for_prompt()
    assert set(schema) >= {"valid_from_hour", "zones", "night_setback_c", "precool_hours"}
    assert len(json.dumps(schema)) < 800, "prompt schema must stay compact for small models"


# ------------------------------------------------------------------ commit_policy

def test_commit_policy_accepts_valid(box):
    result = box.commit_policy(valid_policy_dict())
    assert result["accepted"] is True
    assert result["zones_updated"] == ["CORE_ZN"]
    assert box.store.drain_inbox(), "accepted policy should reach the inbox"


def test_commit_policy_rejects_unknown_zone(box):
    d = valid_policy_dict()
    d["zones"][0]["zone"] = "NO_SUCH_ZONE"
    result = box.commit_policy(d)
    assert result["accepted"] is False
    assert "NO_SUCH_ZONE" in result["reason"]
    assert not box.store.drain_inbox(), "rejected policy must not reach the inbox"


def test_commit_policy_rejection_is_traced(box):
    d = valid_policy_dict()
    d["zones"][0]["cooling_sp_c"] = 99.0
    box.commit_policy(d)
    events = [r["event"] for r in box.store.read_trace()]
    assert "policy_rejected" in events


def test_query_timeseries_unknown_variable_lists_options(box):
    out = box.query_timeseries("bananas")
    assert "error" in out and "available" in out


def test_carbon_curve_has_peak_and_trough(box):
    out = box.get_grid_carbon_intensity(hour=18)
    assert out["g_co2_per_kwh"] == 620.0
    assert box.get_grid_carbon_intensity(hour=3)["g_co2_per_kwh"] == 250.0
    assert len(out["cheapest_hours"]) == 4


def test_get_sim_status_without_simulation(box):
    assert box.get_sim_status()["status"] == "no_simulation_running"


# --------------------------------------------------------------- runtime store

def test_snapshot_round_trip(tmp_path):
    store = RuntimeStore(tmp_path / "s")
    store.write_snapshot({"sim_hour": 12.5, "aggregates": {"electricity_kwh": 4.2}})
    snap = store.read_snapshot()
    assert snap["sim_hour"] == 12.5
    assert snap["aggregates"]["electricity_kwh"] == 4.2


def test_drain_inbox_clears(tmp_path):
    store = RuntimeStore(tmp_path / "s")
    store.propose_policy({"a": 1})
    store.propose_policy({"a": 2})
    assert len(store.drain_inbox()) == 2
    assert store.drain_inbox() == []


def test_read_snapshot_missing_is_graceful(tmp_path):
    assert RuntimeStore(tmp_path / "s").read_snapshot()["status"] == "no_snapshot"


# ------------------------------------------------------------------ IDF parsing

IDF_SAMPLE = """
! a comment line
Version,24.1;

Timestep,4;

People,
    CORE_PEOPLE,             !- Name
    CORE_ZN,                 !- Zone Name
    OCC_SCHED,               !- Number of People Schedule Name
    People,                  !- Calculation Method
    5,                       !- Number of People
    ,                        !- People per Floor Area
    ,                        !- Floor Area per Person
    0.3,                     !- Fraction Radiant
    ,                        !- Sensible Heat Fraction
    ACTIVITY_SCHED;          !- Activity Level Schedule Name

Zone,
    CORE_ZN;
"""


def test_idf_parser_preserves_positional_empty_fields():
    """Critical: instrument_idf.py edits People field 20 by index. If interior
    blanks were dropped, every later field would shift and the model would break."""
    objects = parse_idf_objects_from_text(IDF_SAMPLE)
    people = [o for o in objects if o["type"] == "People"][0]
    assert people["fields"][0] == "CORE_PEOPLE"
    assert people["fields"][5] == ""   # 'People per Floor Area' is blank
    assert people["fields"][6] == ""   # 'Floor Area per Person' is blank
    assert people["fields"][7] == "0.3"
    assert people["fields"][9] == "ACTIVITY_SCHED"


def test_idf_parser_strips_comments():
    objects = parse_idf_objects_from_text(IDF_SAMPLE)
    types = [o["type"] for o in objects]
    assert "Version" in types and "Timestep" in types and "Zone" in types
    assert not any("!" in o["type"] for o in objects)


def test_idf_round_trip_is_stable(tmp_path):
    p = tmp_path / "m.idf"
    p.write_text(IDF_SAMPLE, encoding="utf-8")
    first = parse_idf_objects(p)
    p.write_text(serialize_idf_objects(first), encoding="utf-8")
    second = parse_idf_objects(p)
    assert [o["type"] for o in first] == [o["type"] for o in second]
    assert [o["fields"] for o in first] == [o["fields"] for o in second]


def parse_idf_objects_from_text(text: str):
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".idf", delete=False, encoding="utf-8") as fh:
        fh.write(text)
        name = fh.name
    try:
        return parse_idf_objects(Path(name))
    finally:
        Path(name).unlink(missing_ok=True)
