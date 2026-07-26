"""EnergyPlus data-exchange layer: handle binding, sampling, aggregation.

Three EnergyPlus API rules this module exists to enforce, each of which silently
breaks a run if you get it wrong:

  1. `request_variable` must be called BEFORE run_energyplus, or the variable is
     never tracked and its handle comes back as -1.
  2. Handles are invalid until `api_data_fully_ready(state)` returns True, and
     must be fetched INSIDE a callback, not before the run starts.
  3. Warmup timesteps produce data that must not be used for control or metrics.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

J_TO_KWH = 1.0 / 3_600_000.0


@dataclass
class Sample:
    """One timestep of simulation state."""

    sim_hour: float
    month: int
    day: int
    hour: int
    minute: int
    outdoor_c: float
    zone_temps: dict[str, float] = field(default_factory=dict)
    zone_pmv: dict[str, float] = field(default_factory=dict)
    zone_occupants: dict[str, float] = field(default_factory=dict)
    occupants: float = 0.0
    unmet_heating_h: float = 0.0
    unmet_cooling_h: float = 0.0

    @property
    def occupied(self) -> bool:
        """True when anyone is actually in the building.

        Comfort metrics are only valid here. Energy metrics apply always.
        """
        return self.occupants > 0.0
    # keyed by ROLE ('electricity', 'gas'), not by model-specific meter name
    meters_j: dict[str, float] = field(default_factory=dict)
    setpoints: dict[str, tuple[float, float]] = field(default_factory=dict)

    @property
    def electricity_kwh(self) -> float:
        return self.meters_j.get("electricity", 0.0) * J_TO_KWH

    @property
    def gas_kwh(self) -> float:
        return self.meters_j.get("gas", 0.0) * J_TO_KWH


class SensorBus:
    """Owns every EnergyPlus handle and the rolling history buffer."""

    def __init__(self, api, var_specs: list[dict], meter_roles: dict[str, str], zones: list[str]):
        self._api = api
        self._var_specs = var_specs
        self._meter_roles = dict(meter_roles)   # role -> model-specific meter name
        self._zones = [z.upper() for z in zones]

        self._var_handles: dict[tuple[str, str], int] = {}
        self._meter_handles: dict[str, int] = {}   # keyed by role
        self._actuator_handles: dict[tuple[str, str], int] = {}  # (zone, 'heating'|'cooling')
        self._bound = False
        self.bind_errors: list[str] = []

        self.history: deque[Sample] = deque(maxlen=8760 * 4)  # ~1 year at 15-min steps
        self.total_j: dict[str, float] = {role: 0.0 for role in self._meter_roles}

    # ------------------------------------------------------------- pre-run setup

    def request_variables(self, state) -> None:
        """MUST be called before run_energyplus."""
        for spec in self._var_specs:
            self._api.exchange.request_variable(state, spec["name"], spec["key"])

    # ------------------------------------------------------------ handle binding

    @property
    def bound(self) -> bool:
        return self._bound

    def bind(self, state) -> bool:
        """Fetch all handles. Call from inside the callback; returns False until ready."""
        if self._bound:
            return True
        if not self._api.exchange.api_data_fully_ready(state):
            return False

        ex = self._api.exchange
        errors: list[str] = []

        for spec in self._var_specs:
            name, key = spec["name"], spec["key"]
            h = ex.get_variable_handle(state, name, key)
            if h < 0:
                errors.append(f"variable not found: '{name}' key='{key}'")
            else:
                self._var_handles[(name, key)] = h

        for role, meter in self._meter_roles.items():
            h = ex.get_meter_handle(state, meter)
            if h < 0:
                errors.append(
                    f"meter not found: '{meter}' (role '{role}'). "
                    "Not every model exposes every meter — run "
                    "`python scripts/dump_api_data.py` for the list this model has."
                )
            else:
                self._meter_handles[role] = h

        # 'Zone Temperature Control' is the EMS actuator that overrides the
        # thermostat setpoint schedules. Confirm the exact triple for your model
        # with scripts/dump_actuators.py (reads eplusout.edd).
        for zone in self._zones:
            for control, label in (("Heating Setpoint", "heating"), ("Cooling Setpoint", "cooling")):
                h = ex.get_actuator_handle(state, "Zone Temperature Control", control, zone)
                if h < 0:
                    errors.append(
                        f"actuator not found: ('Zone Temperature Control', '{control}', '{zone}')"
                    )
                else:
                    self._actuator_handles[(zone, label)] = h

        self.bind_errors = errors
        self._bound = True  # bind once even on partial failure; caller decides what to do
        return True

    def bind_error_report(self) -> str:
        detail = "\n  - ".join(self.bind_errors)
        return (
            "EnergyPlus handle binding failed:\n  - "
            + detail
            + "\n\nMost likely causes:\n"
            "  * zone names in config.yaml do not match the IDF (they are UPPERCASE in the API)\n"
            "  * the variable was not requested before the run\n"
            "  * the meter name does not exist in this model — run "
            "`python scripts/dump_api_data.py` for the authoritative list\n"
            "  * this actuator does not exist for this model — run "
            "`python scripts/dump_actuators.py` and check eplusout.edd\n"
        )

    def assert_bound_ok(self) -> None:
        """Fail loudly. Safe to call from the CLI, NOT from inside a callback.

        Raising inside an EnergyPlus ctypes callback does not abort the run:
        the exception is swallowed ("Exception ignored on calling ctypes
        callback function") and re-raised on every subsequent timestep, which
        buries the real message in thousands of lines. Callers inside the
        callback should check `bind_errors` and bail out once instead.
        """
        if self.bind_errors:
            raise RuntimeError(self.bind_error_report())

    # -------------------------------------------------------------------- runtime

    def is_warmup(self, state) -> bool:
        return bool(self._api.exchange.warmup_flag(state))

    def is_weather_run(self, state) -> bool:
        """True only during a weather-file RunPeriod, not a sizing/design day.

        KindOfSim: 1 = DesignDay, 2 = RunPeriodDesign, 3 = RunPeriodWeather.

        Design-day results look completely plausible — exit code 0, no warnings,
        sane totals — but they are two isolated days (typically 21 Jan and
        21 Jul), not a continuous period. Mixing them into an A/B comparison
        produces a heating-dominated 'saving' that does not exist. Filter here
        so the run is correct even if SimulationControl is misconfigured.
        """
        try:
            return int(self._api.exchange.kind_of_sim(state)) == 3
        except Exception:
            return True  # API unavailable: don't block the run

    def sim_clock(self, state) -> tuple[int, int, int, int, float]:
        """(month, day, hour, minute, cumulative_sim_hour)."""
        ex = self._api.exchange
        month = int(ex.month(state))
        day = int(ex.day_of_month(state))
        hour = int(ex.hour(state))
        minute = int(ex.minutes(state))
        day_of_year = int(ex.day_of_year(state))
        sim_hour = (day_of_year - 1) * 24 + hour + minute / 60.0
        return month, day, hour, minute, sim_hour

    def read(self, state) -> Sample:
        ex = self._api.exchange
        month, day, hour, minute, sim_hour = self.sim_clock(state)

        def var(name: str, key: str, default: float = 0.0) -> float:
            h = self._var_handles.get((name, key))
            return float(ex.get_variable_value(state, h)) if h is not None else default

        zone_temps = {z: var("Zone Mean Air Temperature", z) for z in self._zones}
        zone_pmv = {}
        zone_occupants: dict[str, float] = {}
        occupants = 0.0
        unmet_h = unmet_c = 0.0
        for (name, key) in self._var_handles:
            if name == "Zone Thermal Comfort Fanger Model PMV":
                zone_pmv[key] = var(name, key)
            elif name == "Zone People Occupant Count":
                n = var(name, key)
                zone_occupants[key] = n
                occupants += n
            elif name == "Zone Heating Setpoint Not Met Time":
                unmet_h += var(name, key)
            elif name == "Zone Cooling Setpoint Not Met Time":
                unmet_c += var(name, key)

        meters_j = {}
        for role, h in self._meter_handles.items():
            val = float(ex.get_meter_value(state, h))
            meters_j[role] = val
            self.total_j[role] = self.total_j.get(role, 0.0) + val

        return Sample(
            sim_hour=sim_hour,
            month=month,
            day=day,
            hour=hour,
            minute=minute,
            outdoor_c=var("Site Outdoor Air Drybulb Temperature", "ENVIRONMENT"),
            zone_temps=zone_temps,
            zone_pmv=zone_pmv,
            zone_occupants=zone_occupants,
            occupants=occupants,
            unmet_heating_h=unmet_h,
            unmet_cooling_h=unmet_c,
            meters_j=meters_j,
        )

    def write_setpoints(self, state, zone: str, heating_c: float, cooling_c: float) -> None:
        ex = self._api.exchange
        zone_u = zone.upper()
        hh = self._actuator_handles.get((zone_u, "heating"))
        ch = self._actuator_handles.get((zone_u, "cooling"))
        if hh is not None:
            ex.set_actuator_value(state, hh, heating_c)
        if ch is not None:
            ex.set_actuator_value(state, ch, cooling_c)

    def push(self, sample: Sample) -> None:
        self.history.append(sample)

    # ---------------------------------------------------------------- aggregation

    def aggregates(self, window_hours: int = 24) -> dict[str, Any]:
        """Compact rolling summary for the agent.

        This is the context-compression boundary: the agent NEVER sees raw
        timeseries. A 24 h window at 15-min steps is 96 samples per variable;
        this collapses it to a handful of numbers.
        """
        if not self.history:
            return {"status": "no_data"}

        now = self.history[-1].sim_hour
        window = [s for s in self.history if s.sim_hour >= now - window_hours] or [self.history[-1]]

        def stats(values: list[float]) -> dict[str, float]:
            if not values:
                return {"min": 0.0, "mean": 0.0, "max": 0.0}
            return {
                "min": round(min(values), 2),
                "mean": round(sum(values) / len(values), 2),
                "max": round(max(values), 2),
            }

        zone_stats = {
            z: stats([s.zone_temps.get(z, 0.0) for s in window]) for z in self._zones
        }
        # Comfort is scored ONLY while occupied — see Sample.occupied.
        occ_window = [s for s in window if s.occupied]
        pmv_all = [v for s in occ_window for v in s.zone_pmv.values()]
        elec_kwh = sum(s.electricity_kwh for s in window)
        gas_kwh = sum(s.gas_kwh for s in window)
        peak_kw = 0.0
        if len(window) > 1:
            dt_h = max((window[-1].sim_hour - window[0].sim_hour) / (len(window) - 1), 1e-6)
            peak_kw = round(max(s.electricity_kwh for s in window) / dt_h, 2)

        return {
            "sim_hour": round(now, 2),
            "window_hours": window_hours,
            "samples": len(window),
            "outdoor_c": stats([s.outdoor_c for s in window]),
            "zone_temp_c": zone_stats,
            "pmv": stats(pmv_all) if pmv_all else None,
            "pmv_note": "occupied hours only",
            "occupied_samples": len(occ_window),
            "electricity_kwh": round(elec_kwh, 3),
            "gas_kwh": round(gas_kwh, 3),
            "peak_electric_kw": peak_kw,
            "unmet_heating_hours_occupied": round(sum(s.unmet_heating_h for s in occ_window), 2),
            "unmet_cooling_hours_occupied": round(sum(s.unmet_cooling_h for s in occ_window), 2),
            "unmet_heating_hours": round(sum(s.unmet_heating_h for s in window), 2),
            "unmet_cooling_hours": round(sum(s.unmet_cooling_h for s in window), 2),
            "cumulative_electricity_kwh": round(
                self.total_j.get("electricity", 0.0) * J_TO_KWH, 2
            ),
            "cumulative_gas_kwh": round(
                self.total_j.get("gas", 0.0) * J_TO_KWH, 2
            ),
        }
