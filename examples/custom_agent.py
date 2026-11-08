"""Verify YOUR OWN Python agent with ProofLoop (framework-independent adapter demo).

The agent below is an ordinary Python function that receives a toolbox. It is
deliberately buggy: it updates the right employee but also "tidies up" another
record. ProofLoop wraps the toolbox so every tool call is traced, captures
independent before/after snapshots and returns an evidence-backed verdict.

    python examples/custom_agent.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.agent_adapter import NativePythonAdapter  # noqa: E402
from app.collector.trace_collector import TraceCollector  # noqa: E402
from app.collector.trace_storage import TraceStorage  # noqa: E402
from app.config import Settings  # noqa: E402
from app.database.connection import Database  # noqa: E402
from app.environments.registry import EnvironmentRegistry  # noqa: E402
from app.interpretation.plan_validator import build_plan  # noqa: E402
from app.interpretation.rule_parser import parse_instruction  # noqa: E402
from app.verification.engine import VerificationEngine, VerificationInput  # noqa: E402
from app.verification.state_inspector import capture_snapshot  # noqa: E402


def my_agent(instruction: str, tools) -> tuple[str, str]:
    """A hand-written agent with a hidden bug."""
    tools.call("db_update", {"table": "employees", "record_id": 1042, "values": {"email": "new@example.com"}})
    tools.call("db_update", {"table": "employees", "record_id": 1001, "values": {"title": "Temp"}})  # oops
    return "success", "Done! Employee 1042 now has the new email."


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(data_dir=tmp)
    settings.ensure_dirs()
    db = Database(settings.db_path)
    db.create_all()
    env = EnvironmentRegistry(settings.sandbox_dir, settings.sample_data_dir).get("database")
    env.reset()

    instruction = "Update the email address of employee ID 1042 to new@example.com"
    plan = build_plan(parse_instruction(instruction, env.schema), env.schema, instruction, "rules")

    from app.database.models import Execution

    with db.session() as s:
        s.add(Execution(id="exec_custom0001", task_description=instruction, environment="database", agent_name="my-agent"))

    storage = TraceStorage(db)
    before = capture_snapshot(env, "before")
    agent = NativePythonAdapter(my_agent, env.toolbox(), name="my-agent")
    agent.run(instruction, TraceCollector(storage, "exec_custom0001"))
    after = capture_snapshot(env, "after")

    outcome = VerificationEngine().verify(VerificationInput(
        execution_id="exec_custom0001", plan=plan, environment=env, before=before, after=after,
        trace=storage.load("exec_custom0001"),
    ))
    print(f"Verdict: {outcome.verdict.value}")
    for line in outcome.diagnosis:
        print(f"  - {line}")
    db.dispose()


if __name__ == "__main__":
    main()
