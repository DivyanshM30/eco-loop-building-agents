# Prompt Engineering Notes

Working notes for the cognitive tier. Keep this updated as you tune — the spec
asks for prompt engineering strategy as a named deliverable, and a document that
records what *failed* is far more credible than one that only lists what worked.

---

## Model

- TODO: model and tag (e.g. `llama3.1:8b-instruct-q4_K_M`)
- TODO: backend (Ollama / vLLM), host spec, VRAM
- TODO: tokens/s observed

## Current prompt structure

System prompt (`SYSTEM_PROMPT` in `src/agent_loop.py`):

1. Role and objective — one sentence.
2. Hard rules as prose bullets (occupied hours, deadband ≥ 2 °C, PMV band,
   levers available).
3. Trimmed output schema from `Policy.json_schema_for_prompt()`.
4. The list of controllable zones.

User prompt (`USER_TEMPLATE`):

1. Aggregated performance over the window (compact JSON).
2. Constraint report.
3. Carbon intensity: current value + cheapest hours.
4. Currently active policy.
5. The required `valid_from_hour`.
6. On retries: the rejection feedback block, appended.

## What worked

- TODO
- e.g. "JSON mode raised first-attempt validity from __% to __%"
- e.g. "restating bounds in prose as well as schema cut out-of-range setpoints"

## What did not work

- TODO
- e.g. "asking for per-timestep setpoints — model produced plausible but
  unusable output and latency made it moot"
- e.g. "indented JSON in tool results — more tokens, no adherence gain"

## Failure modes seen

| Symptom | Frequency | Mitigation |
|---|---|---|
| Setpoint outside bounds | TODO | Schema rejection + feedback retry |
| Deadband < 2 °C | TODO | Model validator, then Tier 1 clamp |
| Unknown zone name | TODO | `commit_policy` checks against `config.yaml` zones |
| Prose wrapped around JSON | TODO | `extract_json` fence/brace extraction |
| Timeout | TODO | Hard 20 s limit, keep last-known-good |
| Repeats the current policy unchanged | TODO | TODO |

## Prompt variants tried

| Variant | First-attempt valid % | Mean savings | Notes |
|---|---|---|---|
| v1 zero-shot | TODO | TODO | baseline prompt |
| v2 + one worked example | TODO | TODO | TODO |
| v3 + carbon curve in prompt | TODO | TODO | TODO |

## Ideas not yet tried

- One-shot example of a *good* policy in the system prompt (usually a large win
  at 7–8B).
- Chain-of-thought in a separate field, discarded before validation.
- Asking for two candidate policies and picking the lower-predicted-energy one.
- Few-shot examples drawn from the best-performing policies of the current run.
