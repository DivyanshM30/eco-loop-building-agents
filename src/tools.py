"""The agent's tool surface — implemented once, exposed twice.

Both consumers call these same functions:
  * mcp_server.py  wraps them as MCP tools (satisfies the spec's MCP requirement
                   and lets you drive the building from any MCP client, e.g. for
                   the demo video)
  * agent_loop.py  calls them in-process on the fast path during a live run

Design rules:
  * Tools return SMALL structured payloads. Aggregates, never raw timeseries.
  * `commit_policy` is the only tool that writes anything.
  * Rejections are structured (field + reason) so the agent can self-correct
    instead of blindly retrying.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import Config, carbon_intensity
from .runtime_store import RuntimeStore
from .schemas import parse_policy

MAX_LOG_LINES = 40


class ToolBox:
    def __init__(self, cfg: Config, store: RuntimeStore):
        self.cfg = cfg
        self.store = store

    # ------------------------------------------------------------------- reads

    def get_sim_status(self) -> dict[str, Any]:
        """Cheap orientation call. The agent should call this first."""
        snap = self.store.read_snapshot()
        if snap.get("status") == "no_snapshot":
            return {"status": "no_simulation_running"}
        return {
            "status": "running",
            "sim_hour": snap.get("sim_hour"),
            "date": f"{snap.get('month'):02d}-{snap.get('day'):02d}"
            if snap.get("month") else None,
            "hour_of_day": snap.get("hour"),
            "zones": list((snap.get("aggregates", {}).get("zone_temp_c") or {}).keys()),
            "outdoor_c": (snap.get("aggregates", {}).get("outdoor_c") or {}).get("mean"),
            "last_window_electricity_kwh": snap.get("aggregates", {}).get("electricity_kwh"),
            "cumulative_electricity_kwh": snap.get("aggregates", {}).get(
                "cumulative_electricity_kwh"
            ),
            "active_policy_source": snap.get("active_policy", {}).get("source"),
        }

    def query_timeseries(
        self, variable: str, zone: str | None = None, window_hours: int = 24
    ) -> dict[str, Any]:
        """Aggregated stats only — min/mean/max over the window.

        Deliberately cannot return arrays. This is the context-budget guard.
        """
        agg = self.store.read_snapshot().get("aggregates", {})
        var = variable.strip().lower()
        if var in ("zone_temp", "zone_temp_c", "temperature"):
            data = agg.get("zone_temp_c", {})
            if zone:
                return {"variable": "zone_temp_c", "zone": zone.upper(),
                        "stats": data.get(zone.upper()), "window_hours": window_hours}
            return {"variable": "zone_temp_c", "by_zone": data, "window_hours": window_hours}
        if var in ("outdoor", "outdoor_c", "oat"):
            return {"variable": "outdoor_c", "stats": agg.get("outdoor_c"),
                    "window_hours": window_hours}
        if var in ("pmv", "comfort"):
            return {"variable": "pmv", "stats": agg.get("pmv"), "window_hours": window_hours,
                    "note": "None means the Fanger model is not enabled on People objects"}
        if var in ("electricity", "energy", "kwh"):
            return {"variable": "electricity_kwh", "window_total": agg.get("electricity_kwh"),
                    "peak_kw": agg.get("peak_electric_kw"),
                    "cumulative": agg.get("cumulative_electricity_kwh"),
                    "window_hours": window_hours}
        return {
            "error": f"unknown variable '{variable}'",
            "available": ["zone_temp", "outdoor", "pmv", "electricity"],
        }

    def get_constraint_report(self) -> dict[str, Any]:
        """Structured answer to 'how am I doing against comfort and limits'."""
        snap = self.store.read_snapshot()
        agg = snap.get("aggregates", {})
        lim = self.cfg.get_path("limits", {})
        pmv = agg.get("pmv")

        violations: list[str] = []
        if pmv and pmv.get("min") is not None:
            if pmv["min"] < -0.5:
                violations.append(f"PMV too cold: min {pmv['min']} < -0.5")
            if pmv["max"] > 0.5:
                violations.append(f"PMV too warm: max {pmv['max']} > +0.5")
        unmet = (agg.get("unmet_heating_hours") or 0) + (agg.get("unmet_cooling_hours") or 0)
        if unmet > 1.0:
            violations.append(f"setpoint not met for {round(unmet, 2)} h in window")

        return {
            "sim_hour": agg.get("sim_hour"),
            "pmv": pmv,
            "pmv_target_band": [-0.5, 0.5],
            "unmet_heating_hours": agg.get("unmet_heating_hours"),
            "unmet_cooling_hours": agg.get("unmet_cooling_hours"),
            "violations": violations,
            "allowed_setpoint_ranges": {
                "cooling_sp_c": [lim.get("cooling_sp_min_c"), lim.get("cooling_sp_max_c")],
                "heating_sp_c": [lim.get("heating_sp_min_c"), lim.get("heating_sp_max_c")],
                "min_deadband_c": lim.get("min_deadband_c"),
            },
        }

    def get_grid_carbon_intensity(self, hour: int | None = None) -> dict[str, Any]:
        if hour is None:
            hour = int(self.store.read_snapshot().get("hour", 12) or 12)
        curve = {h: carbon_intensity(self.cfg, h) for h in range(24)}
        return {
            "hour": hour,
            "g_co2_per_kwh": carbon_intensity(self.cfg, hour),
            "next_24h_curve": curve,
            "cheapest_hours": sorted(curve, key=curve.get)[:4],
            "assumption": "synthetic diurnal curve from config.yaml (not measured data)",
        }

    def inspect_idf(self, object_type: str, name: str | None = None) -> dict[str, Any]:
        """Parse the active IDF and return matching objects. Satisfies the spec's
        'the LLM must use these tools to parse files' requirement."""
        idf_path = self.cfg.resolve("energyplus.idf")
        if not idf_path.exists():
            return {"error": f"IDF not found: {idf_path}"}
        try:
            objects = parse_idf_objects(idf_path)
        except Exception as exc:
            return {"error": f"IDF parse failed: {exc}"}

        want = object_type.strip().lower()
        hits = [o for o in objects if o["type"].lower() == want]
        if name:
            hits = [o for o in hits if o["fields"] and o["fields"][0].lower() == name.lower()]
        return {
            "object_type": object_type,
            "count": len(hits),
            "objects": hits[:10],
            "truncated": len(hits) > 10,
        }

    def read_error_log(self, severity: str = "warning", tail_n: int = MAX_LOG_LINES) -> dict[str, Any]:
        """Filter + dedupe eplusout.err.

        Raw .err files run to tens of thousands of lines. Sending them to a 7B
        model is impossible; sending a deduped count is useful. This is the
        'handling lengthy simulation logs' deliverable in executable form.
        """
        out_dir = self.cfg.resolve("energyplus.output_dir_ai")
        err = out_dir / "eplusout.err"
        if not err.exists():
            return {"status": "no_log_yet", "path": str(err)}

        raw = err.read_text(encoding="utf-8", errors="replace").splitlines()
        sev = severity.strip().lower()
        wanted = {"fatal": ("** Fatal"), "severe": ("** Severe"),
                  "warning": ("** Warning", "** Severe", "** Fatal")}.get(sev, ("** Warning",))

        counts: dict[str, int] = {}
        for line in raw:
            if any(w in line for w in (wanted if isinstance(wanted, tuple) else (wanted,))):
                key = re.sub(r"[-+]?\d*\.?\d+", "N", line.strip())[:220]
                counts[key] = counts.get(key, 0) + 1

        ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:tail_n]
        return {
            "severity": sev,
            "total_lines_in_file": len(raw),
            "unique_messages": len(counts),
            "compression_ratio": f"{len(raw)}:{len(ranked)}" if ranked else "n/a",
            "messages": [{"message": m, "count": c} for m, c in ranked],
        }

    # ------------------------------------------------------------------- write

    def commit_policy(self, policy: dict[str, Any]) -> dict[str, Any]:
        """The ONLY write tool. Validates, then queues for the simulation.

        Returns either {"accepted": true, ...} or a structured rejection the
        agent can act on.
        """
        parsed, rejection = parse_policy(policy)
        if rejection is not None:
            self.store.trace(
                "policy_rejected",
                rejected_fields=rejection.rejected_fields,
                reason=rejection.reason,
            )
            return rejection.model_dump()

        known = {str(z).upper() for z in self.cfg.get_path("zones", [])}
        unknown = [z.zone for z in parsed.zones if z.zone not in known]
        if unknown:
            rej = {
                "accepted": False,
                "rejected_fields": ["zones"],
                "reason": f"unknown zone(s) {unknown}; valid zones are {sorted(known)}",
            }
            self.store.trace("policy_rejected", **rej)
            return rej

        self.store.propose_policy(parsed.model_dump(), source="commit_policy")
        self.store.trace("policy_accepted", zones=len(parsed.zones),
                         rationale=parsed.rationale[:200])
        return {
            "accepted": True,
            "zones_updated": [z.zone for z in parsed.zones],
            "note": "queued; the reflex controller will apply it at the next timestep",
        }


# ----------------------------------------------------------------- IDF parsing

def parse_idf_objects(path: str | Path) -> list[dict[str, Any]]:
    """Minimal IDF parser.

    IDF grammar is simple enough to handle directly: '!' starts a comment,
    objects are comma-separated fields terminated by ';'. Avoids an eppy
    dependency (and eppy needs an IDD file, which is one more thing to configure).
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    # strip comments
    lines = [line.split("!", 1)[0] for line in text.splitlines()]
    flat = " ".join(lines)
    objects: list[dict[str, Any]] = []
    for chunk in flat.split(";"):
        parts = [p.strip() for p in chunk.split(",")]
        # Interior empty fields MUST be preserved — IDF is positional, and
        # scripts/instrument_idf.py edits fields by index (e.g. field 20 =
        # 'Thermal Comfort Model 1 Type'). Dropping blanks would shift everything.
        if not parts or not parts[0]:
            continue
        objects.append({"type": parts[0], "fields": parts[1:]})
    return objects


def serialize_idf_objects(objects: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for obj in objects:
        fields = obj["fields"]
        if not fields:
            out.append(f"{obj['type']};")
            continue
        body = ",\n    ".join(str(f) for f in fields)
        out.append(f"{obj['type']},\n    {body};")
    return "\n\n".join(out) + "\n"
