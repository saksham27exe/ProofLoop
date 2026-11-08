# Privacy and data storage

ProofLoop is local-first. Local inference alone does not guarantee privacy, so every network path is listed here.

## What leaves the machine

| When | What | Where |
|---|---|---|
| Setup only | Python packages | PyPI (`pip install -r requirements.txt`) |
| Setup only (optional) | Ollama installer | ollama.com (you download it) |
| Setup only (optional, after confirmation) | Model weights | Ollama registry via `ollama pull` / `scripts/download_model.py` |
| Normal operation (default, local model) | **Nothing** | – |
| Only if an admin enabled the **cloud model** | Task instructions (redacted), data-source structure, rows/file contents the AI agent reads, report narrative facts (if enabled) | The cloud endpoint (default NVIDIA NIM, `integrate.api.nvidia.com`) |

With the default local backend, no prompts, instructions, traces, tool arguments, API responses, file contents,
reports or credentials are ever sent to an external service. There is no telemetry, crash reporting or analytics.

### The opt-in cloud model

Switching to the cloud model is a deliberate administrator decision: the server refuses it unless the request
carries `cloud_consent=true`, i.e. the administrator accepted the notice in `app/llm/cloud_notice.py`, which states
that data leaves the machine, that the provider's terms apply, and that **the user is solely responsible** for
that decision and its consequences. The acceptance (who, when, notice version, endpoint) is stored in
`data/proofloop.db` and written to the audit log; switching back is audited too. The backend can never be set
from `.env`. The API key lives only in `.env`, is held as a secret in memory, sent only in the `Authorization`
header over HTTPS, and never stored in the database, returned by the API or written to logs.

Verification itself never uses the cloud: snapshots, comparisons and verdicts stay local and deterministic.

## Enforced, not just promised

* `PROOFLOOP_OLLAMA_URL` must be a loopback address; `Settings` refuses anything else unless
  `PROOFLOOP_ALLOW_REMOTE_OLLAMA=true` is set deliberately.
* The Ollama provider re-checks the URL and uses `trust_env=False`, so `HTTP(S)_PROXY` variables cannot
  route prompts through a proxy.
* There is no automatic fallback to the cloud. A test fails if a vendor SDK import or an unexpected remote URL
  appears in `app/` or `frontend/`; the cloud endpoint may appear only in `app/config.py` and
  `app/llm/cloud_provider.py`. Another test proves the cloud backend cannot be enabled from the environment.
* `.streamlit/config.toml` sets `gatherUsageStats = false` and binds to `127.0.0.1`; `run.ps1` passes the same flags.
* The API binds to `127.0.0.1` by default.

## Where data is stored

Everything is under `data/` (git-ignored):

| Path | Contents |
|---|---|
| `data/proofloop.db` | Users (hashed passwords), session token hashes, audit log, executions, traces, plans, snapshots, checks, evidence, reports |
| `data/sandbox/` | Synthetic sandbox systems (company database, ticket service, files) |
| `data/practice/` | The fictional Practice store database |
| `data/backups/` | A copy of a **connected data source** taken before each AI-agent run (for "Undo run") |
| `data/benchmark/` | Benchmark results (JSON) |
| `data/logs/` | Local rotating log files (no request payloads) |

Delete it all with `python scripts\initialize_db.py --reset` (asks for confirmation).

## Redaction

Before a trace event is stored, keys such as `password`, `token`, `api_key`, `authorization`, `secret`,
`ssn`, `card_number`, `connection_string` are replaced with `***REDACTED***`, and values that look like
bearer tokens, `sk-…` keys, AWS keys, private keys, SSN- or card-shaped numbers and database URLs are
masked. Instructions are also redacted before they are sent to the local model.

## Connected (real) data

When you connect your own database or folder, **copies of its contents are stored locally inside `data/`**:
the before/after snapshots of every verified run (in `data/proofloop.db`, kept as immutable evidence) and the
pre-run backups (`data/backups/`). The connected data is sent to the model only as far as the agent's
tool calls return it (rows it looks up, files it reads). With the local backend that never leaves the machine;
**with the cloud backend those rows and files are sent to the cloud provider.** Protect the `data/`
folder like the source itself, and delete old backups you no longer need.

Disconnecting a source never touches the source; it only removes ProofLoop's connection settings.

## Sample data

All sample people, companies, e-mails (`.example` domain) and phone numbers (555-01xx) - in the sandboxes and
the Practice store - are fictional. Start with a **copy** of real data; ProofLoop is not designed or audited for
live production systems.

## Browser session cookie

"Keep me signed in" stores the session token in a `SameSite=Strict` cookie for `localhost:8501` until it
expires (12 h by default) or you sign out (the token is then revoked server-side). Leave it unticked on shared computers.
