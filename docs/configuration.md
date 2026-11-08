# Configuration

All settings come from environment variables prefixed with `PROOFLOOP_`, usually set in `.env` (copied from
`.env.example` by `setup.ps1`; git-ignored). Model settings can also be changed by an administrator on
**Settings → Model** in the dashboard.

## Model backends

ProofLoop uses a language model for two things only: **interpreting free-form tasks** and **driving the AI agent**.
Verdicts never use a model. Two backends are available:

| | Local (default) | Cloud (opt-in) |
|---|---|---|
| Provider | [Ollama](https://ollama.com) on `127.0.0.1` | [NVIDIA NIM](https://build.nvidia.com) (OpenAI-compatible) |
| Privacy | Nothing leaves the machine | Task text, data-source structure and the rows the agent reads are sent to the provider |
| Cost | Free | Free developer key, ~40 requests/minute |
| Speed (CPU laptop) | 15-60 s per task | 5-20 s per task |
| Quality | `qwen2.5:3b` handles simple tasks, makes real mistakes | Much better at tool use and ambiguity |
| Enabled by | Default | An admin on **Settings → Model**, after accepting the cloud-data notice |

Without any model, ProofLoop still works in **verification-only mode**: tasks written in the documented grammar
are interpreted by deterministic rules (see the [user guide](user_guide.md#supported-phrasing)).

### Local model (Ollama)

1. Install Ollama from <https://ollama.com/download>; it listens on `127.0.0.1:11434`.
2. Download a model once: `ollama pull qwen2.5:3b` (≈1.9 GB), or run `scripts\download_model.py`, which asks first.
3. ProofLoop detects it automatically. Do not expose Ollama beyond localhost (`OLLAMA_HOST=0.0.0.0`).

| Setting | Default | Meaning |
|---|---|---|
| `PROOFLOOP_OLLAMA_MODEL` | `qwen2.5:3b` | Any installed Ollama model (`llama3.2:3b`, `qwen2.5:7b`, …) |
| `PROOFLOOP_OLLAMA_URL` | `http://127.0.0.1:11434` | Must be loopback unless `PROOFLOOP_ALLOW_REMOTE_OLLAMA=true` |
| `PROOFLOOP_OLLAMA_TIMEOUT_SECONDS` | `180` | Per call; CPU-only machines and the first call (model load) are slow |
| `PROOFLOOP_OLLAMA_MAX_RETRIES` | `2` | Retries when the model returns output that fails the JSON schema |

### Cloud model (NVIDIA NIM)

1. Create a free key at <https://build.nvidia.com> (NVIDIA account → *Get API Key*).
2. Add `PROOFLOOP_CLOUD_API_KEY=nvapi-...` to `.env` and restart ProofLoop.
3. As an administrator open **Settings → Model**, choose **Cloud - NVIDIA NIM**, read the notice, tick the
   acceptance box and click **Switch to cloud model**.

**What you accept** (full text in [`app/llm/cloud_notice.py`](../app/llm/cloud_notice.py)): your data leaves the
machine and the provider's terms apply; don't send personal, confidential or regulated data unless authorised;
the agent can still make mistakes; **you are solely responsible** for the decision and its consequences.

**Safeguards:** a key in `.env` never enables the cloud by itself (only the consent flow can, and a test enforces
it) · the server refuses the switch without `cloud_consent=true` · the acceptance (who, when, notice version) is
stored and written to the audit log · HTTPS only · the key is never stored in the database, returned by the API or
logged, and `nvapi-` keys are redacted from traces · a red *Cloud LLM* indicator and a warning on **New Execution**
while it is active · HTTP 429 is retried with `Retry-After` / backoff.

| Setting | Default | Meaning |
|---|---|---|
| `PROOFLOOP_CLOUD_API_KEY` | *(none)* | NVIDIA key (`nvapi-…`); keep it in `.env` only |
| `PROOFLOOP_CLOUD_MODEL` | `nvidia/nemotron-3.5-lightning-30b-a3b` | Must support tool calling; changeable on the Settings page |
| `PROOFLOOP_CLOUD_BASE_URL` | `https://integrate.api.nvidia.com/v1` | Any OpenAI-compatible HTTPS endpoint |
| `PROOFLOOP_CLOUD_TIMEOUT_SECONDS` | `120` | Per call |

**Models.** NVIDIA adds and retires models often, so the Settings page reads the **live catalog** (a public list -
no key or data is sent) and flags models that are no longer offered. Recommended (all list function calling on
build.nvidia.com as of 2026-11-08):

| Model | Notes |
|---|---|
| `nvidia/nemotron-3.5-lightning-30b-a3b` | Default. Built for agents; fast (3B active parameters) |
| `nvidia/nemotron-3.5-super-vl-120b-a12b` | Stronger reasoning and tool use; slower |
| `deepseek-ai/deepseek-v4.1-flash` | Alternative with function calling and long context |

These are reasoning models: ProofLoop strips their `<think>` output and allows 4,096 output tokens per call.

## Interpretation

| Setting | Default | Meaning |
|---|---|---|
| `PROOFLOOP_INTERPRETER_MODE` | `auto` | `auto`: documented grammar by rules instantly, free-form wording by the model · `llm`: always ask the model (cross-checked by rules) · `rules`: never use a model |
| `PROOFLOOP_LLM_ENABLED` | `true` | Master switch for model features |
| `PROOFLOOP_LLM_NARRATIVE_ENABLED` | `false` | Adds a clearly labelled AI narrative to reports (never affects the verdict) |

The model only *proposes* an interpretation (JSON schema enforced). Deterministic guard rails
([`app/interpretation/llm_guard.py`](../app/interpretation/llm_guard.py)) decide what is used:

1. **Validation** - entities, fields and values are checked against the data source's structure; the verification
   requirements are derived by ProofLoop, never written by the model.
2. **Grounding** - every id, name, e-mail, phone number and text the model proposes must literally appear in the
   instruction; otherwise the plan is flagged as a possible hallucination.
3. **Reconciliation** - when the rule grammar can also read the instruction and the model disagrees, the
   deterministic reading wins and the disagreement is shown.
4. **Prompt-injection markers** (`</instruction>`, "ignore previous rules", `System:` …) are flagged; a
   model-only plan for such an instruction needs explicit human confirmation.

## Storage, network and accounts

| Setting | Default | Meaning |
|---|---|---|
| `PROOFLOOP_DATA_DIR` | `data` | Database, sandboxes, backups, reports and logs (relative to the project) |
| `PROOFLOOP_API_HOST` / `PROOFLOOP_API_PORT` | `127.0.0.1` / `8000` | Backend binding |
| `PROOFLOOP_SESSION_TTL_HOURS` | `12` | Sign-in session lifetime |
| `PROOFLOOP_MAX_FAILED_LOGINS` | `5` | Failed sign-ins before a temporary lockout |
| `PROOFLOOP_LOCKOUT_MINUTES` | `5` | Lockout duration |
| `PROOFLOOP_LOG_LEVEL` | `INFO` | Log verbosity (`data\logs\`) |

`run.ps1` options: `-NoBrowser`, `-ApiPort 8010`, `-UiPort 8510`.
