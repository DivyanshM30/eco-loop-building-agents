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

Your job: given the last 24 hours of building performance, output a control \
policy that reduces energy use and carbon while keeping occupants comfortable.

Hard rules:
- Occupied hours are 07:00-19:00. Outside them, comfort matters much less.
- Widening the deadband and increasing night setback saves energy. This is your \
main lever.
- Pre-cooling before occupancy shifts load into cheaper, lower-carbon hours.
- Never let predicted mean vote (PMV) leave the -0.5..+0.5 band during occupied \
hours. Comfort violations are worse than missed savings.
- cooling_sp_c must be at least 2.0 C above heating_sp_c.

Respond with ONLY a JSON object matching this shape - no prose, no markdown:
{schema}

Zones you may control: {zones}
"""

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
