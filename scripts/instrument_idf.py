"""Prepare an EnergyPlus model for the Eco-Loop controller.

This script exists because of one specific trap: **the rubric names PMV, but
EnergyPlus does not report PMV unless the Fanger thermal comfort model is
explicitly enabled on every People object** — and Fanger in turn requires work
efficiency, clothing insulation and air velocity schedules to exist. Discovering
that at hour 20 of a hackathon is expensive. This does it in one command.

What it does:
  1. Lists Zone and People object names (paste these into config.yaml).
  2. Sets 'Thermal Comfort Model 1 Type' = Fanger on every People object and
     fills in the schedules Fanger requires, creating them if absent.
  3. Adds Output:EnergyManagementSystem (Verbose) so the run emits eplusout.edd,
     the authoritative list of valid actuators for THIS model.
  4. Adds Output:Variable entries for the control and scoring variables.
  5. Optionally shortens the RunPeriod for fast development iterations.

Usage:
    python scripts/instrument_idf.py --list-people
    python scripts/instrument_idf.py --source "C:/EnergyPlusV24-1-0/ExampleFiles/RefBldgSmallOfficeNew2004_Chicago.idf"
    python scripts/instrument_idf.py --source <path> --run-period-days 7
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_config  # noqa: E402
from src.tools import parse_idf_objects, serialize_idf_objects  # noqa: E402

# 1-based field indices in the People object (EnergyPlus Input Output Reference).
F_NAME = 1
F_WORK_EFF_SCHED = 15
F_CLO_METHOD = 16
F_CLO_METHOD_SCHED = 17
F_CLO_SCHED = 18
F_AIR_VEL_SCHED = 19
F_COMFORT_MODEL_1 = 20

SCHED_WORK_EFF = "ECOLOOP_WORKEFF"
SCHED_CLOTHING = "ECOLOOP_CLOTHING"
SCHED_AIRVEL = "ECOLOOP_AIRVEL"

# RunPeriod field indices
RP_BEGIN_MONTH = 2
RP_BEGIN_DAY = 3
RP_END_MONTH = 5
RP_END_DAY = 6


def get_field(obj: dict, idx: int) -> str:
    fields = obj["fields"]
    return fields[idx - 1] if len(fields) >= idx else ""


def set_field(obj: dict, idx: int, value: str) -> None:
    fields = obj["fields"]
    while len(fields) < idx:
        fields.append("")
    fields[idx - 1] = value


def find(objects: list[dict], type_name: str) -> list[dict]:
    t = type_name.lower()
    return [o for o in objects if o["type"].lower() == t]


def name_of(obj: dict) -> str:
    return get_field(obj, F_NAME)


def report(objects: list[dict]) -> None:
    zones = [name_of(o) for o in find(objects, "Zone")]
    people = [name_of(o) for o in find(objects, "People")]
    print("\n--- Paste into config.yaml ---\n")
    print("zones:")
    for z in zones:
        print(f'  - "{z.upper()}"')
    print("\npeople_objects:")
    for p in people:
        print(f'  - "{p.upper()}"')
    print("\n(zone/people names are UPPERCASE in the EnergyPlus API)")
    print(f"\n{len(zones)} zones, {len(people)} People objects\n")


def ensure_constant_schedule(objects: list[dict], name: str, value: float) -> bool:
    """Add a Schedule:Constant if it does not already exist. Returns True if added.

    Type Limits is left blank deliberately: supplying a name would require the
    matching ScheduleTypeLimits object to exist, which is one more dependency
    that can fail. Blank is valid.
    """
    for obj in find(objects, "Schedule:Constant"):
        if name_of(obj).lower() == name.lower():
            return False
    objects.append({"type": "Schedule:Constant", "fields": [name, "", str(value)]})
    return True


def enable_fanger(objects: list[dict]) -> tuple[int, list[str]]:
    """Enable the Fanger comfort model on every People object."""
    people = find(objects, "People")
    if not people:
        return 0, []

    added = []
    for nm, val in ((SCHED_WORK_EFF, 0.0), (SCHED_CLOTHING, 1.0), (SCHED_AIRVEL, 0.137)):
        if ensure_constant_schedule(objects, nm, val):
            added.append(nm)

    touched = 0
    for obj in people:
        if not get_field(obj, F_WORK_EFF_SCHED):
            set_field(obj, F_WORK_EFF_SCHED, SCHED_WORK_EFF)
        # ClothingInsulationSchedule is the simplest of the three methods —
        # it just reads a constant schedule instead of computing clo dynamically.
        set_field(obj, F_CLO_METHOD, "ClothingInsulationSchedule")
        set_field(obj, F_CLO_METHOD_SCHED, "")
        if not get_field(obj, F_CLO_SCHED):
            set_field(obj, F_CLO_SCHED, SCHED_CLOTHING)
        if not get_field(obj, F_AIR_VEL_SCHED):
            set_field(obj, F_AIR_VEL_SCHED, SCHED_AIRVEL)
        set_field(obj, F_COMFORT_MODEL_1, "Fanger")
        touched += 1

    return touched, added


def add_ems_reporting(objects: list[dict]) -> bool:
    """Emit eplusout.edd — the authoritative actuator list for this model."""
    if find(objects, "Output:EnergyManagementSystem"):
        return False
    objects.append({
        "type": "Output:EnergyManagementSystem",
        "fields": ["Verbose", "Verbose", "Verbose"],
    })
    return True


def add_output_variables(objects: list[dict], people: list[str]) -> int:
    """Add the Output:Variable objects the controller and scoring need.

    Note: request_variable() in the API also works at runtime, but declaring them
    here means a plain `energyplus` CLI run produces the same data — useful for
    debugging without Python in the loop.
    """
    wanted = [
        "Zone Mean Air Temperature",
        "Zone Air Relative Humidity",
        "Zone Thermostat Heating Setpoint Temperature",
        "Zone Thermostat Cooling Setpoint Temperature",
        "Zone Heating Setpoint Not Met Time",
        "Zone Cooling Setpoint Not Met Time",
        "Zone Air System Sensible Heating Energy",
        "Zone Air System Sensible Cooling Energy",
        "Site Outdoor Air Drybulb Temperature",
    ]
    if people:
        wanted.append("Zone Thermal Comfort Fanger Model PMV")
        wanted.append("Zone Thermal Comfort Fanger Model PPD")

    existing = {
        (get_field(o, 1).lower(), get_field(o, 2).lower())
        for o in find(objects, "Output:Variable")
    }
    added = 0
    for var in wanted:
        if ("*", var.lower()) in existing:
            continue
        objects.append({"type": "Output:Variable", "fields": ["*", var, "Timestep"]})
        added += 1

    if not find(objects, "Output:Meter"):
        for meter in ("Electricity:Facility", "NaturalGas:Facility"):
            objects.append({"type": "Output:Meter", "fields": [meter, "Timestep"]})
            added += 1
    return added


def shorten_run_period(objects: list[dict], days: int) -> bool:
    """Shrink the RunPeriod for fast iteration. Use annual only for final runs."""
    rps = find(objects, "RunPeriod")
    if not rps:
        return False
    # July window: exercises cooling, which is where setback/precool savings show.
    start_month, start_day = 7, 1
    end_day = min(start_day + days - 1, 28)
    for rp in rps:
        set_field(rp, RP_BEGIN_MONTH, str(start_month))
        set_field(rp, RP_BEGIN_DAY, str(start_day))
        set_field(rp, RP_END_MONTH, str(start_month))
        set_field(rp, RP_END_DAY, str(end_day))
    return True


def ensure_timestep(objects: list[dict], per_hour: int = 4) -> None:
    ts = find(objects, "Timestep")
    if ts:
        set_field(ts[0], 1, str(per_hour))
    else:
        objects.append({"type": "Timestep", "fields": [str(per_hour)]})


def main() -> int:
    ap = argparse.ArgumentParser(description="Instrument an IDF for Eco-Loop")
    ap.add_argument("--source", help="source .idf (defaults to config energyplus.idf)")
    ap.add_argument("--out", default="models/ai_instrumented.idf")
    ap.add_argument("--baseline-out", default="models/baseline.idf",
                    help="also write an uninstrumented-control copy for the A/B run")
    ap.add_argument("--list-people", action="store_true", help="report names and exit")
    ap.add_argument("--run-period-days", type=int, default=0,
                    help="shorten RunPeriod to N days (0 = leave unchanged)")
    ap.add_argument("--timestep", type=int, default=4, help="timesteps per hour")
    args = ap.parse_args()

    cfg = load_config()
    src = Path(args.source) if args.source else cfg.resolve("energyplus.idf")
    if not src.exists():
        print(f"[FAIL] source IDF not found: {src}", file=sys.stderr)
        print("       pass --source <path to an EnergyPlus ExampleFiles/*.idf>", file=sys.stderr)
        return 1

    objects = parse_idf_objects(src)
    print(f"[read] {src}  ({len(objects)} objects)")

    if args.list_people:
        report(objects)
        return 0

    repo = Path(__file__).resolve().parents[1]

    # Baseline copy: same model, same outputs, but the controller writes nothing.
    baseline_objects = parse_idf_objects(src)
    ensure_timestep(baseline_objects, args.timestep)
    people_names = [name_of(o) for o in find(baseline_objects, "People")]
    enable_fanger(baseline_objects)
    add_output_variables(baseline_objects, people_names)
    if args.run_period_days:
        shorten_run_period(baseline_objects, args.run_period_days)
    baseline_path = repo / args.baseline_out
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(serialize_idf_objects(baseline_objects), encoding="utf-8")
    print(f"[write] {baseline_path}")

    # AI copy: identical plus EMS reporting so we get eplusout.edd.
    ensure_timestep(objects, args.timestep)
    touched, added_scheds = enable_fanger(objects)
    ems = add_ems_reporting(objects)
    n_vars = add_output_variables(objects, people_names)
    shortened = shorten_run_period(objects, args.run_period_days) if args.run_period_days else False

    out_path = repo / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(serialize_idf_objects(objects), encoding="utf-8")
    print(f"[write] {out_path}")

    print(f"\n[fanger]   enabled on {touched} People object(s)")
    if added_scheds:
        print(f"[schedule] created {', '.join(added_scheds)}")
    print(f"[ems]      Output:EnergyManagementSystem added: {ems} (produces eplusout.edd)")
    print(f"[outputs]  {n_vars} Output:Variable/Meter objects added")
    if shortened:
        print(f"[period]   RunPeriod shortened to {args.run_period_days} day(s) in July")
    if touched == 0:
        print("\n[warn] no People objects found -> no PMV. Pick a model that has occupants "
              "(RefBldgSmallOfficeNew2004_Chicago does).")

    report(objects)
    print("Next:")
    print("  1. paste the zones/people_objects above into config.yaml")
    print("  2. python -m src.runner --check")
    print("  3. python -m src.runner --mode baseline")
    print("  4. python scripts/dump_actuators.py     # confirm actuator names\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
