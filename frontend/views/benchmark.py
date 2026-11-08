"""Benchmark: ProofLoop vs. agent self-reporting vs. tool-response checking."""

from __future__ import annotations

import json

import altair as alt
import pandas as pd
import streamlit as st

from frontend.api_client import ApiError
from frontend.components.ui import (
    api,
    chip,
    esc,
    has_role,
    kpi_row,
    page_header,
    pct,
    show_html,
)

NAMES = {"proofloop": "ProofLoop", "baseline_self_report": "Baseline A · agent self-report",
         "baseline_tool_response": "Baseline B · tool responses"}
COLORS = ["#4f46e5", "#f59e0b", "#94a3b8"]
FAULT_LABEL = {
    "none": "Correct execution", "tool_failure_false_claim": "Tool failure + false claim", "wrong_record": "Wrong record",
    "duplicate_operation": "Duplicate operation", "partial_execution": "Partial execution", "side_effect": "Side effect",
    "missing_evidence": "Missing evidence", "fabricated_success": "Fabricated success",
    "contradictory_logs": "Contradictory logs", "inspector_unavailable": "Inspector unavailable",
}


def render() -> None:
    page_header("Evaluate", "Benchmark",
                "A reproducible synthetic benchmark: every case really runs the agent, injects a known fault and is "
                "verified. Ground truth comes from the fault-injection harness, never from ProofLoop itself.")
    with st.container(border=True):
        a, b, c, d = st.columns([1, 1, 1.3, 1.2], vertical_alignment="bottom")
        variants = a.slider("Task variants per family", 1, 6, 3, help="More variants = more cases (~58 per variant).")
        seed = b.number_input("Random seed", 0, 10_000_000, 42)
        interp = c.selectbox("Interpreter", ["rules", "auto", "llm"],
                             format_func={"rules": "Deterministic rules", "auto": "Local LLM if available", "llm": "Local LLM (required)"}.get)
        run = d.button("Run benchmark", type="primary", icon=":material/rocket_launch:", width="stretch",
                       disabled=not has_role("analyst"))
    if run:
        with st.spinner("Running every case end-to-end (agent → snapshots → verification)..."):
            try:
                st.session_state["bench"] = api().run_benchmark(seed=int(seed), variants=int(variants), interpreter=interp)
            except ApiError as err:
                st.error(err.message)
    results = st.session_state.get("bench")
    if results is None:
        try:
            results = api().latest_benchmark()
            st.session_state["bench"] = results
        except ApiError as err:
            if err.status == 404:
                st.info("No benchmark has been run yet. Click **Run benchmark** (takes a few seconds with the rule-based interpreter).",
                        icon=":material/info:")
            else:
                st.error(err.message)
            return

    s = results["systems"]
    pl, a_, b_ = s["proofloop"], s["baseline_self_report"], s["baseline_tool_response"]
    st.caption(f"Run {results['generated_at']} · {results['dataset']['size']} cases · interpreter "
               f"**{results['config']['interpreter']}** · seed {results['config']['seed']} · "
               f"total {results['total_duration_ms'] / 1000:.1f} s")
    kpi_row([
        ("Failure recall", pct(pl["failure_recall"]), "failed runs correctly flagged FAILED", "#4f46e5"),
        ("Unsafe acceptance", pct(pl["unsafe_acceptance_rate"]), f"Baseline A {pct(a_['unsafe_acceptance_rate'])} · B {pct(b_['unsafe_acceptance_rate'])}", "#ef4444"),
        ("False positive rate", pct(pl["false_positive_rate"]), "successful runs wrongly FAILED", "#10b981"),
        ("Gain vs best baseline", f"+{results['improvement_over_best_baseline_pp']} pp", "in failure recall", "#7c3aed"),
        ("Inconclusive rate", pct(pl["inconclusive_rate"]), "honest 'not enough evidence'", "#f59e0b"),
    ])
    st.space("small")

    left, right = st.columns([1.5, 1], gap="large")
    with left:
        with st.container(border=True):
            st.markdown("**Head-to-head**")
            metrics = [("failure_recall", "Recall ↑"), ("unsafe_acceptance_rate", "Unsafe accept ↓"),
                       ("false_positive_rate", "False pos. ↓"), ("verdict_accuracy", "Accuracy ↑")]
            short = {"proofloop": "ProofLoop", "baseline_self_report": "A: self-report", "baseline_tool_response": "B: tool responses"}
            rows = [{"metric": label, "system": short[k], "value": (s[k][m] or 0) * 100} for m, label in metrics for k in short]
            chart = (
                alt.Chart(pd.DataFrame(rows))
                .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
                .encode(
                    x=alt.X("metric:N", title=None, sort=[label for _, label in metrics], axis=alt.Axis(labelAngle=0)),
                    xOffset=alt.XOffset("system:N", sort=list(short.values())),
                    y=alt.Y("value:Q", title="%", scale=alt.Scale(domain=[0, 100])),
                    color=alt.Color("system:N", scale=alt.Scale(domain=list(short.values()), range=COLORS),
                                    legend=alt.Legend(orient="top", title=None)),
                    tooltip=["metric", "system", alt.Tooltip("value:Q", format=".1f")],
                )
                .properties(height=280)
            )
            st.altair_chart(chart, width="stretch")
    with right:
        targets = results["targets"]
        rows_html = "".join(
            f'<div class="pl-target"><span>{esc(name.replace("_", " ").capitalize())}<br/>'
            f'<span style="color:#94a3b8;font-size:.78rem">target {esc(t["target"])}</span></span>'
            f'<span><b>{esc(t["actual"])}</b> {chip("met" if t["met"] else "not met", "green" if t["met"] else "red")}</span></div>'
            for name, t in targets.items()
        )
        show_html(f'<div class="pl-card"><div class="pl-card-title">PRD performance targets</div>{rows_html}</div>')
        se = results["side_effect_detection"]
        lat = results["latency_ms"]["verification"]
        show_html(
            f'<div class="pl-card"><div class="pl-card-title">Side effects · evidence · latency</div>'
            f'<div class="pl-target"><span>Side-effect precision / recall</span><b>{pct(se["precision"])} / {pct(se["recall"])}</b></div>'
            f'<div class="pl-target"><span>Evidence coverage</span><b>{pct(results["evidence_coverage"])}</b></div>'
            f'<div class="pl-target"><span>Verification latency p50 / p95</span><b>{lat["p50"]} / {lat["p95"]} ms</b></div>'
            f'<div class="pl-target"><span>Cost per verification</span><b>${results["cost_per_verification_usd"]:.2f}</b></div></div>'
        )

    st.markdown("#### Comparison table")
    table = pd.DataFrame([{
        "System": NAMES[k],
        "Failure recall": pct(m["failure_recall"]), "Unsafe acceptance": pct(m["unsafe_acceptance_rate"]),
        "False positive rate": pct(m["false_positive_rate"]), "Precision": pct(m["precision"]),
        "Inconclusive": pct(m["inconclusive_rate"]), "Verdict accuracy": pct(m["verdict_accuracy"]),
    } for k, m in s.items()])
    st.dataframe(table, hide_index=True, width="stretch")

    st.markdown("#### Per fault type")
    pf = pd.DataFrame([{
        "Fault": FAULT_LABEL.get(r["fault_mode"], r["fault_mode"]), "Cases": r["cases"], "Expected": r["expected_verdict"],
        "ProofLoop correct": f'{r["proofloop_correct"]}/{r["cases"]}',
        "Baseline A correct": f'{r["baseline_self_report_correct"]}/{r["cases"]}',
        "Baseline B correct": f'{r["baseline_tool_response_correct"]}/{r["cases"]}',
    } for r in results["per_fault"]])
    st.dataframe(pf, hide_index=True, width="stretch")

    with st.expander("Methodology & limitations", icon=":material/menu_book:"):
        st.markdown(
            "- **Dataset:** generated from the synthetic seed data with a fixed seed; each task template instance is "
            "combined with every fault applicable to its operation, plus ambiguous-target tasks.\n"
            "- **Ground truth:** the harness knows what it actually did (e.g. it really wrote the wrong record). "
            "Correct, missing-evidence, contradictory-log and outage cases genuinely succeed; the rest genuinely fail.\n"
            "- **Baselines** can only answer VERIFIED or FAILED. Baseline A trusts the agent's final claim; "
            "Baseline B trusts state-changing tool/API responses.\n"
            "- **Limitations:** faults are synthetic and were designed together with the verifier, so these numbers "
            "show the mechanisms work - they are *not* a claim about accuracy on real-world agents. "
            "Free-form instructions interpreted by a small local LLM can be misread; review plans before approval."
        )
    st.download_button("Download full results (JSON)", json.dumps(results, indent=2), file_name="proofloop_benchmark.json",
                       mime="application/json", icon=":material/download:")
