"""Connectors to user-provided data sources (real data, not the built-in sandboxes).

A connected source is exposed as an :class:`app.environments.base.Environment`, so the
whole verification pipeline (snapshots, trace attribution, side-effect detection,
verdict policy) works on it unchanged. Each source gives the agent a write-capable
toolbox and gives ProofLoop a separate **read-only** inspector.
"""
