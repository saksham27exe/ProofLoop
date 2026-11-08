"""End-to-end fault-injection scenarios: real agent runs, real state, real verdicts."""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import re

import pytest

from app.agent.fault_injection import FAULTS, FaultMode, applicable_faults
from app.services.execution_service import load_scenarios
from app.verification.engine import VerificationEngine, VerificationInput
from app.verification.models import ChangeKind

SCENARIOS = load_scenarios(__import__("app.config", fromlist=["PROJECT_ROOT"]).PROJECT_ROOT / "sample_data")


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_predefined_scenarios_produce_expected_verdict(scenario, run_task):
    report = run_task(scenario["instruction"], scenario["environment"], scenario["fault_mode"])
    assert report["verdict"] == scenario["expected_verdict"], report["summary"]
    # Every decisive, non-skipped check must cite evidence.
    for c in report["checks"]:
        if c["decisive"] and c["status"] != "skipped":
            assert c["evidence_ids"], c["id"]


TASKS = {
    ("database", "UPDATE"): "Update the phone number of customer 105 to +1-555-0199",
    ("database", "CREATE"): "Create a new employee record with name Lena Moreau and department Finance",
    ("database", "DELETE"): "Delete customer 112",
    ("api", "UPDATE"): "Set the priority of ticket 3 to low",
    ("api", "CREATE"): "Create a ticket titled 'Docking station not detected' with priority high",
    ("api", "DELETE"): "Delete ticket 7",
    ("file", "UPDATE"): "Overwrite the file notes/todo.md with 'All tasks complete.'",
    ("file", "CREATE"): "Create a file reports/new_report.txt with content 'Numbers approved.'",
    ("file", "DELETE"): "Delete the file logs/agent.log",
}
MATRIX = [(env, op, f) for (env, op) in TASKS for f in applicable_faults(env, op)]


@pytest.mark.parametrize(("env", "op", "fault"), MATRIX, ids=[f"{e}-{o}-{f.value}" for e, o, f in MATRIX])
def test_every_fault_in_every_environment(env, op, fault, run_task):
    report = run_task(TASKS[(env, op)], env, fault.value)
    assert report["verdict"] == FAULTS[fault].expected_verdict, f"{report['summary']} | {report['diagnosis']}"
    if FAULTS[fault].unintended_change:
        assert any(s["kind"] == "side_effect" for s in report["side_effects"])
    if FAULTS[fault].ground_truth == "failure" and fault != FaultMode.NONE:
        claim_check = next(c for c in report["checks"] if c["id"] == "agent_claim_consistent")
        assert claim_check["status"] == "failed"  # the agent's false success claim is exposed


def test_wrong_record_report_explains_what_happened(run_task):
    report = run_task("Update the email address of employee ID 1042 to new@example.com", fault="wrong_record")
    assert report["summary"] == "The agent modified employee 1043 instead of employee 1042."
    assert "employee 1043" in [s["label"] for s in report["side_effects"]]


def test_audit_trigger_rows_are_not_side_effects(run_task):
    report = run_task("Update the email address of employee ID 1042 to new@example.com")
    deps = [c for c in report["changes"] if c["kind"] == ChangeKind.PERMITTED_DEPENDENCY.value]
    assert {c["collection"] for c in deps} == {"employees", "audit_log"}
    assert report["side_effects"] == []


def test_idempotent_update_is_verified_with_note(run_task):
    report = run_task("Update the email address of employee ID 1042 to hana.dubois@acme.example")
    assert report["verdict"] == "VERIFIED"
    field = next(c for c in report["checks"] if c["id"] == "field_matches:email")
    assert "already had this value" in field["explanation"]


def test_nonexistent_target_fails(run_task):
    report = run_task("Update the email of employee 9999 to x@acme.example")
    assert report["verdict"] == "FAILED"
    assert any(r["code"] == "TARGET_NOT_FOUND" for r in report["reasons"])


def test_environment_changed_between_run_and_verify_is_inconclusive(service):
    a = service.create_execution(instruction="Delete customer 112", environment="database", reset_sandbox=True)
    service.run_execution(a, approved=True)
    b = service.create_execution(instruction="Delete customer 113", environment="database")
    service.run_execution(b, approved=True)
    report = service.verify_execution(a)
    assert report["verdict"] == "INCONCLUSIVE" and report["decision_rule"] == "R1"
    assert service.verify_execution(b)["verdict"] == "VERIFIED"


def test_approval_and_clarification_gates(service):
    from app.services.execution_service import ExecutionError

    exec_id = service.create_execution(instruction="Delete customer 112", environment="database")
    with pytest.raises(ExecutionError) as e:
        service.run_execution(exec_id)
    assert e.value.code == "approval_required"
    amb = service.create_execution(instruction="Update the employee's email", environment="database")
    with pytest.raises(ExecutionError) as e:
        service.run_execution(amb)
    assert e.value.code == "clarification_required"
    service.run_execution(amb, force=True)
    assert service.verify_execution(amb)["verdict"] == "INCONCLUSIVE"
    with pytest.raises(ExecutionError) as e:
        service.verify_execution(amb)
    assert e.value.code == "already_verified"


def test_inapplicable_fault_is_rejected(service):
    from app.services.execution_service import ExecutionError

    with pytest.raises(ExecutionError) as e:
        service.create_execution(instruction="Delete customer 112", environment="database", fault_mode="duplicate_operation")
    assert e.value.code == "fault_not_applicable"


def test_verification_engine_never_sees_the_fault_mode():
    """The engine's only inputs are observable artefacts."""
    fields = {f.name for f in dataclasses.fields(VerificationInput)}
    assert fields == {"execution_id", "plan", "environment", "before", "after", "trace", "window_intact", "window_detail"}
    for module in ("engine", "checks", "side_effect_detector", "evidence_validator", "verdict_engine", "state_comparator"):
        tree = ast.parse(inspect.getsource(importlib.import_module(f"app.verification.{module}")))
        identifiers: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
            elif isinstance(node, ast.arg):
                identifiers.add(node.arg)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                identifiers.update(a.name for a in node.names)
                identifiers.add(getattr(node, "module", "") or "")
        assert not [i for i in identifiers if re.search(r"(^|_|\.)fault", i.lower())], module
    assert VerificationEngine.verify.__code__.co_argcount == 2
