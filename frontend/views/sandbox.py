"""Sandbox explorer: the synthetic systems the agent acts on (read through the verifier's read-only path)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from frontend.components.ui import ENV_ICON, ENV_LABEL, api, call, has_role, page_header


def render() -> None:
    page_header("Evaluate", "Sandbox",
                "Synthetic data only. This view reads each environment through the same read-only inspector the verifier uses.")
    env = st.segmented_control("Environment", list(ENV_LABEL), format_func=lambda e: ENV_LABEL[e], default="database",
                               key="sbx_env", label_visibility="collapsed") or "database"
    data = call(api().sandbox, env)
    if data is None:
        return
    head_l, head_r = st.columns([3, 1], vertical_alignment="center")
    head_l.markdown(f"### {ENV_ICON[env]} {data['title']}")
    head_l.caption(f"Resource: `{data['resource']}`")
    with head_r:
        with st.popover("Reset to seed data", icon=":material/restart_alt:", width="stretch", disabled=not has_role("analyst")):
            st.write("Restore the deterministic synthetic seed state for this environment. "
                     "Executions that ran but are not yet verified will verify as INCONCLUSIVE.")
            if st.button("Confirm reset", type="primary", key=f"reset_{env}"):
                if call(api().reset_sandbox, env) is not None:
                    st.toast(f"{ENV_LABEL[env]} sandbox reset.", icon=":material/check_circle:")
                    st.rerun()

    collections = data["collections"]
    tabs = st.tabs([f"{name} ({len(rows)})" for name, rows in collections.items()])
    for tab, (_name, rows) in zip(tabs, collections.items(), strict=True):
        with tab:
            if not rows:
                st.caption("Empty.")
                continue
            df = pd.DataFrame(rows)
            if env == "file":
                df = df[["path", "size", "sha256", "content"]]
                df["sha256"] = df["sha256"].str.slice(0, 16) + "…"
                st.dataframe(df[["path", "size", "sha256"]], hide_index=True, width="stretch")
                for row in rows:
                    with st.expander(f"{row['path']}  ·  {row['size']} bytes"):
                        st.code(row.get("content") or "(binary or too large to preview)", language=None)
            else:
                st.dataframe(df, hide_index=True, width="stretch", height=min(520, 40 + 35 * len(df)))
