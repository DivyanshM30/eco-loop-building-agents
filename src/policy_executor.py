"""Tier 1 — the reflex controller.

Design contract, and the reason the 30% "System Integration" criterion is winnable:

  * NO network calls, NO LLM imports, NO file I/O, NO exceptions raised.
  * Pure arithmetic on plain floats. Runs inside the EnergyPlus timestep callback.
  * If Tier 2 (the agent) is slow, broken, or dead, this keeps running the
    last-known-good policy and the simulation completes.
  * Every value written to an actuator passes through `_clamp` first, so a
    hallucinated setpoint is physically incapable of reaching the simulation.

Keep this module dependency-free so it stays trivially unit-testable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Limits:
    """Hard safety envelope — safety layer #2, independent of Pydantic bounds."""

    cooling_sp_min_c: float = 22.0
    cooling_sp_max_c: float = 28.0
    heating_sp_min_c: float = 16.0
    heating_sp_max_c: float = 22.0
    min_deadband_c: float = 2.0
    max_setback_c: float = 5.0
    max_precool_hours: int = 4

    @classmethod
    def from_config(cls, cfg_limits: dict) -> "Limits":
        known = {f: cfg_limits[f] for f in cls.__dataclass_fields__ if f in cfg_limits}
        return cls(**known)


@dataclass
class ActivePolicy:
    """Flat, primitive-only view of a policy. Deliberately not the Pydantic model:
    Tier 1 must not depend on Tier 2's schema layer."""

    zone_setpoints: dict[str, tuple[float, float]]  # zone -> (heating_c, cooling_c)
    night_setback_c: float = 0.0
    precool_hours: int = 0
    occupied_start: int = 7
    occupied_end: int = 19
    source: str = "default"
    valid_from_hour: int = 0


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp to [lo, hi], mapping NaN/inf to the conservative bound.

    The non-finite check is not paranoia: `nan < lo` and `nan > hi` are both
    False, so a naive clamp lets NaN straight through to set_actuator_value,
    which corrupts the simulation silently. See tests/test_policy_executor.py.
    """
    if not math.isfinite(value):
        return lo
    return lo if value < lo else hi if value > hi else value


class PolicyExecutor:
    """Applies the active policy at every timestep. Cannot fail."""

    def __init__(self, limits: Limits, default_policy: ActivePolicy) -> None:
        self._limits = limits
        self._default = default_policy
        self._active = default_policy
        self.applied_count = 0
        self.fallback_count = 0

    # ---------------------------------------------------------------- policy I/O

    @property
    def active(self) -> ActivePolicy:
        return self._active

    def set_policy(self, policy: ActivePolicy) -> None:
        """Install a new policy. Sanitised on the way in, so even a policy that
        somehow bypassed schema validation is safe."""
        lim = self._limits
        clean: dict[str, tuple[float, float]] = {}
        for zone, (heat, cool) in policy.zone_setpoints.items():
            clean[zone.upper()] = self._clamp_pair(float(heat), float(cool))
        self._active = ActivePolicy(
            zone_setpoints=clean,
            night_setback_c=_clamp(float(policy.night_setback_c), 0.0, lim.max_setback_c),
            precool_hours=int(_clamp(float(policy.precool_hours), 0, lim.max_precool_hours)),
            occupied_start=int(_clamp(float(policy.occupied_start), 0, 23)),
            occupied_end=int(_clamp(float(policy.occupied_end), 1, 24)),
            source=policy.source,
            valid_from_hour=policy.valid_from_hour,
        )

    def revert_to_default(self, reason: str = "") -> None:
        """Called when the agent fails in a way that makes the active policy suspect.

        Uses dataclasses.replace so the pristine default is never mutated — it has
        to stay clean because every subsequent fallback reuses it.
        """
        self.fallback_count += 1
        self._active = replace(
            self._default,
            source=f"default (fallback: {reason})" if reason else "default",
        )

    # ------------------------------------------------------------------ the loop

    def compute(self, zone: str, hour: float, occupied: bool | None = None) -> tuple[float, float]:
        """Return (heating_setpoint_c, cooling_setpoint_c) for this zone and hour.

        `occupied` should be the MEASURED occupancy for this zone. Pass None to
        fall back to the clock window.

        Prefer the measured signal: a clock-only rule cannot tell Saturday from
        Tuesday, so it conditions an empty building all weekend and burns more
        energy than the baseline schedule it was meant to beat. That is not a
        hypothetical — it cost 1.5% extra electricity on the first real A/B run.

        This is the only function on the critical path. Total cost: a few
        comparisons and additions.
        """
        pol = self._active
        zone_u = zone.upper()
        heat, cool = pol.zone_setpoints.get(
            zone_u,
            self._default.zone_setpoints.get(zone_u, (20.0, 24.0)),
        )

        h = int(hour) % 24
        if occupied is None:
            occupied = pol.occupied_start <= h < pol.occupied_end
        # Precool only ahead of the clock-scheduled occupied window. Gated on
        # `not occupied` so it never fights the occupied band.
        precool_window = (
            not occupied
            and pol.precool_hours > 0
            and (pol.occupied_start - pol.precool_hours) <= h < pol.occupied_start
        )

        if precool_window:
            # Pull the cooling setpoint down ahead of occupancy: shifts load into
            # cheaper, lower-carbon hours and reduces the morning peak.
            cool = cool - 1.5
        elif not occupied:
            # Relax both directions overnight — the main source of savings.
            cool = cool + pol.night_setback_c
            heat = heat - pol.night_setback_c

        self.applied_count += 1
        return self._clamp_pair(heat, cool)

    # ------------------------------------------------------------------ internals

    def _clamp_pair(self, heat: float, cool: float) -> tuple[float, float]:
        """Clamp to the envelope, then enforce the deadband by pushing cooling up.

        Note the asymmetry: we widen by raising the cooling setpoint rather than
        lowering the heating setpoint, because raising cooling is the
        energy-conservative direction in a cooling-dominated hour and never
        creates a comfort risk on the heating side.
        """
        lim = self._limits
        heat = _clamp(heat, lim.heating_sp_min_c, lim.heating_sp_max_c)
        cool = _clamp(cool, lim.cooling_sp_min_c, lim.cooling_sp_max_c)
        if cool - heat < lim.min_deadband_c:
            cool = _clamp(heat + lim.min_deadband_c, lim.cooling_sp_min_c, lim.cooling_sp_max_c)
            if cool - heat < lim.min_deadband_c:
                heat = _clamp(cool - lim.min_deadband_c, lim.heating_sp_min_c, lim.heating_sp_max_c)
        return heat, cool


def default_policy_from_config(cfg) -> ActivePolicy:
    """Build the fallback policy from config.yaml (also the Gate-2 static policy)."""
    dp = cfg.get_path("default_policy", {})
    zones = [str(z).upper() for z in cfg.get_path("zones", [])]
    occ = dp.get("occupied_hours", [7, 19])
    cool = float(dp.get("cooling_sp_c", 24.0))
    heat = float(dp.get("heating_sp_c", 20.0))
    return ActivePolicy(
        zone_setpoints={z: (heat, cool) for z in zones},
        night_setback_c=float(dp.get("night_setback_c", 3.0)),
        precool_hours=int(dp.get("precool_hours", 0)),
        occupied_start=int(occ[0]),
        occupied_end=int(occ[1]),
        source="default",
    )


def policy_to_active(policy, cfg) -> ActivePolicy:
    """Convert a validated Pydantic `Policy` into the primitive Tier 1 form."""
    occ = cfg.get_path("default_policy.occupied_hours", [7, 19])
    return ActivePolicy(
        zone_setpoints={z.zone: (z.heating_sp_c, z.cooling_sp_c) for z in policy.zones},
        night_setback_c=policy.night_setback_c,
        precool_hours=policy.precool_hours,
        occupied_start=int(occ[0]),
        occupied_end=int(occ[1]),
        source="agent",
        valid_from_hour=policy.valid_from_hour,
    )
