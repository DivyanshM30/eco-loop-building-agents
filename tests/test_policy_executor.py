"""Tier 1 safety tests.

These are the tests that back the claim "a hallucinated setpoint cannot reach
the simulation". Run: pytest -q
"""

from __future__ import annotations

import math

import pytest

from src.policy_executor import ActivePolicy, Limits, PolicyExecutor

LIMITS = Limits()


def make_executor(**overrides) -> PolicyExecutor:
    default = ActivePolicy(
        zone_setpoints={"CORE_ZN": (20.0, 24.0)},
        night_setback_c=3.0,
        precool_hours=0,
        occupied_start=7,
        occupied_end=19,
    )
    for k, v in overrides.items():
        setattr(default, k, v)
    return PolicyExecutor(LIMITS, default)


# --------------------------------------------------------------- envelope clamp

@pytest.mark.parametrize(
    "heat,cool",
    [
        (999.0, 999.0),
        (-999.0, -999.0),
        (float(50), float(5)),
        (0.0, 100.0),
        (21.9, 22.0),
    ],
)
def test_absurd_policies_are_clamped_into_envelope(heat, cool):
    ex = make_executor()
    ex.set_policy(ActivePolicy(zone_setpoints={"CORE_ZN": (heat, cool)}))
    h, c = ex.compute("CORE_ZN", hour=12)
    assert LIMITS.heating_sp_min_c <= h <= LIMITS.heating_sp_max_c
    assert LIMITS.cooling_sp_min_c <= c <= LIMITS.cooling_sp_max_c
    assert c - h >= LIMITS.min_deadband_c - 1e-9


def test_nan_and_inf_do_not_escape():
    ex = make_executor()
    for bad in (float("nan"), float("inf"), float("-inf")):
        ex.set_policy(ActivePolicy(zone_setpoints={"CORE_ZN": (bad, bad)}))
        h, c = ex.compute("CORE_ZN", hour=12)
        assert not math.isnan(h) and not math.isnan(c), "NaN reached the actuator"
        assert math.isfinite(h) and math.isfinite(c)
        assert LIMITS.heating_sp_min_c <= h <= LIMITS.heating_sp_max_c
        assert LIMITS.cooling_sp_min_c <= c <= LIMITS.cooling_sp_max_c


def test_deadband_is_always_enforced():
    ex = make_executor()
    ex.set_policy(ActivePolicy(zone_setpoints={"CORE_ZN": (22.0, 22.0)}))
    h, c = ex.compute("CORE_ZN", hour=12)
    assert c - h >= LIMITS.min_deadband_c - 1e-9


def test_setback_and_precool_are_bounded():
    ex = make_executor()
    ex.set_policy(
        ActivePolicy(
            zone_setpoints={"CORE_ZN": (20.0, 24.0)},
            night_setback_c=99.0,
            precool_hours=99,
        )
    )
    assert ex.active.night_setback_c <= LIMITS.max_setback_c
    assert ex.active.precool_hours <= LIMITS.max_precool_hours
    for hour in range(24):
        h, c = ex.compute("CORE_ZN", hour=hour)
        assert LIMITS.cooling_sp_min_c <= c <= LIMITS.cooling_sp_max_c
        assert LIMITS.heating_sp_min_c <= h <= LIMITS.heating_sp_max_c


# ------------------------------------------------------------------- behaviour

def test_night_setback_relaxes_setpoints():
    ex = make_executor()
    ex.set_policy(
        ActivePolicy(zone_setpoints={"CORE_ZN": (20.0, 24.0)}, night_setback_c=3.0)
    )
    _, cool_day = ex.compute("CORE_ZN", hour=12)
    heat_night, cool_night = ex.compute("CORE_ZN", hour=2)
    assert cool_night > cool_day, "unoccupied cooling setpoint should be relaxed upward"
    assert heat_night < 20.0, "unoccupied heating setpoint should be relaxed downward"


def test_precool_lowers_cooling_setpoint_before_occupancy():
    ex = make_executor()
    ex.set_policy(
        ActivePolicy(
            zone_setpoints={"CORE_ZN": (20.0, 26.0)},
            night_setback_c=3.0,
            precool_hours=2,
        )
    )
    _, cool_precool = ex.compute("CORE_ZN", hour=6)   # inside 5..7 precool window
    _, cool_occupied = ex.compute("CORE_ZN", hour=12)
    assert cool_precool < cool_occupied


def test_unknown_zone_falls_back_to_default_not_crash():
    ex = make_executor()
    h, c = ex.compute("ZONE_THAT_DOES_NOT_EXIST", hour=12)
    assert math.isfinite(h) and math.isfinite(c)
    assert c - h >= LIMITS.min_deadband_c - 1e-9


def test_revert_to_default_restores_safe_policy():
    ex = make_executor()
    ex.set_policy(ActivePolicy(zone_setpoints={"CORE_ZN": (16.0, 28.0)}))
    ex.revert_to_default("llm timeout")
    h, c = ex.compute("CORE_ZN", hour=12)
    assert (h, c) == (20.0, 24.0)
    assert ex.fallback_count == 1
    assert "fallback" in ex.active.source


def test_case_insensitive_zone_names():
    ex = make_executor()
    ex.set_policy(ActivePolicy(zone_setpoints={"core_zn": (20.0, 25.0)}))
    h, c = ex.compute("CORE_ZN", hour=12)
    assert c == 25.0


# ------------------------------------------------- measured-occupancy override

def test_measured_occupancy_overrides_the_clock():
    """A weekday-hour timestep with nobody present must get setback.

    This is the weekend bug: clock-only logic held the occupied setpoint on
    Saturday afternoon and burned more energy than the baseline schedule.
    """
    ex = make_executor()
    ex.set_policy(
        ActivePolicy(zone_setpoints={"CORE_ZN": (20.0, 25.0)}, night_setback_c=4.0)
    )
    _, cool_clock_occupied = ex.compute("CORE_ZN", hour=12, occupied=True)
    _, cool_actually_empty = ex.compute("CORE_ZN", hour=12, occupied=False)
    assert cool_clock_occupied == 25.0
    assert cool_actually_empty > cool_clock_occupied, "empty building must be setback"


def test_measured_occupancy_conditions_outside_clock_window():
    """Someone working at 22:00 should get the occupied band, not setback."""
    ex = make_executor()
    ex.set_policy(
        ActivePolicy(zone_setpoints={"CORE_ZN": (20.0, 25.0)}, night_setback_c=4.0)
    )
    heat, cool = ex.compute("CORE_ZN", hour=22, occupied=True)
    assert (heat, cool) == (20.0, 25.0)


def test_none_falls_back_to_clock_window():
    ex = make_executor()
    ex.set_policy(
        ActivePolicy(zone_setpoints={"CORE_ZN": (20.0, 25.0)}, night_setback_c=4.0)
    )
    _, cool_day = ex.compute("CORE_ZN", hour=12, occupied=None)
    _, cool_night = ex.compute("CORE_ZN", hour=3, occupied=None)
    assert cool_day == 25.0
    assert cool_night > cool_day


def test_precool_never_overrides_measured_occupancy():
    ex = make_executor()
    ex.set_policy(
        ActivePolicy(
            zone_setpoints={"CORE_ZN": (20.0, 26.0)},
            night_setback_c=3.0,
            precool_hours=2,
        )
    )
    # hour 6 is inside the precool window, but someone is present
    heat, cool = ex.compute("CORE_ZN", hour=6, occupied=True)
    assert (heat, cool) == (20.0, 26.0), "occupied band must win over precool"


def test_compute_is_cheap_and_counted():
    ex = make_executor()
    for _ in range(1000):
        ex.compute("CORE_ZN", hour=9)
    assert ex.applied_count == 1000
