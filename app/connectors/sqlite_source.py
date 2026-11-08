"""A user's own SQLite database as a verification target.

* **Schema discovery** - tables, primary keys, column types, NOT NULL and
  ``CHECK (col IN (...))`` value lists are read from the database itself.
* **Agent toolbox** - structured tools only (list/describe/find/count/insert/
  update/delete). Table and column names are validated against the discovered
  schema, values are bound parameters, updates/deletes require a condition and
  are capped. The agent never sends SQL.
* **Inspector** - a separate ``mode=ro`` connection with ``PRAGMA query_only``;
  snapshots are refused (INCONCLUSIVE) above a row limit rather than truncated.
* **Backups** - :meth:`SQLiteSourceEnvironment.backup` / :meth:`restore` use the
  SQLite online-backup API so a run can be undone.
"""

from __future__ import annotations

import hashlib
import html
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.environments.base import (
    CascadeSpec,
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

ROWID = "_rowid_"
MANAGED_NAMES = re.compile(r"^(updated_at|updated_on|modified_at|modified_on|last_modified|last_updated|created_at|created_on)$", re.I)
LABEL_CANDIDATES = ("name", "full_name", "title", "username", "email", "sku", "code")
OPS_SQL = {"=": "=", "!=": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">="}


class SourceConfig(BaseModel):
    """Settings of a connected data source (stored in the ProofLoop database)."""

    id: str
    name: str
    kind: str  # "sqlite" | "folder"
    location: str
    excluded: list[str] = Field(default_factory=list, description="Tables/sub-folders hidden from the agent and the verifier")
    exclusive: bool = Field(True, description="Nothing but ProofLoop's agent writes to this source during a run")
    max_rows_changed: int = Field(200, ge=1, le=100_000, description="Upper limit for one update/delete tool call")
    max_snapshot_rows: int = Field(50_000, ge=100, le=1_000_000)


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _singular(word: str) -> str:
    w = word.lower()
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("sses", "xes", "ches", "shes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def _field_type(decl: str, name: str, enum_values: list[str]) -> str:
    if enum_values:
        return "enum"
    d = (decl or "").upper()
    n = name.lower()
    if "INT" in d:
        return "integer"
    if any(t in d for t in ("REAL", "FLOA", "DOUB", "NUMERIC", "DECIMAL")):
        return "number"
    if "email" in n:
        return "email"
    if "phone" in n or n in ("mobile", "tel", "telephone"):
        return "phone"
    return "string"


def _check_enums(create_sql: str) -> dict[str, list[str]]:
    """``CHECK (status IN ('a','b'))`` -> {"status": ["a", "b"]}."""
    out: dict[str, list[str]] = {}
    for m in re.finditer(r"CHECK\s*\(\s*\"?(\w+)\"?\s+IN\s*\(([^)]*)\)\s*\)", create_sql or "", re.I):
        values = re.findall(r"'((?:[^']|'')*)'", m.group(2))
        if values:
            out[m.group(1)] = [v.replace("''", "'") for v in values]
    return out


def _normalise(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "blob:sha256:" + hashlib.sha256(bytes(value)).hexdigest()
    return value


class TableInfo(BaseModel):
    table: str
    key: str  # primary-key column or ROWID
    columns: list[dict[str, Any]]
    trigger_target: bool = False
    foreign_keys: list[dict[str, Any]] = Field(default_factory=list)  # {column, parent, parent_column, on_delete}


def discover(path: Path, excluded: list[str]) -> tuple[list[TableInfo], list[str]]:
    """Return (tables, warnings). Raises sqlite3.Error if the file is not a database."""
    warnings: list[str] = []
    tables: list[TableInfo] = []
    uri = f"file:{path.as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as con:
        con.execute("PRAGMA query_only = ON")
        rows = con.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        trigger_sql = " ".join(r[0] or "" for r in con.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger'"))
        for name, sql in rows:
            if name in excluded:
                continue
            info = con.execute(f"PRAGMA table_info({_q(name)})").fetchall()
            pks = sorted((r for r in info if r[5]), key=lambda r: r[5])
            without_rowid = bool(re.search(r"WITHOUT\s+ROWID", sql or "", re.I))
            if len(pks) == 1:
                key = pks[0][1]
            elif not without_rowid:
                key = ROWID
            else:
                warnings.append(f"Table '{name}' has a composite primary key without rowid and is not supported yet; it was skipped.")
                continue
            enums = _check_enums(sql or "")
            cols = [{"name": r[1], "decl": r[2] or "", "notnull": bool(r[3]), "default": r[4], "pk": bool(r[5]),
                     "enum": enums.get(r[1], [])} for r in info]
            targeted = bool(re.search(rf"INSERT\s+INTO\s+\"?{re.escape(name)}\"?", trigger_sql, re.I))
            # PRAGMA foreign_key_list: id, seq, table, from, to, on_update, on_delete, match
            fks = [{"column": r[3], "parent": r[2], "parent_column": r[4], "on_delete": (r[6] or "").upper()}
                   for r in con.execute(f"PRAGMA foreign_key_list({_q(name)})").fetchall()]
            tables.append(TableInfo(table=name, key=key, columns=cols, trigger_target=targeted, foreign_keys=fks))
    return tables, warnings


def build_schema(source: SourceConfig, tables: list[TableInfo]) -> EnvironmentSchema:
    used: set[str] = set()
    entities: list[EntitySpec] = []
    for t in tables:
        single = _singular(t.table)
        name = single if single not in used else t.table.lower()
        used.add(name)
        fields = []
        if t.key == ROWID:
            fields.append(FieldSpec(name=ROWID, type="integer", description="SQLite row id"))
        for c in t.columns:
            ftype = _field_type(c["decl"], c["name"], c["enum"])
            desc = f"{c['decl'] or 'untyped'} column" + (", required" if c["notnull"] else "")
            fields.append(FieldSpec(
                name=c["name"], type=ftype, description=desc, enum_values=c["enum"],
                synonyms=[c["name"].replace("_", " ")] if "_" in c["name"] else [],
                required_on_create=c["notnull"] and c["default"] is None and not c["pk"],
            ))
        col_names = [c["name"] for c in t.columns]
        label = next((c for c in LABEL_CANDIDATES if c in col_names), t.key)
        display = name.replace("_", " ").title()
        aliases = sorted({t.table.lower(), single, t.table.lower().replace("_", " "), single.replace("_", " ")})
        int_key = t.key == ROWID or any(c["name"] == t.key and "INT" in (c["decl"] or "").upper() for c in t.columns)
        entities.append(EntitySpec(
            name=name, collection=t.table, display_name=display, aliases=aliases, key_field=t.key,
            key_type="integer" if int_key else "string",
            label_field=label, fields=fields,
            managed_fields=[c for c in col_names if MANAGED_NAMES.match(c)],
        ))
    names = {t.table for t in tables}
    keys = {t.table: t.key for t in tables}
    cascades = []
    for t in tables:
        for fk in t.foreign_keys:
            if fk["on_delete"] in ("CASCADE", "SET NULL") and fk["parent"] in names:
                cascades.append(CascadeSpec(child_collection=t.table, child_field=fk["column"],
                                            parent_collection=fk["parent"],
                                            parent_field=fk["parent_column"] or keys[fk["parent"]],
                                            action=fk["on_delete"]))
    return EnvironmentSchema(
        name=f"src_{source.id}",
        title=f"{source.name} (SQLite)",
        description=f"Connected SQLite database {Path(source.location).name} - {len(tables)} table(s).",
        entities=entities,
        cascades=cascades,
        tools=SQLITE_TOOLS,
        exclusive_access=source.exclusive,
    )


_WHERE_SCHEMA = {
    "type": "object",
    "description": "Column/value pairs a row must equal, e.g. {\"city\": \"Berlin\"}.",
    "additionalProperties": True,
}
_CONDITIONS_SCHEMA = {
    "type": "array",
    "description": "Comparisons, e.g. [{\"column\": \"total\", \"op\": \">\", \"value\": 100}]. op: = != < <= > >= contains.",
    "items": {"type": "object", "properties": {"column": {"type": "string"}, "op": {"type": "string"}, "value": {}},
              "required": ["column", "op", "value"]},
}
SQLITE_TOOLS = [
    ToolSpec(name="list_tables", description="List all tables with their columns, key column and row count.", mutating=False),
    ToolSpec(name="describe_table", description="Show a table's columns, types, required columns, allowed values and 3 sample rows.",
             mutating=False, parameters={"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]}),
    ToolSpec(name="find_rows", description="Find rows matching conditions (read-only). Returns at most 'limit' rows.",
             mutating=False, parameters={"type": "object", "properties": {
                 "table": {"type": "string"}, "where": _WHERE_SCHEMA, "conditions": _CONDITIONS_SCHEMA,
                 "limit": {"type": "integer", "description": "1-100, default 20"}}, "required": ["table"]}),
    ToolSpec(name="count_rows", description="Count rows matching conditions (read-only).", mutating=False,
             parameters={"type": "object", "properties": {"table": {"type": "string"}, "where": _WHERE_SCHEMA,
                                                          "conditions": _CONDITIONS_SCHEMA}, "required": ["table"]}),
    ToolSpec(name="insert_row", description="Insert one new row. Returns the new row's key.", mutating=True,
             parameters={"type": "object", "properties": {"table": {"type": "string"},
                                                          "values": {"type": "object", "description": "column -> value"}},
                         "required": ["table", "values"]}),
    ToolSpec(name="update_rows", description="Set column values on every row matching the conditions. A condition is required.",
             mutating=True, parameters={"type": "object", "properties": {
                 "table": {"type": "string"}, "where": _WHERE_SCHEMA, "conditions": _CONDITIONS_SCHEMA,
                 "values": {"type": "object", "description": "column -> new value"}}, "required": ["table", "values"]}),
    ToolSpec(name="delete_rows", description="Delete every row matching the conditions. A condition is required.", mutating=True,
             parameters={"type": "object", "properties": {"table": {"type": "string"}, "where": _WHERE_SCHEMA,
                                                          "conditions": _CONDITIONS_SCHEMA}, "required": ["table"]}),
]


class ToolInputError(ValueError):
    pass


class SQLiteToolbox(Toolbox):
    """The agent's write-capable access. Opens its own connection per call."""

    def __init__(self, path: Path, tables: list[TableInfo], max_rows_changed: int) -> None:
        self._path = path
        self._tables = {t.table: t for t in tables}
        self._max = max_rows_changed

    @property
    def specs(self) -> list[ToolSpec]:
        return SQLITE_TOOLS

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def _table(self, args: dict[str, Any]) -> TableInfo:
        name = args.get("table")
        if not isinstance(name, str) or name not in self._tables:
            # Be forgiving about case only; never about unknown names.
            match = next((t for t in self._tables if isinstance(name, str) and t.lower() == name.strip().lower()), None)
            if match is None:
                raise ToolInputError(f"Unknown table {name!r}. Available: {', '.join(sorted(self._tables))}")
            return self._tables[match]
        return self._tables[name]

    @staticmethod
    def _column(t: TableInfo, name: Any) -> str:
        cols = {c["name"] for c in t.columns} | ({ROWID} if t.key == ROWID else set())
        if isinstance(name, str) and name in cols:
            return name
        match = next((c for c in cols if isinstance(name, str) and c.lower() == name.strip().lower()), None)
        if match is None:
            raise ToolInputError(f"Unknown column {name!r} in table '{t.table}'. Columns: {', '.join(sorted(cols))}")
        return match

    @staticmethod
    def _scalar(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        raise ToolInputError(f"Values must be text, numbers, true/false or null (got {type(value).__name__}).")

    @staticmethod
    def _split_prefixed(value: Any) -> tuple[str, Any]:
        """``">900"`` / ``"&gt;=5"`` / ``"=pending"`` -> (op, value). Plain values mean equality."""
        if not isinstance(value, str):
            return "=", value
        text = html.unescape(value).strip()
        m = re.match(r"^(>=|<=|!=|<>|==|=|>|<)\s*(.+)$", text)
        if not m:
            return "=", value
        op, rest = m.group(1), m.group(2).strip()
        op = {"==": "=", "<>": "!="}.get(op, op)
        try:
            number: Any = float(rest)
            rest = int(number) if number.is_integer() and "." not in rest else number
        except ValueError:
            pass
        return op, rest

    def _normalise_filters(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        """Accept every shape small models produce for a filter and return a list of {column, op, value}:

        ``where={"city": "Oslo"}``, ``where={"total": ">900"}``, ``where={"total": {">": 900}}``,
        ``where={"column": "id", "op": "==", "value": 3}``, ``where=[{...}]`` and ``conditions=[{...}]``.
        """
        out: list[dict[str, Any]] = []
        where = args.get("where")
        if isinstance(where, list):
            out.extend(c for c in where if isinstance(c, dict))
        elif isinstance(where, dict):
            if {"column", "value"} <= set(where) or {"field", "value"} <= set(where):
                out.append(where)
            else:
                for col, val in where.items():
                    if isinstance(val, dict) and len(val) == 1 and next(iter(val)) not in ("column", "field"):
                        (op, v), = val.items()
                        out.append({"column": col, "op": op, "value": v})
                    elif isinstance(val, dict) and "op" in val:
                        out.append({"column": col, "op": val.get("op"), "value": val.get("value")})
                    else:
                        op, v = self._split_prefixed(val)
                        out.append({"column": col, "op": op, "value": v})
        elif where not in (None, "", {}):
            raise ToolInputError("'where' must be an object of column/value pairs, e.g. {\"city\": \"Berlin\"}.")
        conditions = args.get("conditions") or []
        if isinstance(conditions, dict):
            conditions = [conditions]
        if not isinstance(conditions, list):
            raise ToolInputError("'conditions' must be a list.")
        out.extend(c for c in conditions if isinstance(c, dict))
        return out

    def _where(self, t: TableInfo, args: dict[str, Any]) -> tuple[str, list[Any]]:
        parts: list[str] = []
        params: list[Any] = []
        for cond in self._normalise_filters(args):
            if not isinstance(cond, dict):
                raise ToolInputError("Each condition must be an object with column, op and value.")
            c = self._column(t, cond.get("column") or cond.get("field"))
            op = html.unescape(str(cond.get("op", "=") or "=")).strip().lower()
            op = {"==": "=", "<>": "!=", "like": "contains", "eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<",
                  "lte": "<="}.get(op, op)
            val = self._scalar(cond.get("value"))
            if op == "=":
                op, val = self._split_prefixed(val)
            if op == "contains":
                parts.append(f"{_q(c)} LIKE ?")
                params.append(f"%{val}%")
            elif op in OPS_SQL:
                if val is None and op in ("=", "!="):
                    parts.append(f"{_q(c)} IS {'NOT ' if op == '!=' else ''}NULL")
                else:
                    parts.append(f"{_q(c)} {OPS_SQL[op]} ?")
                    params.append(val)
            else:
                raise ToolInputError(f"Unsupported operator {op!r}. Use = != < <= > >= contains.")
        return (" WHERE " + " AND ".join(parts)) if parts else "", params

    def _key_expr(self, t: TableInfo) -> str:
        return "rowid" if t.key == ROWID else _q(t.key)

    def _values(self, t: TableInfo, values: Any) -> dict[str, Any]:
        if not isinstance(values, dict) or not values:
            raise ToolInputError("'values' must be a non-empty object of column -> value.")
        out = {}
        for col, val in values.items():
            c = self._column(t, col)
            if c == ROWID:
                raise ToolInputError("The row id cannot be set.")
            if isinstance(val, str) and re.match(r"^\s*(>=|<=|!=|==|=|>|<|&gt;|&lt;)", val):
                raise ToolInputError(f"The new value for {c!r} starts with a comparison operator ({val!r}). "
                                     "In 'values' give the plain new value; comparisons belong in 'where'.")
            out[c] = self._scalar(val)
        return out

    def _rows(self, con: sqlite3.Connection, t: TableInfo, where: str, params: list[Any], limit: int) -> list[dict[str, Any]]:
        select = f"SELECT rowid AS {ROWID}, * " if t.key == ROWID else "SELECT * "
        cur = con.execute(f"{select}FROM {_q(t.table)}{where} LIMIT ?", [*params, limit])
        return [{k: _normalise(r[k]) for k in r.keys()} for r in cur.fetchall()]

    def call(self, name: str, args: dict[str, Any], *, inject_failure: bool = False) -> ToolResult:
        if inject_failure:
            return ToolResult.failure("sqlite3.OperationalError: database is locked")
        args = args if isinstance(args, dict) else {}
        try:
            with closing(self._connect()) as con:
                if name == "list_tables":
                    out = []
                    for t in self._tables.values():
                        n = con.execute(f"SELECT COUNT(*) FROM {_q(t.table)}").fetchone()[0]
                        out.append({"table": t.table, "key": t.key, "rows": n, "columns": [c["name"] for c in t.columns]})
                    return ToolResult.success({"tables": out})
                t = self._table(args)
                if name == "describe_table":
                    sample = self._rows(con, t, "", [], 3)
                    cols = [{"name": c["name"], "type": c["decl"] or "any", "required": c["notnull"] and c["default"] is None
                             and not c["pk"], "allowed_values": c["enum"] or None} for c in t.columns]
                    return ToolResult.success({"table": t.table, "key": t.key, "columns": cols, "sample_rows": sample})
                where, params = self._where(t, args)
                if name == "find_rows":
                    limit = max(1, min(int(args.get("limit") or 20), 100))
                    rows = self._rows(con, t, where, params, limit)
                    total = con.execute(f"SELECT COUNT(*) FROM {_q(t.table)}{where}", params).fetchone()[0]
                    return ToolResult.success({"table": t.table, "rows": rows, "returned": len(rows), "total_matching": total})
                if name == "count_rows":
                    n = con.execute(f"SELECT COUNT(*) FROM {_q(t.table)}{where}", params).fetchone()[0]
                    return ToolResult.success({"table": t.table, "count": n})
                if name == "insert_row":
                    values = self._values(t, args.get("values"))
                    cols = ", ".join(_q(c) for c in values)
                    marks = ", ".join("?" for _ in values)
                    with con:
                        cur = con.execute(f"INSERT INTO {_q(t.table)} ({cols}) VALUES ({marks})", list(values.values()))
                        rowid = cur.lastrowid
                        key = rowid if t.key == ROWID else con.execute(
                            f"SELECT {_q(t.key)} FROM {_q(t.table)} WHERE rowid = ?", [rowid]).fetchone()[0]
                    return ToolResult.success({"table": t.table, "inserted": 1, "key": str(key), "keys": [str(key)]})
                if name in ("update_rows", "delete_rows"):
                    if not where:
                        raise ToolInputError(f"{name} needs at least one condition ('where' or 'conditions'); "
                                             "changing every row of a table is not allowed.")
                    with con:
                        keys = [str(r[0]) for r in con.execute(
                            f"SELECT {self._key_expr(t)} FROM {_q(t.table)}{where}", params).fetchall()]
                        if len(keys) > self._max:
                            raise ToolInputError(f"{len(keys)} rows match, above this source's limit of {self._max} rows per call. "
                                                 "Narrow the conditions.")
                        if name == "update_rows":
                            values = self._values(t, args.get("values"))
                            sets = ", ".join(f"{_q(c)} = ?" for c in values)
                            n = con.execute(f"UPDATE {_q(t.table)} SET {sets}{where}", [*values.values(), *params]).rowcount
                            return ToolResult.success({"table": t.table, "updated": n, "keys": keys[:500]})
                        n = con.execute(f"DELETE FROM {_q(t.table)}{where}", params).rowcount
                        return ToolResult.success({"table": t.table, "deleted": n, "keys": keys[:500]})
                return ToolResult.failure(f"Unknown tool {name!r}")
        except ToolInputError as exc:
            return ToolResult.failure(str(exc), status_code=400)
        except sqlite3.IntegrityError as exc:
            return ToolResult.failure(f"IntegrityError: {exc}", status_code=409)
        except sqlite3.Error as exc:
            return ToolResult.failure(f"{type(exc).__name__}: {exc}", status_code=500)
        except (TypeError, ValueError) as exc:
            return ToolResult.failure(f"Invalid arguments: {exc}", status_code=400)


class SQLiteInspector(StateInspector):
    source = "verifier:sqlite-readonly"

    def __init__(self, path: Path, tables: list[TableInfo], max_rows: int) -> None:
        self._path = path
        self._tables = tables
        self._max_rows = max_rows

    def snapshot(self) -> Snapshot:
        if not self._path.is_file():
            raise InspectorUnavailable(f"Database file not found: {self._path}")
        try:
            uri = f"file:{self._path.as_posix()}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=10)) as con:
                con.execute("PRAGMA query_only = ON")
                con.row_factory = sqlite3.Row
                total = 0
                for t in self._tables:
                    total += con.execute(f"SELECT COUNT(*) FROM {_q(t.table)}").fetchone()[0]
                if total > self._max_rows:
                    raise InspectorUnavailable(
                        f"The connected tables hold {total} rows, above the snapshot limit of {self._max_rows}. "
                        "Exclude large tables or raise the limit for this source.")
                state: Snapshot = {}
                for t in self._tables:
                    select = f"SELECT rowid AS {ROWID}, * " if t.key == ROWID else "SELECT * "
                    rows = {}
                    for r in con.execute(f"{select}FROM {_q(t.table)}"):
                        rec = {k: _normalise(r[k]) for k in r.keys()}
                        rows[str(rec[t.key])] = rec
                    state[t.table] = rows
                return state
        except sqlite3.Error as exc:
            raise InspectorUnavailable(f"Read-only inspection failed: {exc}") from exc


class SQLiteSourceEnvironment(Environment):
    supports_outage = False

    def __init__(self, config: SourceConfig) -> None:
        super().__init__()
        self.config = config
        self.path = Path(config.location)
        self.name = f"src_{config.id}"
        self.resource_identifier = str(self.path)
        self.warnings: list[str] = []
        self._tables: list[TableInfo] = []
        self._schema: EnvironmentSchema | None = None
        self.refresh()

    def refresh(self) -> None:
        """(Re-)discover the schema. Raises sqlite3.Error / FileNotFoundError on a bad source."""
        if not self.path.is_file():
            raise FileNotFoundError(f"Database file not found: {self.path}")
        excluded = list(self.config.excluded)
        tables, warnings = discover(self.path, excluded)
        # Tables written by triggers (audit logs) would show up as "side effects" of every change: exclude them
        # by default and say so, unless the user listed exclusions explicitly.
        auto = [t.table for t in tables if t.trigger_target] if not self.config.excluded else []
        if auto:
            warnings.append("Trigger-written table(s) excluded automatically: " + ", ".join(auto))
            tables = [t for t in tables if t.table not in auto]
        self._tables, self.warnings = tables, warnings
        self._schema = build_schema(self.config, tables)

    @property
    def tables(self) -> list[TableInfo]:
        return self._tables

    @property
    def schema(self) -> EnvironmentSchema:
        assert self._schema is not None
        return self._schema

    def ensure_initialized(self) -> None:
        if not self.path.is_file():
            raise FileNotFoundError(f"Database file not found: {self.path}")

    def reset(self) -> None:
        raise NotImplementedError("Connected data sources are never reset; use 'Undo run' to restore a backup.")

    def toolbox(self) -> SQLiteToolbox:
        return SQLiteToolbox(self.path, self._tables, self.config.max_rows_changed)

    def inspector(self) -> SQLiteInspector:
        return SQLiteInspector(self.path, self._tables, self.config.max_snapshot_rows)

    def trace_refs(self, tool_name: str | None, args: Any, output: Any) -> list[tuple[str, str]]:
        if tool_name not in ("insert_row", "update_rows", "delete_rows") or not isinstance(output, dict):
            return []
        table = output.get("table") or (args.get("table") if isinstance(args, dict) else None)
        keys = output.get("keys") or ([output["key"]] if output.get("key") else [])
        return [(str(table), str(k)) for k in keys] if table else []

    def label(self, collection: str, key: str) -> str:
        entity = self.schema.entity_for_collection(collection)
        return f"{(entity.display_name if entity else collection).lower()} {key}"

    # ------------------------------------------------------------- backups
    def backup(self, dest: Path) -> int:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as src, closing(sqlite3.connect(dest)) as dst:
            src.backup(dst)
        return dest.stat().st_size

    def restore(self, backup_file: Path) -> None:
        if not backup_file.is_file():
            raise FileNotFoundError(f"Backup not found: {backup_file}")
        with closing(sqlite3.connect(backup_file)) as src, closing(sqlite3.connect(self.path, timeout=10)) as dst:
            src.backup(dst)
