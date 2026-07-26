"""Orchestrator — wires EnergyPlus, Tier 1 and Tier 2 together.

Usage:
    python -m src.runner --mode baseline          # stock schedules, no writes
    python -m src.runner --mode static            # Gate 2: hardcoded policy, no LLM
    python -m src.runner --mode ai                # full closed loop
    python -m src.runner --check                  # environment pre-flight, no sim

Run baseline and ai as SEPARATE processes. reset_state() clears registered
callbacks, and one process per run keeps the comparison honest.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

from .agent_loop import AgentLoop
from .config import expand_sensor_specs, load_config, load_energyplus_api
from .idf_snapshot import snapshot_summary, write_policy_snapshot
from .llm_client import LLMClient
from .policy_executor import (
    Limits,
    PolicyExecutor,
    default_policy_from_config,
    policy_to_active,
)
from .runtime_store import RuntimeStore
from .sensor_bus import SensorBus
from .tools import ToolBox

CSV_HEADER = [
    "sim_hour", "month", "day", "hour", "minute", "outdoor_c",
    "electricity_kwh", "gas_kwh",
    "cum_electricity_kwh", "cum_gas_kwh",
    "unmet_heating_h", "unmet_cooling_h",
    "mean_zone_c", "mean_pmv", "occupants", "policy_source",
]


def preflight(cfg) -> int:
    """Check every external dependency before burning simulation time."""
    ok = True
    print("=== Eco-Loop pre-flight ===")

    try:
        api = load_energyplus_api(cfg)
        print(f"  [ok]   pyenergyplus imported from {cfg.get_path('energyplus.install_root')}")
        try:
            ver = api.functional.ep_version()
            print(f"  [ok]   EnergyPlus version: {ver}")
        except Exception:
            pass
    except Exception as exc:
        ok = False
        print(f"  [FAIL] EnergyPlus: {exc}")

    for key in ("energyplus.idf", "energyplus.epw"):
        p = cfg.resolve(key)
        if p.exists():
            print(f"  [ok]   {key}: {p}")
        else:
            ok = False
            print(f"  [FAIL] {key} not found: {p}")

    health = LLMClient.from_config(cfg).health()
    if health.get("ok"):
        print(f"  [ok]   LLM endpoint reachable; model_available={health.get('model_available')}")
        if health.get("model_available") is False:
            print(f"         configured model '{cfg.get_path('llm.model')}' not in {health.get('models')}")
    else:
        print(f"  [warn] LLM endpoint unreachable: {health.get('error')}")
        print("         baseline and static modes still work; ai mode will fall back to default policy")

    zones = cfg.get_path("zones", [])
    print(f"  [info] {len(zones)} zones configured: {', '.join(zones) or '(none)'}")
    if not cfg.get_path("people_objects"):
        print("  [warn] people_objects is empty -> no PMV output -> cannot score the 20% comfort criterion")
        print("         run: python scripts/instrument_idf.py --list-people")

    print("=== " + ("PASS" if ok else "FAIL") + " ===")
    return 0 if ok else 1


def run(mode: str, cfg, run_hours_limit: float | None = None) -> Path:
    api = load_energyplus_api(cfg)
    state = api.state_manager.new_state()

    if not cfg.get_path("energyplus.console_output", False):
        try:
            api.runtime.set_console_output_status(state, False)
        except Exception:
            pass

    zones = [str(z).upper() for z in cfg.get_path("zones", [])]
    var_specs, meters = expand_sensor_specs(cfg)
    bus = SensorBus(api, var_specs, meters, zones)

    limits = Limits.from_config(cfg.get_path("limits", {}))
    executor = PolicyExecutor(limits, default_policy_from_config(cfg))

    store = RuntimeStore(cfg.resolve("paths.store_dir"))
    store.reset()
    box = ToolBox(cfg, store)

    # Deliverable #2: materialise each installed policy as a runnable .idf.
    gen_dir = cfg.resolve("energyplus.idf").parent / "generated"
    snap_state = {"n": 0}
    MAX_SNAPSHOTS = 20   # an annual run installs 365 policies; cap the artifacts

    def snapshot(active, sim_hour: float, source: str, rationale: str = "") -> None:
        if snap_state["n"] >= MAX_SNAPSHOTS:
            return
        snap_state["n"] += 1
        write_policy_snapshot(
            cfg.resolve("energyplus.idf"), gen_dir,
            active.zone_setpoints, sim_hour, snap_state["n"], source, rationale,
        )

    agent: AgentLoop | None = None
    if mode == "ai" and cfg.get_path("agent.enabled", True):
        def on_agent_policy(p):
            active = policy_to_active(p, cfg)
            executor.set_policy(active)
            store.record_installed_policy(p.model_dump(), p.valid_from_hour, "agent")
            snapshot(active, p.valid_from_hour, "agent", p.rationale)

        agent = AgentLoop(cfg, store, box, on_policy=on_agent_policy)

    results_dir = cfg.resolve("paths.results_dir")
    csv_path = results_dir / f"run_{mode}.csv"
    fh = open(csv_path, "w", newline="", encoding="utf-8")
    writer = csv.writer(fh)
    writer.writerow(CSV_HEADER)

    cadence = float(cfg.get_path("agent.cadence_sim_hours", 24))
    window = int(cfg.get_path("agent.aggregate_window_hours", 24))
    counters = {"steps": 0, "warmup_skipped": 0, "last_agent_hour": -1e9, "errors": 0,
                "bind_failed": False, "bind_reported": False, "design_day_skipped": 0}
    t_start = time.time()

    def on_timestep(s):
        # Rule 2: handles are invalid until the API says data is ready.
        if not bus.bind(s):
            return

        # Bind failure: report ONCE and go inert. Raising here would be swallowed
        # by the ctypes boundary and repeated every timestep, burying the message.
        if counters["bind_failed"]:
            return
        if bus.bind_errors and not counters["bind_reported"]:
            counters["bind_reported"] = True
            counters["bind_failed"] = True
            print("\n" + "=" * 70, file=sys.stderr)
            print(bus.bind_error_report(), file=sys.stderr)
            print("=" * 70, file=sys.stderr)
            print("Simulation will finish but no data is being collected.\n", file=sys.stderr)
            return

        # Rule 3: warmup data is not physical, never use it.
        if bus.is_warmup(s):
            counters["warmup_skipped"] += 1
            return

        # Rule 4: design-day environments are not the RunPeriod. Excluding them
        # is what keeps the A/B comparison honest.
        if not bus.is_weather_run(s):
            counters["design_day_skipped"] += 1
            return

        try:
            sample = bus.read(s)

            # --- Tier 1: always runs, pure arithmetic, cannot block -----------
            if mode in ("static", "ai"):
                for zone in zones:
                    # Measured per-zone occupancy drives setback. Falls back to
                    # building-wide occupancy, then to the clock, so a model
                    # without occupant-count output still works.
                    if zone in sample.zone_occupants:
                        occupied = sample.zone_occupants[zone] > 0.0
                    elif sample.zone_occupants:
                        occupied = sample.occupants > 0.0
                    else:
                        occupied = None
                    heat, cool = executor.compute(zone, sample.hour, occupied)
                    sample.setpoints[zone] = (heat, cool)
                    bus.write_setpoints(s, zone, heat, cool)

            bus.push(sample)
            counters["steps"] += 1

            # --- Bookkeeping + Tier 2 trigger --------------------------------
            if counters["steps"] % 4 == 0:  # ~hourly at 15-min timesteps
                agg = bus.aggregates(window)
                store.write_snapshot({
                    "sim_hour": sample.sim_hour,
                    "month": sample.month, "day": sample.day,
                    "hour": sample.hour, "minute": sample.minute,
                    "mode": mode,
                    "aggregates": agg,
                    "active_policy": {
                        "source": executor.active.source,
                        "night_setback_c": executor.active.night_setback_c,
                        "precool_hours": executor.active.precool_hours,
                        "zone_setpoints": {
                            k: list(v) for k, v in executor.active.zone_setpoints.items()
                        },
                    },
                })

                for proposal in store.drain_inbox():
                    from .schemas import parse_policy
                    pol, _ = parse_policy(proposal.get("policy", {}))
                    if pol is not None:
                        active = policy_to_active(pol, cfg)
                        executor.set_policy(active)
                        store.record_installed_policy(pol.model_dump(), sample.sim_hour, "mcp")
                        snapshot(active, sample.sim_hour, "mcp", pol.rationale)

                if agent is not None and sample.sim_hour - counters["last_agent_hour"] >= cadence:
                    counters["last_agent_hour"] = sample.sim_hour
                    agent.maybe_invoke(sample.sim_hour, agg, {
                        "source": executor.active.source,
                        "night_setback_c": executor.active.night_setback_c,
                        "precool_hours": executor.active.precool_hours,
                    })

            mean_zone = (
                sum(sample.zone_temps.values()) / len(sample.zone_temps)
                if sample.zone_temps else 0.0
            )
            mean_pmv = (
                sum(sample.zone_pmv.values()) / len(sample.zone_pmv)
                if sample.zone_pmv else ""
            )
            writer.writerow([
                round(sample.sim_hour, 3), sample.month, sample.day, sample.hour, sample.minute,
                round(sample.outdoor_c, 2),
                round(sample.electricity_kwh, 6), round(sample.gas_kwh, 6),
                round(bus.total_j.get("electricity", 0.0) / 3.6e6, 4),
                round(bus.total_j.get("gas", 0.0) / 3.6e6, 4),
                round(sample.unmet_heating_h, 4), round(sample.unmet_cooling_h, 4),
                round(mean_zone, 3),
                round(mean_pmv, 3) if mean_pmv != "" else "",
                round(sample.occupants, 2),
                executor.active.source,
            ])

        except Exception as exc:
            # A bug in our bookkeeping must not abort a multi-hour simulation.
            counters["errors"] += 1
            if counters["errors"] <= 5:
                print(f"[warn] timestep error #{counters['errors']}: {exc!r}", file=sys.stderr)

    # Static mode installs exactly one policy, so snapshot it up front.
    if mode == "static":
        snapshot(executor.active, 0.0, "static (config default_policy)")

    bus.request_variables(state)  # Rule 1: before the run, not after
    api.runtime.callback_end_zone_timestep_after_zone_reporting(state, on_timestep)

    out_dir = cfg.resolve(
        "energyplus.output_dir_ai" if mode != "baseline" else "energyplus.output_dir_baseline"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    argv = ["-d", str(out_dir), "-w", str(cfg.resolve("energyplus.epw")),
            str(cfg.resolve("energyplus.idf"))]

    print(f"[run] mode={mode} -> {out_dir}")
    exit_code = api.runtime.run_energyplus(state, argv)

    if agent is not None:
        agent.join(timeout=float(cfg.get_path("agent.timeout_s", 20)) + 5)

    fh.close()
    api.state_manager.delete_state(state)

    elapsed = time.time() - t_start
    elec = bus.total_j.get("electricity", 0.0) / 3.6e6
    gas = bus.total_j.get("gas", 0.0) / 3.6e6
    print(f"[done] exit={exit_code} steps={counters['steps']} "
          f"warmup_skipped={counters['warmup_skipped']} "
          f"design_days_skipped={counters['design_day_skipped']} "
          f"errors={counters['errors']} wall={elapsed:.1f}s")

    # Report the simulated span explicitly. A non-contiguous span (e.g. 01-21
    # and 07-21) means design days leaked in; a span far shorter than the
    # RunPeriod means it never ran.
    if bus.history:
        first, last = bus.history[0], bus.history[-1]
        span_h = last.sim_hour - first.sim_hour
        print(f"[span] {first.month:02d}-{first.day:02d} {first.hour:02d}:00 "
              f"-> {last.month:02d}-{last.day:02d} {last.hour:02d}:00 "
              f"({span_h / 24:.1f} days, {len(bus.history)} samples)")
        expected = len(bus.history) / 4.0  # hours at 4 timesteps/hour
        if span_h > 0 and expected / (span_h + 1e-9) < 0.9:
            print("[warn] sample count is well below the span — the period may be "
                  "discontinuous (design days?). Check the CSV dates.")
    print(f"[energy] electricity={elec:.1f} kWh  gas={gas:.1f} kWh")
    print(f"[tier1] applied={executor.applied_count} fallbacks={executor.fallback_count} "
          f"policy_source={executor.active.source}")
    if agent is not None:
        print(f"[tier2] {agent.stats()}")
    snaps = snapshot_summary(gen_dir)
    if snaps["count"]:
        print(f"[models] {snaps['count']} runtime policy snapshot(s) -> {gen_dir}")
    print(f"[csv] {csv_path}")

    if counters["bind_failed"]:
        print("\n[FAILED] handle binding failed — no data collected. See the error above.",
              file=sys.stderr)
        raise SystemExit(2)
    if counters["steps"] == 0:
        print("\n[FAILED] zero timesteps recorded. Check the RunPeriod in your IDF.",
              file=sys.stderr)
        raise SystemExit(2)
    return csv_path


def main() -> int:
    ap = argparse.ArgumentParser(description="Eco-Loop Building Agents")
    ap.add_argument("--mode", choices=["baseline", "static", "ai"], default="ai")
    ap.add_argument("--config", default=None)
    ap.add_argument("--check", action="store_true", help="pre-flight only, no simulation")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.check:
        return preflight(cfg)
    run(args.mode, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
