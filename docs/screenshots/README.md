# Dashboard screenshots

Capture these from `streamlit run src/dashboard.py` and save them here with
**exactly these filenames** — the deck and the architecture document reference
them by name.

| Filename | What to capture | Used for |
|---|---|---|
| `01-cumulative-energy.png` | Panel 1 — cumulative energy, baseline vs Eco-Loop, the two curves diverging | Deliverable #3, Artifacts slide |
| `02-comfort-pmv.png` | Panel 2 — PMV histograms with the shaded ASHRAE comfort band, plus the unmet-hours table | Thermal Comfort criterion (20%) |
| `03-setpoints-timeline.png` | Panel 3 — zone temperature and outdoor temperature with the vertical policy-change markers | Visual proof the loop is closed |
| `04-agent-trace.png` | Panel 4 — the four metrics (invocations, policies installed, rejections, self-corrections), the latency caption, and the trace table | Agentic Autonomy criterion (15%) |
| `05-kpi-header.png` | The four KPI cards at the top: total energy saved, baseline total, AI total, comfort guardrail PASS | Headline figure |

## Capture tips

- **Full browser width**, browser zoom at 100%. A cropped chart looks careless.
- Hide the Streamlit hamburger menu and "Deploy" button by pressing `f` for
  fullscreen on a chart, or just crop them out.
- PNG, not JPEG — charts have hard edges and JPEG artefacts show badly.
- Windows: `Win + Shift + S` for a region snip.

## Priority if you are short on time

`04-agent-trace.png` first — it is the only visual evidence of rejections and
self-corrections, which is 15% of the grade. Then `01` and `02`.
