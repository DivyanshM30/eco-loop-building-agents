"""Tier 2 — the cognitive layer.

Runs on a background thread so it can NEVER block the EnergyPlus timestep
callback. Contract with Tier 1:

  * Tier 2 only ever hands over a fully validated policy.
  * If the LLM is slow, unreachable, or produces garbage after all retries,
    Tier 2 gives up silently and Tier 1 keeps running the last-known-good policy.
  * Every attempt — including failures — is written to agent_trace.jsonl.

The retry path is the interesting part: a rejection comes back with the offending
field and reason, that text is appended to the next prompt, and the model
corrects itself. That loop is the "self-correction" the spec asks for.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable

from .config import Config
from .llm_client import LLMClient, LLMError
from .runtime_store import RuntimeStore
from .schemas import Policy, parse_policy
from .tools import ToolBox

SYSTEM_PROMPT = """You are a building energy optimisation agent controlling an \
EnergyPlus simulation of an office building through a supervisory control policy.

It is the COOLING season. Nearly all energy goes to cooling, so the cooling \
setpoint is your one significant lever.

DIRECTION OF EFFECT - get this right, it is the whole task:
- RAISING cooling_sp_c uses LESS energy (the chiller runs less).
- LOWERING cooling_sp_c uses MORE energy. Only ever do this if occupants are \
too warm.
- heating_sp_c has almost no effect in the cooling season. Leave it near 20.0. \
Lowering it does not save energy and does not help comfort.

HOW TO READ PMV (occupied hours only, target band -0.5 to +0.5):
- PMV below -0.2  =>  occupants are TOO COLD, the building is being overcooled. \
RAISE cooling_sp_c by 1.0 to 2.0 C. This saves energy AND improves comfort.
- PMV above +0.2  =>  occupants are TOO WARM. LOWER cooling_sp_c by 0.5 to 1.0 C.
- PMV between -0.2 and +0.2  =>  near ideal. Keep cooling_sp_c, and instead \
raise night_setback_c to save energy while nobody is present.

If pmv is null or missing, you have no comfort evidence. In that case NEVER \
lower cooling_sp_c - keep it, or raise it slightly. Lowering it without evidence \
spends energy for no known benefit.

Other rules:
- Include an entry for EVERY zone listed below. A policy that omits a zone is \
rejected, because omitted zones silently revert to a default.
- Be decisive. A change smaller than 0.5 C is not worth making.
- night_setback_c relaxes setpoints whenever zones are empty, including \
weekends. Higher is better for energy and costs no comfort. Prefer 4.0 to 5.0.
- precool_hours cools ahead of occupancy, shifting load to cheaper, \
lower-carbon hours. 1 to 2 is reasonable.
- cooling_sp_c must be at least 2.0 C above heating_sp_c.
- Never push occupied PMV outside -0.5..+0.5. A comfort violation is worse than \
a missed saving.

WORKED EXAMPLE. Given mean occupied PMV of -0.40 and cooling setpoints at \
24.0 C, the correct response is:
{example}

Respond with ONLY a JSON object matching this shape - no prose, no markdown:
{schema}

Zones you may control: {zones}
"""

# One-shot example. Small models follow a demonstrated policy far more reliably
# than a described one, and this one encodes the key inference: negative PMV
# means overcooling, so raise the cooling setpoint.
EXAMPLE_POLICY = {
    "valid_from_hour": 4344,
    "zones": [
        {"zone": "CORE_ZN", "cooling_sp_c": 25.5, "heating_sp_c": 20.0},
        {"zone": "PERIMETER_ZN_1", "cooling_sp_c": 25.5, "heating_sp_c": 20.0},
    ],
    "night_setback_c": 4.0,
    "precool_hours": 1,
    "rationale": "Occupied PMV of -0.40 shows the building is overcooled, so the "
                 "cooling setpoint is raised 1.5 C. This cuts cooling energy and "
                 "moves comfort toward neutral at the same time.",
}

USER_TEMPLATE = """Current simulation state (aggregated over the last {window} hours):

{aggregates}

Constraint report:
{constraints}

Grid carbon intensity:
{carbon}

Currently active policy:
{active_policy}

Produce an improved policy as JSON. valid_from_hour must be {sim_hour}.
"""


class AgentLoop:
    """Owns the LLM, the tools, and the retry/self-correction loop."""

    def __init__(
        self,
        cfg: Config,
        store: RuntimeStore,
        tools: ToolBox,
        on_policy: Callable[[Policy], None],
        llm: LLMClient | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.tools = tools
        self.on_policy = on_policy
        self.llm = llm or LLMClient.from_config(cfg)

        self.max_retries = int(cfg.get_path("agent.max_retries", 2))
        self.window_hours = int(cfg.get_path("agent.aggregate_window_hours", 24))
        self.zones = [str(z).upper() for z in cfg.get_path("zones", [])]

        self._thread: threading.Thread | None = None
        self._busy = threading.Event()
        self.invocations = 0
        self.successes = 0
        self.failures = 0

    # ------------------------------------------------------------ orchestration

    @property
    def busy(self) -> bool:
        return self._busy.is_set()

    def maybe_invoke(self, sim_hour: float, aggregates: dict[str, Any],
                     active_policy: dict[str, Any]) -> bool:
        """Fire the agent if it is not already working. Returns immediately.

        Called from the simulation callback, so it must not block for even one
        network round trip — hence the thread and the `busy` guard. Skipping an
        invocation is always preferable to stalling the simulation.
        """
        if self._busy.is_set():
            self.store.trace("agent_skipped", sim_hour=sim_hour, reason="previous call still running")
            return False

        self._busy.set()
        self._thread = threading.Thread(
            target=self._run_once,
            args=(sim_hour, aggregates, active_policy),
            name="eco-loop-agent",
            daemon=True,
        )
        self._thread.start()
        return True

    def invoke_blocking(self, sim_hour: float, aggregates: dict[str, Any],
                        active_policy: dict[str, Any]) -> bool:
        """Run one agent cycle synchronously. Returns True if a policy installed.

        Used only for the warm start. On a short simulation the wall clock is a
        second or two while a local LLM call is tens of seconds, so a purely
        asynchronous agent lands at most one policy per run and usually zero.
        Blocking for the FIRST policy guarantees every run is genuinely
        LLM-driven; every later invocation goes back through maybe_invoke() and
        stays off the critical path.
        """
        if self._busy.is_set():
            return False
        self._busy.set()
        before = self.successes
        try:
            self._run_once(sim_hour, aggregates, active_policy)
        finally:
            self._busy.clear()
        return self.successes > before

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # ------------------------------------------------------------- the agent run

    def _run_once(self, sim_hour: float, aggregates: dict[str, Any],
                  active_policy: dict[str, Any]) -> None:
        try:
            self.invocations += 1
            self.store.trace("agent_invoked", sim_hour=sim_hour, invocation=self.invocations)

            constraints = self.tools.get_constraint_report()
            carbon = self.tools.get_grid_carbon_intensity(int(aggregates.get("sim_hour", 12)) % 24)
            self.store.trace(
                "tool_calls",
                sim_hour=sim_hour,
                tools=["get_constraint_report", "get_grid_carbon_intensity"],
            )

            system = SYSTEM_PROMPT.format(
                schema=json.dumps(Policy.json_schema_for_prompt(), indent=2),
                example=json.dumps(EXAMPLE_POLICY, indent=2),
                zones=", ".join(self.zones),
            )
            user = USER_TEMPLATE.format(
                window=self.window_hours,
                aggregates=_compact(aggregates),
                constraints=_compact(constraints),
                carbon=_compact({k: carbon[k] for k in ("hour", "g_co2_per_kwh", "cheapest_hours")}),
                active_policy=_compact(active_policy),
                sim_hour=int(sim_hour),
            )

            feedback = ""
            for attempt in range(1, self.max_retries + 2):  # first try + retries
                prompt = user if not feedback else f"{user}\n\n{feedback}"
                try:
                    payload, meta = self.llm.complete_json(system, prompt)
                except LLMError as exc:
                    # Timeout or transport failure: give up, Tier 1 carries on.
                    self.failures += 1
                    self.store.trace("agent_llm_error", sim_hour=sim_hour,
                                     attempt=attempt, error=str(exc))
                    return

                self.store.trace("llm_response", sim_hour=sim_hour, attempt=attempt, **meta)

                policy, rejection = parse_policy(payload)
                if policy is not None:
                    result = self.tools.commit_policy(payload)
                    if result.get("accepted"):
                        self.successes += 1
                        self.store.trace(
                            "policy_installed", sim_hour=sim_hour, attempt=attempt,
                            rationale=policy.rationale[:200],
                            self_corrected=attempt > 1,
                        )
                        self.on_policy(policy)
                        return
                    feedback = (
                        f"Your previous policy was REJECTED.\n"
                        f"Invalid fields: {', '.join(result.get('rejected_fields', [])) or '(unspecified)'}\n"
                        f"Reason: {result.get('reason', '')}\n"
                        f"Fix only these fields and resubmit valid JSON."
                    )
                else:
                    # Trace schema-level rejections too, not just tool-level ones.
                    # These are the COMMON case (a bad setpoint never reaches
                    # commit_policy), so without this the dashboard would report
                    # zero rejections while self-corrections were happening.
                    self.store.trace(
                        "policy_rejected",
                        sim_hour=sim_hour,
                        attempt=attempt,
                        stage="schema",
                        rejected_fields=rejection.rejected_fields,
                        reason=rejection.reason,
                    )
                    feedback = rejection.as_feedback()

                self.store.trace("agent_retry", sim_hour=sim_hour, attempt=attempt,
                                 feedback=feedback[:300])

            self.failures += 1
            self.store.trace("agent_exhausted_retries", sim_hour=sim_hour,
                             attempts=self.max_retries + 1,
                             note="keeping last-known-good policy")

        except Exception as exc:  # last-resort guard: this thread must never crash loudly
            self.failures += 1
            self.store.trace("agent_unhandled_error", sim_hour=sim_hour, error=repr(exc))
        finally:
            self._busy.clear()

    # ---------------------------------------------------------------- diagnostics

    def stats(self) -> dict[str, Any]:
        return {
            "invocations": self.invocations,
            "successes": self.successes,
            "failures": self.failures,
            "success_rate": round(self.successes / self.invocations, 3)
            if self.invocations else None,
        }


def _compact(obj: Any) -> str:
    """Render tool output as compact JSON.

    Small models follow compact structures better than deeply indented ones, and
    it costs far fewer tokens — which matters because we call the model hundreds
    of times over an annual run.
    """
    try:
        return json.dumps(obj, separators=(",", ":"), default=str)
    except Exception:
        return str(obj)
