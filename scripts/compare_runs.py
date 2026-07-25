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

# Pass criterion stated up front, so the result is a test rather than a story.
MAX_UNMET_HOUR_INCREASE_PCT = 1.0
PMV_BAND = (-0.5, 0.5)


def load_run(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def num(row: dict, key: str, default: float = 0.0) -> float:
    try:
        v = row.get(key, "")
        return float(v) if v not in ("", None) else default
    except (TypeError, ValueError):
        return default


def summarise(rows: list[dict], label: str) -> dict:
    if not rows:
        return {"label": label, "error": "no rows"}

    elec = sum(num(r, "electricity_kwh") for r in rows)
    gas = sum(num(r, "gas_kwh") for r in rows)
    unmet_h = sum(num(r, "unmet_heating_h") for r in rows)
    unmet_c = sum(num(r, "unmet_cooling_h") for r in rows)

    hours = [num(r, "sim_hour") for r in rows]
    span_h = (max(hours) - min(hours)) if len(hours) > 1 else 1.0
    dt_h = span_h / max(len(rows) - 1, 1)
    peak_kw = max((num(r, "electricity_kwh") / dt_h) for r in rows) if dt_h > 0 else 0.0

    pmv_vals = [num(r, "mean_pmv", default=float("nan")) for r in rows]
    pmv_vals = [v for v in pmv_vals if v == v]  # drop NaN
    in_band = (
        sum(1 for v in pmv_vals if PMV_BAND[0] <= v <= PMV_BAND[1]) / len(pmv_vals) * 100
        if pmv_vals else None
    )

    return {
        "label": label,
        "timesteps": len(rows),
        "sim_hours": round(span_h, 1),
        "electricity_kwh": round(elec, 2),
        "gas_kwh": round(gas, 2),
        "total_kwh": round(elec + gas, 2),
        "peak_electric_kw": round(peak_kw, 2),
        "unmet_heating_hours": round(unmet_h, 2),
        "unmet_cooling_hours": round(unmet_c, 2),
        "unmet_total_hours": round(unmet_h + unmet_c, 2),
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

    a = summarise(load_run(pa), "baseline")
    b = summarise(load_run(pb), "ai")

    savings = {
        "electricity_pct": pct_change(a["electricity_kwh"], b["electricity_kwh"]),
        "gas_pct": pct_change(a["gas_kwh"], b["gas_kwh"]),
        "total_energy_pct": pct_change(a["total_kwh"], b["total_kwh"]),
        "peak_demand_pct": pct_change(a["peak_electric_kw"], b["peak_electric_kw"]),
    }

    unmet_increase_pct = None
    if a["unmet_total_hours"] > 0:
        unmet_increase_pct = round(
            (b["unmet_total_hours"] - a["unmet_total_hours"]) / a["unmet_total_hours"] * 100, 2
        )
    elif b["unmet_total_hours"] > 0:
        unmet_increase_pct = float("inf")

    comfort_pass = (
        unmet_increase_pct is None
        or unmet_increase_pct <= MAX_UNMET_HOUR_INCREASE_PCT
    )

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
        ("unmet_total_hours", "{:.2f}"),
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

    print("\n  COMFORT GUARDRAIL")
    print("  " + "-" * 60)
    print(f"  unmet-hour change            {unmet_increase_pct}%  "
          f"(limit +{MAX_UNMET_HOUR_INCREASE_PCT}%)")
    print(f"  verdict                      {'PASS' if comfort_pass else 'FAIL'}")
    if b.get("pmv_mean") is None:
        print("  [warn] no PMV in the AI run -> Fanger model not enabled. "
              "Run scripts/instrument_idf.py, and set people_objects in config.yaml.")
    print("=" * 64 + "\n")

    out = {
        "baseline": a, "ai": b, "savings": savings,
        "comfort": {
            "unmet_increase_pct": unmet_increase_pct,
            "limit_pct": MAX_UNMET_HOUR_INCREASE_PCT,
            "pass": comfort_pass,
            "pmv_band": list(PMV_BAND),
        },
    }
    out_path = repo / "results" / "comparison.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"[json] {out_path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
