"""Emit a modified .idf for each policy the controller installs.

Deliverable #2 asks for "the base baseline building file along with the modified
versions generated during runtime evaluation". The controller drives the building
through EMS actuators rather than by rewriting the model, so nothing would land
in models/generated/ on its own. This materialises each active policy as a real,
runnable EnergyPlus model: the thermostat setpoint schedules are replaced with
Schedule:Constant objects holding that policy's occupied setpoints, per zone.

Each snapshot is a genuine artifact — open it in EnergyPlus and it reproduces the
occupied setpoints the agent chose at that point in the run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .tools import parse_idf_objects, serialize_idf_objects

# Field indices (1-based, after the object type)
ZCT_ZONE = 2          # ZoneControl:Thermostat -> 'Zone or ZoneList Name'
ZCT_CONTROL_NAME = 5  # ZoneControl:Thermostat -> 'Control 1 Name'
DUAL_HEAT_SCHED = 2   # ThermostatSetpoint:DualSetpoint -> heating schedule name
DUAL_COOL_SCHED = 3   # ThermostatSetpoint:DualSetpoint -> cooling schedule name


def _get(obj: dict, idx: int) -> str:
    f = obj["fields"]
    return f[idx - 1] if len(f) >= idx else ""


def _set(obj: dict, idx: int, value: str) -> None:
    f = obj["fields"]
    while len(f) < idx:
        f.append("")
    f[idx - 1] = value


def _find(objects: list[dict], type_name: str) -> list[dict]:
    t = type_name.lower()
    return [o for o in objects if o["type"].lower() == t]


def zone_to_thermostat(objects: list[dict]) -> dict[str, str]:
    """Map ZONE NAME -> ThermostatSetpoint:DualSetpoint object name."""
    dual_names = {_get(o, 1).upper() for o in _find(objects, "ThermostatSetpoint:DualSetpoint")}
    mapping: dict[str, str] = {}
    for zct in _find(objects, "ZoneControl:Thermostat"):
        zone = _get(zct, ZCT_ZONE).upper()
        control = _get(zct, ZCT_CONTROL_NAME).upper()
        if zone and control in dual_names:
            mapping[zone] = control
    return mapping


def write_policy_snapshot(
    base_idf: str | Path,
    out_dir: str | Path,
    zone_setpoints: dict[str, tuple[float, float]],
    sim_hour: float,
    index: int,
    source: str = "agent",
    rationale: str = "",
) -> Path | None:
    """Write one modified .idf reflecting `zone_setpoints`. Returns the path.

    Never raises: this runs inside a live simulation and a snapshot failure must
    not take the run down.
    """
    try:
        objects = parse_idf_objects(base_idf)
        mapping = zone_to_thermostat(objects)
        by_name = {_get(o, 1).upper(): o for o in _find(objects, "ThermostatSetpoint:DualSetpoint")}

        changed = 0
        for zone, (heat, cool) in zone_setpoints.items():
            dual_name = mapping.get(zone.upper())
            if not dual_name or dual_name not in by_name:
                continue
            htg = f"ECOLOOP_HTG_{zone.upper()}"
            clg = f"ECOLOOP_CLG_{zone.upper()}"
            objects.append({"type": "Schedule:Constant", "fields": [htg, "", f"{heat:.2f}"]})
            objects.append({"type": "Schedule:Constant", "fields": [clg, "", f"{cool:.2f}"]})
            _set(by_name[dual_name], DUAL_HEAT_SCHED, htg)
            _set(by_name[dual_name], DUAL_COOL_SCHED, clg)
            changed += 1

        if changed == 0:
            return None

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"policy_{index:03d}_simhour_{int(sim_hour):05d}.idf"

        header = [
            "! ------------------------------------------------------------------",
            "! Eco-Loop Building Agents - runtime policy snapshot",
            f"! Policy #{index}  installed at simulation hour {sim_hour:.2f}",
            f"! Source: {source}",
        ]
        if rationale:
            header.append(f"! Rationale: {rationale[:180]}")
        header += [
            "!",
            "! Occupied setpoints written into Schedule:Constant objects and wired",
            "! into each zone's ThermostatSetpoint:DualSetpoint:",
        ]
        for zone, (heat, cool) in sorted(zone_setpoints.items()):
            header.append(f"!   {zone:<18} heating {heat:5.2f} C   cooling {cool:5.2f} C")
        header += [
            "!",
            "! Note: unoccupied setback and precool are applied at runtime by the",
            "! reflex controller and are not representable in a constant schedule.",
            "! ------------------------------------------------------------------",
            "",
        ]

        path.write_text("\n".join(header) + serialize_idf_objects(objects), encoding="utf-8")
        return path
    except Exception:
        return None


def snapshot_summary(out_dir: str | Path) -> dict[str, Any]:
    out_dir = Path(out_dir)
    files = sorted(out_dir.glob("policy_*.idf"))
    return {"count": len(files), "files": [f.name for f in files]}
