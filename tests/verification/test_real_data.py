"""Real-data mode: connected sources, the tool-calling agent loop, bulk verification, backups/undo.

The local model is replaced by a scripted stand-in that returns tool calls, so the *real* agent loop,
real SQLite writes, real snapshots and the real verifier run deterministically. Live-model runs are in
docs/benchmarking.md.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.connectors.folder_source import FolderSourceEnvironment
from app.connectors.practice_store import generate
from app.connectors.sqlite_source import SourceConfig, SQLiteSourceEnvironment
from app.environments.base import InspectorUnavailable
from app.interpretation.plan_validator import build_plan
from app.interpretation.rule_parser import parse_instruction
from app.llm.model_manager import ModelManager
from app.services.data_sources import DataSourceService
from app.services.execution_service import ExecutionError, ExecutionService
from app.services.runtime_settings import RuntimeSettings

OSLO = ["8", "22", "46", "51", "58", "63", "70", "94", "118"]  # customers in Oslo in the seeded practice store


class ScriptedModel:
    """Stands in for the local model: replays a fixed list of assistant messages."""

    model = "scripted-test-model"

    def __init__(self, *turns: dict) -> None:
        self.turns = list(turns)
        self.seen: list[list[dict]] = []

    def chat_with_tools(self, messages, tools):
        self.seen.append(list(messages))
        return (self.turns.pop(0) if self.turns else {"content": "DONE: finished."}), self.model


def call(name: str, **args) -> dict:
    return {"content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def say(text: str) -> dict:
    return {"content": text}


@pytest.fixture
def real(settings, db, registry, monkeypatch):
    sources = DataSourceService(settings, db, registry)
    sources.load_all()
    runtime = RuntimeSettings(ollama_model="qwen2.5:3b", interpreter_mode="rules", llm_enabled=True, llm_narrative_enabled=False)
    svc = ExecutionService(settings, db, registry, ModelManager(settings), runtime_override=runtime, sources=sources)
    holder: dict = {}
    monkeypatch.setattr(svc.models, "provider", lambda model=None: holder["model"])

    def run(instruction: str, *turns: dict) -> dict:
        holder["model"] = ScriptedModel(*turns)
        exec_id = svc.create_execution(instruction=instruction, environment="src_practice", agent_config="ai_agent")
        report = svc.run_and_verify(exec_id, approved=True, force=True)
        report["_exec_id"] = exec_id
        return report

    return svc, sources, run


def _verdict(report: dict) -> str:
    return report["verdict"] if isinstance(report["verdict"], str) else report["verdict"]["value"]


def _tier(path: Path, cid: int) -> str:
    with closing(sqlite3.connect(path)) as con:
        return con.execute("SELECT tier FROM customers WHERE id = ?", [cid]).fetchone()[0]


# ------------------------------------------------------------------ connector
def test_sqlite_discovery_and_toolbox_safety(tmp_path):
    db_path = tmp_path / "shop.db"
    generate(db_path)
    env = SQLiteSourceEnvironment(SourceConfig(id="t1", name="Shop", kind="sqlite", location=str(db_path)))
    customer = env.schema.entity("customers")
    assert customer and customer.key_field == "id" and customer.field("tier").enum_values == ["standard", "gold", "platinum"]
    assert env.schema.entity("order").field("total").type == "number"
    tb = env.toolbox()
    assert not tb.call("update_rows", {"table": "customers", "values": {"tier": "gold"}}).ok  # no condition
    assert not tb.call("delete_rows", {"table": "customers"}).ok
    assert "Unknown table" in tb.call("find_rows", {"table": "customers; DROP TABLE orders"}).error
    assert "Unknown column" in tb.call("find_rows", {"table": "customers", "where": {"x) OR 1=1 --": 1}}).error
    res = tb.call("update_rows", {"table": "customers", "where": {"city": "Oslo"}, "values": {"tier": "platinum"}})
    assert res.ok and sorted(res.output["keys"], key=int) == OSLO
    assert env.trace_refs("update_rows", {}, res.output)[0] == ("customers", "8")


def test_inspector_is_read_only_and_refuses_oversized_sources(tmp_path):
    db_path = tmp_path / "shop.db"
    generate(db_path)
    env = SQLiteSourceEnvironment(SourceConfig(id="t2", name="Shop", kind="sqlite", location=str(db_path)))
    snap = env.inspector().snapshot()
    assert len(snap["customers"]) == 120 and snap["customers"]["5"]["city"] == "Berlin"
    small = SQLiteSourceEnvironment(SourceConfig(id="t3", name="Shop", kind="sqlite", location=str(db_path), max_snapshot_rows=100))
    with pytest.raises(InspectorUnavailable, match="snapshot limit"):
        small.inspector().snapshot()


def test_folder_source_confines_paths(tmp_path):
    root = tmp_path / "docs"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "a.txt").write_text("hello", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("secret", encoding="utf-8")
    env = FolderSourceEnvironment(SourceConfig(id="f1", name="Docs", kind="folder", location=str(root)))
    assert set(env.inspector().snapshot()["files"]) == {"notes/a.txt"}
    tb = env.toolbox()
    assert not tb.call("read_file", {"path": "../outside.txt"}).ok
    assert not tb.call("write_file", {"path": "C:/Windows/x.txt", "content": "x"}).ok
    assert not tb.call("read_file", {"path": ".git/config"}).ok
    assert tb.call("write_file", {"path": "notes/b.txt", "content": "new"}).ok
    bk = tmp_path / "bk.zip"
    env.backup(bk)
    tb.call("delete_file", {"path": "notes/a.txt"})
    tb.call("write_file", {"path": "notes/c.txt", "content": "later"})
    env.restore(bk)
    assert set(env.inspector().snapshot()["files"]) == {"notes/a.txt", "notes/b.txt"}


# ------------------------------------------------------------ interpretation
def test_rule_grammar_for_bulk_tasks(real):
    svc, _, _ = real
    schema = svc.registry.get("src_practice").schema
    text = "Set the tier of all customers where city is Oslo to gold"
    plan = build_plan(parse_instruction(text, schema), schema, text, "rules")
    assert plan.verifiable and plan.target.bulk and plan.expected_state == {"tier": "gold"}
    assert [(c.field, c.op, c.value) for c in plan.target.conditions] == [("city", "=", "Oslo")]
    assert plan.requires_approval and any(r.id == "bulk_field_matches:tier" for r in plan.requirements)
    text2 = "Delete all orders where status is cancelled and total is less than 50"
    plan2 = build_plan(parse_instruction(text2, schema), schema, text2, "rules")
    assert plan2.verifiable and [(c.field, c.op) for c in plan2.target.conditions] == [("status", "="), ("total", "<")]
    # Talking about "all" records without a readable selection is refused, never read as one record.
    vague = "Update the email of all customers named Maria Garcia to m@x.example"
    assert not build_plan(parse_instruction(vague, schema), schema, vague, "rules").verifiable


# --------------------------------------------------------- real agent loop
def test_correct_bulk_update_is_verified(real):
    svc, sources, run = real
    report = run("Set the tier of all customers where city is Oslo to gold",
                 call("find_rows", table="customers", where={"city": "Oslo"}),
                 call("update_rows", table="customers", where={"city": "Oslo"}, values={"tier": "gold"}),
                 say("DONE: set tier gold for the 9 customers in Oslo."))
    assert _verdict(report) == "VERIFIED", report["summary"]
    assert "9 customer record(s)" in report["summary"]
    assert _tier(sources.practice_path, 8) == "gold"


def test_partial_bulk_update_fails(real):
    _, _, run = real
    report = run("Set the tier of all customers where city is Oslo to gold",
                 call("update_rows", table="customers", conditions=[{"column": "id", "op": "<", "value": 50}],
                      where={"city": "Oslo"}, values={"tier": "gold"}),
                 say("DONE: all Oslo customers are gold now."))
    assert _verdict(report) == "FAILED"
    assert any("Not every selected record was updated" in d for d in report["diagnosis"])


def test_over_broad_update_is_a_side_effect(real):
    _, _, run = real
    report = run("Set the tier of all customers where city is Berlin to gold",
                 call("update_rows", table="customers", where={"country": "Germany"}, values={"tier": "gold"}),
                 say("DONE: Berlin customers upgraded."))
    assert _verdict(report) == "FAILED"
    assert any(c["id"] == "no_changes_to_other_records" and c["status"] == "failed" for c in report["checks"])


def test_false_success_claim_without_any_change_fails(real):
    _, _, run = real
    report = run("Update the email of customer 5 to new.mail@shop.example",
                 call("find_rows", table="customers", where={"id": 5}),
                 say("DONE: the email of customer 5 is now new.mail@shop.example."))
    assert _verdict(report) == "FAILED"


def test_correct_single_update_and_undo(real):
    svc, sources, run = real
    report = run("Update the email of customer 5 to new.mail@shop.example",
                 call("update_rows", table="customers", where={"id": 5}, values={"email": "new.mail@shop.example"}),
                 say("DONE: updated customer 5."))
    assert _verdict(report) == "VERIFIED", report["summary"]
    exec_id = report["_exec_id"]
    with closing(sqlite3.connect(sources.practice_path)) as con:
        assert con.execute("SELECT email FROM customers WHERE id = 5").fetchone()[0] == "new.mail@shop.example"
    svc.undo_execution(exec_id)
    with closing(sqlite3.connect(sources.practice_path)) as con:
        assert con.execute("SELECT email FROM customers WHERE id = 5").fetchone()[0] != "new.mail@shop.example"
    with pytest.raises(ExecutionError, match="already undone"):
        svc.undo_execution(exec_id)


def test_unparsed_native_tool_call_recovers_through_text_mode(real):
    # Real qwen2.5:3b behaviour: the runtime drops a tool call it cannot parse and returns an empty message.
    _, _, run = real
    text_call = '{"name": "update_rows", "arguments": {"table": "customers", "where": {"id": 5}, "values": {"email": "t@shop.example"}}}'
    report = run("Update the email of customer 5 to t@shop.example",
                 {"content": ""},          # native tool channel: empty
                 {"content": text_call},   # text-mode fallback: JSON tool call
                 say("DONE: updated customer 5."))
    assert _verdict(report) == "VERIFIED", report["summary"]


def test_only_the_latest_run_can_be_undone(real):
    svc, _, run = real
    first = run("Update the email of customer 5 to a@shop.example",
                call("update_rows", table="customers", where={"id": 5}, values={"email": "a@shop.example"}), say("DONE"))
    run("Update the email of customer 6 to b@shop.example",
        call("update_rows", table="customers", where={"id": 6}, values={"email": "b@shop.example"}), say("DONE"))
    with pytest.raises(ExecutionError, match="most recent run"):
        svc.undo_execution(first["_exec_id"])


CANCELLED_CHEAP = "Delete all orders where status is cancelled and total is less than 100"


def test_on_delete_cascade_children_are_permitted_dependencies(real):
    # Real finding with qwen2.5:3b: a correct bulk delete was FAILED because the database's own
    # ON DELETE CASCADE removed the orders' line items. Those are consequences, not side effects.
    _, _, run = real
    report = run(CANCELLED_CHEAP,
                 call("delete_rows", table="orders", conditions=[{"column": "status", "op": "=", "value": "cancelled"},
                                                                 {"column": "total", "op": "<", "value": 100}]),
                 say("DONE: deleted the cancelled orders under 100."))
    assert _verdict(report) == "VERIFIED", report["summary"]
    kinds = {(c["collection"], c["kind"]) for c in report["changes"]}
    assert ("order_items", "permitted_dependency") in kinds


def test_unrelated_child_deletion_is_still_a_side_effect(real):
    _, _, run = real
    report = run(CANCELLED_CHEAP,
                 call("delete_rows", table="orders", conditions=[{"column": "status", "op": "=", "value": "cancelled"},
                                                                 {"column": "total", "op": "<", "value": 100}]),
                 call("delete_rows", table="order_items", where={"order_id": 1}),
                 say("DONE"))
    assert _verdict(report) == "FAILED"


def test_llm_condition_and_value_repairs():
    from app.interpretation.llm_guard import normalize_llm_draft
    from app.interpretation.verification_plan import Condition, InterpretationDraft

    # Real qwen2.5:3b outputs for "Mark all pending orders with a total above 900 as paid":
    assert Condition(field="total", op=">", value="*gt;900").value == 900
    assert Condition(field="total", op="=", value="&gt;900").op == ">"
    # The exact draft qwen2.5:3b produced: conditions right, but the condition echoed into values plus an invented field.
    with_invented = InterpretationDraft(operation="UPDATE", entity="order", target_scope="all",
                                        conditions=[Condition(field="status", value="pending"),
                                                    Condition(field="total", op=">", value=900)],
                                        values={"status": "paid", "total": "*gt;900", "status_change_date": "*now"})
    instruction = "Mark all pending orders with a total above 900 as paid"
    assert normalize_llm_draft(with_invented, _practice_schema(), instruction).values == {"status": "paid"}


def _practice_schema():
    import tempfile

    path = Path(tempfile.mkdtemp()) / "s.db"
    generate(path)
    return SQLiteSourceEnvironment(SourceConfig(id="s", name="S", kind="sqlite", location=str(path))).schema


def test_condition_matching_nothing_is_inconclusive(real):
    _, _, run = real
    report = run("Set the tier of all customers where city is Atlantis to gold",
                 call("find_rows", table="customers", where={"city": "Atlantis"}),
                 say("FAILED: no customers in Atlantis."))
    assert _verdict(report) == "INCONCLUSIVE"


def test_agent_sees_tool_results_and_repeats_are_cut_off(real):
    svc, _, run = real
    loop = [call("count_rows", table="customers") for _ in range(4)]
    report = run("Update the email of customer 5 to c@shop.example", *loop, say("FAILED: gave up."))
    assert _verdict(report) == "FAILED"  # nothing changed
    seen = svc.models.provider().seen  # type: ignore[call-arg]
    tool_msgs = [m for m in seen[-1] if m["role"] == "tool"]
    assert '"count": 120' in tool_msgs[0]["content"] and "already made this exact call" in tool_msgs[-1]["content"]


def test_sources_cannot_use_fault_injection_or_sandbox_agents(real):
    svc, _, _ = real
    with pytest.raises(ExecutionError, match="cannot run on a connected data source"):
        svc.create_execution(instruction="Update the email of customer 5 to x@y.example", environment="src_practice",
                             agent_config="scripted")
    with pytest.raises(ExecutionError, match="Fault injection"):
        svc.create_execution(instruction="Update the email of customer 5 to x@y.example", environment="src_practice",
                             agent_config="ai_agent", fault_mode="side_effect")


# ------------------------------------------------------------------- API
def test_real_run_and_undo_through_the_api(client, admin_headers, monkeypatch):
    """Regression: POST /executions/{id}/undo returned 500 'database is locked' (request session held a lock)."""
    from app.llm.model_manager import ModelManager

    model = ScriptedModel(call("update_rows", table="customers", where={"id": 5}, values={"email": "api@shop.example"}),
                          say("DONE: updated."))
    monkeypatch.setattr(ModelManager, "provider", lambda self, m=None: model)
    h = admin_headers
    client.put("/settings", headers=h, json={"interpreter_mode": "rules", "llm_enabled": True})
    r = client.post("/executions", headers=h, json={"instruction": "Update the email of customer 5 to api@shop.example",
                                                     "environment": "src_practice", "agent_config": "ai_agent"})
    assert r.status_code == 201, r.text
    exec_id = r.json()["id"]
    res = client.post(f"/executions/{exec_id}/run", headers=h, json={"verify": True}).json()
    assert res["verdict"] == "VERIFIED" and res["real_data"] is True and res["backup"]["can_undo"] is True
    assert res["harness"]["expected_verdict"] is None
    undone = client.post(f"/executions/{exec_id}/undo", headers=h)
    assert undone.status_code == 200, undone.text
    assert undone.json()["backup"]["restored_by"] == "admin" and undone.json()["backup"]["can_undo"] is False
    assert client.post(f"/executions/{exec_id}/undo", headers=h).status_code == 409


def test_sources_api(client, admin_headers, tmp_path, settings):
    h = admin_headers
    listed = client.get("/sources", headers=h).json()
    assert listed[0]["id"] == "practice" and listed[0]["status"] == "connected"
    shop = tmp_path / "mine.db"
    generate(shop)
    r = client.post("/sources", headers=h, json={"name": "My shop", "location": str(shop)})
    assert r.status_code == 201, r.text
    src = r.json()
    assert src["kind"] == "sqlite" and {e["collection"] for e in src["entities"]} >= {"customers", "orders"}
    assert client.post("/sources", headers=h, json={"name": "dup", "location": str(shop)}).status_code == 409
    not_db = tmp_path / "notes.txt"
    not_db.write_text("hello")
    assert "not a SQLite database" in client.post("/sources", headers=h, json={"name": "x", "location": str(not_db)}).json()["error"]["message"]
    assert client.post("/sources", headers=h, json={"name": "x", "location": str(settings.data_dir)}).status_code == 400
    assert client.post("/sources", headers=h, json={"name": "x", "location": "relative/path.db"}).status_code == 400
    assert client.delete("/sources/practice", headers=h).status_code == 409
    assert client.post("/executions/exec_doesnotexist/undo", headers=h).status_code == 404
    assert client.delete(f"/sources/{src['id']}", headers=h).status_code == 200
    assert shop.is_file()  # disconnecting never touches the data


def test_write_policy_also_binds_proofloops_own_ai_agent(real):
    from app.services.policies import SourcePolicy, save_policy

    svc, _sources, run = real
    env = svc.registry.get("src_practice")
    with svc.db.session() as s:
        save_policy(s, env, SourcePolicy(protected_columns=["customers.email"]), "admin")
    report = run("Change the email of customer 5 to jonas.m@newmail.example",
                 call("update_rows", table="customers", where={"id": 5}, values={"email": "jonas.m@newmail.example"}),
                 say("DONE: updated the email."))
    assert _verdict(report) == "FAILED"  # the call was blocked, so the requested change never happened
    events = svc.storage.load(report["_exec_id"])
    blocked = [e for e in events.events if e.event_type == "tool_response" and "Blocked by policy" in str(e.output)]
    assert blocked
