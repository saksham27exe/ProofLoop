"""Common abstractions for sandbox environments.

An *environment* is a controlled local system that an agent acts on. Each
environment exposes two deliberately separate capabilities:

* :class:`Toolbox` - the agent's privileged, write-capable tools.
* :class:`StateInspector` - ProofLoop's **read-only** view used to capture
  independent before/after snapshots. It never shares a connection or
  credential with the agent's toolbox.

Snapshots share one normalised shape across all environments::

    {"<collection>": {"<record key>": {<field>: <value>, ...}, ...}, ...}
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Any, Literal

from pydantic import BaseModel, Field

Snapshot = dict[str, dict[str, dict[str, Any]]]


class InspectorUnavailable(Exception):
    """The verifier could not observe the environment's state."""


class FieldSpec(BaseModel):
    name: str
    type: Literal["string", "integer", "number", "email", "phone", "enum", "text"] = "string"
    description: str = ""
    enum_values: list[str] = Field(default_factory=list)
    synonyms: list[str] = Field(default_factory=list)
    required_on_create: bool = False


class DependencySpec(BaseModel):
    """A collection whose rows are legitimate by-products of changes elsewhere.

    Example: ``audit_log`` rows written by database triggers whenever an
    ``employees`` row changes. ``parent_collection_field`` names the column
    holding the parent collection, ``parent_key_field`` the parent record key.
    """

    collection: str
    parent_collection_field: str
    parent_key_field: str
    description: str = ""


class CascadeSpec(BaseModel):
    """A foreign key declared ``ON DELETE CASCADE`` / ``SET NULL`` in the data source itself.

    When an expected deletion removes a parent row, child rows removed (CASCADE) or whose ``child_field`` was
    nulled (SET NULL) because of it are legitimate consequences, not side effects.
    """

    child_collection: str
    child_field: str
    parent_collection: str
    parent_field: str
    action: Literal["CASCADE", "SET NULL"]


class ToolSpec(BaseModel):
    name: str
    description: str
    mutating: bool
    # JSON schema of the arguments - required for tools offered to a tool-calling LLM agent.
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class EntitySpec(BaseModel):
    name: str
    collection: str
    display_name: str
    aliases: list[str] = Field(default_factory=list)
    key_field: str = "id"
    key_type: Literal["integer", "string", "path"] = "integer"
    label_field: str = "name"
    fields: list[FieldSpec] = Field(default_factory=list)
    managed_fields: list[str] = Field(default_factory=list)
    operations: list[str] = Field(default_factory=lambda: ["CREATE", "UPDATE", "DELETE", "READ"])

    def field(self, name: str) -> FieldSpec | None:
        for f in self.fields:
            if f.name == name:
                return f
        return None

    @property
    def writable_fields(self) -> list[str]:
        return [f.name for f in self.fields if f.name not in self.managed_fields and f.name != self.key_field]


class EnvironmentSchema(BaseModel):
    name: str
    title: str
    description: str
    entities: list[EntitySpec]
    dependencies: list[DependencySpec] = Field(default_factory=list)
    cascades: list[CascadeSpec] = Field(default_factory=list)
    tools: list[ToolSpec] = Field(default_factory=list)
    exclusive_access: bool = True

    def entity(self, name_or_alias: str | None) -> EntitySpec | None:
        if not name_or_alias:
            return None
        needle = name_or_alias.strip().lower()
        for e in self.entities:
            if needle in {e.name, e.collection, e.display_name.lower(), *[a.lower() for a in e.aliases]}:
                return e
        return None

    def entity_for_collection(self, collection: str) -> EntitySpec | None:
        for e in self.entities:
            if e.collection == collection:
                return e
        return None

    def dependency(self, collection: str) -> DependencySpec | None:
        for d in self.dependencies:
            if d.collection == collection:
                return d
        return None

    @property
    def mutating_tools(self) -> set[str]:
        return {t.name for t in self.tools if t.mutating}


class ToolResult(BaseModel):
    ok: bool
    status: Literal["success", "error"]
    status_code: int | None = None
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None

    @classmethod
    def success(cls, output: dict[str, Any] | None = None, status_code: int | None = None) -> ToolResult:
        return cls(ok=True, status="success", output=output or {}, status_code=status_code)

    @classmethod
    def failure(cls, error: str, status_code: int | None = None, output: dict[str, Any] | None = None) -> ToolResult:
        return cls(ok=False, status="error", error=error, status_code=status_code, output=output or {})


class Toolbox(ABC):
    """The agent's write-capable tool surface for one environment."""

    @property
    @abstractmethod
    def specs(self) -> list[ToolSpec]: ...

    @abstractmethod
    def call(self, name: str, args: dict[str, Any], *, inject_failure: bool = False) -> ToolResult:
        """Invoke a tool. ``inject_failure`` makes the call fail the way the
        underlying system would (used only by the fault-injection harness)."""


class StateInspector(ABC):
    """Read-only, independent observer of an environment's state."""

    source: str = "inspector"

    @abstractmethod
    def snapshot(self) -> Snapshot:
        """Return the full normalised state or raise :class:`InspectorUnavailable`."""


class Environment(ABC):
    name: str
    resource_identifier: str = ""
    supports_outage: bool = False

    def __init__(self) -> None:
        self.lock = threading.RLock()

    @property
    @abstractmethod
    def schema(self) -> EnvironmentSchema: ...

    @abstractmethod
    def ensure_initialized(self) -> None: ...

    @abstractmethod
    def reset(self) -> None:
        """Restore the deterministic synthetic seed state."""

    @abstractmethod
    def toolbox(self) -> Toolbox: ...

    @abstractmethod
    def inspector(self) -> StateInspector: ...

    @abstractmethod
    def trace_refs(self, tool_name: str | None, args: Any, output: Any) -> list[tuple[str, str]]:
        """Which (collection, key) records a logged tool call refers to.

        Used by the side-effect detector to attribute observed changes to
        logged tool invocations. Works purely on trace data.
        """

    @contextmanager
    def simulate_outage(self):
        """Make the inspector unavailable for the duration of the block."""
        raise NotImplementedError(f"{self.name} environment does not support outage simulation")
        yield  # pragma: no cover

    def label(self, collection: str, key: str) -> str:
        entity = self.schema.entity_for_collection(collection)
        if entity is None:
            return f"{collection}:{key}"
        if entity.key_type == "path":
            return f"{entity.display_name.lower()} '{key}'"
        return f"{entity.display_name.lower()} {key}"
