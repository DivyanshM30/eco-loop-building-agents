"""Parse eplusout.edd into the actuator list that actually exists for your model.

Guessed actuator names do not raise — `get_actuator_handle` returns -1 and every
subsequent `set_actuator_value` is a silent no-op. You get a simulation that runs
beautifully and controls nothing. This script is the antidote.

Prerequisite: the model must include
    Output:EnergyManagementSystem, Verbose, Verbose, Verbose;
(scripts/instrument_idf.py adds it) and you must have completed one run.

Usage:
    python scripts/dump_actuators.py
    python scripts/dump_actuators.py --filter "Zone Temperature Control"
    python scripts/dump_actuators.py --edd out_ai/eplusout.edd
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_config  # noqa: E402


def parse_edd(path: Path) -> list[tuple[str, str, str, str]]:
    """Return (component_type, control_type, key, units) tuples.

    .edd lines look like:
        EnergyManagementSystem:Actuator Available,ZONE ONE,Zone Temperature Control,Heating Setpoint,[C]
    Field order in the file is key, component_type, control_type — note that this
    is NOT the argument order of get_actuator_handle(state, type, control, key).
    """
    rows: list[tuple[str, str, str, str]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "Actuator Available" not in line:
                continue
            parts = [p.strip() for p in line.strip().split(",")]
            if len(parts) < 4:
                continue
            key, comp_type, control_type = parts[1], parts[2], parts[3]
            units = parts[4] if len(parts) > 4 else ""
            rows.append((comp_type, control_type, key, units))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edd", default=None, help="path to eplusout.edd")
    ap.add_argument("--filter", default=None, help="substring filter on component type")
    ap.add_argument("--csv", default="results/actuators.csv")
    args = ap.parse_args()

    cfg = load_config()
    repo = Path(__file__).resolve().parents[1]

    if args.edd:
        edd = Path(args.edd)
    else:
        candidates = [
            cfg.resolve("energyplus.output_dir_ai") / "eplusout.edd",
            cfg.resolve("energyplus.output_dir_baseline") / "eplusout.edd",
        ]
        edd = next((c for c in candidates if c.exists()), candidates[0])

    if not edd.exists():
        print(f"[FAIL] no .edd file at {edd}", file=sys.stderr)
        print("\nTo produce one:", file=sys.stderr)
        print("  1. python scripts/instrument_idf.py --source <your.idf>", file=sys.stderr)
        print("     (adds Output:EnergyManagementSystem, Verbose, Verbose, Verbose;)", file=sys.stderr)
        print("  2. python -m src.runner --mode baseline", file=sys.stderr)
        print("  3. re-run this script", file=sys.stderr)
        return 1

    rows = parse_edd(edd)
    if args.filter:
        needle = args.filter.lower()
        rows = [r for r in rows if needle in r[0].lower()]

    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for comp, control, key, _units in rows:
        grouped[(comp, control)].append(key)

    print(f"[read] {edd}")
    print(f"[found] {len(rows)} actuators, {len(grouped)} (type, control) combinations\n")

    for (comp, control), keys in sorted(grouped.items()):
        print(f"  {comp}  |  {control}")
        preview = sorted(set(keys))
        for k in preview[:8]:
            print(f"      key: {k}")
        if len(preview) > 8:
            print(f"      ... and {len(preview) - 8} more")
        print()

    out_csv = repo / args.csv
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["component_type", "control_type", "key", "units"])
        w.writerows(rows)
    print(f"[csv] {out_csv}")

    # Verify what the controller actually intends to use.
    zones = [str(z).upper() for z in cfg.get_path("zones", [])]
    print("\n--- checking config.yaml zones against this model ---")
    ok = True
    for zone in zones:
        for control in ("Heating Setpoint", "Cooling Setpoint"):
            match = any(
                comp == "Zone Temperature Control" and ctrl == control and zone in [k.upper() for k in keys]
                for (comp, ctrl), keys in grouped.items()
            )
            flag = "ok  " if match else "MISS"
            if not match:
                ok = False
            print(f"  [{flag}] ('Zone Temperature Control', '{control}', '{zone}')")
    if not ok:
        print("\n[warn] at least one actuator is missing. Fix config.yaml zones to match the "
              "keys listed above, or the controller will silently write nothing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
