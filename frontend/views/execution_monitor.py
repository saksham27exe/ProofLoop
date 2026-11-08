"""Execution Monitor: lifecycle, tool calls/responses and the hash-chained trace."""

from __future__ import annotations

import streamlit as st

from frontend.components.ui import (
    api,
    call,
    chip,
    empty_state,
    env_chip,
    esc,
    execution_picker,
    fmt_ms,
    fmt_time,
    goto,
    has_role,
    kv,
    page_header,
    show_html,
    timeline,
    verdict_badge,
)

STATUS_TONE = {"created": "slate", "interpreted": "blue", "needs_clarification": "amber", "running": "violet",
               "completed": "indigo", "verified": "green"}


def render() -> None:
    page_header("Monitor", "Execution monitor",
                "Everything the agent did, in order - as recorded by the append-only, hash-chained trace collector.")
    exec_id = execution_picker("monitor_pick")
    if not exec_id:
        empty_state("No executions yet", "Start one from New Execution.", "&#9201;")
        return
    detail = call(api().execution, exec_id)
    trace = call(api().traces, exec_id)
    if not detail or trace is None:
        return

    events = trace["events"]
    tool_calls = sum(1 for e in events if e["event_type"] == "tool_call")
    errors = sum(1 for e in events if e["event_type"] == "error" or (e["event_type"] == "tool_response" and e.get("status") == "error"))
    integrity = trace["integrity"]

    head = st.container(border=True)
    with head:
        a, b = st.columns([4, 1.3], vertical_alignment="center")
        with a:
            show_html(
                f'<div style="font-size:1.15rem;font-weight:750;color:#0f172a">{esc(detail["task_description"])}</div>'
                f'<div style="margin-top:.45rem">{env_chip(detail["environment"])} '
                f'{chip(detail["id"], "slate", mono=True)} '
                f'{chip(detail["execution_status"].replace("_", " "), STATUS_TONE.get(detail["execution_status"], "slate"))} '
                f'{chip(detail["agent_name"] + " · " + detail["agent_config"], "indigo")} '
                f'{chip("interpreter: " + detail["interpreter"], "violet")}</div>'
            )
        with b:
            show_html(f'<div style="text-align:right">{verdict_badge(detail.get("verdict"))}</div>')
            if detail.get("verdict") and st.button("View verification", icon=":material/fact_check:", width="stretch"):
                goto("results", exec_id)

    if detail["execution_status"] in ("interpreted", "needs_clarification") and has_role("analyst"):
        note, action = st.columns([4, 1.3], vertical_alignment="center")
        note.info("This task has been interpreted, but the agent has not run yet.", icon=":material/pending:")
        if action.button("Review & run", type="primary", icon=":material/play_arrow:", width="stretch", key="mon_resume"):
            st.session_state["pending_exec"] = detail
            st.session_state.pop("last_result", None)
            st.session_state["reopen_review"] = True
            goto("new", exec_id)

    d = detail["durations_ms"]
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Trace events", len(events))
    m2.metric("Tool calls", tool_calls)
    m3.metric("Errors logged", errors)
    m4.metric("Agent run time", fmt_ms(d.get("execution")))
    m5.metric("Verification time", fmt_ms(d.get("verification")))

    left, right = st.columns([2.1, 1], gap="large")
    with left:
        st.markdown("#### Timeline")
        if integrity["valid"]:
            st.success(f"Trace integrity verified - hash chain intact over {integrity['checked_events']} events.",
                       icon=":material/verified:")
        else:
            st.error(f"Trace integrity FAILED: {integrity['detail']}", icon=":material/gpp_bad:")
        if events:
            timeline(events)
        else:
            empty_state("No events yet", "The agent has not run for this execution.", "&#9711;")
    with right:
        st.markdown("#### Agent's final response")
        claimed = detail.get("agent_claimed_status")
        tone = {"success": "green", "failure": "red"}.get(claimed or "", "slate")
        show_html(
            f'<div class="pl-card"><div class="pl-quote">&ldquo;{esc(detail.get("agent_claim") or "(not run yet)")}&rdquo;</div>'
            f'{chip("claimed " + (claimed or "-"), tone)} {chip("not evidence", "amber")}</div>'
        )
        st.markdown("#### Lifecycle")
        rows = [
            ("Created", fmt_time(detail.get("created_at"))),
            ("Started", fmt_time(detail.get("started_at"))),
            ("Completed", fmt_time(detail.get("completed_at"))),
            ("Verified", fmt_time(detail.get("verified_at"))),
            ("Created by", detail.get("created_by") or "-"),
            ("Approved by", detail.get("approved_by") or "not required"),
            ("Interpretation", fmt_ms(d.get("interpretation"))),
        ]
        show_html(f'<div class="pl-card">{kv(rows)}</div>')
        snaps = call(api().snapshots, exec_id, quiet=True) or {}
        st.markdown("#### Independent snapshots")
        snap_rows = []
        for phase in ("before", "after"):
            s = snaps.get(phase)
            if not s:
                snap_rows.append((phase.capitalize(), "not captured"))
            elif s["available"]:
                counts = ", ".join(f"{k}: {v}" for k, v in s["record_counts"].items())
                snap_rows.append((phase.capitalize(), f"sha256 {s['state_hash'][:14]}… ({counts})"))
            else:
                snap_rows.append((phase.capitalize(), f"unavailable - {s['error']}"))
        show_html(f'<div class="pl-card">{kv(snap_rows)}</div>')
        h = detail.get("harness") or {}
        with st.expander("Test harness (hidden from the verifier)", icon=":material/science:"):
            st.caption(h.get("note", ""))
            st.markdown(f"**Injected behaviour:** {h.get('fault_label', '-')}  \n{h.get('fault_description', '')}")
            if h.get("scenario_title"):
                st.markdown(f"**Scenario:** {h['scenario_title']}  \n**Expected verdict:** {h.get('expected_verdict')}")
