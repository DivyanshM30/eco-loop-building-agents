"""Policy schemas — safety layer #1.

Every action the LLM proposes must deserialise into `Policy` before it goes
anywhere near the simulation. Bounds live here; the Tier 1 clamp in
policy_executor.py is an independent second layer.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class ZonePolicy(BaseModel):
    """Setpoint band for one thermal zone."""

    zone: str = Field(description="Zone name, must match the IDF (case-insensitive)")
    cooling_sp_c: float = Field(ge=22.0, le=28.0, description="Occupied cooling setpoint (C)")
    heating_sp_c: float = Field(ge=16.0, le=22.0, description="Occupied heating setpoint (C)")

    @field_validator("zone")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()

    @model_validator(mode="after")
    def _deadband(self) -> "ZonePolicy":
        if self.cooling_sp_c - self.heating_sp_c < 2.0:
            raise ValueError(
                f"deadband too narrow for {self.zone}: "
                f"cooling {self.cooling_sp_c} - heating {self.heating_sp_c} < 2.0 C. "
                "Widen the band; simultaneous heating and cooling wastes energy."
            )
        return self


class Policy(BaseModel):
    """A complete supervisory control policy, valid from a given sim hour."""

    valid_from_hour: int = Field(ge=0, description="Simulation hour this policy takes effect")
    zones: list[ZonePolicy] = Field(min_length=1)
    night_setback_c: float = Field(
        default=0.0, ge=0.0, le=5.0,
        description="Degrees to relax setpoints outside occupied hours",
    )
    precool_hours: int = Field(
        default=0, ge=0, le=4,
        description="Hours before occupancy to pre-cool, exploiting low-carbon/off-peak periods",
    )
    rationale: str = Field(
        default="", max_length=600,
        description="Why this policy — shown to operators and logged for the audit trail",
    )

    def zone_map(self) -> dict[str, ZonePolicy]:
        return {z.zone: z for z in self.zones}

    @staticmethod
    def json_schema_for_prompt() -> dict[str, Any]:
        """Trimmed schema to embed in the system prompt (full schema is too verbose
        for a 7-8B model to follow reliably)."""
        return {
            "valid_from_hour": "int",
            "zones": [
                {
                    "zone": "str (one of the zone names given)",
                    "cooling_sp_c": "float 22.0-28.0",
                    "heating_sp_c": "float 16.0-22.0 (must be >= 2.0 below cooling_sp_c)",
                }
            ],
            "night_setback_c": "float 0.0-5.0",
            "precool_hours": "int 0-4",
            "rationale": "str, one or two sentences",
        }


class PolicyRejection(BaseModel):
    """Structured rejection returned to the agent — this is what makes
    self-correction possible instead of a blind retry."""

    accepted: bool = False
    rejected_fields: list[str] = Field(default_factory=list)
    reason: str = ""

    def as_feedback(self) -> str:
        fields = ", ".join(self.rejected_fields) if self.rejected_fields else "(unspecified)"
        return (
            f"Your previous policy was REJECTED.\n"
            f"Invalid fields: {fields}\n"
            f"Reason: {self.reason}\n"
            f"Fix only these fields and resubmit valid JSON."
        )


def parse_policy(payload: Any) -> tuple[Policy | None, PolicyRejection | None]:
    """Validate an arbitrary payload into a Policy.

    Returns (policy, None) on success or (None, rejection) with per-field detail.
    Never raises — the caller is inside a control loop that must not die.
    """
    try:
        return Policy.model_validate(payload), None
    except Exception as exc:  # pydantic ValidationError or anything else
        fields: list[str] = []
        errors = getattr(exc, "errors", None)
        if callable(errors):
            try:
                for err in exc.errors():
                    loc = ".".join(str(p) for p in err.get("loc", ()))
                    fields.append(loc or "(root)")
            except Exception:
                pass
        return None, PolicyRejection(
            accepted=False,
            rejected_fields=sorted(set(fields)),
            reason=str(exc)[:500],
        )
