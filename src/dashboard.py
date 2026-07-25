"""Quantitative Savings Dashboard (spec deliverable #3).

    streamlit run src/dashboard.py

Four panels, chosen to map onto the rubric:
  1. Cumulative energy, baseline vs AI      -> Energy Efficiency Realized (25%)
  2. Comfort: PMV distribution + unmet hrs  -> Thermal Comfort & Constraints (20%)
  3. Zone temp / setpoint timeline with policy-change markers
                                            -> visual proof the loop is closed
  4. Agent trace: rejections and self-corrections
                                            -> Agentic Autonomy (15%)

Panel 3 is the one to screenshot for the Artifacts slide — the policy-change
markers are what make the closed loop legible at a glance.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "results"
STORE = RESULTS / "store"

st.set_page_config(page_title="Eco-Loop Building Agents", layout="wide")


def load_csv(name: str) -> pd.DataFrame | None:
    p = RESULTS / name
    if not p.exists():
        return None
    df = pd.read_csv(p)
    return df if not df.empty else None


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


st.title("Eco-Loop Building Agents")
st.caption("Autonomous closed-loop control of an EnergyPlus building model via an open-source LLM")

base = load_csv("run_baseline.csv")
ai = load_csv("run_ai.csv") or load_csv("run_static.csv")

if base is None or ai is None:
    st.warning(
        "Need both runs. Execute:\n\n"
        "```\npython -m src.runner --mode baseline\npython -m src.runner --mode ai\n```"
    )
    st.stop()

comparison = {}
cmp_path = RESULTS / "comparison.json"
if cmp_path.exists():
    comparison = json.loads(cmp_path.read_text(encoding="utf-8"))

# ------------------------------------------------------------------ headline KPIs

base_elec = base["electricity_kwh"].sum()
ai_elec = ai["electricity_kwh"].sum()
base_total = base_elec + base["gas_kwh"].sum()
ai_total = ai_elec + ai["gas_kwh"].sum()
saving_pct = ((base_total - ai_total) / base_total * 100) if base_total else 0.0

c1, c2, c3, c4 = st.columns(4)
c1.metric("Total energy saved", f"{saving_pct:.1f}%",
          delta=f"{ai_total - base_total:.0f} kWh")
c2.metric("Baseline total", f"{base_total:,.0f} kWh")
c3.metric("AI-controlled total", f"{ai_total:,.0f} kWh")
if comparison:
    verdict = comparison.get("comfort", {}).get("pass")
    c4.metric("Comfort guardrail", "PASS" if verdict else "FAIL",
              delta=f"{comparison['comfort'].get('unmet_increase_pct')}% unmet hours",
              delta_color="inverse")
else:
    c4.metric("Comfort guardrail", "run compare_runs.py")

st.divider()

# ------------------------------------------------------- 1. cumulative energy A/B

st.subheader("1. Cumulative energy — baseline vs AI-driven closed loop")
fig = go.Figure()
for df, label, dash in ((base, "Baseline (stock schedules)", "dash"), (ai, "AI closed loop", None)):
    cum = (df["electricity_kwh"] + df["gas_kwh"]).cumsum()
    fig.add_trace(go.Scatter(
        x=df["sim_hour"], y=cum, name=label,
        line=dict(dash=dash, width=2),
    ))
fig.update_layout(
    xaxis_title="Simulation hour", yaxis_title="Cumulative energy (kWh)",
    height=380, legend=dict(orientation="h", y=1.12), margin=dict(t=40),
)
st.plotly_chart(fig, use_container_width=True)

# ------------------------------------------------------------------- 2. comfort

st.subheader("2. Thermal comfort — did we save energy at the occupants' expense?")
col_a, col_b = st.columns([2, 1])

with col_a:
    if "mean_pmv" in ai.columns and ai["mean_pmv"].notna().any():
        fig2 = go.Figure()
        for df, label in ((base, "Baseline"), (ai, "AI")):
            vals = pd.to_numeric(df["mean_pmv"], errors="coerce").dropna()
            if not vals.empty:
                fig2.add_trace(go.Histogram(x=vals, name=label, opacity=0.65, nbinsx=40))
        fig2.add_vrect(x0=-0.5, x1=0.5, fillcolor="green", opacity=0.10,
                       annotation_text="ASHRAE comfort band", annotation_position="top left")
        fig2.update_layout(barmode="overlay", xaxis_title="PMV",
                           yaxis_title="Timesteps", height=340,
                           legend=dict(orientation="h", y=1.15), margin=dict(t=40))
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info(
            "No PMV data. The Fanger comfort model is not enabled on the People objects.\n\n"
            "Fix: `python scripts/instrument_idf.py --source <your.idf>` then set "
            "`people_objects` in config.yaml. **This is 20% of the rubric.**"
        )

with col_b:
    unmet = pd.DataFrame({
        "run": ["baseline", "ai"],
        "unmet hours": [
            base["unmet_heating_h"].sum() + base["unmet_cooling_h"].sum(),
            ai["unmet_heating_h"].sum() + ai["unmet_cooling_h"].sum(),
        ],
    })
    st.dataframe(unmet, hide_index=True, use_container_width=True)
    st.caption("Setpoint-not-met hours. The AI run must not materially exceed baseline.")

# --------------------------------------------- 3. setpoints + policy change markers

st.subheader("3. Zone temperature and control actions")
history = load_jsonl(STORE / "policy_history.jsonl")

fig3 = go.Figure()
fig3.add_trace(go.Scatter(x=ai["sim_hour"], y=ai["mean_zone_c"],
                          name="Mean zone temp (AI)", line=dict(width=2)))
fig3.add_trace(go.Scatter(x=base["sim_hour"], y=base["mean_zone_c"],
                          name="Mean zone temp (baseline)", line=dict(width=1, dash="dot")))
fig3.add_trace(go.Scatter(x=ai["sim_hour"], y=ai["outdoor_c"],
                          name="Outdoor", line=dict(width=1), opacity=0.5))

for rec in history[:80]:
    h = rec.get("sim_hour")
    if h is None:
        continue
    fig3.add_vline(x=h, line_width=1, line_dash="dot", opacity=0.45)

fig3.update_layout(xaxis_title="Simulation hour", yaxis_title="Temperature (C)",
                   height=380, legend=dict(orientation="h", y=1.12), margin=dict(t=40))
st.plotly_chart(fig3, use_container_width=True)
st.caption(f"Vertical dotted lines mark the {len(history)} policy updates installed by the agent.")

# ------------------------------------------------------------------ 4. agent trace

st.subheader("4. Agent behaviour — tool calls, rejections, self-corrections")
trace = load_jsonl(STORE / "agent_trace.jsonl")

if not trace:
    st.info("No agent trace yet. Run `python -m src.runner --mode ai` with the LLM server up.")
else:
    counts: dict[str, int] = {}
    for rec in trace:
        counts[rec.get("event", "?")] = counts.get(rec.get("event", "?"), 0) + 1

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Agent invocations", counts.get("agent_invoked", 0))
    m2.metric("Policies installed", counts.get("policy_installed", 0))
    m3.metric("Rejections", counts.get("policy_rejected", 0))
    m4.metric("Self-corrections", sum(
        1 for r in trace if r.get("event") == "policy_installed" and r.get("self_corrected")
    ))

    lat = [r.get("latency_ms") for r in trace
           if r.get("event") == "llm_response" and r.get("latency_ms")]
    if lat:
        st.caption(
            f"LLM latency: mean {sum(lat)/len(lat):.0f} ms, max {max(lat)} ms "
            f"over {len(lat)} calls — the reflex controller never waits on this."
        )

    st.dataframe(pd.DataFrame(trace[-40:]), use_container_width=True, height=300)

st.divider()
st.caption(
    "Architecture: EnergyPlus <-> reflex controller (every timestep, deterministic) "
    "<-> LLM policy layer (coarse cadence, via MCP tools). "
    "The LLM writes control policy; it is never on the critical path."
)
