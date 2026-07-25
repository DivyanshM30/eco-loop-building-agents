"""Configuration loading and EnergyPlus API bootstrapping.

Every path, bound and cadence in the project comes from config.yaml via this
module. Nothing else in src/ reads the filesystem for configuration.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "config.yaml"


class Config(dict):
    """dict with dotted-path access: cfg.get_path('agent.timeout_s')."""

    def get_path(self, dotted: str, default: Any = None) -> Any:
        node: Any = self
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def resolve(self, dotted: str, default: Any = None) -> Path:
        """Resolve a config value to an absolute path (relative to repo root)."""
        raw = self.get_path(dotted, default)
        if raw is None:
            raise KeyError(f"missing path config: {dotted}")
        p = Path(str(raw)).expanduser()
        return p if p.is_absolute() else (REPO_ROOT / p).resolve()


def load_config(path: str | Path | None = None) -> Config:
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    if not cfg_path.exists():
        raise FileNotFoundError(f"config not found: {cfg_path}")
    with open(cfg_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    cfg = Config(data)

    # Environment overrides — handy for CI and for teammates with different installs.
    if os.environ.get("ECOLOOP_EPLUS_ROOT"):
        cfg["energyplus"]["install_root"] = os.environ["ECOLOOP_EPLUS_ROOT"]
    if os.environ.get("ECOLOOP_LLM_BASE_URL"):
        cfg["llm"]["base_url"] = os.environ["ECOLOOP_LLM_BASE_URL"]
    if os.environ.get("ECOLOOP_LLM_MODEL"):
        cfg["llm"]["model"] = os.environ["ECOLOOP_LLM_MODEL"]

    for key in ("paths.results_dir", "paths.store_dir"):
        cfg.resolve(key).mkdir(parents=True, exist_ok=True)
    return cfg


def load_energyplus_api(cfg: Config):
    """Import pyenergyplus from the configured install root and return the API.

    pyenergyplus is NOT a pip package — it ships inside the EnergyPlus install.
    We must prepend the install root to sys.path before importing.
    """
    root = Path(str(cfg.get_path("energyplus.install_root"))).expanduser()
    if not root.exists():
        raise FileNotFoundError(
            f"EnergyPlus install root not found: {root}\n"
            "Set energyplus.install_root in config.yaml (or $ECOLOOP_EPLUS_ROOT) "
            "to the directory that contains the 'pyenergyplus' folder."
        )
    if not (root / "pyenergyplus").is_dir():
        raise FileNotFoundError(
            f"No 'pyenergyplus' folder inside {root}. "
            "This should be the EnergyPlus install root, e.g. C:/EnergyPlusV24-1-0"
        )
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from pyenergyplus.api import EnergyPlusAPI  # noqa: E402  (path set above)

    return EnergyPlusAPI()


def expand_sensor_specs(cfg: Config) -> tuple[list[dict], list[str]]:
    """Expand the '__ZONE__' placeholder in sensor specs into per-zone entries."""
    zones = [str(z).upper() for z in cfg.get_path("zones", [])]
    people = [str(p).upper() for p in cfg.get_path("people_objects", []) or []]
    out: list[dict] = []
    for spec in cfg.get_path("sensors.variables", []):
        name, key = spec["name"], str(spec["key"])
        if key == "__ZONE__":
            out.extend({"name": name, "key": z} for z in zones)
        elif key == "__PEOPLE__":
            out.extend({"name": name, "key": p} for p in people)
        else:
            out.append({"name": name, "key": key.upper()})

    # PMV output is keyed on the People object name, not the zone.
    for p in people:
        out.append({"name": "Zone Thermal Comfort Fanger Model PMV", "key": p})

    meters = [str(m) for m in cfg.get_path("sensors.meters", [])]
    return out, meters


def carbon_intensity(cfg: Config, hour: int) -> float:
    """Synthetic diurnal grid carbon intensity in gCO2/kWh.

    Documented assumption, not measured data. Replace with a real feed if you
    have an API key — the interface is intentionally a single function.
    """
    c = cfg.get_path("carbon", {})
    peak_lo, peak_hi = c.get("peak_hours", [17, 21])
    trough_lo, trough_hi = c.get("trough_hours", [1, 5])
    h = int(hour) % 24
    if peak_lo <= h < peak_hi:
        return float(c.get("peak_g_per_kwh", 620.0))
    if trough_lo <= h < trough_hi:
        return float(c.get("trough_g_per_kwh", 250.0))
    return float(c.get("base_g_per_kwh", 380.0))
