"""New Execution: guided scenarios and custom tasks, with plan review and approval."""

from __future__ import annotations

import streamlit as st

from frontend.api_client import ApiError
from frontend.components.ui import (
    ENV_LABEL,
    VERDICT,
    api,
    call,
    chip,
    claim_vs_reality,
    cloud_active,
    current_user,
    env_chip,
    esc,
    goto,
    has_role,
    kv,
    model_phrase,
    page_header,
    show_html,
    undo_panel,
    verdict_badge,
    verdict_banner,
)

PRACTICE_EXAMPLES = [
    "Change the email of customer 5 to jonas.m@newmail.example",
    "Give every customer in Oslo the gold tier",
    "Mark all pending orders with a total above 900 as paid",
    "Set the stock of product 3 to 75",
    "Delete all cancelled orders with a total under 100",
]
FOLDER_EXAMPLES = [
    "Create a file notes/meeting.md with content 'Standup moved to 10:00.'",
    "Delete the file drafts/old.txt",
]

EXAMPLES = {
    "database": [
        "Update the email address of employee ID 1042 to new@example.com",
        "Set customer 105 phone number to +1-555-0199 and tier to gold",
        "Create a new employee record with name John Doe and department Engineering",
        "Delete customer 112",
    ],
    "api": [
        "Close ticket 12",
        "Assign ticket 4 to Priya Sharma",
        "Create a ticket titled 'VPN drops hourly' with priority high",
        "Set the priority of ticket 3 to low",
    ],
    "file": [
        "Create a file reports/summary.txt with content 'Q3 done'",
        "Overwrite the file notes/todo.md with 'All tasks complete'",
        "Delete the file logs/agent.log",
    ],
}


def _plan_view(detail: dict) -> None:
    plan = detail.get("plan") or {}
    harness = detail.get("harness") or {}
    chips = [env_chip(detail["environment"]), chip(f"interpreter: {plan.get('interpreter', '-')}", "violet")]
    if plan.get("operation"):
        chips.append(chip(plan["operation"], "indigo"))
    if harness.get("fault_mode") and harness["fault_mode"] != "none":
        chips.append(chip(f"injected fault: {harness.get('fault_label')}", "red",
                          title="Test-harness setting. The verifier never sees it."))
    show_html(f'<div style="margin-bottom:.5rem">{"".join(chips)}</div>')
    show_html(f'<div class="pl-quote">&ldquo;{esc(detail["task_description"])}&rdquo;</div>')

    target = plan.get("target") or {}
    if target.get("scope") == "all":
        conds = " and ".join(f"{c['field']} {c['op']} {c['value']!r}" for c in target.get("conditions") or [])
        target_text = f"every {(plan.get('entity_display') or 'record').lower()} where {conds}"
    else:
        target_text = (target.get("key") or ", ".join(f"{k} = {v}" for k, v in (target.get("match") or {}).items())
                       or "(new record)")
    left, right = st.columns(2, gap="medium")
    with left:
        expected = plan.get("expected_state") or {}
        rows = [("Operation", plan.get("operation") or "?"), ("Entity", plan.get("entity_display") or "?"), ("Target", target_text)]
        rows += [(f"→ {k}", v if not isinstance(v, str) or len(v) < 80 else v[:77] + "...") for k, v in expected.items()]
        show_html(f'<div class="pl-card"><div class="pl-card-title">Interpreted task</div>{kv(rows)}</div>')
    with right:
        reqs = "".join(
            f'<div class="pl-req"><span class="n">{i}</span><span>{esc(r["description"])}</span></div>'
            for i, r in enumerate(plan.get("requirements") or [], start=1)
        ) or '<div class="pl-kpi-sub">No requirements could be derived.</div>'
        show_html(f'<div class="pl-card"><div class="pl-card-title">Deterministic verification plan</div>{reqs}</div>')
    for w in plan.get("warnings") or []:
        st.warning(w, icon=":material/warning:")
    for a in plan.get("ambiguities") or []:
        st.error(a, icon=":material/help:")


def _result_view(detail: dict) -> None:
    report = detail.get("report") or {}
    verdict_banner(detail.get("verdict"), report.get("summary", ""), [
        f'<span class="pl-chip mono">{esc(detail["id"])}</span>',
        f'<span class="pl-chip">rule {esc(report.get("decision_rule", "-"))}</span>',
    ])
    claim_vs_reality(detail.get("agent_claim"), detail.get("agent_claimed_status"), detail.get("verdict"),
                     report.get("summary", ""))
    expected = (detail.get("harness") or {}).get("expected_verdict")
    if expected:
        match = expected == detail.get("verdict")
        st.caption(("✅ " if match else "⚠️ ") + f"Scenario expectation: {expected}"
                   + ("" if match else " - the verifier reached a different verdict; inspect the report."))
    a, b = st.columns(2)
    if a.button("Open full report", type="primary", icon=":material/fact_check:", width="stretch", key=f"res_{detail['id']}"):
        goto("results", detail["id"])
    if b.button("Open execution timeline", icon=":material/timeline:", width="stretch", key=f"mon_{detail['id']}"):
        goto("monitor", detail["id"])
    undo_panel(detail)


@st.dialog("Run & verify", width="large")
def review_dialog() -> None:
    detail = st.session_state.get("pending_exec")
    if not detail:
        st.write("Nothing to review.")
        return
    result = st.session_state.get("last_result")
    if result and result["id"] == detail["id"]:
        _result_view(result)
        return
    _plan_view(detail)
    plan = detail.get("plan") or {}
    approved = force = False
    if plan.get("requires_approval"):
        st.warning(plan.get("approval_reason") or "This operation requires approval.", icon=":material/gpp_maybe:")
        where = "this data source" if detail.get("real_data") else "the sandbox"
        approved = st.checkbox(f"I approve this change to {where}", key=f"approve_{detail['id']}")
    if detail["execution_status"] == "needs_clarification":
        st.info("This task is ambiguous. Rephrasing it is recommended; running it anyway will produce an INCONCLUSIVE verdict.")
        force = st.checkbox("Run anyway to observe the INCONCLUSIVE verdict", key=f"force_{detail['id']}")
    if detail.get("real_data"):
        st.info("The AI agent will work on **real data** and decide its own steps. A backup is taken first, so the run "
                "can be undone afterwards.", icon=":material/shield:")
    blocked = (plan.get("requires_approval") and not approved) or (detail["execution_status"] == "needs_clarification" and not force)
    c1, c2 = st.columns([2, 1])
    if c1.button("Run agent & verify", type="primary", icon=":material/play_arrow:", width="stretch", disabled=bool(blocked)):
        if not detail.get("real_data"):
            wait = "Running the agent and independently verifying the outcome..."
        elif cloud_active():
            wait = f"The AI agent is working on {model_phrase()} (usually 5-30 s)..."
        else:
            wait = "The AI agent is working (each step is a local-model call; on a CPU this can take a few minutes)..."
        with st.spinner(wait, show_time=True):
            try:
                res = api().run_execution(detail["id"], approved=approved, force=force, verify=True)
                st.session_state["last_result"] = res
                st.session_state["selected_exec"] = res["id"]
            except ApiError as err:
                st.error(err.message)
                return
        st.rerun(scope="fragment")
    if c2.button("Discard", width="stretch"):
        st.session_state.pop("pending_exec", None)
        st.rerun()


def _create_and_maybe_run(body: dict, auto_run: bool) -> None:
    try:
        msg = (f"Interpreting the task... free-form wording is read by {model_phrase()} (usually a few seconds)."
               if cloud_active() else
               "Interpreting the task... free-form wording is read by the local LLM, which can take 10-60 s on a "
               "CPU-only machine.")
        with st.spinner(msg, show_time=True):
            detail = api().create_execution(**body)
    except ApiError as err:
        st.error(err.message, icon=":material/error:")
        return
    st.session_state["pending_exec"] = detail
    st.session_state.pop("last_result", None)
    plan = detail.get("plan") or {}
    if auto_run and not plan.get("requires_approval") and detail["execution_status"] != "needs_clarification":
        try:
            with st.spinner("Running the agent and verifying the outcome..."):
                res = api().run_execution(detail["id"], verify=True)
            st.session_state["last_result"] = res
            st.session_state["selected_exec"] = res["id"]
        except ApiError as err:
            st.error(err.message)
            return
    review_dialog()


def _scenarios_tab(can_run: bool) -> None:
    scenarios = call(api().scenarios) or []
    env_filter = st.segmented_control("Environment", ["All", "Database", "Ticket API", "File system"], default="All",
                                      key="scn_filter", label_visibility="collapsed")
    reverse = {v: k for k, v in ENV_LABEL.items()}
    if env_filter and env_filter != "All":
        scenarios = [s for s in scenarios if s["environment"] == reverse[env_filter]]
    st.caption("Each scenario resets the sandbox to its seed state, runs the sample agent with the listed behaviour "
               "and verifies the result independently. The injected fault is never shown to the verifier.")
    cols = st.columns(3, gap="medium")
    for i, s in enumerate(scenarios):
        with cols[i % 3]:
            fault_tone = "green" if s["fault_mode"] == "none" else "red"
            show_html(
                f'<div class="pl-scn"><div style="display:flex;justify-content:space-between;gap:.4rem;align-items:center">'
                f'{env_chip(s["environment"])}<span style="font-size:.72rem;color:#94a3b8">expects</span></div>'
                f'<div class="pl-scn-title">{esc(s["title"])}</div><div class="pl-scn-desc">{esc(s["description"])}</div>'
                f'<div class="pl-scn-instr">{esc(s["instruction"])}</div>'
                f'<div style="display:flex;justify-content:space-between;align-items:center;margin-top:auto">'
                f'{chip(s["fault_label"], fault_tone)}{verdict_badge(s["expected_verdict"])}</div></div>'
            )
            if st.button("Run scenario", key=f"scn_{s['id']}", icon=":material/play_arrow:", width="stretch",
                         disabled=not can_run, type="secondary"):
                _create_and_maybe_run({"instruction": s["instruction"], "environment": s["environment"],
                                       "fault_mode": s["fault_mode"], "scenario_id": s["id"], "reset_sandbox": True},
                                      auto_run=True)
            st.space("small")


def _custom_tab(can_run: bool) -> None:
    options = call(api().options) or {"agents": [], "faults": []}
    left, right = st.columns([1.7, 1], gap="large")
    with right:
        with st.container(border=True):
            st.markdown("**Example instructions**")
            env_for_examples = st.session_state.get("custom_env", "database")
            for ex in EXAMPLES.get(env_for_examples, []):
                if st.button(ex, key=f"ex_{ex}", type="tertiary", width="stretch"):
                    st.session_state["custom_instruction"] = ex
                    st.rerun()
            st.caption(f"{model_phrase()[0].upper() + model_phrase()[1:]} accepts free-form phrasing. In verification-only "
                       "mode the deterministic interpreter supports the documented grammar (see README).")
    with left:
        env = st.selectbox("Environment", list(ENV_LABEL), format_func=lambda e: ENV_LABEL[e], key="custom_env")
        instruction = st.text_area("Task instruction for the agent", key="custom_instruction", height=110,
                                   placeholder="e.g. " + EXAMPLES[env][0], max_chars=2000)
        a, b = st.columns(2)
        agents = {a["id"]: a for a in options["agents"]}
        agent = a.selectbox("Agent configuration", list(agents) or ["scripted"],
                            format_func=lambda x: agents.get(x, {}).get("name", x),
                            help="\n\n".join(f"**{v['name']}** - {v['description']}" for v in agents.values()))
        faults = {f["mode"]: f for f in options["faults"]}
        fault = b.selectbox("Agent behaviour (fault injection)", list(faults) or ["none"],
                            format_func=lambda x: faults.get(x, {}).get("label", x),
                            help="Simulates a misbehaving agent. Only the test harness knows this setting.")
        if fault in faults:
            st.caption(f"{faults[fault]['description']}  ·  Typical verdict: **{faults[fault]['expected_verdict']}**")
        reset = st.toggle("Reset the sandbox to its seed data before running", value=False)
        if st.button("Interpret task", type="primary", icon=":material/psychology:", disabled=not can_run or not instruction.strip()):
            _create_and_maybe_run({"instruction": instruction.strip(), "environment": env, "agent_config": agent,
                                   "fault_mode": fault, "reset_sandbox": reset}, auto_run=False)


def _real_tab(can_run: bool) -> None:
    options = call(api().options) or {"environments": []}
    sources = {e["name"]: e for e in options["environments"] if e.get("real_data")}
    if not sources:
        st.info("No data source is connected. Open **Data Sources** to connect a SQLite file or a folder.",
                icon=":material/storage:")
        return
    names = list(sources)
    pre = st.session_state.pop("real_source", None)
    if pre in names:
        st.session_state["real_env"] = pre
    left, right = st.columns([1.7, 1], gap="large")
    with left:
        env = st.selectbox("Data source", names, format_func=lambda n: sources[n]["title"], key="real_env")
        src = sources[env]
        st.caption(src["description"] + "  ·  " + ", ".join(e["display_name"] for e in src["entities"][:8]))
        instruction = st.text_area("What should the AI agent do?", key="real_instruction", height=110, max_chars=2000,
                                   placeholder="e.g. Give every customer in Oslo the gold tier")
        st.caption(f"Agent: **AI agent on {model_phrase()}** - it explores the data, decides its own steps and reports back. "
                   "Nothing is scripted or injected. A backup is taken before it starts.")
        if st.button("Interpret task", type="primary", icon=":material/psychology:", key="real_go",
                     disabled=not can_run or not instruction.strip()):
            _create_and_maybe_run({"instruction": instruction.strip(), "environment": env, "agent_config": "ai_agent"},
                                  auto_run=False)
    with right:
        with st.container(border=True):
            st.markdown("**Try a task**")
            examples = FOLDER_EXAMPLES if any(e["key_type"] == "path" for e in src["entities"]) else (
                PRACTICE_EXAMPLES if env == "src_practice" else [])
            for ex in examples:
                # on_click runs before the next render, so the text box (already drawn above) can take the value.
                st.button(ex, key=f"rex_{ex}", type="tertiary", width="stretch",
                          on_click=st.session_state.__setitem__, args=("real_instruction", ex))
            if not examples:
                st.caption("Describe a change to one record (by id) or to every record matching a condition, e.g. "
                           "'Set the status of all orders older than 2025-01-01 to archived'.")
            st.caption("One change per task: a single record, or every record matching a condition.")


OWN_AGENT_SNIPPET = """import sys; sys.path.insert(0, r"{sdk}")
from proofloop_sdk import ProofLoop

pl = ProofLoop("http://127.0.0.1:8000", username="{user}", password="...")
with pl.session("Give every customer in Oslo the gold tier", source="src_practice",
                agent_name="my-agent", framework="langgraph") as s:
    # Hand these to any framework: s.openai_tools (OpenAI-style specs) or s.tools() (Python functions)
    s.call("find_rows", table="customers", where={{"city": "Oslo"}})
    s.call("update_rows", table="customers", where={{"city": "Oslo"}}, values={{"tier": "gold"}})
    s.finish("DONE: set the Oslo customers to gold")

print(s.verdict, "-", s.report["summary"])"""


def _own_agent_tab() -> None:
    """Bring your own agent: LangChain, CrewAI, AutoGen, plain Python or any language over HTTP."""
    st.markdown("Verify **any** agent - LangChain, LangGraph, CrewAI, AutoGen, the OpenAI/Anthropic SDKs, plain Python "
                "or another language. Observability tools record what an agent *says* it did; ProofLoop checks what "
                "*actually* changed in the data.")
    a, b, c = st.columns(3)
    a.markdown("**1 · Start a session**  \nProofLoop interprets the task, backs up the source and takes its own "
               "*before* snapshot.")
    b.markdown("**2 · Your agent works**  \nIt calls ProofLoop's tools (checked against the write policy and traced "
               "automatically) - or uses its own tools and reports each call.")
    c.markdown("**3 · Finish**  \nProofLoop takes the *after* snapshot and returns VERIFIED / FAILED / INCONCLUSIVE "
               "with evidence. The agent's claim is recorded, never trusted.")
    from pathlib import Path as _P

    sdk_dir = str(_P(__file__).resolve().parents[2] / "sdk")
    st.code(OWN_AGENT_SNIPPET.format(sdk=sdk_dir, user=(current_user().get("username") or "you")), language="python")
    st.caption("SDK: `sdk/proofloop_sdk.py` (single file, needs only httpx) · full example: `examples/own_agent.py` · "
               "REST endpoints under **/executions/{id}/session/** in the API docs (http://localhost:8000/docs). "
               "Results appear in History and Verification Results like any other run.")


def render() -> None:
    page_header("Run", "New execution",
                "Give an agent a task, review how ProofLoop interpreted it, then let the verifier check what really "
                "happened.")
    llm = st.session_state.get("_llm_status") or {}
    if llm.get("backend") == "cloud":
        st.warning(f"**Cloud model active** (`{llm.get('model')}`): your instruction and the data the AI agent reads are "
                   "sent to the cloud provider. You accepted responsibility for this on the Settings page. Switch to "
                   "the local backend in **Settings → Model** to keep data on this machine.", icon=":material/cloud:")
    can_run = has_role("analyst")
    if not can_run:
        st.info("Your role is **viewer**: you can browse results but not start executions.", icon=":material/visibility:")

    last = st.session_state.get("last_result")
    if last and last.get("verdict"):
        v = VERDICT.get(last["verdict"], VERDICT[None])
        with st.container(border=True):
            a, b, c = st.columns([4, 1.2, 1.2], vertical_alignment="center")
            a.html(f'<div style="font-size:.78rem;color:#64748b;font-weight:700;letter-spacing:.06em">LAST RESULT</div>'
                   f'<div style="font-weight:650">{esc(last["task_description"])}</div>'
                   f'<div style="color:{v["fg"]};font-size:.88rem;margin-top:.15rem">{esc((last.get("report") or {}).get("summary", ""))}</div>')
            b.html(verdict_badge(last["verdict"]))
            if c.button("Open report", key="last_open", width="stretch"):
                goto("results", last["id"])

    # An interpreted task survives a closed dialog (Esc, click outside, navigation) - interpreting free-form
    # wording with a local model can take a minute, so it must never have to be repeated.
    open_review = bool(st.session_state.pop("reopen_review", False))
    pending = st.session_state.get("pending_exec")
    if pending and can_run and pending.get("id") != (last or {}).get("id"):
        with st.container(border=True):
            a, b, c = st.columns([4, 1.2, 1.2], vertical_alignment="center")
            a.html('<div style="font-size:.78rem;color:#64748b;font-weight:700;letter-spacing:.06em">'
                   'INTERPRETED - NOT RUN YET</div>'
                   f'<div style="font-weight:650">{esc(pending["task_description"])}</div>')
            if b.button("Review & run", key="pending_open", type="primary", icon=":material/play_arrow:", width="stretch"):
                open_review = True
            if c.button("Discard", key="pending_discard", width="stretch"):
                st.session_state.pop("pending_exec", None)
                st.rerun()
    if open_review and pending:
        review_dialog()

    tab0, tab1, tab2, tab3 = st.tabs(["Real data · AI agent", "Your own agent", "Guided scenarios (demo)", "Sandbox task (demo)"])
    with tab0:
        _real_tab(can_run)
    with tab1:
        _own_agent_tab()
    with tab2:
        _scenarios_tab(can_run)
    with tab3:
        _custom_tab(can_run)
