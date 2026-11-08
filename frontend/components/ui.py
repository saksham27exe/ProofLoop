"""Reusable UI building blocks. Every dynamic value is HTML-escaped."""

from __future__ import annotations

import html
import json
from collections.abc import Callable
from typing import Any, TypeVar

import streamlit as st

from frontend.api_client import ApiClient, ApiError
from frontend.components.theme import LOGO_SVG

T = TypeVar("T")

VERDICT = {
    "VERIFIED": {"fg": "#047857", "bg": "#ecfdf5", "bd": "#a7f3d0", "ico": "&#10003;", "label": "Verified",
                 "blurb": "Independent evidence confirms the task was accomplished, with no unacceptable side effects."},
    "FAILED": {"fg": "#b91c1c", "bg": "#fef2f2", "bd": "#fecaca", "ico": "&#10005;", "label": "Failed",
               "blurb": "Independent evidence establishes that the task was not accomplished correctly."},
    "INCONCLUSIVE": {"fg": "#b45309", "bg": "#fffbeb", "bd": "#fde68a", "ico": "?", "label": "Inconclusive",
                     "blurb": "There is not enough consistent evidence to prove success or failure."},
    None: {"fg": "#475569", "bg": "#f1f5f9", "bd": "#e2e8f0", "ico": "&#8230;", "label": "Pending",
           "blurb": "Not verified yet."},
}
CHECK = {
    "passed": {"fg": "#047857", "bg": "#ecfdf5", "bd": "#a7f3d0", "ico": "&#10003;", "label": "Passed"},
    "failed": {"fg": "#b91c1c", "bg": "#fef2f2", "bd": "#fecaca", "ico": "&#10005;", "label": "Failed"},
    "inconclusive": {"fg": "#b45309", "bg": "#fffbeb", "bd": "#fde68a", "ico": "?", "label": "Inconclusive"},
    "skipped": {"fg": "#64748b", "bg": "#f1f5f9", "bd": "#e2e8f0", "ico": "&#8211;", "label": "Skipped"},
}
TONES = {
    "indigo": ("#4338ca", "#eef2ff", "#c7d2fe"), "green": ("#047857", "#ecfdf5", "#a7f3d0"),
    "red": ("#b91c1c", "#fef2f2", "#fecaca"), "amber": ("#b45309", "#fffbeb", "#fde68a"),
    "blue": ("#1d4ed8", "#eff6ff", "#bfdbfe"), "violet": ("#6d28d9", "#f5f3ff", "#ddd6fe"),
    "slate": ("#334155", "#f8fafc", "#e2e8f0"), "cyan": ("#0e7490", "#ecfeff", "#a5f3fc"),
}
ENV_LABEL = {"database": "Database", "api": "Ticket API", "file": "File system"}
ENV_TONE = {"database": "indigo", "api": "cyan", "file": "violet"}
ENV_ICON = {"database": ":material/database:", "api": ":material/api:", "file": ":material/folder:"}
CATEGORY_TONE = {"direct": "green", "indirect": "blue", "agent_reported": "amber", "derived": "slate",
                 "missing": "red", "contradictory": "red"}
KIND_TONE = {"expected": "green", "permitted_dependency": "slate", "duplicate": "amber", "side_effect": "red", "uncertain": "amber"}


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def show_html(markup: str) -> None:
    st.html(markup)


# ------------------------------------------------------------------ session/api
def api() -> ApiClient:
    return ApiClient(token=st.session_state.get("token"))


def sign_out(message: str | None = None) -> None:
    for key in ("token", "user", "pending_exec", "last_result", "selected_exec", "_llm_status", "bench"):
        st.session_state.pop(key, None)
    st.session_state["_clear_cookie"] = True
    if message:
        st.session_state["flash"] = message
    st.rerun()


def call(fn: Callable[..., T], *args: Any, quiet: bool = False, **kwargs: Any) -> T | None:
    """Invoke an API method; surface errors nicely; sign out on expired sessions."""
    try:
        return fn(*args, **kwargs)
    except ApiError as err:
        if err.status == 401 and st.session_state.get("token"):
            sign_out("Your session has expired. Please sign in again.")
        if not quiet:
            st.error(err.message, icon=":material/error:")
        return None


def current_user() -> dict:
    return st.session_state.get("user") or {}


def has_role(minimum: str) -> bool:
    rank = {"viewer": 0, "analyst": 1, "admin": 2}
    return rank.get(current_user().get("role", "viewer"), 0) >= rank[minimum]


def goto(page: str, exec_id: str | None = None) -> None:
    if exec_id:
        st.session_state["selected_exec"] = exec_id
    pages = st.session_state.get("_pages", {})
    if page in pages:
        st.switch_page(pages[page])


# ------------------------------------------------------------------ atoms
def chip(text: Any, tone: str = "slate", mono: bool = False, title: str | None = None) -> str:
    fg, bg, bd = TONES.get(tone, TONES["slate"])
    t = f' title="{esc(title)}"' if title else ""
    return f'<span class="pl-chip{" mono" if mono else ""}" style="--fg:{fg};--bg:{bg};--bd:{bd}"{t}>{esc(text)}</span>'


def verdict_badge(verdict: str | None) -> str:
    v = VERDICT.get(verdict, VERDICT[None])
    return (f'<span class="pl-badge" style="--fg:{v["fg"]};--bg:{v["bg"]};--bd:{v["bd"]}">'
            f'<span class="ico">{v["ico"]}</span>{esc(v["label"]).upper()}</span>')


def check_badge(status: str) -> str:
    c = CHECK.get(status, CHECK["skipped"])
    return chip(c["label"], {"passed": "green", "failed": "red", "inconclusive": "amber"}.get(status, "slate"))


def source_names() -> dict[str, str]:
    """environment name (src_<id>) -> display name of connected data sources, cached briefly per session."""
    import time

    cached = st.session_state.get("_source_names")
    if cached and time.time() - cached[0] < 30:
        return cached[1]
    data = call(api().sources, quiet=True) or []
    names = {s["environment"]: s["name"] for s in data}
    st.session_state["_source_names"] = (time.time(), names)
    return names


def env_label(env: str) -> str:
    if env.startswith("src_"):
        return source_names().get(env, env.removeprefix("src_"))
    return ENV_LABEL.get(env, env)


def env_chip(env: str) -> str:
    if env.startswith("src_"):
        return chip("Real data · " + env_label(env), "green", title="Connected data source")
    return chip(ENV_LABEL.get(env, env), ENV_TONE.get(env, "slate"))


def fmt_time(value: str | None) -> str:
    if not value:
        return "-"
    return value.replace("T", " ")[:19] + " UTC"


def fmt_ms(value: Any) -> str:
    if value is None:
        return "-"
    value = float(value)
    return f"{value / 1000:.2f} s" if value >= 1000 else f"{value:.0f} ms"


def pct(value: Any) -> str:
    return "-" if value is None else f"{float(value) * 100:.1f}%"


def json_block(data: Any, max_chars: int = 2500) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n..."
    return f'<pre class="pl-code">{esc(text)}</pre>'


# ------------------------------------------------------------------ molecules
def page_header(eyebrow: str, title: str, subtitle: str = "") -> None:
    show_html(f'<div class="pl-page-head"><div><div class="pl-eyebrow">{esc(eyebrow)}</div>'
           f'<div class="pl-title">{esc(title)}</div><div class="pl-sub">{esc(subtitle)}</div></div></div>')


def hero(title: str, subtitle: str, quote: str | None = None) -> None:
    small_logo = LOGO_SVG.replace("<img", '<img style="width:16px;height:16px"')
    q = f'<div class="pl-hero-quote">{small_logo}{esc(quote)}</div>' if quote else ""
    show_html(f'<div class="pl-hero"><h1>{esc(title)}</h1><p>{esc(subtitle)}</p>{q}</div>')


def kpi(label: str, value: Any, sub: str = "", tone: str = "#4f46e5") -> str:
    return (f'<div class="pl-kpi" style="--tone:{tone}"><div class="pl-kpi-label">{esc(label)}</div>'
            f'<div class="pl-kpi-value">{esc(value)}</div><div class="pl-kpi-sub">{esc(sub)}</div></div>')


def kpi_row(items: list[tuple[str, Any, str, str]]) -> None:
    cols = st.columns(len(items))
    for col, (label, value, sub, tone) in zip(cols, items, strict=True):
        with col:
            show_html(kpi(label, value, sub, tone))


def card(inner_html: str, title: str | None = None) -> None:
    head = f'<div class="pl-card-title">{esc(title)}</div>' if title else ""
    show_html(f'<div class="pl-card">{head}{inner_html}</div>')


def kv(rows: list[tuple[str, Any]], raw: bool = False) -> str:
    cells = "".join(f'<div class="k">{esc(k)}</div><div class="v">{v if raw else esc(v)}</div>' for k, v in rows)
    return f'<div class="pl-kv">{cells}</div>'


def empty_state(title: str, text: str, icon: str = "&#9671;") -> None:
    show_html(f'<div class="pl-empty"><div class="big">{icon}</div><h4>{esc(title)}</h4><div>{esc(text)}</div></div>')


def verdict_banner(verdict: str | None, summary: str, extra_chips: list[str] | None = None) -> None:
    v = VERDICT.get(verdict, VERDICT[None])
    chips = "".join(extra_chips or [])
    show_html(
        f'<div class="pl-verdict" style="--fg:{v["fg"]};--bg:{v["bg"]};--bd:{v["bd"]}">'
        f'<div class="pl-verdict-ico">{v["ico"]}</div><div style="flex:1">'
        f'<div class="pl-verdict-label">Verification verdict</div>'
        f'<div class="pl-verdict-title">{esc(v["label"])}</div>'
        f'<div class="pl-verdict-summary">{esc(summary)}</div>'
        f'<div class="pl-verdict-meta">{chips}</div></div></div>'
    )


def claim_vs_reality(claim: str | None, claimed_status: str | None, verdict: str | None, summary: str) -> None:
    claim_tone = {"success": "green", "failure": "red"}.get(claimed_status or "", "slate")
    v = VERDICT.get(verdict, VERDICT[None])
    left = (f'<div class="pl-card"><div class="pl-card-title">What the agent claimed</div>'
            f'<div class="pl-quote">&ldquo;{esc(claim or "(no claim)")}&rdquo;</div>'
            f'{chip("claimed " + (claimed_status or "unknown"), claim_tone)} {chip("agent-reported, not evidence", "amber")}</div>')
    right = (f'<div class="pl-card" style="border-color:{v["bd"]}"><div class="pl-card-title">What actually happened</div>'
             f'<div class="pl-quote" style="border-color:{v["fg"]}">{esc(summary)}</div>'
             f'{verdict_badge(verdict)} {chip("independently observed", "green")}</div>')
    show_html(f'<div class="pl-vs">{left}<div class="pl-vs-mid">VS</div>{right}</div>')


def check_list(checks: list[dict]) -> None:
    rows = []
    for c in checks:
        s = CHECK.get(c["status"], CHECK["skipped"])
        meta = [chip(c["category"], "slate")]
        if not c.get("decisive", True):
            meta.append(chip("informational", "slate"))
        for eid in c.get("evidence_ids", [])[:6]:
            meta.append(chip(eid, "blue", mono=True))
        parts = [f"<b>{label}:</b> {esc(_short(c.get(key)))}" for label, key in (("Expected", "expected"), ("Actual", "actual"))
                 if c.get(key) is not None]
        exp_act = f'<div class="pl-check-exp">{" &nbsp; ".join(parts)}</div>' if parts else ""
        rows.append(
            f'<div class="pl-check" style="--fg:{s["fg"]}"><div class="pl-check-ico">{s["ico"]}</div><div style="flex:1">'
            f'<div class="pl-check-name">{esc(c["name"])} <span style="color:#94a3b8;font-weight:500;font-size:.8rem">'
            f'&middot; {esc(c["id"])}</span></div><div class="pl-check-exp">{esc(c.get("explanation", ""))}</div>'
            f'{exp_act}<div class="pl-check-meta">{"".join(meta)}</div></div></div>'
        )
    show_html(f'<div class="pl-card">{"".join(rows)}</div>')


def _short(value: Any, n: int = 90) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= n else text[: n - 3] + "..."


EVENT_STYLE = {
    "execution_started": ("#4f46e5", "Execution started"),
    "agent_plan": ("#7c3aed", "Agent plan"),
    "tool_call": ("#0e7490", "Tool call"),
    "tool_response": ("#047857", "Tool response"),
    "error": ("#b91c1c", "Error"),
    "agent_claim": ("#b45309", "Agent claim"),
    "execution_completed": ("#334155", "Execution completed"),
    "annotation": ("#64748b", "Annotation"),
}


def timeline(events: list[dict]) -> None:
    items = []
    for e in events:
        color, label = EVENT_STYLE.get(e["event_type"], ("#64748b", e["event_type"]))
        if e["event_type"] == "tool_response" and e.get("status") == "error":
            color = "#b91c1c"
        head = [f'<span class="pl-tl-type" style="--fg:{color}">{esc(label)}</span>']
        if e.get("tool_name"):
            head.append(chip(e["tool_name"], "indigo", mono=True))
        if e.get("call_id"):
            head.append(chip(e["call_id"], "slate", mono=True))
        if e.get("status"):
            head.append(chip(e["status"], {"success": "green", "error": "red"}.get(e["status"], "slate")))
        if e.get("status_code"):
            head.append(chip(f"HTTP {e['status_code']}", "blue", mono=True))
        head.append(chip(f"#{e['sequence']}", "slate", mono=True, title=f"hash {e.get('event_hash', '')}"))
        head.append(f'<span class="pl-tl-time">{esc((e.get("timestamp") or "")[11:23])}</span>')
        body = f'<div class="pl-tl-msg">{esc(e.get("message", ""))}</div>' if e.get("message") else ""
        payload = e.get("input") if e["event_type"] in ("tool_call", "agent_plan", "execution_started") else e.get("output")
        if payload not in (None, {}, []):
            body += json_block(payload, 1200)
        items.append(f'<div class="pl-tl-item" style="--fg:{color}"><div class="pl-tl-dot"></div>'
                     f'<div class="pl-tl-body"><div class="pl-tl-head">{"".join(head)}</div>{body}</div></div>')
    show_html(f'<div class="pl-tl">{"".join(items)}</div>')


def execution_picker(key: str, label: str = "Execution") -> str | None:
    """Selectbox of recent executions, preselecting ``st.session_state.selected_exec``."""
    data = call(api().executions, limit=200, quiet=True) or {"items": []}
    items = data["items"]
    selected = st.session_state.get("selected_exec")
    if selected and all(i["id"] != selected for i in items):
        detail = call(api().execution, selected, quiet=True)
        if detail:
            items = [detail, *items]
    if not items:
        return None
    ids = [i["id"] for i in items]
    labels = {
        i["id"]: f"{i['id']}  ·  {(i.get('verdict') or i['execution_status']).upper()}  ·  {i['task_description'][:70]}"
        for i in items
    }
    index = ids.index(selected) if selected in ids else 0
    choice = st.selectbox(label, ids, index=index, format_func=lambda x: labels.get(x, x), key=key)
    st.session_state["selected_exec"] = choice
    return choice


def undo_panel(detail: dict) -> None:
    """'Undo this run' for runs on connected data (restores the backup taken right before the run)."""
    backup = detail.get("backup")
    if not backup:
        return
    if backup.get("restored_at"):
        st.caption(f"↩️ This run was undone by {backup.get('restored_by')} at {backup['restored_at'][:19].replace('T', ' ')} UTC.")
        return
    if not backup.get("can_undo"):
        st.caption("A later run changed this data source, so this run can no longer be undone on its own.")
        return
    with st.popover("Undo this run", icon=":material/undo:", width="stretch", disabled=not has_role("analyst")):
        st.write("Restore the data source exactly as it was right before this run (backup taken "
                 f"{backup['created_at'][:19].replace('T', ' ')} UTC). The report is kept.")
        if st.button("Restore the backup", type="primary", key=f"undo_{detail['id']}"):
            res = call(api().undo_execution, detail["id"])
            if res is not None:
                st.session_state["last_result"] = {**detail, "backup": res.get("backup")}
                st.toast("Run undone - the data is back to its previous state.", icon=":material/undo:")
                st.rerun()

