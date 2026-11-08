"""Data Sources: connect real SQLite databases / folders, browse them read-only, manage the practice store."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from frontend.components.ui import api, call, chip, esc, goto, has_role, page_header, show_html

KIND_ICON = {"sqlite": ":material/database:", "folder": ":material/folder_open:"}


def _browse(env: str, kind: str) -> None:
    data = call(api().sandbox, env)
    if data is None:
        return
    collections = data["collections"]
    if not collections:
        st.caption("No tables or files.")
        return
    tabs = st.tabs([f"{name} ({len(rows)})" for name, rows in collections.items()])
    for tab, (_name, rows) in zip(tabs, collections.items(), strict=True):
        with tab:
            if not rows:
                st.caption("Empty.")
                continue
            df = pd.DataFrame(rows[:1000])
            if kind == "folder":
                df = df[["path", "size", "sha256"]]
                df["sha256"] = df["sha256"].str.slice(0, 16) + "…"
            st.dataframe(df, hide_index=True, width="stretch", height=min(460, 40 + 35 * len(df)))
            if len(rows) > 1000:
                st.caption(f"Showing the first 1,000 of {len(rows):,} rows.")


def _source_card(s: dict, is_admin: bool, can_run: bool) -> None:
    status_tone = {"connected": "green", "unavailable": "red", "disabled": "slate"}.get(s["status"], "slate")
    with st.container(border=True):
        left, right = st.columns([3.2, 1.3], vertical_alignment="top")
        with left:
            chips = [chip(s["status"], status_tone), chip("SQLite database" if s["kind"] == "sqlite" else "Folder", "indigo"),
                     chip("practice data (fictional)" if s["is_practice"] else "your data", "violet" if s["is_practice"] else "amber"),
                     chip("exclusive during runs" if s["exclusive"] else "shared with other apps", "slate",
                          title="Exclusive: only ProofLoop's agent writes during a run, so every out-of-scope change is "
                                "attributed to it. Shared: unexplained changes are reported as uncertain.")]
            show_html(f'<div style="font-size:1.12rem;font-weight:750;color:#0f172a">{esc(s["name"])}</div>'
                      f'<div style="margin:.35rem 0">{"".join(chips)}</div>'
                      f'<div class="pl-kpi-sub" style="word-break:break-all"><code>{esc(s["location"])}</code></div>')
            if s["entities"] and s["kind"] == "folder":
                show_html('<div style="margin-top:.4rem;font-size:.9rem;color:#334155">Text files in this folder and its '
                          'sub-folders (hidden and tool folders such as .git are skipped)</div>')
            elif s["entities"]:
                parts = [f'{esc(e["collection"])} <span style="color:#94a3b8">({len(e["fields"])} columns)</span>'
                         for e in s["entities"]]
                show_html('<div style="margin-top:.4rem;font-size:.9rem;color:#334155">' + " · ".join(parts) + "</div>")
            if s.get("error"):
                st.error(s["error"], icon=":material/error:")
            for w in s.get("warnings") or []:
                st.caption(f"⚠️ {w}")
        with right:
            if s["status"] == "connected" and st.button("Run a task here", key=f"run_{s['id']}", type="primary",
                                                        icon=":material/play_arrow:", width="stretch", disabled=not can_run):
                st.session_state["real_source"] = s["environment"]
                goto("new")
            if st.button("Re-read structure", key=f"ref_{s['id']}", icon=":material/refresh:", width="stretch",
                         disabled=not can_run, help="Use after tables or columns changed."):
                if call(api().refresh_source, s["id"]) is not None:
                    st.session_state.pop("_source_names", None)
                    st.toast("Structure re-read.", icon=":material/check_circle:")
                    st.rerun()
            if s["is_practice"]:
                with st.popover("Reset practice data", icon=":material/restart_alt:", width="stretch", disabled=not can_run):
                    st.write("Regenerate the fictional practice store in its original state. Undo history for it is lost.")
                    if st.button("Confirm reset", type="primary", key="practice_reset"):
                        res = call(api().reset_practice)
                        if res is not None:
                            st.toast("Practice store reset.", icon=":material/check_circle:")
                            st.rerun()
            elif is_admin:
                with st.popover("Disconnect", icon=":material/link_off:", width="stretch"):
                    st.write("ProofLoop forgets this source. **Your data is not touched**, and past reports are kept.")
                    if st.button("Confirm disconnect", type="primary", key=f"del_{s['id']}"):
                        if call(api().disconnect_source, s["id"]) is not None:
                            st.session_state.pop("_source_names", None)
                            st.toast("Disconnected.", icon=":material/check_circle:")
                            st.rerun()
        if s["status"] == "connected":
            with st.expander("Browse the data (read-only)", icon=":material/table_view:"):
                _browse(s["environment"], s["kind"])


def _connect_form() -> None:
    st.markdown("#### Connect your own data")
    st.info("**Start with a copy.** The AI agent really changes the data you connect. ProofLoop backs it up before every "
            "run (so you can undo it) and checks the result through a separate read-only connection, but do not point it at "
            "live production systems.", icon=":material/shield:")
    with st.form("connect_source", border=True):
        a, b = st.columns([1, 2])
        name = a.text_input("Name", placeholder="e.g. Shop copy", max_chars=80)
        location = b.text_input("Full path of a SQLite file or a folder",
                                placeholder=r"C:\Data\shop_copy.db   or   C:\Data\Documents")
        c, d = st.columns(2)
        excluded = c.text_input("Hide these tables / sub-folders (optional, comma-separated)",
                                help="Hidden tables are invisible to the agent and not checked, e.g. large log tables.")
        max_rows = d.number_input("Max rows one agent action may change", min_value=1, max_value=100_000, value=200,
                                  help="Safety limit: an update or delete that would touch more rows is refused.")
        exclusive = st.checkbox("Nothing else writes to this data while a task runs", value=True,
                                help="Lets ProofLoop attribute every unexpected change to the agent. Untick for data "
                                     "that other apps update; unexplained changes are then reported as uncertain.")
        if st.form_submit_button("Test & connect", type="primary", icon=":material/link:"):
            if not name.strip() or not location.strip():
                st.error("Enter a name and the full path.")
                return
            res = call(api().connect_source, name=name.strip(), location=location.strip(), exclusive=exclusive,
                       max_rows_changed=int(max_rows), excluded=[x.strip() for x in excluded.split(",") if x.strip()])
            if res is not None:
                st.session_state.pop("_source_names", None)
                found = "folder connected" if res["kind"] == "folder" else f"{len(res['entities'])} table(s) found"
                st.toast(f"Connected {res['name']} - {found}.", icon=":material/check_circle:")
                st.rerun()


def render() -> None:
    page_header("Real data", "Data sources",
                "The AI agent works on these for real. ProofLoop reads them through a separate read-only connection and "
                "takes a backup before every run, so any run can be undone.")
    sources = call(api().sources) or []
    is_admin, can_run = has_role("admin"), has_role("analyst")
    for s in sources:
        _source_card(s, is_admin, can_run)
    st.space("small")
    if is_admin:
        _connect_form()
    else:
        st.caption("Only administrators can connect new data sources.")
