"""Sign-in, first-run administrator setup and forced password change."""

from __future__ import annotations

import streamlit as st

from frontend.api_client import ApiClient, ApiError
from frontend.components.theme import LOGO_SVG
from frontend.components.ui import api, esc, show_html

PASSWORD_HINT = "At least 8 characters, including a letter and a digit."


def _brand(subtitle: str) -> None:
    show_html(
        f'<div class="pl-login-brand"><div class="pl-logo">{LOGO_SVG}</div>'
        f'<div class="pl-login-title">ProofLoop</div><div class="pl-login-tag">{esc(subtitle)}</div></div>'
    )


def _store_session(payload: dict, remember: bool = True) -> None:
    st.session_state["token"] = payload["access_token"]
    st.session_state["user"] = payload["user"]
    st.session_state.pop("flash", None)
    if remember:
        st.session_state["_set_cookie"] = (payload["access_token"], payload.get("expires_at"))


def _backend_down(message: str) -> None:
    _, mid, _ = st.columns([1, 2.2, 1])
    with mid:
        _brand("Agent outcome verification")
        st.error("The ProofLoop backend is not reachable.", icon=":material/cloud_off:")
        st.markdown(f"{message}\n\nStart everything with:\n```powershell\n.\\run.ps1\n```")
        if st.button("Retry", type="primary", width="stretch"):
            st.rerun()


def render_login() -> None:
    client = ApiClient()
    try:
        needs_bootstrap = client.bootstrap_status()["needs_bootstrap"]
    except ApiError as err:
        _backend_down(err.message)
        return

    _, mid, _ = st.columns([1, 1.35, 1])
    with mid:
        if needs_bootstrap:
            _brand("Welcome! Create the first administrator account.")
            with st.container(border=True):
                st.markdown("##### :material/shield_person: Set up your workspace")
                st.caption("This account is stored only on this computer (local SQLite, PBKDF2-hashed password).")
                with st.form("bootstrap", border=False):
                    full_name = st.text_input("Full name", placeholder="Ada Lovelace")
                    username = st.text_input("Username", placeholder="admin", autocomplete="username")
                    pw = st.text_input("Password", type="password", help=PASSWORD_HINT, autocomplete="new-password")
                    pw2 = st.text_input("Confirm password", type="password", autocomplete="new-password")
                    submitted = st.form_submit_button("Create account & sign in", type="primary", width="stretch")
                if submitted:
                    if pw != pw2:
                        st.error("The passwords do not match.")
                    else:
                        try:
                            _store_session(client.bootstrap(username.strip(), pw, full_name.strip()))
                            st.rerun()
                        except ApiError as err:
                            st.error(err.message)
        else:
            _brand("Don't trust what an AI agent says it did. Verify what actually happened.")
            if st.session_state.get("flash"):
                st.info(st.session_state["flash"], icon=":material/info:")
            with st.container(border=True):
                st.markdown("##### Sign in")
                with st.form("login", border=False):
                    username = st.text_input("Username", autocomplete="username")
                    pw = st.text_input("Password", type="password", autocomplete="current-password")
                    remember = st.checkbox("Keep me signed in on this computer", value=True,
                                           help="Stores your session token in a browser cookie for this site until it expires or you sign out.")
                    submitted = st.form_submit_button("Sign in", type="primary", width="stretch", icon=":material/login:")
                if submitted:
                    if not username or not pw:
                        st.warning("Enter your username and password.")
                    else:
                        try:
                            _store_session(client.login(username.strip(), pw), remember)
                            st.rerun()
                        except ApiError as err:
                            st.error(err.message, icon=":material/lock:")
                st.caption("Forgot your password? Ask an administrator to reset it from **Settings → Users**.")
        show_html('<div class="pl-foot-note">Local-first &middot; no cloud APIs &middot; no telemetry &middot; data never leaves this machine</div>')


def render_force_password_change() -> None:
    _, mid, _ = st.columns([1, 1.35, 1])
    with mid:
        _brand("Please choose a new password to continue.")
        with st.container(border=True):
            st.info("An administrator created or reset your password. Set a personal password now.", icon=":material/key:")
            with st.form("force_change", border=False):
                current = st.text_input("Current (temporary) password", type="password")
                new = st.text_input("New password", type="password", help=PASSWORD_HINT)
                confirm = st.text_input("Confirm new password", type="password")
                ok = st.form_submit_button("Update password", type="primary", width="stretch")
            if ok:
                if new != confirm:
                    st.error("The new passwords do not match.")
                else:
                    try:
                        api().change_password(current, new)
                        st.session_state["user"]["must_change_password"] = False
                        st.success("Password updated.")
                        st.rerun()
                    except ApiError as err:
                        st.error(err.message)
            if st.button("Sign out", width="stretch"):
                try:
                    api().logout()
                finally:
                    for k in ("token", "user"):
                        st.session_state.pop(k, None)
                    st.rerun()
