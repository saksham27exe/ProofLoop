"""SQLite sandbox: a synthetic company database (employees, customers, audit log).

* The agent's toolbox opens a normal read/write connection, but only to
  whitelisted tables/columns (table and column names never come from free
  text; values are always bound parameters).
* The verifier's inspector opens a separate ``mode=ro`` URI connection: the
  SQLite engine itself refuses any write through it.
* Triggers append to ``audit_log`` on every employee/customer change. These
  rows are *legitimate dependencies* of a change, not side effects.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from app.environments.base import (
    DependencySpec,
    EntitySpec,
    Environment,
    EnvironmentSchema,
    FieldSpec,
    InspectorUnavailable,
    Snapshot,
    StateInspector,
    Toolbox,
    ToolResult,
    ToolSpec,
)
from app.utils.timeutil import utc_iso_now

SEED_TIMESTAMP = "2026-01-01T00:00:00.000Z"

_SCHEMA_SQL = """
CREATE TABLE employees (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT,
    department TEXT,
    title TEXT,
    phone TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    address TEXT,
    tier TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    record_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    changed_at TEXT NOT NULL
);
"""

_TRIGGERS_SQL = "\n".join(
    f"""
CREATE TRIGGER trg_{table}_{op.lower()} AFTER {op} ON {table}
BEGIN
    INSERT INTO audit_log (table_name, record_id, action, changed_at)
    VALUES ('{table}', {ref}.id, '{op}', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
END;"""
    for table in ("employees", "customers")
    for op, ref in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD"))
)

_DEPARTMENTS = ["Engineering", "Sales", "Marketing", "Finance", "Human Resources", "Operations", "Support", "Legal"]

SCHEMA = EnvironmentSchema(
    name="database",
    title="Company database (SQLite)",
    description="Synthetic HR/CRM database with employees and customers. Changes are audited by triggers.",
    entities=[
        EntitySpec(
            name="employee",
            collection="employees",
            display_name="Employee",
            aliases=["employees", "staff", "staff member", "worker", "team member", "emp"],
            key_field="id",
            key_type="integer",
            label_field="name",
            fields=[
                FieldSpec(name="id", type="integer", description="Employee ID"),
                FieldSpec(name="name", type="string", description="Full name", required_on_create=True,
                          synonyms=["name", "full name", "named", "called"]),
                FieldSpec(name="email", type="email", description="E-mail address",
                          synonyms=["email", "email address", "e-mail", "e-mail address", "mail", "work email"]),
                FieldSpec(name="department", type="string", description="Department name (e.g. " + ", ".join(_DEPARTMENTS) + ")",
                          synonyms=["department", "dept", "team", "division"]),
                FieldSpec(name="title", type="string", description="Job title",
                          synonyms=["title", "job title", "role", "position", "designation"]),
                FieldSpec(name="phone", type="phone", description="Phone number",
                          synonyms=["phone", "phone number", "telephone", "mobile", "mobile number", "contact number"]),
                FieldSpec(name="updated_at", type="string", description="Last modification time (system managed)"),
            ],
            managed_fields=["id", "updated_at"],
        ),
        EntitySpec(
            name="customer",
            collection="customers",
            display_name="Customer",
            aliases=["customers", "client", "clients", "account"],
            key_field="id",
            key_type="integer",
            label_field="name",
            fields=[
                FieldSpec(name="id", type="integer", description="Customer ID"),
                FieldSpec(name="name", type="string", description="Customer (company) name", required_on_create=True,
                          synonyms=["name", "company name", "named", "called"]),
                FieldSpec(name="email", type="email", description="Contact e-mail",
                          synonyms=["email", "email address", "e-mail", "contact email"]),
                FieldSpec(name="phone", type="phone", description="Contact phone number",
                          synonyms=["phone", "phone number", "telephone", "contact number", "mobile"]),
                FieldSpec(name="address", type="string", description="Postal address",
                          synonyms=["address", "postal address", "mailing address", "street address"]),
                FieldSpec(name="tier", type="enum", description="Customer tier", enum_values=["standard", "gold", "platinum"],
                          synonyms=["tier", "plan", "level", "membership"]),
                FieldSpec(name="updated_at", type="string", description="Last modification time (system managed)"),
            ],
            managed_fields=["id", "updated_at"],
        ),
    ],
    dependencies=[
        DependencySpec(
            collection="audit_log",
            parent_collection_field="table_name",
            parent_key_field="record_id",
            description="Rows appended by database triggers for every employee/customer change.",
        )
    ],
    tools=[
        ToolSpec(name="db_find", description="Find records in a table matching field values", mutating=False),
        ToolSpec(name="db_get", description="Read one record by id", mutating=False),
        ToolSpec(name="db_list_ids", description="List record ids of a table", mutating=False),
        ToolSpec(name="db_insert", description="Insert a record", mutating=True),
        ToolSpec(name="db_update", description="Update fields of one record by id", mutating=True),
        ToolSpec(name="db_delete", description="Delete one record by id", mutating=True),
    ],
)

_TABLES = {e.collection: e for e in SCHEMA.entities}
_SNAPSHOT_TABLES = {"employees": "id", "customers": "id", "audit_log": "id"}


def _row_to_record(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


class DatabaseToolbox(Toolbox):
    """Agent-side write access (separate connection per call)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def specs(self) -> list[ToolSpec]:
        return SCHEMA.tools

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _entity(table: Any) -> EntitySpec:
        entity = _TABLES.get(str(table))
        if entity is None:
            raise ValueError(f"Unknown table {table!r}")
        return entity

    @staticmethod
    def _clean_values(entity: EntitySpec, values: Any) -> dict[str, Any]:
        if not isinstance(values, dict) or not values:
            raise ValueError("values must be a non-empty object")
        allowed = set(entity.writable_fields)
        unknown = [k for k in values if k not in allowed]
        if unknown:
            raise ValueError(f"Unknown or read-only column(s): {', '.join(unknown)}")
        return dict(values)

    def call(self, name: str, args: dict[str, Any], *, inject_failure: bool = False) -> ToolResult:
        if inject_failure:
            # Fail exactly the way SQLite does under write contention - before
            # anything is committed, so state is untouched.
            return ToolResult.failure("sqlite3.OperationalError: database is locked")
        try:
            handler = {
                "db_find": self._find,
                "db_get": self._get,
                "db_list_ids": self._list_ids,
                "db_insert": self._insert,
                "db_update": self._update,
                "db_delete": self._delete,
            }[name]
        except KeyError:
            return ToolResult.failure(f"Unknown tool {name!r}")
        try:
            return handler(args)
        except (ValueError, TypeError) as exc:
            return ToolResult.failure(str(exc))
        except sqlite3.Error as exc:
            return ToolResult.failure(f"sqlite3.{type(exc).__name__}: {exc}")

    def _find(self, args: dict[str, Any]) -> ToolResult:
        entity = self._entity(args.get("table"))
        where = args.get("where") or {}
        if not isinstance(where, dict) or not where:
            raise ValueError("where must be a non-empty object")
        cols = [k for k in where if entity.field(k)]
        if len(cols) != len(where):
            raise ValueError("Unknown column in where clause")
        clause = " AND ".join(f"{c} = ?" for c in cols)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM {entity.collection} WHERE {clause} ORDER BY id", [where[c] for c in cols]
            ).fetchall()
        return ToolResult.success({"records": [_row_to_record(r) for r in rows], "count": len(rows)})

    def _get(self, args: dict[str, Any]) -> ToolResult:
        entity = self._entity(args.get("table"))
        record_id = int(args.get("record_id"))
        with closing(self._connect()) as conn:
            row = conn.execute(f"SELECT * FROM {entity.collection} WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            return ToolResult.failure(f"No {entity.name} with id {record_id}")
        return ToolResult.success({"record": _row_to_record(row)})

    def _list_ids(self, args: dict[str, Any]) -> ToolResult:
        entity = self._entity(args.get("table"))
        with closing(self._connect()) as conn:
            ids = [r[0] for r in conn.execute(f"SELECT id FROM {entity.collection} ORDER BY id")]
        return ToolResult.success({"ids": ids})

    def _insert(self, args: dict[str, Any]) -> ToolResult:
        entity = self._entity(args.get("table"))
        values = self._clean_values(entity, args.get("values"))
        values["updated_at"] = utc_iso_now()
        cols = list(values)
        conn = self._connect()
        try:
            cur = conn.execute(
                f"INSERT INTO {entity.collection} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                [values[c] for c in cols],
            )
            conn.commit()
            return ToolResult.success({"record_id": cur.lastrowid, "rows_affected": cur.rowcount})
        finally:
            conn.close()

    def _update(self, args: dict[str, Any]) -> ToolResult:
        entity = self._entity(args.get("table"))
        record_id = int(args.get("record_id"))
        values = self._clean_values(entity, args.get("values"))
        values["updated_at"] = utc_iso_now()
        cols = list(values)
        conn = self._connect()
        try:
            cur = conn.execute(
                f"UPDATE {entity.collection} SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?",
                [*[values[c] for c in cols], record_id],
            )
            conn.commit()
            if cur.rowcount == 0:
                return ToolResult.failure(f"No {entity.name} with id {record_id}", output={"rows_affected": 0})
            return ToolResult.success({"record_id": record_id, "rows_affected": cur.rowcount})
        finally:
            conn.close()

    def _delete(self, args: dict[str, Any]) -> ToolResult:
        entity = self._entity(args.get("table"))
        record_id = int(args.get("record_id"))
        conn = self._connect()
        try:
            cur = conn.execute(f"DELETE FROM {entity.collection} WHERE id = ?", (record_id,))
            conn.commit()
            if cur.rowcount == 0:
                return ToolResult.failure(f"No {entity.name} with id {record_id}", output={"rows_affected": 0})
            return ToolResult.success({"record_id": record_id, "rows_affected": cur.rowcount})
        finally:
            conn.close()


class DatabaseInspector(StateInspector):
    """Read-only observer: opens the database with SQLite's ``mode=ro`` URI."""

    source = "verifier:sqlite-readonly"

    def __init__(self, path: Path) -> None:
        self._path = path

    def connect(self) -> sqlite3.Connection:
        if not self._path.exists():
            raise InspectorUnavailable(f"Database file not found: {self._path.name}")
        uri = self._path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def snapshot(self) -> Snapshot:
        try:
            conn = self.connect()
            try:
                state: Snapshot = {}
                for table, key in _SNAPSHOT_TABLES.items():
                    rows = conn.execute(f"SELECT * FROM {table} ORDER BY {key}").fetchall()
                    state[table] = {str(r[key]): _row_to_record(r) for r in rows}
                return state
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise InspectorUnavailable(f"Read-only database inspection failed: {exc}") from exc


class DatabaseEnvironment(Environment):
    name = "database"

    def __init__(self, sandbox_dir: Path, sample_data_dir: Path) -> None:
        super().__init__()
        self.path = sandbox_dir / "company.db"
        self.sample_data_dir = sample_data_dir
        self.resource_identifier = "sandbox/company.db"

    @property
    def schema(self) -> EnvironmentSchema:
        return SCHEMA

    def ensure_initialized(self) -> None:
        if not self.path.exists():
            self.reset()
            return
        try:
            with closing(sqlite3.connect(self.path)) as conn:
                names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"employees", "customers", "audit_log"} <= names:
                self.reset()
        except sqlite3.Error:
            self.reset()

    def reset(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            employees = json.loads((self.sample_data_dir / "synthetic_employees.json").read_text(encoding="utf-8"))
            customers = json.loads((self.sample_data_dir / "synthetic_customers.json").read_text(encoding="utf-8"))
            conn = sqlite3.connect(self.path, timeout=10)
            try:
                conn.executescript(
                    "DROP TABLE IF EXISTS employees; DROP TABLE IF EXISTS customers; DROP TABLE IF EXISTS audit_log;"
                )
                conn.executescript(_SCHEMA_SQL)
                conn.execute("DELETE FROM sqlite_sequence")
                conn.executemany(
                    "INSERT INTO employees (id, name, email, department, title, phone, updated_at) VALUES (?,?,?,?,?,?,?)",
                    [(e["id"], e["name"], e["email"], e["department"], e["title"], e["phone"], SEED_TIMESTAMP) for e in employees],
                )
                conn.executemany(
                    "INSERT INTO customers (id, name, email, phone, address, tier, updated_at) VALUES (?,?,?,?,?,?,?)",
                    [(c["id"], c["name"], c["email"], c["phone"], c["address"], c["tier"], SEED_TIMESTAMP) for c in customers],
                )
                conn.executescript(_TRIGGERS_SQL)
                conn.commit()
            finally:
                conn.close()

    def toolbox(self) -> DatabaseToolbox:
        return DatabaseToolbox(self.path)

    def inspector(self) -> DatabaseInspector:
        return DatabaseInspector(self.path)

    def trace_refs(self, tool_name: str | None, args: Any, output: Any) -> list[tuple[str, str]]:
        if not isinstance(args, dict):
            return []
        table = args.get("table")
        if table not in _TABLES:
            return []
        refs: list[tuple[str, str]] = []
        if tool_name in ("db_update", "db_delete", "db_get") and args.get("record_id") is not None:
            refs.append((table, str(args["record_id"])))
        if tool_name == "db_insert" and isinstance(output, dict) and output.get("record_id") is not None:
            refs.append((table, str(output["record_id"])))
        return refs
