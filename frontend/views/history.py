"""History: browse, filter, search and export past verifications."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from frontend.components.ui import ENV_LABEL, api, call, empty_state, env_label, goto, page_header, source_names

VERDICT_TEXT = {"VERIFIED": "✅ Verified", "FAILED": "❌ Failed", "INCONCLUSIVE": "⚠️ Inconclusive", None: "… Pending"}


def render() -> None:
    page_header("Audit", "History", "Every execution and its immutable verification report, searchable and exportable.")
    f1, f2, f3 = st.columns([2.2, 1.2, 1.6])
    verdicts = f1.pills("Verdict", ["VERIFIED", "FAILED", "INCONCLUSIVE", "PENDING"], selection_mode="multi",
                        key="hist_verdicts", format_func=lambda v: v.title())
    env = f2.selectbox("Environment", ["all", *ENV_LABEL, *source_names()], format_func=lambda e: "All environments" if e == "all" else env_label(e),
                       key="hist_env")
    query = f3.text_input("Search", placeholder="Execution id or instruction text", key="hist_q", icon=":material/search:")

    data = call(api().executions, verdict=verdicts or None, environment=None if env == "all" else env,
                q=query.strip() or None, limit=500)
    if data is None:
        return
    items = data["items"]
    st.caption(f"{data['total']} matching execution(s)" + (" - showing the latest 500" if data["total"] > 500 else ""))
    if not items:
        empty_state("No matching executions", "Adjust the filters or run a new execution.", "&#128269;")
        return

    df = pd.DataFrame([{
        "Execution": i["id"],
        "Verdict": VERDICT_TEXT.get(i["verdict"], i["verdict"]),
        "Instruction": i["task_description"],
        "Environment": env_label(i["environment"]),
        "Agent claimed": i.get("agent_claimed_status") or "-",
        "Status": i["execution_status"].replace("_", " "),
        "Interpreter": i["interpreter"],
        "Created by": i.get("created_by") or "-",
        "Created (UTC)": (i["created_at"] or "").replace("T", " ")[:19],
    } for i in items])
    event = st.dataframe(
        df, hide_index=True, width="stretch", height=min(560, 40 + 35 * len(df)),
        on_select="rerun", selection_mode="single-row", key="hist_table",
        column_config={"Instruction": st.column_config.TextColumn(width="large"),
                       "Execution": st.column_config.TextColumn(width="small")},
    )
    rows = event.selection.rows if event and hasattr(event, "selection") else []
    if not rows:
        st.caption("Select a row to open its report, timeline or export.")
        return
    item = items[rows[0]]
    with st.container(border=True):
        st.markdown(f"**{item['id']}** - {item['task_description']}")
        a, b, c, d = st.columns(4)
        if a.button("Open verification report", type="primary", icon=":material/fact_check:", width="stretch",
                    disabled=not item["verdict"]):
            goto("results", item["id"])
        if b.button("Open execution timeline", icon=":material/timeline:", width="stretch"):
            goto("monitor", item["id"])
        if item["verdict"]:
            detail = call(api().execution, item["id"], quiet=True) or {}
            rid = detail.get("report_id")
            if rid:
                payload = call(api().export_report, rid, "json", quiet=True)
                md = call(api().export_report, rid, "markdown", quiet=True)
                if payload:
                    c.download_button("Export JSON", payload, file_name=f"{rid}.json", mime="application/json",
                                      icon=":material/download:", width="stretch")
                if md:
                    d.download_button("Export Markdown", md, file_name=f"{rid}.md", mime="text/markdown",
                                      icon=":material/description:", width="stretch")
