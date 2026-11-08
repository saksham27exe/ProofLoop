"""Verification Results: verdict, checks, expected vs actual, side effects, evidence."""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from frontend.components.ui import (
    CATEGORY_TONE,
    KIND_TONE,
    api,
    call,
    check_list,
    chip,
    claim_vs_reality,
    empty_state,
    env_chip,
    esc,
    execution_picker,
    fmt_ms,
    goto,
    json_block,
    page_header,
    show_html,
    undo_panel,
    verdict_banner,
)


def _value(v):
    if isinstance(v, str) and len(v) > 120:
        return v[:117] + "..."
    return v


def _expected_vs_actual(report: dict) -> None:
    expected = report.get("expected_state") or {}
    actual = report.get("actual_state") or {}
    values = expected.get("values") or {}
    before = actual.get("before") or {}
    after = actual.get("after") or {}
    left, right = st.columns([1, 1], gap="large")
    with left:
        st.markdown("##### Expected (from the instruction)")
        target = expected.get("target") or {}
        rows = [("Operation", expected.get("operation")), ("Entity", expected.get("entity")),
                ("Target", target.get("key") or json.dumps(target.get("match") or {}) if target else "(new record)")]
        st.dataframe(pd.DataFrame([{"property": k, "value": str(v)} for k, v in rows] +
                                  [{"property": f"→ {k}", "value": str(_value(v))} for k, v in values.items()]),
                     hide_index=True, width="stretch")
        cons = expected.get("constraints") or {}
        st.caption(f"Constraints: exactly one record affected = {cons.get('exactly_one_affected')}, "
                   f"unexpected changes allowed = {cons.get('unexpected_changes_allowed')}, "
                   f"system-managed fields = {', '.join(cons.get('managed_fields') or []) or '-'}")
    with right:
        st.markdown("##### Record comparison")
        st.caption("Independently captured before- and after-snapshots of the affected record, compared with the "
                   "expected values. System-managed fields (e.g. updated_at) are allowed to change.")
    st.markdown(f"##### Observed record `{actual.get('record_key') or '-'}`")
    if not actual.get("available", True):
        st.error(f"After-state unavailable: {actual.get('error')}")
    fields = list(dict.fromkeys([*values.keys(), *after.keys(), *before.keys()]))
    if not fields:
        st.info("No single record to compare (see side effects and checks).")
        return
    table = []
    for f in fields:
        exp = values.get(f, "")
        aft = after.get(f) if after else None
        bef = before.get(f) if before else None
        status = ""
        if f in values:
            status = "✅ match" if str(aft).strip().lower() == str(exp).strip().lower() else "❌ mismatch"
        elif before and after and bef != aft:
            status = "Δ changed"
        table.append({"field": f, "before": _value(bef), "after": _value(aft), "expected": _value(exp) if f in values else "",
                      "result": status})
    st.dataframe(pd.DataFrame(table).astype(str), hide_index=True, width="stretch")
    if actual.get("candidates"):
        st.warning(f"Ambiguous target - candidates: {', '.join(actual['candidates'])}")


def _changes(report: dict) -> None:
    changes = report.get("changes") or []
    side = [c for c in changes if c["kind"] in ("side_effect", "uncertain")]
    if side:
        st.error(f"{len(side)} change(s) outside the task scope.", icon=":material/report:")
    else:
        st.success("No unintended changes detected.", icon=":material/check_circle:")
    if not changes:
        st.caption("No state changes were observed (or no before/after comparison was possible).")
        return
    for c in changes:
        fields = ", ".join(
            f"{k}: {_value(v.get('before'))!r} → {_value(v.get('after'))!r}" for k, v in list(c["fields"].items())[:6]
        ) if c["change_type"] == "modified" else f"{len(c['fields'])} field(s)"
        show_html(
            f'<div class="pl-card"><div style="display:flex;gap:.4rem;align-items:center;flex-wrap:wrap">'
            f'{chip(c["kind"].replace("_", " "), KIND_TONE.get(c["kind"], "slate"))}'
            f'{chip(c["change_type"], "slate")}<b style="margin-left:.2rem">{esc(c["label"])}</b></div>'
            f'<div class="pl-check-exp" style="margin-top:.35rem">{esc(c["reason"])}.</div>'
            f'<div class="pl-check-exp"><b>Fields:</b> {esc(fields)}</div>'
            f'<div class="pl-check-exp" style="color:#64748b"><b>Attribution:</b> {esc(c.get("attribution", ""))}</div></div>'
        )


def _evidence(report: dict) -> None:
    evidence = report.get("evidence") or []
    cats = sorted({e["category"] for e in evidence})
    chosen = st.pills("Category", cats, selection_mode="multi", default=cats, key="ev_cats")
    st.caption("**direct** = independently observed state · **indirect** = execution logs · **agent_reported** = the agent's own words · "
               "**derived** = deterministic computation · **missing / contradictory** = gaps and conflicts")
    shown = [e for e in evidence if e["category"] in (chosen or [])]
    df = pd.DataFrame([{"id": e["id"], "category": e["category"], "type": e["evidence_type"], "source": e["source"],
                        "description": e["description"], "sha256": e["integrity_hash"][:16] + "…"} for e in shown])
    if not df.empty:
        st.dataframe(df, hide_index=True, width="stretch", height=min(420, 38 + 35 * len(df)))
    for e in shown:
        with st.expander(f"{e['id']} · {e['evidence_type']} · {e['category']}"):
            show_html(f'{chip(e["category"], CATEGORY_TONE.get(e["category"], "slate"))} {chip(e["source"], "slate", mono=True)} '
                   f'{chip("sha256 " + e["integrity_hash"][:24] + "…", "slate", mono=True)}')
            st.write(e["description"])
            if e.get("data") is not None:
                show_html(json_block(e["data"], 4000))
    if report.get("missing_evidence"):
        st.markdown("##### Missing evidence")
        for m in report["missing_evidence"]:
            st.markdown(f"- {m}")


def render() -> None:
    page_header("Verify", "Verification results",
                "The verdict, every deterministic check behind it, and the evidence that supports each one.")
    exec_id = execution_picker("results_pick")
    if not exec_id:
        empty_state("Nothing verified yet", "Run a scenario from New Execution to produce your first report.", "&#9989;")
        return
    detail = call(api().execution, exec_id, include_report=True)
    if not detail:
        return
    report = detail.get("report")
    if not report:
        st.info("This execution has not been verified yet.", icon=":material/hourglass_empty:")
        if st.button("Open in Execution Monitor"):
            goto("monitor", exec_id)
        return

    verdict = report["verdict"]
    extra = [chip(f"decision rule {report['decision_rule']}", "slate"), env_chip(report["environment"]),
             chip(report["execution_id"], "slate", mono=True), chip(f"verified in {fmt_ms(report['durations_ms'].get('verification'))}", "slate")]
    extra += [chip(r["code"].replace("_", " ").lower(), "slate") for r in report.get("reasons", [])[:4]]
    verdict_banner(verdict, report["summary"], extra)
    for line in report.get("diagnosis", [])[1:]:
        st.markdown(f"- {line}")
    claim_vs_reality(report["agent_claim"].get("text"), report["agent_claim"].get("claimed_status"), verdict, report["summary"])
    if detail.get("backup"):
        u1, _u2 = st.columns([1, 2])
        with u1:
            undo_panel(detail)

    checks = report["checks"]
    n = {s: sum(1 for c in checks if c["status"] == s) for s in ("passed", "failed", "inconclusive", "skipped")}
    m = st.columns(5)
    m[0].metric("Checks run", len(checks))
    m[1].metric("Passed", n["passed"])
    m[2].metric("Failed", n["failed"])
    m[3].metric("Inconclusive", n["inconclusive"])
    m[4].metric("Evidence items", len(report.get("evidence") or []))

    tabs = st.tabs(["Checks", "Expected vs actual", f"Side effects ({len([c for c in report.get('changes', []) if c['kind'] in ('side_effect', 'uncertain')])})",
                    "Evidence", "Next steps", "Export"])
    with tabs[0]:
        order = {"failed": 0, "inconclusive": 1, "passed": 2, "skipped": 3}
        decisive = sorted([c for c in checks if c.get("decisive", True)], key=lambda c: order.get(c["status"], 9))
        info = [c for c in checks if not c.get("decisive", True)]
        st.markdown("##### Decisive checks")
        check_list(decisive)
        if info:
            st.markdown("##### Informational consistency checks")
            st.caption("These describe the agent's honesty and log quality; they do not decide the verdict.")
            check_list(info)
    with tabs[1]:
        _expected_vs_actual(report)
        interp = report.get("interpretation", {})
        st.caption(f"Interpreted by **{interp.get('interpreter')}** as: {interp.get('headline')}")
    with tabs[2]:
        _changes(report)
    with tabs[3]:
        _evidence(report)
    with tabs[4]:
        st.markdown("##### Recommended next steps")
        for step in report.get("next_steps", []):
            st.markdown(f"- {step}")
        st.markdown("##### Observed facts vs model-generated text")
        fvm = report.get("facts_vs_model", {})
        st.info(fvm.get("observed_facts", ""), icon=":material/verified:")
        narrative = fvm.get("model_generated_narrative")
        if narrative:
            with st.container(border=True):
                st.markdown(f"**AI narrative** · `{narrative.get('model')}`")
                st.caption(narrative.get("disclaimer", ""))
                st.write(narrative.get("text", ""))
        else:
            st.caption("No model-generated narrative for this report (enable it under Settings → Model).")
    with tabs[5]:
        st.markdown("Reports are immutable once created. The JSON export includes a SHA-256 report hash; "
                    "the API re-checks it on every export.")
        a, b = st.columns(2)
        rid = report["report_id"]
        json_bytes = call(api().export_report, rid, "json", quiet=True)
        md_bytes = call(api().export_report, rid, "markdown", quiet=True)
        if json_bytes:
            a.download_button("Download JSON", json_bytes, file_name=f"{rid}.json", mime="application/json",
                              icon=":material/download:", width="stretch", type="primary")
        if md_bytes:
            b.download_button("Download Markdown", md_bytes, file_name=f"{rid}.md", mime="text/markdown",
                              icon=":material/description:", width="stretch")
        st.caption(f"Report hash: `{report['integrity'].get('report_hash')}` · trace head: `{report['integrity'].get('trace_head_hash')}`")
        with st.expander("Raw report JSON"):
            st.json(report, expanded=False)
