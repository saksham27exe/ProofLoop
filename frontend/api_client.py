"""Thin HTTP client for the ProofLoop backend (localhost only)."""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_API_URL = os.environ.get("PROOFLOOP_API_URL", "http://127.0.0.1:8000")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class ApiClient:
    def __init__(self, base_url: str = DEFAULT_API_URL, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _request(self, method: str, path: str, *, timeout: float = 60.0, raw: bool = False, **kwargs: Any) -> Any:
        headers = kwargs.pop("headers", {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            with httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(timeout, connect=3.0), trust_env=False) as c:
                response = c.request(method, path, headers=headers, **kwargs)
        except httpx.ConnectError:
            raise ApiError(0, "backend_unreachable",
                           f"Cannot reach the ProofLoop backend at {self.base_url}. Start it with .\\run.ps1 "
                           "(or: uvicorn app.main:app --host 127.0.0.1 --port 8000).") from None
        except httpx.TimeoutException:
            raise ApiError(0, "timeout", "The backend took too long to respond. The model may still be loading or busy - try again.") from None
        if response.is_success:
            if raw:
                return response
            return response.json() if response.content else None
        code, message = "http_error", response.reason_phrase
        try:
            err = response.json().get("error", {})
            code, message = err.get("code", code), err.get("message", message)
        except ValueError:
            pass
        raise ApiError(response.status_code, code, message)

    # -- public / auth --------------------------------------------------------
    def health(self) -> dict:
        return self._request("GET", "/health", timeout=5)

    def bootstrap_status(self) -> dict:
        return self._request("GET", "/auth/bootstrap-status", timeout=5)

    def bootstrap(self, username: str, password: str, full_name: str) -> dict:
        return self._request("POST", "/auth/bootstrap", json={"username": username, "password": password, "full_name": full_name})

    def login(self, username: str, password: str) -> dict:
        return self._request("POST", "/auth/login", json={"username": username, "password": password})

    def logout(self) -> None:
        self._request("POST", "/auth/logout")

    def me(self) -> dict:
        return self._request("GET", "/auth/me")

    def change_password(self, current: str, new: str) -> dict:
        return self._request("POST", "/auth/change-password", json={"current_password": current, "new_password": new})

    # -- users -----------------------------------------------------------------
    def users(self) -> list[dict]:
        return self._request("GET", "/users")

    def create_user(self, **body: Any) -> dict:
        return self._request("POST", "/users", json=body)

    def update_user(self, user_id: int, **body: Any) -> dict:
        return self._request("PATCH", f"/users/{user_id}", json=body)

    def reset_password(self, user_id: int, new_password: str) -> dict:
        return self._request("POST", f"/users/{user_id}/reset-password", json={"new_password": new_password})

    def audit(self, limit: int = 200) -> list[dict]:
        return self._request("GET", "/audit", params={"limit": limit})

    # -- platform ----------------------------------------------------------------
    def llm_health(self) -> dict:
        return self._request("GET", "/health/llm", timeout=15)

    def models(self) -> dict:
        return self._request("GET", "/models", timeout=15)

    def source_policy(self, source_id: str) -> dict:
        return self._request("GET", f"/sources/{source_id}/policy")

    def update_source_policy(self, source_id: str, body: dict) -> dict:
        return self._request("PUT", f"/sources/{source_id}/policy", json=body)

    def cloud_models(self) -> dict:
        return self._request("GET", "/models/cloud", timeout=25)

    def stats(self) -> dict:
        return self._request("GET", "/stats")

    def scenarios(self) -> list[dict]:
        return self._request("GET", "/scenarios")

    def options(self) -> dict:
        return self._request("GET", "/options")

    def settings(self) -> dict:
        return self._request("GET", "/settings")

    def update_settings(self, **body: Any) -> dict:
        return self._request("PUT", "/settings", json=body)

    def sandbox(self, env: str) -> dict:
        return self._request("GET", f"/sandbox/{env}")

    def reset_sandbox(self, env: str) -> dict:
        return self._request("POST", f"/sandbox/{env}/reset")

    # -- executions ------------------------------------------------------------------
    def create_execution(self, **body: Any) -> dict:
        return self._request("POST", "/executions", json=body, timeout=900)

    def run_execution(self, exec_id: str, *, approved: bool = False, force: bool = False, verify: bool = True) -> dict:
        return self._request("POST", f"/executions/{exec_id}/run",
                             json={"approved": approved, "force": force, "verify": verify}, timeout=900)

    def executions(self, *, verdict: list[str] | None = None, environment: str | None = None, q: str | None = None,
                   limit: int = 100, offset: int = 0) -> dict:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if verdict:
            params["verdict"] = verdict
        if environment:
            params["environment"] = environment
        if q:
            params["q"] = q
        return self._request("GET", "/executions", params=params)

    def execution(self, exec_id: str, include_report: bool = False) -> dict:
        return self._request("GET", f"/executions/{exec_id}", params={"include_report": include_report})

    def traces(self, exec_id: str) -> dict:
        return self._request("GET", f"/executions/{exec_id}/traces")

    def snapshots(self, exec_id: str) -> dict:
        return self._request("GET", f"/executions/{exec_id}/snapshots")

    def report(self, exec_id: str) -> dict:
        return self._request("GET", f"/executions/{exec_id}/report")

    def export_report(self, report_id: str, fmt: str = "json") -> bytes:
        return self._request("GET", f"/reports/{report_id}/export", params={"format": fmt}, raw=True).content

    def undo_execution(self, exec_id: str) -> dict:
        return self._request("POST", f"/executions/{exec_id}/undo", timeout=300)

    # -- data sources ----------------------------------------------------------------
    def sources(self) -> list[dict]:
        return self._request("GET", "/sources")

    def connect_source(self, **body: Any) -> dict:
        return self._request("POST", "/sources", json=body, timeout=300)

    def refresh_source(self, source_id: str) -> dict:
        return self._request("POST", f"/sources/{source_id}/refresh", timeout=120)

    def disconnect_source(self, source_id: str) -> dict:
        return self._request("DELETE", f"/sources/{source_id}")

    def reset_practice(self) -> dict:
        return self._request("POST", "/sources/practice/reset", timeout=120)

    # -- benchmark -------------------------------------------------------------------
    def run_benchmark(self, **body: Any) -> dict:
        return self._request("POST", "/benchmark/run", json=body, timeout=3600)

    def latest_benchmark(self) -> dict:
        return self._request("GET", "/benchmark/latest")
