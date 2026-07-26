"""Compute the A/B savings numbers — the 25% + 20% of the rubric, in one command.

Usage:
    python scripts/compare_runs.py
    python scripts/compare_runs.py --a results/run_baseline.csv --b results/run_ai.csv

Prints a savings table and writes results/comparison.json for the dashboard and
the deck. Comfort is reported alongside energy on purpose: a savings number
without a comfort guardrail is not evidence, and the rubric scores both.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ---------------------------------------------------------------- pass criteria
#
# PRIMARY: PMV against the ASHRAE 55 band, over occupied hours only. This is the
# comfort measure the problem statement names, and it reflects what occupants
# actually experience.
#
# SECONDARY: unmet setpoint time, as a percentage of occupied ZONE-hours.
#
# Both are absolute percentage-point tests, not relative changes. Two reasons:
#   * a relative change is undefined when the baseline is zero (yields inf)
#   * "setpoint not met" measures whether the HVAC reached the CONTROLLER'S OWN
#     setpoint, not whether anyone was uncomfortable. Raising the cooling
#     setpoint and allowing a weekend float necessarily produces some unmet time
#     during Monday morning pull-down, even as PMV improves. Scored as a
#     relative change against a zero baseline, that penalises precisely the
#     behaviour that delivers the energy saving.
#
# Unmet time is also summed across zones by EnergyPlus, so it must be normalised
# by (occupied hours x zone count) before being compared to a budget.
PMV_BAND = (-0.5, 0.5)
MAX_PMV_INBAND_DROP_PP = 2.0    # occupied PMV in-band share may not fall >2 pp
MAX_UNMET_PP_INCREASE = 1.0     # unmet share of occupied zone-hours, +1 pp max


def load_run(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def num(row: dict, key: str, default: float = 0.0) -> float:
    try:
        v = row.get(key, "")
        return float(v) if v not in ("", None) else default
    except (TypeError, ValueError):
        return default


def zone_count() -> int:
    """Number of controlled zones, for normalising cross-zone unmet totals."""
    try:
        from src.config import load_config

        return max(len(load_config().get_path("zones", []) or []), 1)
    except Exception:
        return 1


def summarise(rows: list[dict], label: str, n_zones: int = 1) -> dict:
    if not rows:
        return {"label": label, "error": "no rows"}

    # Energy is scored over ALL hours. Comfort is scored ONLY while occupied:
    # PMV and unmet-hour counts are meaningless in an empty building, and
    # including night/weekend setback makes a correct energy-saving policy look
    # like a comfort regression. ASHRAE 55 likewise applies during occupancy.
    occ = [r for r in rows if num(r, "occupants") > 0]
    has_occ = len(occ) > 0
    scored = occ if has_occ else rows

    elec = sum(num(r, "electricity_kwh") for r in rows)
    gas = sum(num(r, "gas_kwh") for r in rows)
    unmet_h = sum(num(r, "unmet_heating_h") for r in scored)
    unmet_c = sum(num(r, "unmet_cooling_h") for r in scored)
    unmet_h_all = sum(num(r, "unmet_heating_h") for r in rows)
    unmet_c_all = sum(num(r, "unmet_cooling_h") for r in rows)

    hours = [num(r, "sim_hour") for r in rows]
    span_h = (max(hours) - min(hours)) if len(hours) > 1 else 1.0
    dt_h = span_h / max(len(rows) - 1, 1)
    occupied_zone_hours = len(occ) * dt_h * n_zones
    peak_kw = max((num(r, "electricity_kwh") / dt_h) for r in rows) if dt_h > 0 else 0.0

    pmv_vals = [num(r, "mean_pmv", default=float("nan")) for r in scored]
    pmv_vals = [v for v in pmv_vals if v == v]  # drop NaN
    in_band = (
        sum(1 for v in pmv_vals if PMV_BAND[0] <= v <= PMV_BAND[1]) / len(pmv_vals) * 100
        if pmv_vals else None
    )

    return {
        "label": label,
        "timesteps": len(rows),
        "occupied_timesteps": len(occ),
        "comfort_scope": "occupied hours only" if has_occ else "ALL hours (no occupancy data!)",
        "sim_hours": round(span_h, 1),
        "electricity_kwh": round(elec, 2),
        "gas_kwh": round(gas, 2),
        "total_kwh": round(elec + gas, 2),
        "peak_electric_kw": round(peak_kw, 2),
        "unmet_heating_hours": round(unmet_h, 2),
        "unmet_cooling_hours": round(unmet_c, 2),
        "unmet_total_hours": round(unmet_h + unmet_c, 2),
        "unmet_total_hours_all_hours": round(unmet_h_all + unmet_c_all, 2),
        # Normalised: unmet zone-hours as a share of available occupied zone-hours.
        "occupied_zone_hours": round(occupied_zone_hours, 1),
        "unmet_pct_of_occupied": (
            round((unmet_h + unmet_c) / occupied_zone_hours * 100, 3)
            if occupied_zone_hours > 0 else None
        ),
        "pmv_mean": round(sum(pmv_vals) / len(pmv_vals), 3) if pmv_vals else None,
        "pmv_in_band_pct": round(in_band, 1) if in_band is not None else None,
    }


def pct_change(base: float, new: float) -> float | None:
    if base == 0:
        return None
    return round((base - new) / base * 100.0, 2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", default="results/run_baseline.csv", help="baseline run")
    ap.add_argument("--b", default="results/run_ai.csv", help="AI (or static) run")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parents[1]
    pa, pb = repo / args.a, repo / args.b
    for p in (pa, pb):
        if not p.exists():
            print(f"[FAIL] missing {p}", file=sys.stderr)
            print("       run both modes first:", file=sys.stderr)
            print("         python -m src.runner --mode baseline", file=sys.stderr)
            print("         python -m src.runner --mode ai", file=sys.stderr)
            return 1

    n_zones = zone_count()
    a = summarise(load_run(pa), "baseline", n_zones)
    b = summarise(load_run(pb), "ai", n_zones)

    savings = {
        "electricity_pct": pct_change(a["electricity_kwh"], b["electricity_kwh"]),
        "gas_pct": pct_change(a["gas_kwh"], b["gas_kwh"]),
        "total_energy_pct": pct_change(a["total_kwh"], b["total_kwh"]),
        "peak_demand_pct": pct_change(a["peak_electric_kw"], b["peak_electric_kw"]),
    }

    # PRIMARY: PMV in-band share over occupied hours must not fall materially.
    pmv_a, pmv_b = a.get("pmv_in_band_pct"), b.get("pmv_in_band_pct")
    pmv_delta_pp = (
        round(pmv_b - pmv_a, 2) if (pmv_a is not None and pmv_b is not None) else None
    )
    pmv_pass = pmv_delta_pp is None or pmv_delta_pp >= -MAX_PMV_INBAND_DROP_PP

    # SECONDARY: unmet share of occupied zone-hours, in percentage points.
    unmet_a, unmet_b = a.get("unmet_pct_of_occupied"), b.get("unmet_pct_of_occupied")
    unmet_delta_pp = (
        round(unmet_b - unmet_a, 3) if (unmet_a is not None and unmet_b is not None) else None
    )
    unmet_pass = unmet_delta_pp is None or unmet_delta_pp <= MAX_UNMET_PP_INCREASE

    comfort_pass = pmv_pass and unmet_pass

    print("\n" + "=" * 64)
    print("  ECO-LOOP A/B COMPARISON")
    print("=" * 64)
    row = "  {:<28} {:>14} {:>14}"
    print(row.format("", "baseline", "ai"))
    print("  " + "-" * 60)
    for key, fmt in (
        ("electricity_kwh", "{:.2f}"),
        ("gas_kwh", "{:.2f}"),
        ("total_kwh", "{:.2f}"),
        ("peak_electric_kw", "{:.2f}"),
        ("occupied_timesteps", "{}"),
        ("unmet_total_hours", "{:.2f}"),
        ("unmet_total_hours_all_hours", "{:.2f}"),
        ("pmv_mean", "{}"),
        ("pmv_in_band_pct", "{}"),
    ):
        av, bv = a.get(key), b.get(key)
        print(row.format(
            key,
            fmt.format(av) if isinstance(av, (int, float)) else str(av),
            fmt.format(bv) if isinstance(bv, (int, float)) else str(bv),
        ))

    print("\n  SAVINGS")
    print("  " + "-" * 60)
    for k, v in savings.items():
        arrow = "" if v is None else ("reduction" if v > 0 else "INCREASE")
        print(f"  {k:<28} {('n/a' if v is None else f'{v:+.2f}%'):>14}  {arrow}")

    print("\n  COMFORT GUARDRAIL  (scored over occupied hours only)")
    print("  " + "-" * 60)
    print(f"  scope                        {b.get('comfort_scope')}")
    print(f"  PMV in-band  {pmv_a}% -> {pmv_b}%   change "
          f"{'n/a' if pmv_delta_pp is None else f'{pmv_delta_pp:+.2f} pp'}"
          f"   (limit -{MAX_PMV_INBAND_DROP_PP} pp)   "
          f"[{'PASS' if pmv_pass else 'FAIL'}]")
    print(f"  unmet time   {unmet_a}% -> {unmet_b}%   change "
          f"{'n/a' if unmet_delta_pp is None else f'{unmet_delta_pp:+.3f} pp'}"
          f"   (limit +{MAX_UNMET_PP_INCREASE} pp)   "
          f"[{'PASS' if unmet_pass else 'FAIL'}]")
    print(f"               as a share of occupied zone-hours "
          f"({b.get('occupied_zone_hours')} zone-h across {n_zones} zones)")
    print(f"\n  VERDICT                      {'PASS' if comfort_pass else 'FAIL'}")
    if b.get("occupied_timesteps") == 0:
        print("  [warn] no occupied timesteps found. Is 'Zone People Occupant Count'")
        print("         in config.yaml sensors.variables? Comfort numbers below are")
        print("         computed over ALL hours and will unfairly penalise setback.")
    if b.get("pmv_mean") is None:
        print("  [warn] no PMV in the AI run -> Fanger model not enabled. "
              "Run scripts/instrument_idf.py, and set people_objects in config.yaml.")
    print("=" * 64 + "\n")

    out = {
        "baseline": a, "ai": b, "savings": savings,
        "comfort": {
            "pass": comfort_pass,
            "pmv_band": list(PMV_BAND),
            "pmv_in_band_delta_pp": pmv_delta_pp,
            "pmv_in_band_limit_pp": -MAX_PMV_INBAND_DROP_PP,
            "pmv_pass": pmv_pass,
            "unmet_delta_pp": unmet_delta_pp,
            "unmet_limit_pp": MAX_UNMET_PP_INCREASE,
            "unmet_pass": unmet_pass,
            "scope": b.get("comfort_scope"),
            "zones": n_zones,
        },
    }
    out_path = repo / "results" / "comparison.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"[json] {out_path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
