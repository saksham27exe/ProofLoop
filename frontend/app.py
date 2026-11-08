"""ProofLoop dashboard entry point.

    streamlit run frontend/app.py

The dashboard is a client of the local FastAPI backend; it never touches the
evidence database or the sandboxes directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402
import streamlit.components.v1 as components  # noqa: E402

from frontend.api_client import ApiClient, ApiError  # noqa: E402
from frontend.components.theme import inject_css  # noqa: E402
from frontend.components.ui import api, esc, show_html, sign_out  # noqa: E402
from frontend.views import (  # noqa: E402
    benchmark,
    data_sources,
    execution_monitor,
    history,
    login,
    new_execution,
    overview,
    sandbox,
    settings,
    verification_results,
)

st.set_page_config(page_title="ProofLoop", page_icon=str(STATIC / "icon.svg"), layout="wide",
                   initial_sidebar_state="expanded")
inject_css()


def _seconds_until(iso_ts: str) -> int:
    from datetime import UTC, datetime

    try:
        when = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return int((when - datetime.now(UTC)).total_seconds())
    except ValueError:
        return 12 * 3600


def _refresh_user() -> bool:
    try:
        st.session_state["user"] = api().me()
        return True
    except ApiError as err:
        if err.status == 401:
            sign_out("Your session has ended. Please sign in again.")
        st.error(err.message)
        return False


def _sidebar() -> None:
    user = st.session_state.get("user", {})
    initials = "".join(p[0] for p in (user.get("full_name") or user.get("username", "?")).split()[:2]).upper() or "?"
    with st.sidebar:
        llm = st.session_state.get("_llm_status")
        if llm is None:
            try:
                llm = api().llm_health()
            except ApiError:
                llm = {"reachable": False, "model_available": False, "model": "?"}
            st.session_state["_llm_status"] = llm
        cloud_on = llm.get("backend") == "cloud"
        if cloud_on:
            llm_html = (f'<span class="pl-dot off" style="background:#dc2626"></span><b>Cloud LLM</b> &middot; '
                        f'{esc(llm.get("model"))}<br/><small>Data leaves this machine</small>')
        elif llm.get("model_available") and llm.get("llm_enabled", True) and llm.get("interpreter_mode") != "rules":
            llm_html = f'<span class="pl-dot on"></span>Local LLM &middot; {esc(llm.get("model"))}'
        else:
            llm_html = '<span class="pl-dot off"></span>Verification-only mode (rules)'
        show_html(
            f'<div class="pl-side-card"><div class="pl-side-user"><div class="pl-avatar">{esc(initials)}</div><div>'
            f'<div class="pl-side-name">{esc(user.get("full_name") or user.get("username"))}</div>'
            f'<div class="pl-side-role">{esc(user.get("role"))} &middot; @{esc(user.get("username"))}</div></div></div></div>'
            f'<div class="pl-side-card"><div class="pl-side-llm">{llm_html}</div></div>'
        )
        if st.button("Sign out", icon=":material/logout:", width="stretch"):
            try:
                api().logout()
            except ApiError:
                pass
            sign_out("You have been signed out.")
        foot = ("Cloud model ON &middot; no telemetry<br/>Task data is sent to the cloud provider." if cloud_on
                else "Local-first &middot; no telemetry<br/>All data stays on this machine.")
        show_html(f'<div class="pl-side-foot">{foot}</div>')


# Browsers share cookies across ports on the same host, so the name includes the port
# (two ProofLoop instances on one machine must not sign each other out).
COOKIE = f"proofloop_session_{st.get_option('server.port')}"
PROTECTED_PATHS = ["home", "overview", "new-execution", "data-sources", "monitor", "results", "history", "benchmark", "sandbox", "settings"]


def _cookie_script(value: str, max_age: int) -> None:
    """Set/clear the remember-me cookie (SameSite=Strict, this origin only)."""
    safe = "".join(ch for ch in value if ch.isalnum() or ch in "-_")
    with st.sidebar:
        components.html(
            f"<script>document.cookie='{COOKIE}={safe}; path=/; max-age={int(max_age)}; SameSite=Strict';</script>",
            height=0,
        )


def _restore_from_cookie() -> None:
    token = st.context.cookies.get(COOKIE)
    if not token or st.session_state.get("_cookie_rejected") == token:
        return
    try:
        user = ApiClient(token=token).me()
    except ApiError as err:
        if err.status == 401:  # revoked or expired - forget it
            st.session_state["_cookie_rejected"] = token
            st.session_state["_clear_cookie"] = True
        return
    st.session_state["token"], st.session_state["user"] = token, user


if not st.session_state.get("token"):
    _restore_from_cookie()

if not st.session_state.get("token"):
    if st.session_state.pop("_clear_cookie", False):
        _cookie_script("", 0)
    # Every protected URL resolves to the sign-in form, so deep links survive the login.
    login_pages = [st.Page(login.render_login, title="Sign in", icon=":material/login:", url_path="login", default=True)]
    login_pages += [st.Page(login.render_login, title="Sign in", url_path=p) for p in PROTECTED_PATHS]
    st.navigation(login_pages, position="hidden").run()
    st.stop()

if "_set_cookie" in st.session_state:
    token, expires = st.session_state.pop("_set_cookie")
    _cookie_script(token, 12 * 3600 if not expires else max(60, _seconds_until(expires)))

if not _refresh_user():
    st.stop()

if st.session_state["user"].get("must_change_password"):
    st.navigation([st.Page(login.render_force_password_change, title="Change password", url_path="change-password")],
                  position="hidden").run()
    st.stop()

pages = {
    "overview": st.Page(overview.render, title="Overview", icon=":material/space_dashboard:", url_path="home", default=True),
    "new": st.Page(new_execution.render, title="New Execution", icon=":material/play_circle:", url_path="new-execution"),
    "sources": st.Page(data_sources.render, title="Data Sources", icon=":material/storage:", url_path="data-sources"),
    "monitor": st.Page(execution_monitor.render, title="Execution Monitor", icon=":material/timeline:", url_path="monitor"),
    "results": st.Page(verification_results.render, title="Verification Results", icon=":material/fact_check:", url_path="results"),
    "history": st.Page(history.render, title="History", icon=":material/history:", url_path="history"),
    "benchmark": st.Page(benchmark.render, title="Benchmark", icon=":material/monitoring:", url_path="benchmark"),
    "sandbox": st.Page(sandbox.render, title="Sandbox", icon=":material/inventory_2:", url_path="sandbox"),
    "settings": st.Page(settings.render, title="Settings", icon=":material/settings:", url_path="settings"),
}
st.session_state["_pages"] = pages
# Hidden aliases so bookmarked /overview and /login URLs land on the dashboard instead of "page not found".
aliases = [st.Page(overview.render, title="Overview", url_path=p, visibility="hidden") for p in ("overview", "login")]
navigation = st.navigation(
    {
        "Verify": [pages["overview"], pages["new"], pages["sources"], pages["monitor"], pages["results"], pages["history"], *aliases],
        "Evaluate": [pages["benchmark"], pages["sandbox"]],
        "Administer": [pages["settings"]],
    },
    position="sidebar",
    expanded=True,
)
st.logo(str(STATIC / "logo.svg"), size="large", icon_image=str(STATIC / "icon.svg"))
_sidebar()
navigation.run()
