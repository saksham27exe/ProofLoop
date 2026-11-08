"""REST API integration tests (in-process FastAPI TestClient)."""

from __future__ import annotations

from tests.conftest import ADMIN_PASSWORD


def test_public_health_and_bootstrap_flow(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/auth/bootstrap-status").json() == {"needs_bootstrap": True}
    r = client.post("/auth/bootstrap", json={"username": "admin", "password": "weak", "full_name": ""})
    assert r.status_code == 422  # schema min length
    r = client.post("/auth/bootstrap", json={"username": "admin", "password": "password123", "full_name": ""})
    assert r.status_code == 400 and r.json()["error"]["code"] == "weak_password"
    r = client.post("/auth/bootstrap", json={"username": "admin", "password": ADMIN_PASSWORD, "full_name": "A"})
    assert r.status_code == 201
    assert client.post("/auth/bootstrap", json={"username": "second", "password": ADMIN_PASSWORD}).status_code == 409


def test_authentication_is_required(client, admin_headers):
    for path in ("/executions", "/stats", "/scenarios", "/settings", "/health/llm", "/benchmark/latest"):
        assert client.get(path).status_code == 401, path
    assert client.get("/executions", headers={"Authorization": "Bearer forged"}).status_code == 401
    assert client.get("/executions", headers=admin_headers).status_code == 200


def test_oauth2_token_endpoint_for_swagger(client, admin_headers):
    r = client.post("/auth/token", data={"username": "admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 200 and r.json()["token_type"] == "bearer"


def test_rbac_viewer_cannot_run(client, admin_headers):
    r = client.post("/users", headers=admin_headers, json={"username": "vic", "password": "Viewer-Pass-1", "role": "viewer"})
    assert r.status_code == 201 and r.json()["must_change_password"] is True
    tok = client.post("/auth/login", json={"username": "vic", "password": "Viewer-Pass-1"}).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert client.get("/executions", headers=h).status_code == 200
    r = client.post("/executions", headers=h, json={"instruction": "Delete customer 112", "environment": "database"})
    assert r.status_code == 403
    assert client.get("/users", headers=h).status_code == 403
    assert client.put("/settings", headers=h, json={"interpreter_mode": "rules"}).status_code == 403
    # password change revokes nothing for the current session but changes the hash
    r = client.post("/auth/change-password", headers=h, json={"current_password": "Viewer-Pass-1", "new_password": "Viewer-Pass-2"})
    assert r.status_code == 200
    assert client.get("/auth/me", headers=h).json()["must_change_password"] is False


def test_full_execution_lifecycle_and_report_export(client, admin_headers):
    h = admin_headers
    r = client.post("/executions", headers=h, json={"instruction": "Delete customer 112", "environment": "database",
                                                     "reset_sandbox": True})
    assert r.status_code == 201
    detail = r.json()
    assert detail["plan"]["requires_approval"] and detail["execution_status"] == "interpreted"
    eid = detail["id"]
    r = client.post(f"/executions/{eid}/run", headers=h, json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "approval_required"
    r = client.post(f"/executions/{eid}/run", headers=h, json={"approved": True})
    assert r.status_code == 200 and r.json()["execution_status"] == "completed"
    report = client.post(f"/executions/{eid}/verify", headers=h).json()
    assert report["verdict"] == "VERIFIED"
    assert client.post(f"/executions/{eid}/verify", headers=h).status_code == 409  # immutable
    traces = client.get(f"/executions/{eid}/traces", headers=h).json()
    assert traces["integrity"]["valid"] and traces["events"][0]["event_type"] == "execution_started"
    snaps = client.get(f"/executions/{eid}/snapshots", headers=h).json()
    assert snaps["before"]["available"] and snaps["after"]["available"]
    rid = client.get(f"/executions/{eid}", headers=h).json()["report_id"]
    exp = client.get(f"/reports/{rid}/export?format=json", headers=h)
    assert exp.status_code == 200 and exp.headers["x-proofloop-integrity"] == "ok"
    md = client.get(f"/reports/{rid}/export?format=markdown", headers=h)
    assert md.text.startswith("# ProofLoop Verification Report - VERIFIED")
    listing = client.get("/executions?verdict=VERIFIED&q=customer", headers=h).json()
    assert listing["total"] == 1
    stats = client.get("/stats", headers=h).json()
    assert stats["verified"] == 1 and stats["verification_success_rate"] == 1.0


def test_false_claim_is_counted(client, admin_headers):
    r = client.post("/executions", headers=admin_headers, json={"instruction": "Close ticket 12", "environment": "api",
                                                                "fault_mode": "tool_failure_false_claim", "reset_sandbox": True})
    eid = r.json()["id"]
    out = client.post(f"/executions/{eid}/run", headers=admin_headers, json={"verify": True}).json()
    assert out["verdict"] == "FAILED" and out["agent_claimed_status"] == "success"
    assert client.get("/stats", headers=admin_headers).json()["false_success_claims_caught"] == 1


def test_validation_and_not_found_errors_are_clean(client, admin_headers):
    r = client.post("/executions", headers=admin_headers, json={"instruction": "x", "environment": "mainframe"})
    assert r.status_code == 422 and "environment" in r.json()["error"]["message"]
    assert client.get("/executions/exec_nope", headers=admin_headers).status_code == 404
    assert client.get("/reports/rpt_nope/export", headers=admin_headers).status_code == 404
    assert client.get("/sandbox/nope", headers=admin_headers).status_code == 404


def test_sandbox_settings_and_options(client, admin_headers):
    h = admin_headers
    state = client.get("/sandbox/database", headers=h).json()
    assert len(state["collections"]["employees"]) == 60
    assert client.post("/sandbox/file/reset", headers=h).json()["status"] == "reset"
    r = client.put("/settings", headers=h, json={"interpreter_mode": "rules", "ollama_model": "llama3.2:3b"})
    assert r.json()["runtime"]["ollama_model"] == "llama3.2:3b"
    assert client.put("/settings", headers=h, json={"ollama_model": "bad model; rm -rf"}).status_code == 422
    opts = client.get("/options", headers=h).json()
    assert {e["name"] for e in opts["environments"]} == {"database", "api", "file", "src_practice"}
    assert next(e for e in opts["environments"] if e["name"] == "src_practice")["real_data"] is True
    assert len(client.get("/scenarios", headers=h).json()) >= 15
    audit = client.get("/audit", headers=h).json()
    assert any(a["action"] == "settings_changed" for a in audit)


def test_lockout_via_api(client, admin_headers):
    for _ in range(5):
        r = client.post("/auth/login", json={"username": "admin", "password": "wrong-password-1"})
    r = client.post("/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 423
