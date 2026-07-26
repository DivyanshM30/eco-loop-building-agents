"""Ask EnergyPlus what data the API actually exposes for THIS model.

`get_meter_handle` / `get_variable_handle` return -1 for anything that does not
exist, with no hint as to what does. This dumps the authoritative list via
`list_available_api_data_csv` and probes the meter names we care about.

    python scripts/dump_api_data.py

Writes results/api_data.csv (the full dump) and prints every available meter
plus a pass/fail probe of the names in config.yaml.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_config, load_energyplus_api  # noqa: E402

# Names worth probing: the ones we use, plus the usual alternates and renames.
CANDIDATES = [
    "Electricity:Facility",
    "ELECTRICITY:FACILITY",
    "Electricity:Building",
    "Electricity:HVAC",
    "ElectricityNet:Facility",
    "ElectricityPurchased:Facility",
    "NaturalGas:Facility",
    "Gas:Facility",
    "Cooling:Electricity",
    "Heating:Electricity",
    "InteriorLights:Electricity",
    "InteriorEquipment:Electricity",
    "Fans:Electricity",
]


def main() -> int:
    cfg = load_config()
    api = load_energyplus_api(cfg)
    state = api.state_manager.new_state()
    try:
        api.runtime.set_console_output_status(state, False)
    except Exception:
        pass

    repo = Path(__file__).resolve().parents[1]
    out_csv = repo / "results" / "api_data.csv"
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    done = {"dumped": False}

    def on_timestep(s):
        if done["dumped"]:
            return
        if not api.exchange.api_data_fully_ready(s):
            return
        done["dumped"] = True

        # Full authoritative dump of every variable, meter and actuator.
        try:
            blob = api.exchange.list_available_api_data_csv(s)
            text = blob.decode("utf-8", errors="replace") if isinstance(blob, bytes) else str(blob)
            out_csv.write_text(text, encoding="utf-8")
            print(f"[dump] {out_csv}  ({len(text.splitlines())} lines)")
        except Exception as exc:
            text = ""
            print(f"[warn] list_available_api_data_csv unavailable: {exc}")

        if text:
            meter_lines = sorted({
                ln.strip() for ln in text.splitlines()
                if "meter" in ln.lower() and ln.strip()
            })
            print(f"\n=== meter entries in the dump ({len(meter_lines)}) ===")
            for ln in meter_lines[:60]:
                print("   ", ln)
            if len(meter_lines) > 60:
                print(f"    ... and {len(meter_lines) - 60} more (see the CSV)")

        print("\n=== probing meter names with get_meter_handle ===")
        for name in CANDIDATES:
            h = api.exchange.get_meter_handle(s, name)
            flag = "OK  " if h >= 0 else "MISS"
            print(f"  [{flag}] handle={h:<6} {name}")

        print("\n=== names currently in config.yaml ===")
        for name in cfg.get_path("sensors.meters", []):
            h = api.exchange.get_meter_handle(s, name)
            print(f"  [{'OK  ' if h >= 0 else 'MISS'}] {name}")

        print("\nPut any OK name into sensors.meters in config.yaml.\n")

    api.runtime.callback_end_zone_timestep_after_zone_reporting(state, on_timestep)

    out_dir = repo / "out_probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    argv = ["-d", str(out_dir), "-w", str(cfg.resolve("energyplus.epw")),
            str(cfg.resolve("energyplus.idf"))]
    print("[run] probing (this runs the configured RunPeriod, so keep it short)...")
    api.runtime.run_energyplus(state, argv)
    api.state_manager.delete_state(state)

    if not done["dumped"]:
        print("[FAIL] api_data_fully_ready never became true — did the simulation run at all?")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
