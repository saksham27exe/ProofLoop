"""Overview dashboard."""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from frontend.components.ui import (
    ENV_LABEL,
    api,
    call,
    chip,
    current_user,
    empty_state,
    env_chip,
    esc,
    fmt_ms,
    fmt_time,
    goto,
    hero,
    kpi_row,
    pct,
    show_html,
    verdict_badge,
)

COLORS = {"VERIFIED": "#10b981", "FAILED": "#ef4444", "INCONCLUSIVE": "#f59e0b"}


def render() -> None:
    user = current_user()
    name = (user.get("full_name") or user.get("username") or "").split(" ")[0]
    hero(
        f"Welcome back{', ' + name if name else ''}",
        "ProofLoop independently checks what AI agents actually changed - in databases, APIs and files - "
        "and returns an evidence-backed verdict instead of trusting the agent's word.",
        quote="Don't trust what an AI agent says it did. Verify what actually happened.",
    )
    stats = call(api().stats)
    if stats is None:
        return

    total = stats["total_executions"]
    kpi_row([
        ("Total executions", total, f"{stats['pending']} awaiting verification", "#4f46e5"),
        ("Verified", stats["verified"], "proven by independent evidence", "#10b981"),
        ("Failed", stats["failed"], "violations established", "#ef4444"),
        ("Inconclusive", stats["inconclusive"], "insufficient / conflicting evidence", "#f59e0b"),
        ("Verification success rate", pct(stats["verification_success_rate"]), "verified ÷ all verdicts", "#7c3aed"),
    ])
    st.space("small")

    if total == 0:
        empty_state("No executions yet",
                    "Run a guided scenario to see ProofLoop catch a false success claim, a wrong-record update or a hidden side effect.",
                    icon="&#9889;")
        st.space("small")
        _, mid, _ = st.columns([1, 1, 1])
        with mid:
            if st.button("Run your first scenario", type="primary", icon=":material/play_arrow:", width="stretch"):
                goto("new")
        _llm_card()
        return

    left, right = st.columns([1.55, 1], gap="medium")
    with left:
        with st.container(border=True):
            st.markdown("**Verdicts over the last 14 days**")
            trend = pd.DataFrame(stats["trend"]).melt(id_vars="day", var_name="verdict", value_name="count")
            chart = (
                alt.Chart(trend)
                .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
                .encode(
                    x=alt.X("day:N", title=None, axis=alt.Axis(labelAngle=0, labelExpr="slice(datum.value, 5)")),
                    y=alt.Y("count:Q", title=None, stack=True),
                    color=alt.Color("verdict:N", scale=alt.Scale(domain=list(COLORS), range=list(COLORS.values())),
                                    legend=alt.Legend(orient="top", title=None)),
                    tooltip=["day", "verdict", "count"],
                )
                .properties(height=250)
            )
            st.altair_chart(chart, width="stretch")
    with right:
        with st.container(border=True):
            st.markdown("**Verdict distribution**")
            dist = pd.DataFrame([{"verdict": k, "count": stats[k.lower()]} for k in COLORS if stats[k.lower()]])
            if dist.empty:
                st.caption("No verdicts yet.")
            else:
                donut = (
                    alt.Chart(dist)
                    .mark_arc(innerRadius=62, cornerRadius=4, padAngle=0.02)
                    .encode(
                        theta="count:Q",
                        color=alt.Color("verdict:N", scale=alt.Scale(domain=list(COLORS), range=list(COLORS.values())),
                                        legend=alt.Legend(orient="bottom", title=None)),
                        tooltip=["verdict", "count"],
                    )
                    .properties(height=250)
                )
                st.altair_chart(donut, width="stretch")

    c1, c2, c3 = st.columns(3, gap="medium")
    caught = stats["false_success_claims_caught"]
    claimed = stats["claimed_success_total"] or 0
    with c1:
        show_html(
            f'<div class="pl-card"><div class="pl-card-title">False success claims caught</div>'
            f'<div class="pl-kpi-value" style="color:#b91c1c">{caught}</div>'
            f'<div class="pl-kpi-sub">of {claimed} executions where the agent reported success, '
            f'verification established failure.</div></div>'
        )
    with c2:
        envs = stats["by_environment"]
        rows = "".join(
            f'<div class="pl-target"><span>{env_chip(env)}</span><span>'
            f'{chip(str(v.get("VERIFIED", 0)) + " verified", "green")} {chip(str(v.get("FAILED", 0)) + " failed", "red")} '
            f'{chip(str(v.get("INCONCLUSIVE", 0)) + " inconclusive", "amber")}</span></div>'
            for env, v in envs.items() if env in ENV_LABEL
        )
        show_html(f'<div class="pl-card"><div class="pl-card-title">By environment</div>{rows}</div>')
    with c3:
        _llm_card(stats)

    st.markdown("#### Recent executions")
    for item in stats["recent"]:
        with st.container(border=True):
            a, b, c = st.columns([5.2, 1.6, 1.3], vertical_alignment="center")
            with a:
                show_html(
                    f'<div style="font-weight:650;color:#0f172a">{esc(item["task_description"])}</div>'
                    f'<div style="margin-top:.3rem">{env_chip(item["environment"])} {chip(item["id"], "slate", mono=True)} '
                    f'{chip(fmt_time(item["created_at"]), "slate")} {chip(item["interpreter"], "violet")}</div>'
                )
            with b:
                show_html(verdict_badge(item["verdict"]))
            with c:
                if st.button("Open", key=f"open_{item['id']}", icon=":material/arrow_forward:", width="stretch"):
                    goto("results" if item["verdict"] else "monitor", item["id"])


def _llm_card(stats: dict | None = None) -> None:
    llm = st.session_state.get("_llm_status") or call(api().llm_health, quiet=True) or {}
    online = bool(llm.get("model_available")) and llm.get("interpreter_mode") != "rules" and llm.get("llm_enabled", True)
    state = chip("connected", "green") if online else chip("verification-only mode", "amber")
    avg = fmt_ms(stats.get("avg_verification_ms")) if stats else "-"
    show_html(
        f'<div class="pl-card"><div class="pl-card-title">Local LLM</div>'
        f'<div style="display:flex;justify-content:space-between;align-items:center"><b>{esc(llm.get("model", "-"))}</b>{state}</div>'
        f'<div class="pl-kpi-sub" style="margin-top:.45rem">{esc(llm.get("message", ""))}</div>'
        f'<div class="pl-kpi-sub" style="margin-top:.35rem">Average verification time: <b>{esc(avg)}</b></div></div>'
    )
