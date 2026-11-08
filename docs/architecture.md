# Architecture

ProofLoop is two local processes and a handful of SQLite files.

```
Browser ──► Streamlit dashboard (127.0.0.1:8501) ──HTTP+Bearer──► FastAPI backend (127.0.0.1:8000)
                                                                     │
                     ┌───────────────────────────────────────────────┼───────────────────────────┐
                     ▼                                               ▼                           ▼
            data/proofloop.db                           data/sandbox/*  (synthetic)       Ollama 127.0.0.1:11434
   users · sessions · audit · executions            company.db · ticket_service.db ·      (optional, never required)
   events* · plans* · snapshots* · checks* ·        files/
   evidence* · reports*   (* append-only)
```

## Separation of duties

| Actor | Can do | Cannot do |
|---|---|---|
| **Agent** (`app/agent`) | Call its environment toolbox; emit events through a `TraceCollector` | Read/modify the evidence store; see snapshots; influence the verdict policy |
| **Orchestrator** (`app/services/execution_service.py`) | Interpret, gate (approval/clarification), take snapshots, run the agent, persist results | Decide verdicts |
| **Inspector** (`Environment.inspector()`) | Read the environment through a read-only path | Write (SQLite `mode=ro`, read-only API token) |
| **Verification engine** (`app/verification`) | Turn plan + snapshots + trace into checks, evidence and a verdict | Access the agent, fault mode or network |
| **Local LLM** (`app/llm`) | Propose a structured interpretation; optionally write a labelled narrative | Execute anything, write SQL, decide verdicts |

## Execution lifecycle

```
created ─► interpreted ─┬─► running ─► completed ─► verified
                        └─► needs_clarification ─(force)─► running ...
```

1. **Create** (`POST /executions`) – the instruction is interpreted (Ollama or rules) into an
   `InterpretationDraft`, validated into a `VerificationPlan` (requirements derived deterministically),
   and stored with its hash. Destructive plans get `requires_approval`; ambiguous plans `needs_clarification`.
   An LLM draft additionally passes `app/interpretation/llm_guard.py`: structural normalisation, grounding
   (proposed ids/values must appear in the instruction), prompt-injection flags, and reconciliation with the
   rule parser – a confident deterministic reading is never overridden by a disagreeing model.
2. **Run** (`POST /executions/{id}/run`) – under the environment lock: optional sandbox reset, BEFORE
   snapshot, agent run with trace collection, environment version bump.
3. **Verify** (`POST /executions/{id}/verify`) – checks the environment version (rule R1), takes the AFTER
   snapshot, loads and re-verifies the trace hash chain, runs the engine and stores evidence, checks and the
   report (all append-only). Verifying twice is refused – reports are immutable.

## Environments

| Environment | Store | Agent tools | Verifier path | Legitimate dependencies |
|---|---|---|---|---|
| `database` | `sandbox/company.db` (employees, customers, audit_log) | `db_find/get/list_ids/insert/update/delete` (whitelisted tables/columns, bound parameters) | `sqlite3 file:...?mode=ro` | `updated_at`; `audit_log` rows written by triggers |
| `api` | WSGI ticket service over `sandbox/ticket_service.db` | `api_list/get/create/update/delete_ticket` via HTTP (in-process `httpx.WSGITransport`) | `GET /tickets` with a read-only verifier token | `created_at`, `updated_at` |
| `file` | `sandbox/files/` | `file_list/read/write/delete` (paths confined) | Directory walk with SHA-256 per file | `size`, `sha256` |

All snapshots share one shape: `{collection: {key: record}}`, so diffing and side-effect detection are
environment-agnostic. Adding an environment means implementing `Environment`, `Toolbox`,
`StateInspector` and `trace_refs`.

### Connected data sources (real data)

`app/connectors/` turns a user's SQLite file (`sqlite_source.py`) or folder (`folder_source.py`) into the same
`Environment` interface, with the schema discovered at connect time. `app/services/data_sources.py` stores the
connections (`data_sources` table), registers them in the `EnvironmentRegistry` as `src_<id>`, creates the
Practice store, and takes the pre-run backups (`run_backups` table, files in `data/backups/`).
Only the tool-calling AI agent (`app/agent/tool_agent.py`) runs on connected sources; fault injection and the
scripted agent are limited to the built-in sandboxes. Bulk tasks (`Target.scope = "all"` with `conditions`) are
resolved by the verifier from the before-snapshot (`checks.resolve`), and foreign-key cascades declared in the
source (`EnvironmentSchema.cascades`) are recognised as permitted dependencies by the side-effect detector.
Details: [real_data.md](real_data.md).

## Framework independence

`app/collector/event_schema.py` defines the event format (`execution_started`, `agent_plan`, `tool_call`,
`tool_response`, `error`, `agent_claim`, `execution_completed`, `annotation`). Any agent can be verified by
emitting these through a `TraceCollector`; `InstrumentedToolbox` does it automatically for tool calls.
See `examples/custom_agent.py`.

## Authentication

Local accounts in `proofloop.db`: PBKDF2-SHA256 hashes, random 256-bit bearer tokens stored as SHA-256,
12-hour expiry, lockout after 5 failures for 5 minutes, roles `viewer < analyst < admin`, audit log.
The first visit to the dashboard creates the administrator (`/auth/bootstrap`, allowed only while no user exists).

## Deviations from the original PRD structure (and why)

* `StateSnapshots` are stored as one row per phase (`before`/`after`) instead of one row with two columns,
  so the table can be strictly append-only.
* Dashboard views live in `frontend/views/` (not `frontend/pages/`) so Streamlit's legacy automatic
  multipage discovery does not interfere with the authenticated navigation.
* The sandbox API is a tiny WSGI app served in-process, so the agent and verifier use real HTTP
  semantics without opening another port (`python -m app.environments.api_environment` serves it standalone).
* Orchestration is plain Python (no LangGraph) – simpler, deterministic and sufficient for the MVP.
