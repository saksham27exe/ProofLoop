"""ProofLoop SDK - verify what *your* agent actually did.

A single-file client (only dependency: ``httpx``). Copy it into any project.

    from proofloop_sdk import ProofLoop

    pl = ProofLoop("http://127.0.0.1:8000", username="alice", password="...")
    with pl.session("Give every customer in Oslo the gold tier", source="src_practice",
                    agent_name="my-langgraph-agent", framework="langgraph") as s:
        rows = s.call("find_rows", table="customers", where={"city": "Oslo"})
        s.call("update_rows", table="customers", where={"city": "Oslo"}, values={"tier": "gold"})
        s.finish("DONE: set 9 Oslo customers to gold")
    print(s.verdict, s.report["summary"])

Two ways to let ProofLoop see the agent's work:

* **ProofLoop tools** (recommended): ``s.call(...)`` / ``s.tool(name)`` / ``s.openai_tools``. Every call is
  checked against the source's write policy and recorded in the tamper-evident trace automatically.
  ``s.tool(name)`` returns a plain Python function you can register with LangChain, CrewAI, AutoGen, the
  OpenAI/Anthropic SDKs, etc.
* **Your own tools**: if the agent changes the data itself, report each change with ``s.record(...)``.
  Without any reported tool call, a change cannot be attributed to the agent and the verdict is INCONCLUSIVE.

Either way the verdict (VERIFIED / FAILED / INCONCLUSIVE) is computed by ProofLoop from its own independent
before/after snapshots - the agent's claim is recorded but never trusted.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

__all__ = ["ProofLoop", "ProofLoopError", "Session"]
__version__ = "0.1.0"


class ProofLoopError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"[{status} {code}] {message}")
        self.status, self.code, self.message = status, code, message


class ProofLoop:
    def __init__(self, base_url: str = "http://127.0.0.1:8000", *, token: str | None = None,
                 username: str | None = None, password: str | None = None, timeout: float = 300.0,
                 client: httpx.Client | None = None) -> None:
        self._http = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, trust_env=False)
        self._token = token
        if token is None and username and password:
            self._token = self._request("POST", "/auth/login", json={"username": username, "password": password})["access_token"]
        if not self._token:
            raise ValueError("Pass token=... or username=... and password=...")

    # ------------------------------------------------------------------ http
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        response = self._http.request(method, path, headers=headers, **kwargs)
        if response.is_success:
            return response.json()
        try:
            err = response.json().get("error") or response.json().get("detail") or {}
        except ValueError:
            err = {}
        if isinstance(err, list):  # FastAPI validation errors
            err = {"code": "validation_error", "message": "; ".join(str(e.get("msg")) for e in err)}
        raise ProofLoopError(response.status_code, str(err.get("code", "error")), str(err.get("message", response.text[:300])))

    # ------------------------------------------------------------- sessions
    def session(self, instruction: str, source: str, *, agent_name: str = "external-agent", framework: str = "custom",
                approve: bool = False, force: bool = False) -> Session:
        """Interpret the task, then start a verification session (before-snapshot + backup).

        ``source`` is an environment name: a connected data source (``src_<id>``) or a built-in sandbox
        (``database``, ``api``, ``file``). ``approve`` is required for destructive tasks (deletions, bulk changes).
        """
        detail = self._request("POST", "/executions", json={"instruction": instruction, "environment": source,
                                                            "agent_config": "external"})
        started = self._request("POST", f"/executions/{detail['id']}/session/start",
                                json={"approved": approve, "force": force, "agent_name": agent_name, "framework": framework})
        return Session(self, detail, started)


class Session:
    """One verified run of your agent. Use as a context manager so it always finishes."""

    def __init__(self, client: ProofLoop, detail: dict[str, Any], started: dict[str, Any]) -> None:
        self._c = client
        self.execution_id: str = detail["id"]
        self.plan: dict[str, Any] | None = detail.get("plan")
        self.instruction: str = started["instruction"]
        self.openai_tools: list[dict[str, Any]] = started["tools"]
        self.policy: dict[str, Any] = started.get("policy") or {}
        self.report: dict[str, Any] | None = None
        self._open = True

    # ---------------------------------------------------------------- tools
    @property
    def tool_names(self) -> list[str]:
        return [t["function"]["name"] for t in self.openai_tools]

    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        """Run one of ProofLoop's tools for the agent. Returns {"ok", "output", "error", ...}; never raises on a
        failed tool call (the agent should see the error, as it would from any tool)."""
        return self._c._request("POST", f"/executions/{self.execution_id}/session/tools/{tool}", json={"arguments": arguments})

    def tool(self, name: str) -> Callable[..., dict[str, Any]]:
        """A plain function for agent frameworks: ``fn(**arguments) -> result dict``."""
        spec = next((t["function"] for t in self.openai_tools if t["function"]["name"] == name), None)
        if spec is None:
            raise KeyError(f"Unknown tool {name!r}; available: {self.tool_names}")

        def _fn(**arguments: Any) -> dict[str, Any]:
            return self.call(name, **arguments)

        _fn.__name__ = name
        _fn.__doc__ = spec.get("description", "")
        return _fn

    def tools(self) -> dict[str, Callable[..., dict[str, Any]]]:
        return {name: self.tool(name) for name in self.tool_names}

    def record(self, tool: str, arguments: dict[str, Any] | None = None, output: Any = None, *,
               status: str = "success", message: str = "") -> None:
        """Report a tool call the agent made with its own tools (outside ProofLoop).

        Use ProofLoop's tool names (``update_rows``, ``insert_row``, ``delete_rows``, ``write_file``, ...) and include
        the records you touched in ``output`` - e.g. ``{"table": "customers", "keys": [5]}`` or ``{"path": "a.txt"}`` -
        so each change can be attributed to this call. The report is agent-supplied evidence: it can explain a
        change, but the verdict still depends on what ProofLoop observes in the data.
        """
        self._c._request("POST", f"/executions/{self.execution_id}/session/events",
                         json={"events": [{"kind": "tool", "tool": tool, "arguments": arguments or {}, "output": output,
                                           "status": status, "message": message}]})

    def note(self, message: str) -> None:
        self._c._request("POST", f"/executions/{self.execution_id}/session/events",
                         json={"events": [{"kind": "note", "message": message}]})

    # --------------------------------------------------------------- finish
    def finish(self, claim: str, status: str = "success") -> dict[str, Any]:
        """Record the agent's final report and verify. Returns the immutable report."""
        if not self._open:
            raise RuntimeError("This session is already finished.")
        self.report = self._c._request("POST", f"/executions/{self.execution_id}/session/finish",
                                       json={"claim": claim, "claimed_status": status, "verify": True})
        self._open = False
        return self.report

    def abort(self, reason: str = "") -> None:
        if self._open:
            self._c._request("POST", f"/executions/{self.execution_id}/session/abort", params={"reason": reason})
            self._open = False

    @property
    def verdict(self) -> str | None:
        if not self.report:
            return None
        v = self.report.get("verdict")
        return v.get("value") if isinstance(v, dict) else v

    def __enter__(self) -> Session:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._open:
            return
        # Still verify when the agent crashed: what it changed before crashing matters most.
        if exc is not None:
            self.finish(f"FAILED: the agent raised {exc_type.__name__}: {exc}", "failure")
        else:
            self.finish("(the agent finished without a final report)", "success")
