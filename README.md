<div align="center">

# ProofLoop

**Independent outcome verification for AI agents.**

*Don't trust what an AI agent says it did. Verify what actually happened.*

Local-first · Optional cloud model (opt-in) · No Docker · No telemetry · Windows-first (Python 3.11+)

</div>

---

## Table of contents

1. [Introduction](#1-introduction)
2. [The problem](#2-the-problem)
3. [How ProofLoop works](#3-how-proofloop-works)
4. [Architecture](#4-architecture)
5. [Core features](#5-core-features)
6. [Technology stack](#6-technology-stack)
7. [System requirements](#7-system-requirements)
8. [Installation](#8-installation)
9. [Ollama setup](#9-ollama-setup-optional)
10. [Local model configuration](#10-local-model-configuration)
11. [Running ProofLoop](#11-running-proofloop)
12. [Sample walkthrough](#12-sample-walkthrough)
13. [Verdicts explained](#13-verdicts-explained)
14. [REST API](#14-rest-api)
15. [Benchmarking](#15-benchmarking)
16. [Security & privacy architecture](#16-security--privacy-architecture)
17. [Limitations](#17-limitations)
18. [Roadmap](#18-roadmap)
19. [Contributing](#19-contributing)
20. [License](#20-license)

---

## 1. Introduction

ProofLoop is an **AI Agent Outcome Verification and Reliability Framework**. When an autonomous agent
says *"Successfully updated the employee's email address"*, ProofLoop does not take its word for it. It:

* interprets the original instruction into a structured, validated **verification plan**;
* captures an append-only, **hash-chained execution trace** of every tool call the agent makes;
* takes **independent before/after snapshots** of the real system through a **read-only** channel;
* compares expected vs. actual state with **deterministic rules** (no LLM in the loop);
* detects **unintended side effects**, duplicates, partial and wrong-record changes;
* produces an **evidence-backed report** with one of three verdicts: **VERIFIED**, **FAILED** or **INCONCLUSIVE**.

It works in two modes:

* **Real data with a real AI agent.** Connect your own SQLite database or folder (or use the built-in
  *Practice store*, a realistic fictional shop database). A genuine tool-calling agent on your local model
  explores the data, decides its own steps and makes the change; ProofLoop verifies the outcome independently -
  including tasks on *every record matching a condition* ("give every customer in Oslo the gold tier").
  A backup is taken before every run, so any run can be undone. See [docs/real_data.md](docs/real_data.md).
* **Demo sandboxes with fault injection.** Three synthetic systems (a SQLite company database, a local REST
  ticketing API and a file workspace) and a scripted agent that can be told to misbehave in nine ways, for
  reproducible demonstrations and the benchmark.

It is a **local, single-user-machine application** with a dashboard, sign-in, a REST API, a reproducible
benchmark and a 218-test suite. It is **not** an enterprise or production-certified product.

## 2. The problem

> "Update the email address of employee ID 1042 to new@example.com."
> The agent replies: **"Successfully updated the employee's email address."**

Any of the following may actually be true:

| What really happened | Agent's claim | Tool response | What ProofLoop reports |
|---|---|---|---|
| The record was updated correctly | success | 200 OK | **VERIFIED** |
| The API failed, the agent claimed success anyway | success | 503 | **FAILED** – state unchanged |
| Employee **1043** was updated instead | success | 200 OK | **FAILED** – "modified employee 1043 instead of 1042" |
| The right record changed, but another one did too | success | 200 OK | **FAILED** – side effect, attributed to the tool call |
| The change happened, but nothing in the trace explains it | success | (none) | **INCONCLUSIVE** – missing execution evidence |
| The verifier cannot read the system right now | success | 200 OK | **INCONCLUSIVE** – state unavailable |

The central question ProofLoop answers: **can the outcome be established from independent evidence –
without relying on the agent's statements, its logs or tool responses?**

## 3. How ProofLoop works

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant PL as ProofLoop orchestrator
    participant I as Interpreter (local LLM / rules)
    participant V as Plan validator
    participant Insp as Read-only inspector
    participant A as Agent
    participant T as Trace collector
    participant E as Verification engine
    U->>PL: natural-language task
    PL->>I: interpret (untrusted draft)
    I-->>V: draft {operation, target, values}
    V-->>PL: validated plan + deterministic requirements
    PL->>U: review plan (approval for destructive ops)
    PL->>Insp: BEFORE snapshot (hash)
    PL->>A: run task
    A->>T: tool calls / responses / claim (append-only, hash-chained)
    PL->>Insp: AFTER snapshot (hash)
    PL->>E: plan + snapshots + trace (nothing else)
    E-->>PL: checks, side effects, evidence, verdict
    PL-->>U: immutable report (JSON / Markdown)
```

The engine **never** receives the agent object, the fault-injection setting or any other harness
knowledge – this is enforced by a test that inspects the engine's inputs and source.

## 4. Architecture

```mermaid
flowchart LR
    subgraph UI["Streamlit dashboard :8501 (localhost)"]
      Login[Sign-in / accounts]
      Pages[Overview · New execution · Monitor · Results · History · Benchmark · Sandbox · Settings]
    end
    subgraph API["FastAPI backend :8000 (localhost)"]
      Auth[Auth & RBAC]
      Orch[Execution orchestrator]
      Interp[Task interpretation]
      Engine[Verification engine]
      Report[Report generator]
      Bench[Benchmark runner]
    end
    subgraph Store["ProofLoop evidence store (SQLite, append-only tables)"]
      DB[(proofloop.db)]
    end
    subgraph Sandboxes["Synthetic environments"]
      SQL[(company.db)]
      Tickets[(Ticket REST API)]
      Files[(files/)]
    end
    Ollama[[Ollama :11434 localhost]]
    UI -- bearer token --> API
    Interp -. local only .-> Ollama
    Orch -- agent toolbox (read/write) --> Sandboxes
    Engine -- inspector (read-only) --> Sandboxes
    API --> DB
```

| Module | Responsibility |
|---|---|
| `app/interpretation` | Rule parser, Ollama-backed interpreter, LLM guard rails (grounding, injection flags, reconciliation), strict plan validator |
| `app/agent` | Sample agent (scripted or local-LLM planner), 10 fault modes, framework-independent adapter |
| `app/collector` | Append-only, SHA-256 hash-chained trace storage with redaction |
| `app/environments` | SQLite DB (with audit triggers), WSGI ticket API (agent vs. read-only verifier tokens), file sandbox |
| `app/verification` | Snapshots, diff, side-effect classification, evidence ledger, requirement checks, verdict policy |
| `app/reporting` | Structured JSON report with report hash; Markdown export |
| `app/auth` | PBKDF2 passwords, hashed session tokens, lockout, roles, audit log |
| `app/benchmark` | Reproducible dataset, baselines, metrics, runner |
| `frontend/` | Streamlit dashboard (talks to the API only) |

More detail: [docs/architecture.md](docs/architecture.md) · [docs/verification_engine.md](docs/verification_engine.md)

## 5. Core features

* **Three-state verdicts** from an explicit, ordered policy (`R1`–`R6`) – never from an LLM.
* **Independent state inspection**: read-only SQLite (`mode=ro`), read-only API token, file checksums.
* **Expected-vs-actual comparison** with documented equality rules (e-mail/enum case-insensitive, file content byte-exact).
* **Side-effect detection** that understands *legitimate dependencies* (`updated_at`, trigger-written audit rows) and attributes changes to logged tool calls.
* **Evidence ledger**: every item categorised as *direct, indirect, agent-reported, derived, missing* or *contradictory* and integrity-hashed. A check can only pass on **direct** evidence.
* **Trace integrity**: hash chain + SQLite triggers that reject `UPDATE`/`DELETE` on evidence tables.
* **Fault injection**: tool failure + false claim, wrong record, duplicate, partial, side effect, missing evidence, fabricated success, contradictory logs, inspector outage.
* **Human in the loop**: plan review, mandatory approval for destructive operations, ambiguity flags instead of guesses.
* **Local LLM (optional)**: Ollama with JSON-schema structured output, retries, timeouts, value grounding,
  prompt-injection flags and a deterministic cross-check that the model cannot override. Works fully **without** an LLM ("verification-only mode").
* **Accounts & access control**: first-run admin setup, roles (viewer / analyst / admin), lockout, password change/reset, "keep me signed in", audit log.
* **Dashboard**: overview KPIs and charts, guided scenarios, plan review dialog, execution timeline, verdict reports with evidence explorer, history with export, benchmark, sandbox explorer, settings.

### Screenshots

| | |
|---|---|
| ![Overview](docs/screenshots/overview.jpg) **Overview** – KPIs, verdict trend, false claims caught | ![Guided scenarios](docs/screenshots/new_execution.jpg) **New execution** – one-click fault scenarios |
| ![Result](docs/screenshots/result_dialog.jpg) **Result** – the agent's claim vs. what actually happened | ![Report](docs/screenshots/verification_results.jpg) **Verification results** – verdict, reasons, evidence |
| ![Checks](docs/screenshots/checks.jpg) **Checks** – every deterministic check with its evidence ids | ![Monitor](docs/screenshots/execution_monitor.jpg) **Execution monitor** – hash-chained trace timeline |
| ![Benchmark](docs/screenshots/benchmark.jpg) **Benchmark** – ProofLoop vs. self-report vs. tool responses | |

## 6. Technology stack

| Layer | Technology | Why |
|---|---|---|
| Language | Python 3.11+ | Core implementation |
| Backend | FastAPI + Uvicorn | REST API with OpenAPI docs |
| Dashboard | Streamlit + Altair | Local developer UI |
| Database | SQLite + SQLAlchemy 2 | Zero-install persistence |
| Validation | Pydantic 2 | Schemas for plans, evidence, reports |
| Local LLM | Ollama (Qwen 2.5 / Llama 3.2) | Optional task interpretation |
| HTTP | HTTPX | Local service calls (proxy env ignored) |
| Tests | Pytest, Ruff | 218 tests; lint clean on Python 3.11 syntax |

Deliberately **not** used: Docker, Redis, Celery, PostgreSQL, vector databases, LangChain, vendor LLM SDKs
(the optional cloud model is called with plain HTTPX).
The orchestration is plain Python because it is simpler and fully deterministic for this MVP.

## 7. System requirements

* Windows 10/11 (primary target). The Python code is cross-platform; scripts are PowerShell.
* Python **3.11 or newer** (tested with 3.12) and Git.
* ~500 MB disk for dependencies.
* Optional: [Ollama](https://ollama.com/) + a small model (`qwen2.5:3b` ≈ 1.9 GB download, 8 GB RAM recommended).
* Keep the project in a short path (e.g. `C:\ProofLoop`) – Windows' 260-character path limit can break packages in deep folders. `setup.ps1` warns you.

## 8. Installation

```powershell
git clone https://github.com/<you>/ProofLoop.git
cd ProofLoop
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

`setup.ps1` finds Python 3.11+, creates `.venv`, installs `requirements.txt`, creates `.env`, initialises
the SQLite database and sandboxes, checks Ollama and **asks before** downloading any model.
Options: `-SkipModel` (never ask), `-Dev` (install test tools), `-Python <path>`.

Manual equivalent:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python scripts\initialize_db.py
```

## 9. Ollama setup (optional)

1. Install Ollama from <https://ollama.com/download> and start it (it listens on `127.0.0.1:11434`).
2. Download a model **once** (internet needed only for this step):
   ```powershell
   ollama pull qwen2.5:3b
   ```
   or `.\.venv\Scripts\python.exe scripts\download_model.py` (asks for confirmation).
3. ProofLoop detects it automatically. Without Ollama everything still works in
   **verification-only mode** using the deterministic interpreter.

Do not expose Ollama beyond localhost (do not set `OLLAMA_HOST=0.0.0.0`).

## 10. Local model configuration

Set in `.env` or on **Settings → Model** (admin):

| Setting | Default | Meaning |
|---|---|---|
| `PROOFLOOP_OLLAMA_MODEL` | `qwen2.5:3b` | Any installed Ollama model (`llama3.2:3b`, `qwen2.5:7b`, ...) |
| `PROOFLOOP_INTERPRETER_MODE` | `auto` | `auto` (documented grammar instantly by rules, free-form wording by the LLM), `llm` (always ask the LLM, cross-checked by rules), `rules` |
| `PROOFLOOP_OLLAMA_URL` | `http://127.0.0.1:11434` | Must be loopback unless `PROOFLOOP_ALLOW_REMOTE_OLLAMA=true` |
| `PROOFLOOP_OLLAMA_TIMEOUT_SECONDS` | `180` | Per model call; CPU-only machines and the first call (model load) are slow |
| `PROOFLOOP_LLM_NARRATIVE_ENABLED` | `false` | Adds a clearly labelled AI narrative to reports (never affects the verdict) |

### Optional: cloud model (NVIDIA NIM)

A larger cloud model interprets free-form tasks and drives the AI agent more reliably than a 3B model on a CPU.
ProofLoop supports **NVIDIA NIM** (free developer key, rate-limited to about 40 requests/minute; one task uses
at most ~11 calls) through its OpenAI-compatible API. **Verdicts are not affected**: they are always computed
locally by deterministic rules.

1. Create a free API key at <https://build.nvidia.com> (NVIDIA account → *Get API Key*).
2. Add it to `.env` and restart ProofLoop:
   ```
   PROOFLOOP_CLOUD_API_KEY=nvapi-...
   ```
3. As an administrator open **Settings → Model**, choose **Cloud - NVIDIA NIM**, read the notice, tick the
   acceptance box and click **Switch to cloud model**.

**Consequences you accept:** task instructions, the structure of connected data sources and the actual rows and
file contents the AI agent reads are sent to the provider, whose terms then apply; the agent can still make
mistakes that change or delete data; and **you are solely responsible** for that decision and its consequences
(the full notice is in `app/llm/cloud_notice.py`). ProofLoop is provided "as is" (MIT License).

Safeguards: a key in `.env` alone never enables the cloud (only the admin consent flow can); the server refuses
the switch without `cloud_consent=true`; the acceptance (who, when, notice version) is stored and audited; HTTPS
is required; the key is never stored in the database, returned by the API or logged, and `nvapi-` keys are
redacted from traces; the dashboard shows a red *Cloud LLM* indicator and a warning on **New Execution** while
it is active. Switch back to **Local** at any time.

| Setting | Default | Meaning |
|---|---|---|
| `PROOFLOOP_CLOUD_API_KEY` | *(none)* | NVIDIA NIM key (`nvapi-…`). Keep it in `.env` only (git-ignored) |
| `PROOFLOOP_CLOUD_MODEL` | `meta/llama-3.3-70b-instruct` | Must support tool calling for the AI agent; changeable on the Settings page |
| `PROOFLOOP_CLOUD_BASE_URL` | `https://integrate.api.nvidia.com/v1` | Any OpenAI-compatible HTTPS endpoint |
| `PROOFLOOP_CLOUD_TIMEOUT_SECONDS` | `120` | Per call |

The LLM only proposes an interpretation (JSON schema enforced). Deterministic guard rails then decide what
is used (`app/interpretation/llm_guard.py`):

1. **Validation** – the plan validator checks entities, fields and values against the environment catalogue
   and derives the verification requirements itself (the LLM never writes them).
2. **Grounding** – every id, name, e-mail, phone number and text the model proposes must literally appear
   in the instruction; otherwise the plan is flagged as a possible hallucination and is not verifiable.
3. **Reconciliation** – when the strict rule grammar can also read the instruction and the model disagrees,
   the deterministic reading is kept and the disagreement is shown. The model's reading is used where it adds
   value: free-form wording the grammar cannot parse.
4. **Prompt-injection markers** (`</instruction>`, "ignore previous rules", `System:` …) are flagged; an
   LLM-only plan for such an instruction needs explicit human confirmation.

With no GPU, a 3B model runs on the CPU: expect roughly 10–30 s per interpretation (the first call also loads
the model). Use `rules` mode for instant, reproducible interpretation of the documented grammar.

## 11. Running ProofLoop

```powershell
.\run.ps1
```

* Dashboard: **http://localhost:8501** – the first visit asks you to create the administrator account.
* API: **http://127.0.0.1:8000** – interactive docs at **http://localhost:8000/docs** (use *Authorize*).
* `Ctrl+C` stops both processes. Logs: `data\logs\`.
* Options: `-NoBrowser`, `-ApiPort 8010`, `-UiPort 8510`.

Manual start (two terminals, venv activated):

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000
streamlit run frontend/app.py
```

Other scripts:

| Command | Purpose |
|---|---|
| `python scripts\check_environment.py` | Readiness check (Python, packages, ports, Ollama) |
| `python scripts\initialize_db.py --reset` | Wipe all local data (asks first) |
| `python scripts\create_user.py --username alice --role analyst` | Create a user from the CLI |
| `python scripts\run_benchmark.py` | Run the benchmark |
| `python scripts\evaluate_llm_interpreter.py --mode llm` | Measure how well a local model interprets 34 labelled instructions (grammar, free-form, vague, prompt injection) |
| `python examples\custom_agent.py` | Verify your own Python agent |
| `pytest` | Run the test suite (`setup.ps1 -Dev` installs pytest) |

## 12. Sample walkthrough

### Real data with the AI agent

1. Sign in and open **Data Sources**. The *Practice store* (customers, products, orders, order items,
   employees - all fictional) is connected out of the box; **Browse the data** shows every table read-only.
   Administrators can connect their own SQLite file or folder there (**start with a copy**).
2. Open **New Execution → Real data · AI agent**, pick a source and type a task, e.g.
   *"Give every customer in Oslo the gold tier"*. ProofLoop shows how it understood the task - which records,
   which values, the deterministic checks - before anything runs. Bulk changes and deletions need your approval.
3. **Run agent & verify**: a backup is taken, the AI agent works on the data with real tool calls (on a CPU each
   step takes 10-60 s), then the verifier compares independent before/after snapshots. The result shows what the
   agent *claimed* next to what *actually happened*.
4. Not happy? **Undo this run** restores the data from the backup (most recent run per source).

### Demo sandboxes with fault injection

1. Sign in, open **New Execution → Guided scenarios**.
2. Click **Run scenario** on *Wrong record modified*. ProofLoop resets the sandbox, the agent updates
   employee **1043** instead of **1042** and claims success.
3. The result dialog shows **FAILED – "The agent modified employee 1043 instead of employee 1042"** next to
   the agent's claim.
4. **Verification Results** lists every check (e.g. `field_matches:email` failed: expected
   `new@example.com`, observed the old value), the side effect on employee 1043 *attributed to tool call
   `call_3`*, the before/after record table, and 12 integrity-hashed evidence items. Export JSON or Markdown.
5. **Execution Monitor** shows the hash-chained timeline: plan → `db_get` → `db_update` → claim.
6. Try *Missing execution evidence*: the state is correct, but the agent emitted no tool telemetry →
   **INCONCLUSIVE**, because nothing attributes the change to the agent.
7. **Custom task** lets you type your own instruction, pick an environment and an agent behaviour, review
   the interpreted plan, approve it and run.

Supported phrasing in verification-only mode (examples): *"Update the phone number of customer 105 to
+1-555-0199"*, *"Create a new employee record with name John Doe and department Engineering"*, *"Close
ticket 12"*, *"Assign ticket 4 to Priya Sharma"*, *"Create a file reports/a.txt with content 'x'"*,
*"Delete the file logs/agent.log"*. With a local LLM, free-form phrasing is accepted.

## 13. Verdicts explained

| Verdict | Returned when | Example |
|---|---|---|
| **VERIFIED** | Every decisive requirement is established by direct evidence, no unacceptable side effects, and a consistent trace accounts for the change | Correct update with intact trace |
| **FAILED** | Direct evidence establishes a violation: postcondition unmet, wrong/missing target, duplicate, side effect, constraint violated | API 503 + "success" claim; wrong record; truncated file |
| **INCONCLUSIVE** | Evidence is missing, contradictory, ambiguous or the system cannot be observed – **never silently converted into FAILED or VERIFIED** | No trace; logs say "success" *and* "rolled back"; API outage; two employees named *Alex Kim* |

Decision policy (first matching rule wins): **R1** environment changed between run and verification →
INCONCLUSIVE · **R2** ambiguous task → INCONCLUSIVE · **R3** any decisive check failed → FAILED ·
**R4** any decisive check inconclusive → INCONCLUSIVE · **R5** nothing established → INCONCLUSIVE ·
**R6** all decisive checks passed → VERIFIED. Informational checks (e.g. *"agent claim matches outcome"*)
never decide.

## 14. REST API

All endpoints except `/health` and `/auth/bootstrap*` require `Authorization: Bearer <token>`.

| Method | Endpoint | Purpose | Role |
|---|---|---|---|
| GET | `/health` | Liveness/readiness | public |
| GET | `/health/llm` · `/models` | Local LLM status, installed models | viewer |
| POST | `/auth/bootstrap` · `/auth/login` · `/auth/token` | First admin · sign in · OAuth2 form (Swagger) | public |
| POST | `/auth/logout` · `/auth/change-password` · GET `/auth/me` | Session management | any |
| GET/POST/PATCH | `/users`, `/users/{id}`, `/users/{id}/reset-password` · GET `/audit` | User admin, audit log | admin |
| POST | `/executions` | Create execution + interpret plan | analyst |
| GET | `/executions` · `/executions/{id}` | List (filter by verdict/env/text) · details | viewer |
| GET | `/executions/{id}/traces` · `/executions/{id}/snapshots` | Hash-chained trace · snapshot hashes | viewer |
| POST | `/executions/{id}/run` | Run agent (`approved`, `force`, `verify`) | analyst |
| POST | `/executions/{id}/verify` | Verify (immutable report) | analyst |
| POST | `/executions/{id}/undo` | Restore a connected source to the backup taken before this run | analyst |
| GET/POST/DELETE | `/sources` · `/sources/{id}` | List / connect (tested before saving) / disconnect data sources | viewer / admin / admin |
| POST | `/sources/{id}/refresh` · `/sources/practice/reset` | Re-read structure · regenerate practice store | analyst |
| GET | `/executions/{id}/report` · `/reports/{id}/export?format=json\|markdown` | Report · download (integrity header) | viewer |
| GET | `/stats` · `/scenarios` · `/options` | Dashboard data | viewer |
| GET/POST | `/sandbox/{env}` · `/sandbox/{env}/reset` | Inspect / reset sandbox | viewer / analyst |
| GET/PUT | `/settings` | Runtime settings | viewer / admin |
| POST/GET | `/benchmark/run` · `/benchmark/latest` | Benchmark | analyst / viewer |

```powershell
$t = (Invoke-RestMethod -Method Post http://127.0.0.1:8000/auth/login -ContentType application/json `
      -Body '{"username":"admin","password":"<your password>"}').access_token
$h = @{ Authorization = "Bearer $t" }
$e = Invoke-RestMethod -Method Post http://127.0.0.1:8000/executions -Headers $h -ContentType application/json `
      -Body '{"instruction":"Close ticket 12","environment":"api","fault_mode":"tool_failure_false_claim"}'
(Invoke-RestMethod -Method Post "http://127.0.0.1:8000/executions/$($e.id)/run" -Headers $h `
      -ContentType application/json -Body '{"verify":true}').report.summary
```

## 15. Benchmarking

`python scripts\run_benchmark.py` (or the **Benchmark** page) generates a reproducible synthetic dataset
(seed 42: 174 cases across 3 environments and 10 behaviours), **really runs** every case and scores three
systems against harness ground truth:

* **Baseline A** – trust the agent's final claim. **Baseline B** – trust state-changing tool responses.
* **ProofLoop** – independent inspection + comparison + side-effect detection + evidence validation.

Measured on 2026-11-07 (rule-based interpreter, Python 3.12, Windows 11):

| System | Failure recall | Unsafe acceptance | False-positive rate | Precision | Inconclusive | Verdict accuracy |
|---|---|---|---|---|---|---|
| **ProofLoop** | **100.0%** | **0.0%** | **0.0%** | **100.0%** | 28.7% | **100.0%** |
| Baseline A (self-report) | 0.0% | 100.0% | 0.0% | n/a | 0.0% | 12.1% |
| Baseline B (tool responses) | 22.3% | 77.7% | 30.4% | 52.3% | 0.0% | 25.3% |

Side-effect detection precision/recall 100%/100%, evidence coverage 100%, verification latency
p50 12 ms / p95 26 ms, cost per verification $0 (all local).

**Read these numbers carefully.** The faults are synthetic and were designed together with the verifier,
so the results demonstrate that the mechanisms work as specified; they are **not** an accuracy claim for
real-world agents. Of the 69 genuinely successful runs, 48 are deliberately stripped of evidence (missing
trace, contradictory logs, outage) and are therefore – correctly – INCONCLUSIVE, not VERIFIED. Methodology,
metric definitions and per-fault results: [docs/benchmarking.md](docs/benchmarking.md).

**Interpretation with a real local model** (`scripts\evaluate_llm_interpreter.py`, `qwen2.5:3b` on CPU,
34 hand-labelled instructions incl. free-form wording, vague tasks and prompt injections):

| Mode | Correct | Wrong plans | Notes |
|---|---|---|---|
| `rules` | 26/34 | 0 | refuses the 8 free-form instructions it cannot parse |
| `auto` (default) | **34/34** | **0** | 15 model calls, p50 30 s each on CPU |
| `llm` | 33/34 | 0 | 1 model call timed out (CPU throttling) |

Testing the real model found that it obeys prompt injections and sometimes invents or misplaces values; the
deterministic guard rails described in §10 were built and regression-tested from those findings.

## 16. Security & privacy architecture

* **No external inference by default.** Only Ollama on loopback; non-local Ollama URLs are refused in config and
  in the provider; system proxy variables are ignored for LLM calls; no vendor SDKs (enforced by a test). The
  optional cloud model is off until an administrator accepts the cloud-data notice (see §10); while it is on,
  task and agent data **do** leave the machine.
* **No telemetry.** None implemented; Streamlit usage statistics are disabled.
* **Localhost bindings.** API `127.0.0.1:8000`, dashboard `127.0.0.1:8501`.
* **Least privilege.** The verifier reads through a read-only SQLite connection and a read-only API token;
  the agent never gets a handle to the evidence store.
* **Tamper evidence.** Hash-chained trace, integrity-hashed evidence, report hash, append-only triggers.
* **Untrusted inputs.** Instructions, traces and tool output are data; the LLM never writes SQL/shell, table
  and column names are whitelisted, paths are confined to the sandbox, prompt-injection attempts cannot
  change the verdict policy (tested).
* **Secrets.** Sensitive keys/values are redacted before traces are stored; `.env` and `data/` are git-ignored;
  no secrets are needed at all.
* **Accounts.** PBKDF2-SHA256 (310k iterations), hashed session tokens with expiry, lockout after 5
  failures, role-based access, audit log, forced password change after admin resets.
* "Keep me signed in" stores the session token in a `SameSite=Strict` cookie for this origin; untick it on shared machines.

Network use is limited to setup (pip packages, Ollama installer, model weights). Details: [docs/privacy.md](docs/privacy.md).

## 17. Limitations

* Snapshots are full copies of the connected tables, so a source is limited to 50,000 rows by default
  (configurable; larger sources are refused as INCONCLUSIVE rather than checked partially). Exclude big
  log tables, or connect a sub-folder.
* Connected sources: **SQLite files and folders** today. PostgreSQL/MySQL, spreadsheets and web APIs are not yet
  supported.
* One change per task: create/update/delete/read of a single record, or update/delete of **every record matching
  a condition**. Multi-step workflows ("create an order and reduce stock") and appends are not yet interpreted.
* The AI agent is only as good as the local model. With `qwen2.5:3b` on a CPU it completes simple tasks but
  makes real mistakes (which ProofLoop then reports); larger models (`qwen2.5:7b`, `llama3.1:8b`) are more
  reliable but slower without a GPU.
* "Undo run" restores the whole source to its state before the most recent run; changes made by other
  applications in the meantime would be reverted too (keep sources exclusive during runs).
* The deterministic interpreter understands a documented grammar; free-form text needs the local LLM. Small
  models misread some instructions and can be steered by prompt injection – ProofLoop's guard rails turn such
  readings into "needs review" rather than a wrong plan, but always review the plan before approving it.
  On CPU-only machines a 3B model takes roughly 10–60 s per free-form instruction.
* Real-framework adapters (LangGraph, CrewAI, AutoGen) are interfaces only; see `examples/custom_agent.py`.
* One API worker process (sandbox locks are in-process). Run verification right after execution; otherwise
  rule R1 yields INCONCLUSIVE when the environment changed in between.
* Benchmark results come from synthetic faults (see §15).

## 18. Roadmap

Multi-agent verification · LangGraph / CrewAI / AutoGen adapters · Python SDK · continuous reliability
scoring per agent · adversarial scenario generation · human review queue for INCONCLUSIVE results ·
signed, externally anchored evidence logs · incremental snapshots for large systems.

## 19. Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Run `pytest` and `ruff check .` before opening a pull request.
Never commit real company data, secrets or the `data/` folder.

## 20. License

[MIT](LICENSE)
