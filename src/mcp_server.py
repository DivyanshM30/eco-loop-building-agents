"""MCP server exposing the building as a set of tools.

Run alongside a live simulation:
    python -m src.mcp_server

Then point any MCP client at it (stdio transport). The same ToolBox that the
in-process agent uses is exposed here, so behaviour is identical — no drift
between "what the agent can do" and "what the MCP server offers".

For the demo video this is the money shot: drive the running building from an
external MCP client and watch the setpoints change.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import load_config
from .runtime_store import RuntimeStore
from .tools import ToolBox

cfg = load_config()
store = RuntimeStore(cfg.resolve("paths.store_dir"))
box = ToolBox(cfg, store)

mcp = FastMCP("eco-loop-building-agent")


@mcp.tool()
def get_sim_status() -> dict[str, Any]:
    """Current simulation status: sim time, zones, outdoor temp, energy so far.

    Call this first to orient yourself before any other tool.
    """
    return box.get_sim_status()


@mcp.tool()
def query_timeseries(variable: str, zone: str | None = None, window_hours: int = 24) -> dict[str, Any]:
    """Aggregated statistics (min/mean/max) for a building variable.

    variable: one of "zone_temp", "outdoor", "pmv", "electricity"
    zone: optional zone name, only meaningful for zone_temp
    window_hours: how far back to aggregate

    Returns summary statistics only, never raw timeseries arrays.
    """
    return box.query_timeseries(variable, zone, window_hours)


@mcp.tool()
def get_constraint_report() -> dict[str, Any]:
    """Comfort and constraint status: PMV band, unmet setpoint hours, current
    violations, and the allowed setpoint ranges you must respect."""
    return box.get_constraint_report()


@mcp.tool()
def get_grid_carbon_intensity(hour: int | None = None) -> dict[str, Any]:
    """Grid carbon intensity in gCO2/kWh for a given hour, plus the 24 h curve
    and the four cheapest (lowest-carbon) hours to shift load into."""
    return box.get_grid_carbon_intensity(hour)


@mcp.tool()
def inspect_idf(object_type: str, name: str | None = None) -> dict[str, Any]:
    """Inspect objects in the active EnergyPlus input file.

    object_type: e.g. "Zone", "People", "ThermostatSetpoint:DualSetpoint"
    name: optional exact object name to filter to
    """
    return box.inspect_idf(object_type, name)


@mcp.tool()
def read_error_log(severity: str = "warning", tail_n: int = 40) -> dict[str, Any]:
    """Read EnergyPlus runtime diagnostics from eplusout.err.

    Returns deduplicated messages with occurrence counts rather than raw lines,
    so a log with tens of thousands of lines stays within a usable context budget.

    severity: "warning" | "severe" | "fatal"
    """
    return box.read_error_log(severity, tail_n)


@mcp.tool()
def commit_policy(policy: dict[str, Any]) -> dict[str, Any]:
    """Install a new supervisory control policy. The only tool that writes.

    Expected shape:
      {"valid_from_hour": int,
       "zones": [{"zone": str, "cooling_sp_c": 22.0-28.0, "heating_sp_c": 16.0-22.0}],
       "night_setback_c": 0.0-5.0,
       "precool_hours": 0-4,
       "rationale": str}

    On success returns {"accepted": true, ...}. On failure returns
    {"accepted": false, "rejected_fields": [...], "reason": "..."} — read the
    reason, fix only those fields, and call again.
    """
    return box.commit_policy(policy)


if __name__ == "__main__":
    mcp.run()
