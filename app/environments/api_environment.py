"""Controlled local REST API: a synthetic IT ticketing service.

The service is a tiny standards-compliant WSGI application backed by its own
SQLite file. Both the agent and the verifier talk to it over real HTTP
semantics (status codes, JSON bodies, auth headers) through
``httpx.WSGITransport`` - in-process, so no port is opened and nothing leaves
the machine. It can also be served standalone for manual exploration::

    python -m app.environments.api_environment --port 8100

Least privilege: the service issues two bearer tokens. The agent token may
read and write; the verifier token is *read-only* (non-GET requests -> 403).
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import httpx

from app.environments.base import (
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

PRIORITIES = ["low", "medium", "high", "critical"]
STATUSES = ["open", "in_progress", "resolved", "closed"]
SEED_TIMESTAMP = "2026-01-01T00:00:00.000Z"
BASE_URL = "http://ticket-service.sandbox"

SCHEMA = EnvironmentSchema(
    name="api",
    title="Ticket service (local REST API)",
    description="Synthetic IT help-desk API with tickets. Accessed over HTTP semantics, in-process.",
    entities=[
        EntitySpec(
            name="ticket",
            collection="tickets",
            display_name="Ticket",
            aliases=["tickets", "issue", "issues", "incident", "request", "support ticket", "help desk ticket"],
            key_field="id",
            key_type="integer",
            label_field="title",
            fields=[
                FieldSpec(name="id", type="integer", description="Ticket ID"),
                FieldSpec(name="title", type="string", description="Short summary", required_on_create=True,
                          synonyms=["title", "titled", "subject", "summary", "called", "named"]),
                FieldSpec(name="description", type="string", description="Longer description",
                          synonyms=["description", "details", "body"]),
                FieldSpec(name="priority", type="enum", enum_values=PRIORITIES, description="Ticket priority",
                          synonyms=["priority", "severity", "urgency"]),
                FieldSpec(name="status", type="enum", enum_values=STATUSES, description="Workflow status",
                          synonyms=["status", "state"]),
                FieldSpec(name="assignee", type="string", description="Person the ticket is assigned to",
                          synonyms=["assignee", "owner", "assigned to", "assigned"]),
                FieldSpec(name="created_at", type="string", description="Creation time (system managed)"),
                FieldSpec(name="updated_at", type="string", description="Last modification time (system managed)"),
            ],
            managed_fields=["id", "created_at", "updated_at"],
        )
    ],
    tools=[
        ToolSpec(name="api_list_tickets", description="GET /tickets", mutating=False),
        ToolSpec(name="api_get_ticket", description="GET /tickets/{id}", mutating=False),
        ToolSpec(name="api_create_ticket", description="POST /tickets", mutating=True),
        ToolSpec(name="api_update_ticket", description="PATCH /tickets/{id}", mutating=True),
        ToolSpec(name="api_delete_ticket", description="DELETE /tickets/{id}", mutating=True),
    ],
)

_WRITABLE = {"title", "description", "priority", "status", "assignee"}
_TICKET_PATH = re.compile(r"^/tickets/(\d+)$")
_REASONS = {200: "OK", 201: "Created", 204: "No Content", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
            404: "Not Found", 405: "Method Not Allowed", 422: "Unprocessable Entity", 503: "Service Unavailable"}


class TicketService:
    """WSGI application implementing the ticket REST API."""

    def __init__(self, db_path: Path, agent_token: str, verifier_token: str) -> None:
        self.db_path = db_path
        self.tokens = {agent_token: "agent", verifier_token: "verifier"}

    # -- storage helpers -------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def maintenance(self) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT value FROM service_state WHERE key='maintenance'").fetchone()
        return bool(row and row[0] == "1")

    def set_maintenance(self, on: bool) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO service_state(key, value) VALUES('maintenance', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("1" if on else "0",),
            )
            conn.commit()

    # -- WSGI ------------------------------------------------------------
    def __call__(self, environ: dict[str, Any], start_response: Callable) -> Iterable[bytes]:
        status, body = self._dispatch(environ)
        payload = b"" if body is None else json.dumps(body).encode("utf-8")
        headers = [("Content-Type", "application/json"), ("Content-Length", str(len(payload)))]
        start_response(f"{status} {_REASONS.get(status, 'OK')}", headers)
        return [payload]

    def _dispatch(self, environ: dict[str, Any]) -> tuple[int, Any]:
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/").rstrip("/") or "/"
        auth = environ.get("HTTP_AUTHORIZATION", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        role = self.tokens.get(token)
        if role is None:
            return 401, {"error": "missing or invalid bearer token"}
        try:
            if self.maintenance():
                return 503, {"error": "ticket service is in maintenance mode"}
        except sqlite3.Error:
            return 503, {"error": "ticket service storage unavailable"}
        if environ.get("HTTP_X_SANDBOX_FAULT", "").lower() == "unavailable":
            # Controlled fault: the upstream fails before touching any state.
            return 503, {"error": "upstream dependency timeout"}
        if role == "verifier" and method != "GET":
            return 403, {"error": "verifier token is read-only"}
        try:
            body = self._read_json(environ) if method in ("POST", "PATCH", "PUT") else None
        except ValueError as exc:
            return 400, {"error": str(exc)}

        try:
            if path == "/health" and method == "GET":
                return 200, {"status": "ok"}
            if path == "/tickets":
                if method == "GET":
                    return 200, {"tickets": self._list()}
                if method == "POST":
                    return self._create(body)
                return 405, {"error": "method not allowed"}
            match = _TICKET_PATH.match(path)
            if match:
                ticket_id = int(match.group(1))
                if method == "GET":
                    ticket = self._get(ticket_id)
                    return (200, ticket) if ticket else (404, {"error": f"ticket {ticket_id} not found"})
                if method == "PATCH":
                    return self._update(ticket_id, body)
                if method == "DELETE":
                    return self._delete(ticket_id)
                return 405, {"error": "method not allowed"}
            return 404, {"error": "not found"}
        except sqlite3.Error as exc:
            return 503, {"error": f"storage error: {exc}"}

    @staticmethod
    def _read_json(environ: dict[str, Any]) -> dict[str, Any]:
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        raw = environ["wsgi.input"].read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc.msg}") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    @staticmethod
    def _validate(data: dict[str, Any], creating: bool) -> str | None:
        unknown = set(data) - _WRITABLE
        if unknown:
            return f"unknown or read-only field(s): {', '.join(sorted(unknown))}"
        if creating and not str(data.get("title") or "").strip():
            return "title is required"
        if "priority" in data and data["priority"] not in PRIORITIES:
            return f"priority must be one of {PRIORITIES}"
        if "status" in data and data["status"] not in STATUSES:
            return f"status must be one of {STATUSES}"
        if not creating and not data:
            return "no fields to update"
        return None

    def _list(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM tickets ORDER BY id")]

    def _get(self, ticket_id: int) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return dict(row) if row else None

    def _create(self, data: dict[str, Any]) -> tuple[int, Any]:
        problem = self._validate(data, creating=True)
        if problem:
            return 422, {"error": problem}
        now = utc_iso_now()
        record = {"title": data["title"], "description": data.get("description"), "priority": data.get("priority", "medium"),
                  "status": data.get("status", "open"), "assignee": data.get("assignee"), "created_at": now, "updated_at": now}
        with closing(self._connect()) as conn:
            cur = conn.execute(
                "INSERT INTO tickets (title, description, priority, status, assignee, created_at, updated_at) "
                "VALUES (:title, :description, :priority, :status, :assignee, :created_at, :updated_at)",
                record,
            )
            conn.commit()
            new_id = cur.lastrowid
        return 201, self._get(new_id)

    def _update(self, ticket_id: int, data: dict[str, Any]) -> tuple[int, Any]:
        problem = self._validate(data, creating=False)
        if problem:
            return 422, {"error": problem}
        if self._get(ticket_id) is None:
            return 404, {"error": f"ticket {ticket_id} not found"}
        cols = sorted(data)
        with closing(self._connect()) as conn:
            conn.execute(
                f"UPDATE tickets SET {', '.join(f'{c} = ?' for c in cols)}, updated_at = ? WHERE id = ?",
                [*[data[c] for c in cols], utc_iso_now(), ticket_id],
            )
            conn.commit()
        return 200, self._get(ticket_id)

    def _delete(self, ticket_id: int) -> tuple[int, Any]:
        with closing(self._connect()) as conn:
            cur = conn.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
            conn.commit()
        if cur.rowcount == 0:
            return 404, {"error": f"ticket {ticket_id} not found"}
        return 204, None


def _client(service: TicketService, token: str, timeout: float = 10.0) -> httpx.Client:
    return httpx.Client(
        transport=httpx.WSGITransport(app=service),
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
        trust_env=False,
    )


class ApiToolbox(Toolbox):
    def __init__(self, service: TicketService, agent_token: str) -> None:
        self._service = service
        self._token = agent_token

    @property
    def specs(self) -> list[ToolSpec]:
        return SCHEMA.tools

    def call(self, name: str, args: dict[str, Any], *, inject_failure: bool = False) -> ToolResult:
        headers = {"X-Sandbox-Fault": "unavailable"} if inject_failure else {}
        try:
            if name == "api_list_tickets":
                method, path, body = "GET", "/tickets", None
            elif name == "api_get_ticket":
                method, path, body = "GET", f"/tickets/{int(args['ticket_id'])}", None
            elif name == "api_create_ticket":
                method, path, body = "POST", "/tickets", dict(args.get("fields") or {})
            elif name == "api_update_ticket":
                method, path, body = "PATCH", f"/tickets/{int(args['ticket_id'])}", dict(args.get("fields") or {})
            elif name == "api_delete_ticket":
                method, path, body = "DELETE", f"/tickets/{int(args['ticket_id'])}", None
            else:
                return ToolResult.failure(f"Unknown tool {name!r}")
        except (KeyError, TypeError, ValueError) as exc:
            return ToolResult.failure(f"Invalid arguments: {exc}")
        with _client(self._service, self._token) as client:
            response = client.request(method, path, json=body, headers=headers)
        payload: Any = None
        if response.content:
            try:
                payload = response.json()
            except ValueError:
                payload = {"raw": response.text[:500]}
        output = {"method": method, "path": path, "status_code": response.status_code, "body": payload}
        if response.is_success:
            return ToolResult.success(output, status_code=response.status_code)
        error = payload.get("error") if isinstance(payload, dict) else None
        return ToolResult.failure(f"HTTP {response.status_code}: {error or response.reason_phrase}",
                                  status_code=response.status_code, output=output)


class ApiInspector(StateInspector):
    """Independent read path using the service's read-only verifier token."""

    source = "verifier:http-readonly"

    def __init__(self, service: TicketService, verifier_token: str) -> None:
        self._service = service
        self._token = verifier_token

    def snapshot(self) -> Snapshot:
        try:
            with _client(self._service, self._token) as client:
                response = client.get("/tickets")
        except httpx.HTTPError as exc:
            raise InspectorUnavailable(f"Ticket service unreachable: {exc}") from exc
        if response.status_code != 200:
            raise InspectorUnavailable(f"Ticket service returned HTTP {response.status_code} to the verifier's read request")
        tickets = response.json().get("tickets", [])
        return {"tickets": {str(t["id"]): t for t in tickets}}


class ApiEnvironment(Environment):
    name = "api"
    supports_outage = True

    def __init__(self, sandbox_dir: Path, sample_data_dir: Path) -> None:
        super().__init__()
        self.path = sandbox_dir / "ticket_service.db"
        self.sample_data_dir = sample_data_dir
        self.resource_identifier = "sandbox/ticket-service/tickets"
        self._agent_token = secrets.token_urlsafe(24)
        self._verifier_token = secrets.token_urlsafe(24)
        self.service = TicketService(self.path, self._agent_token, self._verifier_token)

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
            if not {"tickets", "service_state"} <= names:
                self.reset()
            else:
                self.service.set_maintenance(False)
        except sqlite3.Error:
            self.reset()

    def reset(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tickets = json.loads((self.sample_data_dir / "synthetic_tickets.json").read_text(encoding="utf-8"))
            with closing(sqlite3.connect(self.path, timeout=10)) as conn:
                conn.executescript(
                    "DROP TABLE IF EXISTS tickets; DROP TABLE IF EXISTS service_state;"
                    "CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT,"
                    " priority TEXT NOT NULL, status TEXT NOT NULL, assignee TEXT, created_at TEXT NOT NULL,"
                    " updated_at TEXT NOT NULL);"
                    "CREATE TABLE service_state (key TEXT PRIMARY KEY, value TEXT);"
                )
                conn.execute("DELETE FROM sqlite_sequence")
                conn.executemany(
                    "INSERT INTO tickets (id, title, description, priority, status, assignee, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    [(t["id"], t["title"], t["description"], t["priority"], t["status"], t["assignee"],
                      SEED_TIMESTAMP, SEED_TIMESTAMP) for t in tickets],
                )
                conn.execute("INSERT INTO service_state(key, value) VALUES ('maintenance', '0')")
                conn.commit()

    def toolbox(self) -> ApiToolbox:
        return ApiToolbox(self.service, self._agent_token)

    def inspector(self) -> ApiInspector:
        return ApiInspector(self.service, self._verifier_token)

    @contextmanager
    def simulate_outage(self):
        self.service.set_maintenance(True)
        try:
            yield
        finally:
            self.service.set_maintenance(False)

    def trace_refs(self, tool_name: str | None, args: Any, output: Any) -> list[tuple[str, str]]:
        refs: list[tuple[str, str]] = []
        if isinstance(args, dict) and args.get("ticket_id") is not None and tool_name in (
            "api_get_ticket", "api_update_ticket", "api_delete_ticket"
        ):
            refs.append(("tickets", str(args["ticket_id"])))
        if tool_name == "api_create_ticket" and isinstance(output, dict):
            body = output.get("body")
            if isinstance(body, dict) and body.get("id") is not None:
                refs.append(("tickets", str(body["id"])))
        return refs


def _serve() -> None:  # pragma: no cover - manual exploration helper
    import argparse
    from wsgiref.simple_server import make_server

    from app.config import get_settings

    parser = argparse.ArgumentParser(description="Serve the sandbox ticket API on localhost")
    parser.add_argument("--port", type=int, default=8100)
    ns = parser.parse_args()
    settings = get_settings()
    env = ApiEnvironment(settings.sandbox_dir, settings.sample_data_dir)
    env.ensure_initialized()
    print(f"Ticket service on http://127.0.0.1:{ns.port}")
    print(f"  agent token (read/write): {env._agent_token}")
    print(f"  verifier token (read-only): {env._verifier_token}")
    with make_server("127.0.0.1", ns.port, env.service) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":  # pragma: no cover
    _serve()
