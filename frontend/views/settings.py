"""Settings: model backend (local / cloud), account, users (admin), audit log (admin), system & privacy."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from frontend.components.ui import (
    api,
    call,
    chip,
    current_user,
    esc,
    has_role,
    kv,
    page_header,
    show_html,
)

PASSWORD_HINT = "At least 8 characters, including a letter and a digit."


CONSENT_LABEL = ("I have read and understood this notice. I accept full responsibility for sending data to the cloud "
                 "provider and for any consequences to my data.")


def _backend_section(view: dict, is_admin: bool) -> None:
    """Local (Ollama) vs. cloud (NVIDIA NIM). Switching to cloud requires accepting the server's notice."""
    cloud = view.get("cloud") or {}
    rt = view["runtime"]
    active = bool(cloud.get("active"))
    with st.container(border=True):
        st.markdown("**Model backend**")
        if active:
            consent = cloud.get("consent") or {}
            st.error(f"**Cloud model active:** `{rt['cloud_model']}` at `{cloud.get('endpoint')}`. Task instructions, "
                     "data-source structure and the rows the AI agent reads are sent over the internet. Accepted by "
                     f"**{consent.get('accepted_by', '?')}** on {(consent.get('accepted_at') or '?').replace('T', ' ')[:19]} UTC "
                     f"(notice {consent.get('notice_version', '?')}).", icon=":material/cloud:")
        else:
            st.success("**Local model (Ollama).** Prompts and data never leave this machine.", icon=":material/lock:")
        choice = st.radio(
            "Backend", ["local", "cloud"], index=1 if active else 0, horizontal=True, disabled=not is_admin,
            key="backend_choice",
            format_func={"local": "Local - Ollama (private, offline)",
                         "cloud": "Cloud - NVIDIA NIM (stronger model, data leaves this machine)"}.get,
        )
        cloud_model, consent_ok = rt["cloud_model"], True
        if choice == "cloud":
            if "_cloud_catalog" not in st.session_state:
                st.session_state["_cloud_catalog"] = call(api().cloud_models, quiet=True) or {"reachable": False}
            catalog = st.session_state["_cloud_catalog"]
            listed = set(catalog.get("available") or [])
            rec = catalog.get("recommended") or [{"name": m["name"], "note": m["note"], "in_catalog": None}
                                                  for m in cloud.get("recommended_models", [])]
            # Recommended (still offered) first, then the current choice, then everything else in the live catalog.
            options = list(dict.fromkeys([*(m["name"] for m in rec if m.get("in_catalog") is not False),
                                          rt["cloud_model"], *sorted(listed)]))
            notes = {m["name"]: m["note"] for m in rec}

            def _label(name: str) -> str:
                tag = " · recommended" if name in notes else ""
                gone = " · NOT IN CATALOG" if catalog.get("reachable") and name not in listed else ""
                return f"{name}{tag}{gone}"

            cloud_model = st.selectbox("Cloud model", options, format_func=_label, disabled=not is_admin,
                                       help="Recommended models list function calling on build.nvidia.com (needed by the AI agent). "
                                            "Other models from the live catalog may not support tool calling.")
            if cloud_model in notes:
                st.caption(notes[cloud_model])
            if catalog.get("reachable"):
                st.caption(f"Live NVIDIA catalog: {len(listed)} chat models (fetched without your key or data).")
                if cloud_model not in listed:
                    st.warning(f"`{cloud_model}` is no longer offered by NVIDIA - pick a current model.", icon=":material/error:")
            else:
                st.caption("Could not read the live model catalog (offline?); showing the built-in recommendations.")
            if st.button("Refresh model list", icon=":material/refresh:", key="refresh_catalog"):
                st.session_state.pop("_cloud_catalog", None)
                st.rerun()
            if not cloud.get("api_key_configured"):
                st.warning("No API key configured. Create a free key at build.nvidia.com, add "
                           "`PROOFLOOP_CLOUD_API_KEY=nvapi-...` to `.env`, then restart ProofLoop (`run.ps1`).",
                           icon=":material/key:")
            if not active:
                notice = cloud.get("notice") or {}
                st.error("**Read before switching - consequences of using a cloud model**\n\n" + notice.get("text", ""),
                         icon=":material/warning:")
                consent_ok = st.checkbox(CONSENT_LABEL, key="cloud_consent_box", disabled=not is_admin)
        switching_on = choice == "cloud" and not active
        catalog = st.session_state.get("_cloud_catalog") or {}
        retired = choice == "cloud" and catalog.get("reachable") and cloud_model not in set(catalog.get("available") or [])
        blocked = retired or (switching_on and (not consent_ok or not cloud.get("api_key_configured")))
        label = "Switch to cloud model" if switching_on else ("Switch to local model" if active and choice == "local" else "Save backend")
        if st.button(label, type="primary", disabled=not is_admin or blocked, key="save_backend",
                     icon=":material/cloud_upload:" if switching_on else ":material/save:"):
            body: dict = {"llm_backend": choice}
            if choice == "cloud":
                body["cloud_model"] = cloud_model
                if switching_on:
                    body["cloud_consent"] = True
            if call(api().update_settings, **body) is not None:
                st.session_state.pop("_llm_status", None)
                st.session_state.pop("cloud_consent_box", None)
                st.toast("Model backend saved.", icon=":material/check_circle:")
                st.rerun()
        if not is_admin:
            st.caption("Only administrators can change the model backend.")


def _llm_tab() -> None:
    is_admin = has_role("admin")
    health = call(api().llm_health)
    models = call(api().models, quiet=True) or {"installed": [], "recommended": [], "selected": ""}
    view = call(api().settings)
    if health is None or view is None:
        return
    st.session_state["_llm_status"] = health
    rt = view["runtime"]
    _backend_section(view, is_admin)

    is_cloud = health.get("backend") == "cloud"
    left, right = st.columns([1.15, 1], gap="large")
    with left:
        state = (chip("running", "green") if health["reachable"] else chip("not reachable", "red"))
        if is_cloud:
            model_state = chip("available", "green") if health["model_available"] else chip("not available", "amber")
            endpoint_chip = chip("internet - data leaves this machine", "red")
        else:
            model_state = chip("installed", "green") if health["model_available"] else chip("not installed", "amber")
            endpoint_chip = chip("localhost only", "slate")
        mode = chip("verification-only mode", "amber") if health.get("verification_only_mode") else chip("LLM interpretation active", "green")
        show_html(
            f'<div class="pl-card"><div class="pl-card-title">{"Cloud model (NVIDIA NIM)" if is_cloud else "Ollama runtime"}</div>'
            + kv([("Endpoint", esc(health["endpoint"]) + " " + endpoint_chip),
                  ("Status", state + (f" v{esc(health['version'])}" if health.get("version") else "")),
                  ("Selected model", f"<b>{esc(health['model'])}</b> " + model_state),
                  ("Mode", mode)], raw=True)
            + f'<div class="pl-kpi-sub" style="margin-top:.6rem">{esc(health["message"])}</div></div>'
        )
        if st.button("Re-check connection", icon=":material/refresh:"):
            st.session_state.pop("_llm_status", None)
            st.rerun()
    with right:
        with st.container(border=True):
            st.markdown("**Interpretation settings**")
            if is_cloud:
                st.caption("The local model below is used again when you switch back to the local backend.")
            installed = [m["name"] for m in models.get("installed", [])]
            choices = list(dict.fromkeys([rt["ollama_model"], *installed]))
            model = st.selectbox("Local model", choices, index=0, disabled=not is_admin,
                                 help="Only models already installed in Ollama are listed (plus the current setting).")
            custom = st.text_input("…or type a model name", placeholder="e.g. llama3.2:3b", disabled=not is_admin)
            mode = st.radio("Interpreter mode", ["auto", "llm", "rules"], index=["auto", "llm", "rules"].index(rt["interpreter_mode"]),
                            horizontal=True, disabled=not is_admin,
                            format_func={"auto": "Auto (rules first, LLM for free-form)", "llm": "Always LLM (+ rule cross-check)", "rules": "Rules only"}.get)
            enabled = st.toggle("Enable LLM features", value=rt["llm_enabled"], disabled=not is_admin)
            narrative = st.toggle("Add an AI-written narrative to reports (clearly labelled, never affects the verdict)",
                                  value=rt["llm_narrative_enabled"], disabled=not is_admin)
            if st.button("Save settings", type="primary", disabled=not is_admin, icon=":material/save:"):
                body = {"ollama_model": (custom.strip() or model), "interpreter_mode": mode, "llm_enabled": enabled,
                        "llm_narrative_enabled": narrative}
                if call(api().update_settings, **body) is not None:
                    st.session_state.pop("_llm_status", None)
                    st.toast("Settings saved.", icon=":material/check_circle:")
                    st.rerun()
            if not is_admin:
                st.caption("Only administrators can change these settings.")

    st.markdown("##### Installed models")
    if models.get("installed"):
        st.dataframe(pd.DataFrame([{"model": m["name"], "parameters": m.get("parameter_size"), "quantization": m.get("quantization"),
                                    "size (GB)": round((m.get("size_bytes") or 0) / 1e9, 2)} for m in models["installed"]]),
                     hide_index=True, width="stretch")
    else:
        st.caption("No local models installed yet - ProofLoop runs in verification-only mode until you download one.")
    st.markdown("##### Recommended models")
    st.dataframe(pd.DataFrame(models.get("recommended", [])), hide_index=True, width="stretch")
    st.markdown("Model downloads are **never automatic**. To install the default model (≈1.9 GB, one-time internet access):")
    st.code("ollama pull qwen2.5:3b", language="powershell")


def _account_tab() -> None:
    user = current_user()
    show_html('<div class="pl-card"><div class="pl-card-title">Your account</div>'
           + kv([("Username", user.get("username")), ("Full name", user.get("full_name") or "-"), ("Role", user.get("role")),
                 ("Last sign-in", (user.get("last_login_at") or "-").replace("T", " ")[:19] + " UTC")]) + "</div>")
    with st.container(border=True):
        st.markdown("**Change password**")
        with st.form("change_pw", clear_on_submit=True, border=False):
            current = st.text_input("Current password", type="password")
            new = st.text_input("New password", type="password", help=PASSWORD_HINT)
            confirm = st.text_input("Confirm new password", type="password")
            ok = st.form_submit_button("Update password", type="primary")
        if ok:
            if new != confirm:
                st.error("The new passwords do not match.")
            elif call(api().change_password, current, new) is not None:
                st.success("Password updated. All your other sessions were signed out.")


def _users_tab() -> None:
    users = call(api().users)
    if users is None:
        return
    df = pd.DataFrame([{"id": u["id"], "username": u["username"], "full name": u["full_name"], "role": u["role"],
                        "active": u["is_active"], "must change password": u["must_change_password"],
                        "last sign-in (UTC)": (u["last_login_at"] or "-").replace("T", " ")[:19]} for u in users])
    st.dataframe(df, hide_index=True, width="stretch")
    c1, c2 = st.columns(2, gap="large")
    with c1:
        with st.container(border=True):
            st.markdown("**Add a user**")
            with st.form("new_user", clear_on_submit=True, border=False):
                username = st.text_input("Username")
                full_name = st.text_input("Full name")
                role = st.selectbox("Role", ["analyst", "viewer", "admin"],
                                    help="viewer: read-only · analyst: run executions & benchmarks · admin: everything")
                pw = st.text_input("Temporary password", type="password", help=PASSWORD_HINT + " The user must change it at first sign-in.")
                ok = st.form_submit_button("Create user", type="primary")
            if ok and call(api().create_user, username=username.strip(), full_name=full_name.strip(), role=role, password=pw):
                st.toast(f"User {username} created.", icon=":material/person_add:")
                st.rerun()
    with c2:
        with st.container(border=True):
            st.markdown("**Manage a user**")
            by_id = {u["id"]: u for u in users}
            uid = st.selectbox("User", list(by_id), format_func=lambda i: f"{by_id[i]['username']} ({by_id[i]['role']})")
            u = by_id[uid]
            role = st.selectbox("Role ", ["viewer", "analyst", "admin"], index=["viewer", "analyst", "admin"].index(u["role"]),
                                key=f"role_{uid}")
            active = st.toggle("Active", value=u["is_active"], key=f"active_{uid}")
            if st.button("Save changes", key=f"save_{uid}"):
                if call(api().update_user, uid, role=role, is_active=active) is not None:
                    st.toast("User updated.", icon=":material/check_circle:")
                    st.rerun()
            with st.popover("Reset password", icon=":material/key:"):
                temp = st.text_input("New temporary password", type="password", key=f"tmp_{uid}", help=PASSWORD_HINT)
                if st.button("Reset", type="primary", key=f"reset_{uid}"):
                    if call(api().reset_password, uid, temp) is not None:
                        st.success("Password reset. The user must choose a new one at next sign-in; their sessions were revoked.")


def _audit_tab() -> None:
    rows = call(api().audit, 300)
    if rows is None:
        return
    df = pd.DataFrame(rows)
    if df.empty:
        st.caption("No audit events yet.")
        return
    df["created_at"] = df["created_at"].str.replace("T", " ").str.slice(0, 19)
    st.dataframe(df[["created_at", "actor", "action", "detail"]], hide_index=True, width="stretch", height=520)


def _system_tab() -> None:
    view = call(api().settings)
    health = call(api().health, quiet=True) or {}
    if view is None:
        return
    left, right = st.columns(2, gap="large")
    with left:
        st.markdown("##### Privacy guarantees")
        for k, v in view["privacy"].items():
            st.markdown(f"- **{k.replace('_', ' ').capitalize()}:** {v}")
        st.markdown("##### Network access")
        if (view.get("cloud") or {}).get("active"):
            st.markdown("**The cloud model is active**: every interpretation, AI-agent step and (if enabled) report "
                        "narrative is sent to the cloud endpoint. Switch back to the local backend to work offline again.")
        else:
            st.markdown("Internet is only needed during setup: installing Python packages, installing Ollama and "
                        "downloading model weights. Normal operation is fully offline.")
    with right:
        st.markdown("##### Local configuration")
        st.dataframe(pd.DataFrame([{"setting": k, "value": str(v)} for k, v in view["config"].items()]),
                     hide_index=True, width="stretch")
        st.caption(f"Backend {health.get('version', '?')} · database {health.get('database', '?')} · "
                   f"uptime {health.get('uptime_seconds', '?')} s")


def render() -> None:
    page_header("Administer", "Settings", "Model backend, accounts, access control, audit trail and privacy configuration.")
    names = ["Model", "My account"]
    if has_role("admin"):
        names += ["Users", "Audit log"]
    names.append("System & privacy")
    tabs = dict(zip(names, st.tabs(names), strict=True))
    with tabs["Model"]:
        _llm_tab()
    with tabs["My account"]:
        _account_tab()
    if "Users" in tabs:
        with tabs["Users"]:
            _users_tab()
        with tabs["Audit log"]:
            _audit_tab()
    with tabs["System & privacy"]:
        _system_tab()
