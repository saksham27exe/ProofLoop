"""Verifying *your own* agent through a session (REST API + Python SDK), and write policies.

The SDK runs against the in-process FastAPI app (TestClient is an httpx.Client), on the fictional
Practice store - exactly the code path a LangChain/CrewAI/plain-Python agent would use.
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk"))
from proofloop_sdk import ProofLoop, ProofLoopError  # noqa: E402

SRC = "src_practice"


@pytest.fixture
def pl(client, admin_headers) -> ProofLoop:
    return ProofLoop(token=admin_headers["Authorization"].split(" ", 1)[1], client=client)


def _db_path(client, admin_headers) -> Path:
    src = next(s for s in client.get("/sources", headers=admin_headers).json() if s["environment"] == SRC)
    return Path(src["location"])


def _email(path: Path, cid: int) -> str:
    with closing(sqlite3.connect(path)) as con:
        return con.execute("SELECT email FROM customers WHERE id = ?", (cid,)).fetchone()[0]


# ------------------------------------------------------------------------------------ outcomes
def test_correct_agent_using_proofloop_tools_is_verified(pl, client, admin_headers):
    with pl.session("Change the email of customer 5 to jonas.m@newmail.example", SRC,
                    agent_name="my-agent", framework="langgraph") as s:
        assert "update_rows" in s.tool_names and s.openai_tools[0]["type"] == "function"
        found = s.call("find_rows", table="customers", where={"id": 5})
        assert found["ok"] and found["output"]["rows"][0]["id"] == 5
        update = s.tool("update_rows")  # a plain function, as a framework would register it
        assert update(table="customers", where={"id": 5}, values={"email": "jonas.m@newmail.example"})["ok"]
        s.finish("DONE: updated the email of customer 5")
    assert s.verdict == "VERIFIED", s.report["summary"]
    detail = client.get(f"/executions/{s.execution_id}", headers=admin_headers).json()
    assert detail["agent_name"] == "my-agent" and detail["agent_framework"] == "langgraph"
    trace = client.get(f"/executions/{s.execution_id}/traces", headers=admin_headers).json()
    assert {"tool_call", "tool_response", "agent_claim"} <= {e["event_type"] for e in trace["events"]}


def test_agent_that_changes_the_wrong_record_fails(pl):
    with pl.session("Change the email of customer 5 to jonas.m@newmail.example", SRC) as s:
        s.call("update_rows", table="customers", where={"id": 6}, values={"email": "jonas.m@newmail.example"})
        s.finish("DONE: updated the email of customer 5")  # a false claim
    assert s.verdict == "FAILED"


def test_agent_that_claims_success_but_did_nothing_fails(pl):
    with pl.session("Change the email of customer 5 to jonas.m@newmail.example", SRC) as s:
        s.finish("DONE: updated it")
    assert s.verdict == "FAILED"


def test_direct_write_without_reported_calls_is_inconclusive_and_with_them_verified(pl, client, admin_headers):
    path = _db_path(client, admin_headers)
    with pl.session("Change the email of customer 5 to a@direct.example", SRC) as s:
        with closing(sqlite3.connect(path)) as con, con:
            con.execute("UPDATE customers SET email = 'a@direct.example' WHERE id = 5")
        s.finish("DONE")
    assert s.verdict == "INCONCLUSIVE"  # nothing in the trace explains the change

    with pl.session("Change the email of customer 5 to b@direct.example", SRC) as s:
        with closing(sqlite3.connect(path)) as con, con:
            con.execute("UPDATE customers SET email = 'b@direct.example' WHERE id = 5")
        s.record("update_rows", {"table": "customers", "where": {"id": 5}, "values": {"email": "b@direct.example"}},
                 {"table": "customers", "keys": [5], "changed": 1})
        s.finish("DONE")
    assert s.verdict == "VERIFIED", s.report["summary"]


def test_crashing_agent_is_still_verified(pl):
    with pytest.raises(RuntimeError), pl.session("Change the email of customer 5 to c@x.example", SRC) as s:
        s.call("update_rows", table="customers", where={"id": 7}, values={"email": "c@x.example"})
        raise RuntimeError("boom")
    assert s.verdict == "FAILED"  # it changed customer 7 before crashing - verified anyway


# ------------------------------------------------------------------------------------ guard rails
def test_destructive_task_needs_approval(pl):
    with pytest.raises(ProofLoopError) as exc:
        pl.session("Delete order 12", SRC)
    assert exc.value.code == "approval_required"


def test_one_session_per_environment_and_abort(pl, client, admin_headers):
    first = pl.session("Change the email of customer 5 to d@x.example", SRC)
    with pytest.raises(ProofLoopError) as exc:
        pl.session("Change the email of customer 6 to e@x.example", SRC)
    assert exc.value.code == "session_active"
    # ProofLoop's own agent may not run on the reserved environment either.
    r = client.post("/executions", headers=admin_headers,
                    json={"instruction": "Change the email of customer 6 to e@x.example", "environment": SRC,
                          "agent_config": "ai_agent"})
    assert client.post(f"/executions/{r.json()['id']}/run", headers=admin_headers, json={}).json()["error"]["code"] == "session_active"
    first.abort("test")
    with pl.session("Change the email of customer 6 to e@x.example", SRC) as second:
        second.finish("nothing done")
    assert second.verdict == "FAILED"


def test_run_endpoint_refuses_external_executions(client, admin_headers):
    r = client.post("/executions", headers=admin_headers, json={"instruction": "Change the email of customer 5 to f@x.example",
                                                                "environment": SRC, "agent_config": "external"})
    assert client.post(f"/executions/{r.json()['id']}/run", headers=admin_headers, json={}).json()["error"]["code"] == "external_agent"
    assert "external" not in {a["id"] for a in client.get("/options", headers=admin_headers).json()["agents"]}


def test_change_by_proofloop_during_session_makes_window_inconclusive(pl, client, admin_headers):
    with pl.session("Change the email of customer 5 to g@x.example", SRC) as s:
        s.call("update_rows", table="customers", where={"id": 5}, values={"email": "g@x.example"})
        assert client.post("/sources/practice/reset", headers=admin_headers).status_code == 200
        s.finish("DONE")
    assert s.verdict == "INCONCLUSIVE"


def test_write_policy_blocks_calls_before_they_reach_the_data(pl, client, admin_headers):
    path = _db_path(client, admin_headers)
    r = client.put("/sources/practice/policy", headers=admin_headers,
                   json={"protected_columns": ["customers.email"], "read_only": ["products"], "allow_delete": False})
    assert r.status_code == 200 and "no deletions" in r.json()["rules"]
    assert client.put("/sources/practice/policy", headers=admin_headers,
                      json={"protected_columns": ["customers.nope"]}).status_code == 422
    before = _email(path, 5)
    with pl.session("Change the email of customer 5 to h@x.example", SRC) as s:
        assert s.policy["protected_columns"] == ["customers.email"]
        res = s.call("update_rows", table="customers", where={"id": 5}, values={"email": "h@x.example"})
        assert not res["ok"] and "Blocked by policy" in res["error"]
        assert not s.call("update_rows", table="products", where={"id": 3}, values={"stock": 1})["ok"]
        s.finish("DONE: changed the email")  # the claim is false - and the data proves it
    assert s.verdict == "FAILED" and _email(path, 5) == before


def test_policy_requires_admin(client, admin_headers):
    client.post("/users", headers=admin_headers, json={"username": "ana", "password": "Analyst-Pass-1", "role": "analyst"})
    tok = client.post("/auth/login", json={"username": "ana", "password": "Analyst-Pass-1"}).json()["access_token"]
    r = client.put("/sources/practice/policy", headers={"Authorization": f"Bearer {tok}"}, json={"allow_delete": False})
    assert r.status_code == 403


# ------------------------------------------------------------------------------------ policy unit
def test_check_call_rules():
    from app.services.policies import SourcePolicy, check_call

    p = SourcePolicy(read_only=["products", "config"], protected_columns=["customers.email"], allow_delete=False, allow_insert=False)
    assert check_call(p, "find_rows", {"table": "products"}) is None
    assert "read-only" in check_call(p, "update_rows", {"table": "products", "values": {"stock": 1}})
    assert "protected" in check_call(p, "update_rows", {"table": "customers", "values": {"email": "x"}})
    assert check_call(p, "update_rows", {"table": "customers", "values": {"tier": "gold"}}) is None
    assert "deletions" in check_call(p, "delete_rows", {"table": "orders"})
    assert "new records" in check_call(p, "insert_row", {"table": "orders", "values": {}})
    assert "read-only" in check_call(p, "write_file", {"path": "config/app.ini"})
    assert check_call(p, "write_file", {"path": "configuration.txt"}) is None


def test_expired_session_no_longer_blocks_the_environment(pl, client, admin_headers):
    from app.database import repositories as repo

    stale = pl.session("Change the email of customer 5 to i@x.example", SRC)
    ctx = client.app.state.ctx
    with ctx.db.session() as s:
        repo.set_setting(s, f"external_active:{SRC}", {"execution_id": stale.execution_id,
                                                       "started_at": "2026-01-01T00:00:00.000Z", "start_version": 0})
    with pl.session("Change the email of customer 6 to j@x.example", SRC) as fresh:
        fresh.finish("nothing done")
    assert fresh.verdict == "FAILED"
    assert client.get(f"/executions/{stale.execution_id}", headers=admin_headers).json()["execution_status"] == "aborted"
