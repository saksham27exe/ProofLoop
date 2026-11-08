# Contributing to ProofLoop

Thanks for helping make AI agents accountable.

## Development setup

```powershell
.\setup.ps1 -Dev          # creates .venv and installs pytest + ruff
.\.venv\Scripts\Activate.ps1
pytest                    # 237 tests, ~1 minute
ruff check .
```

## Ground rules

1. **Deterministic verification first.** Equality, counts, checksums and state comparisons never use an LLM.
2. **The engine sees only evidence.** Do not pass agent objects, fault modes or harness metadata into
   `app/verification`. `tests/verification/test_fault_scenarios.py` enforces this.
3. **Missing evidence is never failure** and never success – it is INCONCLUSIVE.
4. **Local by default; no telemetry, vendor SDKs or Docker.** Cloud inference exists only behind the admin
   consent flow (`app/services/runtime_settings.py`); a key in `.env` must never enable it.
   `tests/security/test_security.py` scans for vendor SDKs and unexpected non-local URLs.
5. **Never execute LLM-generated SQL or shell.** Table/column names are whitelisted; values are bound parameters.
6. **No real data.** Use the synthetic generators in `scripts/generate_sample_data.py`.
7. **Add tests with every change**, and update the README/docs when behaviour changes.

## Adding a verification environment

Implement `Environment`, `Toolbox`, `StateInspector` and `trace_refs` (see `app/environments/base.py`),
describe entities/fields in an `EnvironmentSchema`, register it in `EnvironmentRegistry`, and add
scenarios plus fault-matrix tests.

## Adding an agent framework adapter

Map the framework's tool-call callbacks onto `TraceCollector.tool_call/tool_response/error/claim`
(or wrap its tools with `InstrumentedToolbox`). See `examples/custom_agent.py`.

Agents outside the ProofLoop process use verification sessions and the SDK (`sdk/proofloop_sdk.py`,
`docs/own_agents.md`). Keep the SDK a single file whose only dependency is `httpx`.

## Pull requests

Keep changes focused, explain *why*, include test output, and never commit `.env` or `data/`.
