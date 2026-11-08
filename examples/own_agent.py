"""Verify your own agent with ProofLoop - a runnable example.

Start ProofLoop first (.\\run.ps1), then:

    $env:PROOFLOOP_USER = "admin"; $env:PROOFLOOP_PASSWORD = "<your password>"
    .\\.venv\\Scripts\\python.exe examples\\own_agent.py

Two tiny "agents" do the same task on the fictional Practice store. One is correct; the other has a classic
bug (it updates the wrong customer) but still reports success - exactly what observability tools would show
as a green, successful trace. ProofLoop checks the data itself and catches it.

To plug in a real framework, hand ``session.tools()`` (plain Python functions) or ``session.openai_tools``
(OpenAI-style function specs) to LangChain / LangGraph / CrewAI / AutoGen / the OpenAI or Anthropic SDKs.
Each practice-store run is backed up first; undo it from the Verification Results page.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
from proofloop_sdk import ProofLoop, Session  # noqa: E402

CORRECT_TASK = "Change the email of customer 5 to jonas.m@newmail.example"
BUGGY_TASK = "Change the email of customer 12 to lena.k@newmail.example"


def correct_agent(s: Session) -> None:
    found = s.call("find_rows", table="customers", where={"id": 5})
    print("   agent looked up:", found["output"]["rows"][0]["name"])
    s.call("update_rows", table="customers", where={"id": 5}, values={"email": "jonas.m@newmail.example"})
    s.finish("DONE: updated the email of customer 5.")


def buggy_agent(s: Session) -> None:
    # Off-by-one bug: updates customer 13 instead of 12, then confidently reports success.
    result = s.call("update_rows", table="customers", where={"id": 13}, values={"email": "lena.k@newmail.example"})
    print("   tool response  :", "success" if result["ok"] else result["error"])
    s.finish("DONE: updated the email of customer 12.")


def main() -> None:
    user, password = os.environ.get("PROOFLOOP_USER"), os.environ.get("PROOFLOOP_PASSWORD")
    if not user or not password:
        sys.exit("Set PROOFLOOP_USER and PROOFLOOP_PASSWORD (an analyst or admin account).")
    pl = ProofLoop(os.environ.get("PROOFLOOP_URL", "http://127.0.0.1:8000"), username=user, password=password)
    for name, agent, task in (("correct agent", correct_agent, CORRECT_TASK), ("buggy agent", buggy_agent, BUGGY_TASK)):
        print(f"\n== {name}: {task}")
        with pl.session(task, source="src_practice", agent_name=name.replace(" ", "-"), framework="plain-python") as s:
            agent(s)
        print(f"   agent claimed : {s.report['agent_claim']['text']}")
        print(f"   ProofLoop says: {s.verdict} - {s.report['summary']}")
        print(f"   execution     : {s.execution_id} (open it under Verification Results)")


if __name__ == "__main__":
    main()
