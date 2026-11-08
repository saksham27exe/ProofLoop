"""Write policies (guardrails) for connected data sources.

A policy is enforced *before* a tool call reaches the data: a blocked call fails with a clear
"Blocked by policy" error, is recorded in the hash-chained trace like any other call, and the
data is never touched. It applies to ProofLoop's own AI agent and to external agents that use
ProofLoop's tools through a verification session.

Policies complement - never replace - outcome verification: an agent that writes to the data
directly (outside ProofLoop's tools) is still judged on the independent before/after snapshots,
where unrequested changes surface as side effects.

Stored in the settings table (``policy:<environment>``), so no schema migration is needed.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import repositories as repo
from app.environments.base import Environment, Toolbox, ToolResult, ToolSpec

WRITE_TOOLS = {"insert_row", "update_rows", "delete_rows", "write_file", "delete_file"}
DELETE_TOOLS = {"delete_rows", "delete_file"}
INSERT_TOOLS = {"insert_row"}


class SourcePolicy(BaseModel):
    read_only: list[str] = Field(default_factory=list, max_length=200,
                                 description="Tables (databases) or path prefixes (folders) the agent may not change")
    protected_columns: list[str] = Field(default_factory=list, max_length=500,
                                         description="'table.column' entries the agent may not write (databases)")
    allow_delete: bool = True
    allow_insert: bool = True

    @property
    def is_empty(self) -> bool:
        return not self.read_only and not self.protected_columns and self.allow_delete and self.allow_insert

    def describe(self) -> list[str]:
        rules = []
        if self.read_only:
            rules.append("read-only: " + ", ".join(self.read_only))
        if self.protected_columns:
            rules.append("protected columns: " + ", ".join(self.protected_columns))
        if not self.allow_delete:
            rules.append("no deletions")
        if not self.allow_insert:
            rules.append("no new records")
        return rules


class PolicyError(ValueError):
    pass


def _key(env_name: str) -> str:
    return f"policy:{env_name}"


def load_policy(session: Session, env_name: str) -> SourcePolicy:
    return SourcePolicy.model_validate(repo.get_setting(session, _key(env_name), None) or {})


def validate_policy(policy: SourcePolicy, env: Environment) -> SourcePolicy:
    """Normalise and check every name against the source's discovered structure."""
    is_folder = any(e.key_type == "path" for e in env.schema.entities)
    if is_folder:
        if policy.protected_columns:
            raise PolicyError("Protected columns only apply to databases; use read-only path prefixes for folders.")
        cleaned = sorted({p.strip().replace("\\", "/").strip("/") for p in policy.read_only if p.strip()})
        if any(".." in p.split("/") for p in cleaned):
            raise PolicyError("Read-only paths must be relative to the folder.")
        return policy.model_copy(update={"read_only": cleaned})
    tables = {e.collection: {f.name for f in e.fields} | {e.key_field} for e in env.schema.entities}
    read_only = sorted({t.strip() for t in policy.read_only if t.strip()})
    unknown = [t for t in read_only if t not in tables]
    if unknown:
        raise PolicyError(f"Unknown table(s): {', '.join(unknown)}. Known: {', '.join(sorted(tables))}.")
    columns = sorted({c.strip() for c in policy.protected_columns if c.strip()})
    for entry in columns:
        table, _, column = entry.partition(".")
        if not column or table not in tables or column not in tables[table]:
            raise PolicyError(f"Unknown column {entry!r}; use 'table.column' with a column of a known table.")
    return policy.model_copy(update={"read_only": read_only, "protected_columns": columns})


def save_policy(session: Session, env: Environment, policy: SourcePolicy, actor: str) -> SourcePolicy:
    policy = validate_policy(policy, env)
    repo.set_setting(session, _key(env.name), policy.model_dump(), actor)
    repo.audit(session, actor, "policy_changed", f"{env.name}: " + ("; ".join(policy.describe()) or "no restrictions"))
    return policy


def check_call(policy: SourcePolicy, name: str, args: dict[str, Any]) -> str | None:
    """Return the reason a call is blocked, or None when it is allowed."""
    if name not in WRITE_TOOLS or policy.is_empty:
        return None
    if name in DELETE_TOOLS and not policy.allow_delete:
        return "deletions are not allowed on this data source"
    if name in INSERT_TOOLS and not policy.allow_insert:
        return "creating new records is not allowed on this data source"
    if "path" in args or name in ("write_file", "delete_file"):
        path = str(args.get("path") or "").replace("\\", "/").strip("/")
        for prefix in policy.read_only:
            if path == prefix or path.startswith(prefix + "/"):
                return f"'{prefix}' is read-only"
        return None
    table = str(args.get("table") or "")
    if table in policy.read_only:
        return f"table '{table}' is read-only"
    values = args.get("values") if isinstance(args.get("values"), dict) else {}
    hits = sorted(f"{table}.{col}" for col in values if f"{table}.{col}" in policy.protected_columns)
    if hits:
        return f"protected column(s) {', '.join(hits)} may not be changed"
    return None


class PolicyToolbox(Toolbox):
    """Wraps a toolbox and refuses calls that violate the source's policy."""

    def __init__(self, inner: Toolbox, policy: SourcePolicy) -> None:
        self._inner = inner
        self.policy = policy

    @property
    def specs(self) -> list[ToolSpec]:
        return self._inner.specs

    def call(self, name: str, args: dict[str, Any], *, inject_failure: bool = False) -> ToolResult:
        reason = check_call(self.policy, name, args if isinstance(args, dict) else {})
        if reason:
            return ToolResult.failure(f"Blocked by policy: {reason}. Nothing was changed.", status_code=403,
                                      output={"blocked_by_policy": True})
        return self._inner.call(name, args, inject_failure=inject_failure)
