# Verifying your own agent

Any agent - LangChain, LangGraph, CrewAI, AutoGen, the OpenAI or Anthropic SDKs, plain Python, or another
language over HTTP - can be verified by ProofLoop without being rewritten.

## Python (SDK)

`sdk/proofloop_sdk.py` is a single file whose only dependency is `httpx`. Copy it into your project.

```python
from proofloop_sdk import ProofLoop

pl = ProofLoop("http://127.0.0.1:8000", username="alice", password="...")
with pl.session("Give every customer in Oslo the gold tier", source="src_practice",
                agent_name="my-agent", framework="langgraph") as s:
    tools = s.tools()            # {"find_rows": fn, "update_rows": fn, ...} - register with your framework
    specs = s.openai_tools       # OpenAI-style function definitions for tool-calling models
    ...                          # your agent runs here
    s.finish("DONE: ...")        # the agent's own report (recorded, never trusted)

print(s.verdict, s.report["summary"])
```

If the block raises, the session is still finished and verified - what a crashing agent already changed
matters most. Runnable demo with a correct and a buggy agent: `examples/own_agent.py`.

### Two ways to show ProofLoop the work

1. **ProofLoop's tools** (recommended): `s.call(name, **args)`, `s.tool(name)`, `s.tools()`. Each call is checked
   against the source's write policy and recorded in the hash-chained trace automatically.
2. **Your own tools**: the agent changes the data itself and reports each change with
   `s.record("update_rows", {...args}, {"table": "customers", "keys": [5]})`. Include the touched records in the
   output so changes can be attributed. A change with **no** reported call is INCONCLUSIVE ("missing execution
   evidence") - ProofLoop does not assume the agent did it.

## REST (any language)

| Step | Endpoint |
|---|---|
| Interpret the task | `POST /executions` with `{"instruction", "environment", "agent_config": "external"}` |
| Start (backup + before-snapshot; returns tool specs and the active policy) | `POST /executions/{id}/session/start` with `{"approved", "force", "agent_name", "framework"}` |
| Call a ProofLoop tool | `POST /executions/{id}/session/tools/{tool}` with `{"arguments": {...}}` |
| Report your own tool calls / notes | `POST /executions/{id}/session/events` with `{"events": [...]}` |
| Finish and verify (returns the report) | `POST /executions/{id}/session/finish` with `{"claim", "claimed_status"}` |
| Abandon without verifying | `POST /executions/{id}/session/abort` |

All endpoints need `Authorization: Bearer <token>` (`POST /auth/login`) and the analyst role. Interactive docs:
<http://localhost:8000/docs> → *external agents*.

## Rules that keep the verdict honest

* **One session per environment.** While a session runs, no other session and no ProofLoop agent may write to
  that environment. If something in ProofLoop changes it anyway (e.g. a practice-store reset), the verdict is
  INCONCLUSIVE because the before/after window no longer reflects the agent alone.
* **Sessions expire after 60 minutes** without `finish`; the next session on that environment marks it aborted.
* Destructive tasks need `approve=True`; ambiguous tasks need `force=True` and end INCONCLUSIVE.
* Connected sources are backed up before the session starts; **Undo run** on the results page restores it.

## Write policies (guardrails)

**Data Sources → Write policy** (admins) or `PUT /sources/{id}/policy`:

```json
{"read_only": ["products"], "protected_columns": ["customers.email"], "allow_delete": false, "allow_insert": true}
```

For folders, `read_only` holds path prefixes. A blocked call fails with *"Blocked by policy: …"* before it reaches
the data and is recorded in the trace. Policies bind ProofLoop's own AI agent and external agents using
ProofLoop's tools. An agent that writes directly is not blocked - but its unrequested changes still show up as side
effects in the verdict.
