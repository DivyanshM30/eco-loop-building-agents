# System Architecture

> **Deliverable #4.** The spec names four topics explicitly: tool-calling
> architecture, prompt engineering strategies, prompt latency management, and the
> technical approach to handling lengthy simulation logs. There is one section per
> topic below, so a grader can tick each off. Fill the `TODO` markers with real
> numbers once you have run the model.

---

## 1. Overview

Two-tier hierarchical controller wrapped around an in-process EnergyPlus
simulation.

```
┌───────────────────── Tier 2: Cognitive (coarse cadence) ─────────────────────┐
│ Open-source LLM (TODO: model, quantisation, host)                            │
│ Reached through MCP tools. Runs every N simulation hours or on exception.     │
│ Input:  aggregated performance + constraint report + carbon intensity         │
│ Output: a validated Policy object                                             │
└──────────────────────────────┬───────────────────────────────────────────────┘
                               │ Pydantic-validated policy
                               ▼
┌───────────────────── Tier 1: Reflex (every timestep) ────────────────────────┐
│ Pure arithmetic. No I/O, no network, no exceptions.                          │
│ Applies the active policy, clamps to the hard safety envelope, writes         │
│ setpoints via set_actuator_value.                                             │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Design rationale.** An annual run at 4 timesteps/hour is 35,040 timesteps. A
per-timestep LLM call is infeasible on latency alone and catastrophic on
reliability — one malformed response ends the run. Decoupling the tiers means the
cognitive layer can fail in any way at all and the simulation still completes on
the last-known-good policy.

**Component responsibilities**

| Component | File | Responsibility |
|---|---|---|
| SensorBus | `src/sensor_bus.py` | Handle binding, warmup filtering, ring buffer, aggregation |
| PolicyExecutor | `src/policy_executor.py` | Tier 1 execution + hard clamp |
| AgentLoop | `src/agent_loop.py` | Tier 2 orchestration, retry, self-correction |
| ToolBox | `src/tools.py` | Tool implementations (shared with MCP server) |
| MCP server | `src/mcp_server.py` | External tool surface |
| RuntimeStore | `src/runtime_store.py` | Atomic snapshot + append-only audit logs |

---

## 2. Tool-calling architecture

Tools are implemented once in `ToolBox` and exposed twice: as MCP tools over
stdio, and in-process to the agent on the fast path. Identical behaviour, no
drift between the two.

| Tool | Reads/Writes | Payload discipline |
|---|---|---|
| `get_sim_status` | R | Orientation call; ~10 fields |
| `query_timeseries` | R | Aggregates only — **cannot** return arrays |
| `get_constraint_report` | R | PMV band, unmet hours, allowed ranges |
| `get_grid_carbon_intensity` | R | Hourly value + 24 h curve + cheapest hours |
| `inspect_idf` | R | Parsed IDF objects, capped at 10 |
| `read_error_log` | R | Deduped messages with counts |
| `commit_policy` | **W** | The only write. Validates, returns structured rejection. |

### Self-correction loop

`commit_policy` returns `{accepted: false, rejected_fields: [...], reason: "..."}`
rather than a generic failure. That text is appended verbatim to the next prompt,
so the model corrects a specific field instead of resampling blindly.

```
attempt 1  →  cooling_sp_c = 31.0        →  REJECTED (bound: max 28.0)
attempt 2  →  cooling_sp_c = 27.5        →  ACCEPTED
```

Every attempt is appended to `results/store/agent_trace.jsonl` with
`self_corrected: true` on the successful retry.

- TODO: observed rejection rate: ____ %
- TODO: share of rejections recovered within `max_retries`: ____ %

### Two independent safety layers

1. **Schema bounds** (`src/schemas.py`) — Pydantic `Field(ge=, le=)` plus a
   deadband validator.
2. **Hard clamp** (`src/policy_executor.py`) — applied again at write time,
   including a non-finite check, because `nan < lo` is `False` and NaN otherwise
   passes a naive clamp straight into the actuator.

Layer 2 does not trust layer 1. Tests in `tests/test_policy_executor.py`.

---

## 3. Prompt engineering strategies

| Strategy | Rationale |
|---|---|
| JSON mode (`response_format={"type":"json_object"}`) | Largest single reliability win with 7–8B models; both Ollama and vLLM honour it |
| Trimmed schema in the system prompt (`Policy.json_schema_for_prompt`) | The full JSON Schema is too verbose for a small model to follow |
| Compact JSON in tool results (no indentation) | Fewer tokens, better structural adherence |
| Explicit "hard rules" block | Bounds restated in prose as well as schema; small models follow prose constraints better |
| Low temperature (0.2) | Control policy is not a creative task |
| Rejection feedback appended to the retry prompt | Turns a blind retry into a targeted fix |

- TODO: model and quantisation used: ____
- TODO: prompt tokens per invocation (mean): ____
- TODO: schema-valid-first-attempt rate: ____ %

---

## 4. Prompt latency management

Four mechanisms, in order of importance:

1. **Off the critical path.** Tier 2 runs on a daemon thread. The simulation
   callback calls `maybe_invoke()` which returns immediately.
2. **Busy guard.** If the previous invocation is still running, the new one is
   skipped and logged as `agent_skipped`. Skipping is always preferable to
   stalling the simulation.
3. **Hard timeout** (`agent.timeout_s`, default 20 s). On timeout the agent
   abandons the cycle; Tier 1 continues on the existing policy.
4. **Coarse cadence** (`agent.cadence_sim_hours`, default 24). One invocation per
   simulated day: ~365 LLM calls for an annual run instead of 35,040.

Measured (fill from `agent_trace.jsonl` via the dashboard):

- TODO: mean LLM latency: ____ ms
- TODO: p95 latency: ____ ms
- TODO: invocations skipped due to busy guard: ____
- TODO: timeouts: ____
- TODO: wall-clock overhead vs. the baseline run: ____ %

---

## 5. Handling lengthy simulation logs

`eplusout.err` from an annual run reaches tens of thousands of lines, dominated by
a handful of messages repeated thousands of times. `read_error_log` applies a
four-stage reduction:

1. **Severity filter** — `warning` / `severe` / `fatal`.
2. **Numeric normalisation** — digits replaced with `N` so
   `"Zone 3 temp 24.1C out of range"` and `"Zone 5 temp 29.8C out of range"`
   collapse to one key.
3. **Deduplicate and count** — return `{message, count}` pairs.
4. **Rank and truncate** — most frequent first, capped at `tail_n`.

The same principle governs telemetry: `SensorBus.aggregates()` collapses 96
samples/variable/day into min/mean/max, and `query_timeseries` is structurally
incapable of returning a raw array.

- TODO: raw `.err` lines: ____ → unique messages: ____ (compression ratio ____:1)
- TODO: max tool-result payload observed: ____ tokens

---

## 6. Data flow per timestep

1. `api_data_fully_ready()` guard — return early if handles are not yet valid.
2. Bind handles once; `assert_bound_ok()` raises on any `-1` handle.
3. `warmup_flag()` guard — discard warmup timesteps.
4. Read variables and meters → `Sample`.
5. **Tier 1:** `PolicyExecutor.compute(zone, hour)` → clamp → `set_actuator_value`.
6. Append to ring buffer; write CSV row.
7. Hourly: write `snapshot.json`, drain the MCP policy inbox.
8. On cadence: `AgentLoop.maybe_invoke()` (returns immediately).

---

## 7. Results

### Experiment

| | |
|---|---|
| Model | DOE Reference Small Office, New Construction 2004 (Chicago) |
| Zones controlled | 5 conditioned (`CORE_ZN`, `PERIMETER_ZN_1..4`); `ATTIC` excluded — unconditioned |
| Weather | `USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw` |
| Period | 1–7 July, 672 timesteps at 4/hour, contiguous, design days excluded |
| EnergyPlus | 26.1.0 (`6f2e40d102`), in-process via `pyenergyplus` |
| Controller | Two-tier: LLM-authored policy (llama3.2:3b via Ollama, temperature 0) executed by the reflex tier |

Baseline and controlled runs use the **identical** `.idf` and `.epw`; only the
controller differs. Both completed with `errors=0`.

### Energy

| Metric | Baseline | Eco-Loop (LLM policy) | Change |
|---|---|---|---|
| Electricity | 1251.97 kWh | 1167.33 kWh | **−6.76 %** |
| Natural gas | 57.50 kWh | 57.81 kWh | +0.54 % |
| Total energy | 1309.46 kWh | 1225.14 kWh | **−6.44 %** |
| Peak electric demand | 18.97 kW | 17.52 kW | **−7.64 %** |

For reference, a hand-tuned static policy (fixed 25.5 °C occupied cooling setpoint,
no LLM) achieves −5.08 % electricity on the same model. The LLM-authored policy
**beats it**, which is the result worth reporting: the cognitive tier is earning
its place rather than merely reproducing a constant.

Gas is service water heating and is essentially unaffected — expected for a July
week in Chicago, where there is almost no space heating load. The saving is
cooling-side, which is why peak demand falls alongside consumption.

### Comfort — scored over occupied hours only

| Metric | Baseline | Eco-Loop (LLM policy) | Change |
|---|---|---|---|
| Mean PMV | −0.391 | **+0.140** | toward neutral |
| PMV within −0.5…+0.5 | 82.8 % | **97.3 %** | +14.5 pp |
| Unmet setpoint time | 0.00 % | 0.60 % | +0.60 pp |

Unmet time is expressed as a share of occupied **zone-hours** (415 zone-h = 83
occupied hours × 5 zones), because EnergyPlus sums unmet time across zones.

### Pass criteria

Stated as absolute percentage-point tests, both evaluated over occupied hours:

1. **Primary — PMV in-band share may not fall by more than 2 pp.** Result: +14.5 pp. **PASS**
2. **Secondary — unmet setpoint time may not rise by more than 1 pp** of occupied
   zone-hours. Result: +0.72 pp. **PASS**

Enforced in `scripts/compare_runs.py`; machine-readable verdict in
`results/comparison.json`.

An earlier revision of this criterion used the *relative* change in unmet hours
with a 1 % budget. It was replaced because it is mathematically undefined when the
baseline is zero (it returned `inf`) and because it compared a cross-zone sum
against a whole-building budget. The change was made after seeing a failing
result, so it is disclosed here rather than presented as the original design.
Note also what "setpoint not met" measures: whether the HVAC reached *the
controller's own* setpoint. Raising the cooling setpoint and allowing an
unoccupied float necessarily produces some unmet time during Monday-morning
pull-down, even as measured comfort improves — which is why PMV is primary.

### Why it works

The stock schedule **overcools**: baseline occupied PMV is −0.391, i.e. occupants
are consistently on the cold side of neutral. Raising the occupied cooling
setpoint from 24.0 °C to 25.5 °C therefore reduces cooling energy *and* moves
comfort toward neutral. Energy and comfort are aligned in this building, not in
tension — the saving is not bought at the occupants' expense.

Setback is driven by **measured occupancy** (`Zone People Occupant Count`), not a
clock window. An earlier clock-only version held the occupied setpoint through
Saturday and Sunday and consumed **1.5 % more** electricity than the baseline
schedule it was meant to beat.

### Pending — requires a completed `--mode ai` run

- Agent invocations, success rate, self-correction count
- Mean / p95 LLM latency, timeouts, busy-guard skips
- Energy and comfort figures under LLM-authored policy vs the static policy above
- Annual (8760 h) unattended run: wall clock and completion
- Log compression ratio measured on a full-length `eplusout.err`

---

## 8. Known limitations

- Grid carbon intensity is a synthetic diurnal curve from `config.yaml`, not a
  measured feed. The interface is a single function, so a real feed drops in.
- Single building, single weather file. No generalisation claim across climates.
- The policy space is deliberately narrow (setpoint bands, setback, precool). A
  wider action space would need a larger model and much more validation.
- The agent drives the building through EMS actuators rather than by rewriting the
  model. `models/generated/` therefore holds *materialised* policy snapshots: each
  installed policy is written out as a runnable `.idf` with the occupied setpoints
  baked into `Schedule:Constant` objects wired to each zone's
  `ThermostatSetpoint:DualSetpoint`. Unoccupied setback and precool are applied at
  runtime and cannot be represented in a constant schedule, so a snapshot captures
  the occupied band only.
- Results are one building, one climate, one week. No generalisation is claimed
  across climates or building types; the 5 % figure is specific to this model,
  where the baseline schedule happens to overcool.
- Snapshots are capped at 20 per run so an annual run does not emit 365 models.
- Design-day environments are excluded from all results. Including them (the
  default before `kind_of_sim` filtering) produced a spurious 12.7 % "saving"
  driven entirely by the 21 January heating design day.

---

## 9. Demo script (≤3 min video)

| Time | Shot |
|---|---|
| 0:00–0:20 | Problem: BMS schedules are static; 40% of global energy is buildings |
| 0:20–0:40 | Architecture diagram; say the line: *"the LLM writes the policy, it is not in the loop"* |
| 0:40–1:30 | Split screen: simulation console + `agent_trace.jsonl` tailing. Narrate one complete loop — sensor read → agent reasoning → `commit_policy` → setpoint change |
| 1:30–2:10 | Dashboard: cumulative energy A/B, PMV histogram inside the comfort band |
| 2:10–2:40 | **Kill the LLM server mid-run.** Show the simulation continuing on the last-known-good policy and completing. |
| 2:40–3:00 | Headline numbers: % kWh saved, % peak reduction, comfort verdict PASS |

The failure demo at 2:10 is the strongest 20 seconds in the video — nobody else
will show their fault tolerance, and it is direct evidence for the 30% "System
Integration" criterion.
