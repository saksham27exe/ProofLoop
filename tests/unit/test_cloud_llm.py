"""Opt-in cloud model (NVIDIA NIM / OpenAI-compatible): provider behaviour and the consent gate.

No test touches the network: the provider runs on an httpx.MockTransport, and API tests never
call an endpoint that would reach the cloud.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from app.llm.base import LLMError, LLMUnavailable
from app.llm.cloud_provider import CloudProvider
from app.llm.model_manager import ModelManager
from app.services.runtime_settings import RuntimeSettings
from tests.conftest import ADMIN_PASSWORD

KEY = "nvapi-test-secret-123"
GOOD = {"operation": "UPDATE", "entity": "employee", "target_id": "1042", "target_lookup": {}, "values": {"email": "new@example.com"},
        "confidence": 0.9, "ambiguities": []}


def _provider(handler, key: str | None = KEY, retries: int = 2) -> CloudProvider:
    return CloudProvider("https://integrate.api.nvidia.com/v1", "meta/llama-3.3-70b-instruct",
                         SecretStr(key) if key else None, max_retries=retries, transport=httpx.MockTransport(handler))


def _reply(content: str = "", tool_calls: list | None = None) -> httpx.Response:
    msg: dict = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return httpx.Response(200, json={"model": "meta/llama-3.3-70b-instruct", "choices": [{"message": msg}]})


def test_refuses_plain_http_endpoint():
    with pytest.raises(LLMError):
        CloudProvider("http://api.example.com/v1", "m", SecretStr(KEY))
    with pytest.raises(ValueError):
        from app.config import Settings

        Settings(cloud_base_url="http://api.example.com/v1")


def test_health_without_key_makes_no_request():
    def handler(request):  # pragma: no cover - must not be called
        raise AssertionError("network used without a key")

    h = _provider(handler, key=None).health()
    assert not h.reachable and "PROOFLOOP_CLOUD_API_KEY" in h.message


def test_generate_sends_bearer_key_and_retries_structured_output():
    from app.interpretation.verification_plan import InterpretationDraft

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        assert request.url.path.endswith("/chat/completions")
        return _reply("sorry, no json") if len(seen) == 1 else _reply("```json\n" + json.dumps(GOOD) + "\n```")

    res = _provider(handler).generate("p", system="s", response_schema=InterpretationDraft)
    assert res.parsed.target_id == "1042" and res.attempts == 2
    body = json.loads(seen[0].content)
    assert body["temperature"] == 0 and "JSON schema" in body["messages"][0]["content"]


def test_tool_calls_are_translated_both_ways():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _reply("", [{"id": "x1", "type": "function",
                            "function": {"name": "find_rows", "arguments": '{"table": "customers", "where": {"id": 5}}'}}])

    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "list_tables", "arguments": {}}}]},
        {"role": "tool", "content": '{"tables": ["customers"]}', "tool_name": "list_tables"},
    ]
    tools = [{"type": "function", "function": {"name": "find_rows", "description": "d", "parameters": {"type": "object"}}}]
    message, _ = _provider(handler).chat_with_tools(history, tools)

    sent = captured["body"]["messages"]
    call_id = sent[2]["tool_calls"][0]["id"]
    assert sent[2]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert sent[3] == {"role": "tool", "tool_call_id": call_id, "content": '{"tables": ["customers"]}'}
    assert captured["body"]["tools"] == tools
    # Back in the Ollama-style shape the agent loop expects, with parsed arguments.
    assert message["tool_calls"] == [{"function": {"name": "find_rows", "arguments": {"table": "customers", "where": {"id": 5}}}}]


def test_errors_are_clear_and_never_leak_the_key():
    def unauthorized(request):
        return httpx.Response(401, json={"detail": "bad key"})

    def limited(request):
        return httpx.Response(429, json={"detail": "slow down"})

    with pytest.raises(LLMUnavailable) as exc:
        _provider(unauthorized).generate("p")
    assert KEY not in str(exc.value)
    with pytest.raises(LLMError, match="rate limit"):
        _provider(limited).generate("p")


def test_model_manager_uses_cloud_only_when_selected(settings):
    mm = ModelManager(settings)
    base = dict(ollama_model="qwen2.5:3b", interpreter_mode="auto", llm_enabled=True, llm_narrative_enabled=False)
    assert mm.for_runtime(RuntimeSettings(**base)).name == "ollama"
    assert mm.for_runtime(RuntimeSettings(**base, llm_backend="cloud")).name == "cloud"


# --------------------------------------------------------------------- consent gate (REST API)
@pytest.fixture
def keyed_client(settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(settings.model_copy(update={"cloud_api_key": SecretStr(KEY)}))) as c:
        yield c


def _admin(client) -> dict[str, str]:
    r = client.post("/auth/bootstrap", json={"username": "admin", "password": ADMIN_PASSWORD, "full_name": "Admin"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_switching_to_cloud_requires_consent_and_a_key(client, admin_headers):
    r = client.put("/settings", headers=admin_headers, json={"llm_backend": "cloud"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "cloud_consent_required"
    r = client.put("/settings", headers=admin_headers, json={"llm_backend": "cloud", "cloud_consent": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "cloud_api_key_missing"
    assert client.get("/settings", headers=admin_headers).json()["runtime"]["llm_backend"] == "local"


def test_consent_is_recorded_and_audited(keyed_client):
    h = _admin(keyed_client)
    view = keyed_client.get("/settings", headers=h).json()
    assert view["cloud"]["active"] is False and view["cloud"]["api_key_configured"] is True
    assert "You are responsible" in view["cloud"]["notice"]["text"]
    assert KEY not in json.dumps(view)  # the key itself is never returned

    r = keyed_client.put("/settings", headers=h, json={"llm_backend": "cloud", "cloud_consent": True,
                                                         "cloud_model": "meta/llama-3.1-70b-instruct"})
    assert r.status_code == 200 and r.json()["runtime"]["llm_backend"] == "cloud"
    view = keyed_client.get("/settings", headers=h).json()
    assert view["cloud"]["active"] and view["cloud"]["consent"]["accepted_by"] == "admin"
    assert view["cloud"]["consent"]["notice_version"] == view["cloud"]["notice"]["version"]
    assert view["privacy"]["external_llm_apis"].startswith("ACTIVE")

    assert keyed_client.put("/settings", headers=h, json={"llm_backend": "local"}).status_code == 200
    actions = [a["action"] for a in keyed_client.get("/audit", headers=h).json()]
    assert "cloud_llm_consent_accepted" in actions and "cloud_llm_disabled" in actions


def test_only_admins_can_switch_backend(keyed_client):
    h = _admin(keyed_client)
    keyed_client.post("/users", headers=h, json={"username": "ana", "password": "Analyst-Pass-1", "role": "analyst"})
    tok = keyed_client.post("/auth/login", json={"username": "ana", "password": "Analyst-Pass-1"}).json()["access_token"]
    r = keyed_client.put("/settings", headers={"Authorization": f"Bearer {tok}"},
                         json={"llm_backend": "cloud", "cloud_consent": True})
    assert r.status_code == 403


def test_nvidia_keys_are_redacted_from_traces():
    from app.utils.security import redact_string

    assert "nvapi-" not in redact_string("use key nvapi-AbCdEf0123456789xyz_LONG please")
