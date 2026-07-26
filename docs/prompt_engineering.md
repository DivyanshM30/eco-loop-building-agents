# Prompt Engineering Notes

A record of what the model actually did wrong and the specific change that fixed
it, with measured outcomes. Every figure here comes from a real run against the
DOE reference small office, 1–7 July, Chicago TMY3.

---

## Model

| | |
|---|---|
| Model | `llama3.2:3b` (default Q4_K_M) |
| Server | Ollama 0.32.4, local, CPU inference |
| Interface | OpenAI-compatible `/chat/completions` with `response_format: json_object` |
| Temperature | **0.0** |
| max_tokens | 400 |
| Prompt tokens | 1403 first attempt, 1568 on a retry |
| Completion tokens | 269–292 |
| Latency | 29.3–36.3 s, mean 33.2 s |

Why 3B rather than 8B: the task is a small, highly constrained JSON emission, and
a 2 GB model pulls and runs on any laptop. `llm.model` in `config.yaml` is the only
change needed to move to `llama3.1:8b` or `qwen2.5:7b-instruct`.

Why temperature 0: with roughly two invocations per 7-day run, **one sampled
policy governs most of the week**. At temperature 0.2, three otherwise identical
runs produced 0.85 %, 3.68 % and 4.35 % electricity savings. A control system
should be reproducible given the same inputs.

---

## Prompt structure

**System prompt** (~650 tokens):

1. Role, and a statement that it is the cooling season
2. **Direction of effect** — which way each lever moves energy
3. **PMV → action rules** with explicit numeric thresholds
4. Null-PMV rule
5. Remaining constraints (all zones required, deadband, decisiveness)
6. One worked example
7. Trimmed output schema from `Policy.json_schema_for_prompt()`
8. The list of controllable zones

**User prompt**: aggregated 24 h performance, constraint report, carbon intensity,
the currently active policy, and the required `valid_from_hour`. Tool results are
rendered as compact JSON (no indentation) to save tokens and improve adherence.
On a retry, the rejection reason is appended verbatim.

---

## Version history — each row is a real failure

### v1 — generic description of the levers

> "Widening the deadband and increasing night setback saves energy. This is your
> main lever."

**What happened:** the first policy set cooling setpoints to **24.5–25.0 °C** —
*below* the hand-tuned 25.5 °C and barely above the baseline's 24.0 °C. It moved
the wrong way on the only lever that matters, then corrected to 25–27 °C two days
later.

**Result:** −1.83 % electricity, occupied PMV −0.181.

**Diagnosis:** the prompt named the levers but never said which direction saves
energy. "Widening the deadband" is ambiguous — it can be satisfied by lowering the
heating setpoint, which does nothing in July.

### v2 — explicit direction of effect, PMV→action rules, one-shot example

Added:

- `RAISING cooling_sp_c uses LESS energy. LOWERING it uses MORE.`
- `PMV below -0.2 => occupants TOO COLD, building overcooled. RAISE cooling_sp_c by 1.0 to 2.0 C.`
- `heating_sp_c has almost no effect in the cooling season. Leave it near 20.0.`
- A complete worked example: given PMV −0.40 and cooling at 24.0 °C, here is the
  correct policy.

**Result:** −3.68 % electricity, occupied PMV −0.071. **Doubled the saving.**

**New failure:** a policy covering only **2 of 5 zones**, with the rationale *"PMV
is null, so no action needed based on PMV."* Because `PolicyExecutor` replaces the
whole setpoint map, the three omitted zones silently reverted to the config
default. The model had also *lowered* `CORE_ZN` to 24.0 °C when PMV was
unavailable.

### v3 — null-PMV rule and all-zones requirement

Added to the prompt:

- `If pmv is null or missing, you have no comfort evidence. NEVER lower cooling_sp_c.`
- `Include an entry for EVERY zone listed. A policy that omits a zone is rejected.`

Added to `commit_policy`: reject any policy that does not cover all configured
zones, naming the missing ones.

**Result:** −4.35 % electricity, all policies covering all 5 zones.

**Remaining problem:** run-to-run variance of 0.85–4.35 % on identical inputs. The
prompt was correct; the model simply did not always follow it.

### v4 — temperature 0 and the direction guard in code

The insight: **a prompt instruction is a request, not a constraint.** The direction
of effect is physics, not a preference, so it belongs in code.

`commit_policy` now rejects any policy that lowers a zone's cooling setpoint while
occupied PMV is below −0.2, returning the measured PMV and the offending zones.
The guard is symmetric: above +0.2 the model may lower setpoints freely, and with
no PMV data it does not fire.

Also deferred the warm start until the building has been occupied, so the first
policy is made on real comfort data (PMV −0.59) rather than a null.

**Result: −6.76 % electricity, −7.64 % peak demand, occupied PMV −0.391 → +0.140,
PMV in band 82.8 % → 97.3 %. Reproducible across runs.** This beats the hand-tuned
static policy (−5.08 %).

---

## Outcome summary

| Version | Key change | Electricity | Occupied PMV | Failure observed |
|---|---|---|---|---|
| v1 | generic lever description | −1.83 % | −0.181 | lowered cooling to 24.5 °C |
| v2 | direction of effect + one-shot | −3.68 % | −0.071 | partial policy, 2 of 5 zones |
| v3 | null-PMV rule, all-zones required | −4.35 % | −0.017 | 0.85–4.35 % run variance |
| v4 | temperature 0 + direction guard in code | **−6.76 %** | **+0.140** | none outstanding |

---

## Failure modes and mitigations

| Symptom | Seen | Mitigation |
|---|---|---|
| Setpoint outside schema bounds | Yes | Pydantic bounds → rejection → feedback retry |
| Cooling setpoint moved the wrong way | Yes, repeatedly | Direction guard in `commit_policy` (code, not prompt) |
| Policy omitted zones | Yes | Zone-completeness check in `commit_policy` |
| Lowered setpoints with no PMV data | Yes | Explicit null-PMV rule in the prompt |
| Deadband < 2 °C | Not observed | Pydantic model validator, then Tier 1 clamp |
| Prose wrapped around JSON | Not observed | JSON mode + `extract_json` fence/brace fallback |
| Timeout | Once, at a 20 s limit | Raised to 120 s; last-known-good policy retained |
| Rationale arithmetic wrong | Yes — claimed "+2.7 C" for a 1.2 C change | Cosmetic only; rationale is not executed |

That last one is worth noting: the model's *stated reasoning* is not always
arithmetically consistent with the policy it emits. Only the structured fields are
executed, and they are validated and clamped. The rationale is an audit aid, not a
control input.

---

## What did not work

- **Describing levers without direction.** v1's "widening the deadband saves
  energy" was satisfied by lowering the heating setpoint — true to the letter,
  useless in July.
- **Relying on the prompt for hard constraints.** Every constraint that mattered
  ended up enforced in code as well. The prompt improves the *quality* of
  proposals; only validation guarantees their *safety*.
- **Indented JSON in tool results.** More tokens, no measurable adherence gain.
- **Temperature 0.2.** Reasonable for prose, wrong for a controller.

## Ideas not pursued

- Multiple candidate policies per invocation, ranked by predicted energy
- Few-shot examples drawn from the best-performing policies of the current run
- A larger model for comparison (`llama3.1:8b`) — one config line, but each run
  costs ~2 minutes and the guarded action space already caps the downside
- Chain-of-thought in a separate discarded field
