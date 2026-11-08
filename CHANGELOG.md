# Changelog

## [Unreleased]

### Added
- **Verify your own agent:** verification sessions over REST (`/executions/{id}/session/*`) and a single-file
  Python SDK (`sdk/proofloop_sdk.py`) with framework-ready tools and OpenAI-style tool definitions. One session per
  environment, a 60-minute expiry, verification even when the agent crashes. Example: `examples/own_agent.py`.
- **Write policies:** per-source read-only tables or paths, protected columns, no deletes and no inserts, enforced
  for ProofLoop's agent and external agents.
- **"Your own agent" tab** on New Execution and a policy editor on Data Sources.
- Retry for cloud HTTP 429 responses (honours `Retry-After`).
- Documentation: user guide, configuration, REST API, own agents, competitor analysis, docs index; README images.
- GitHub Actions CI (Windows, Python 3.11 and 3.12), issue and PR templates, security policy.

### Changed
- Model messages in the dashboard name the active backend instead of always saying "local LLM".

## [0.1.0] - 2026-11-08

### Added
- Deterministic verification engine (rules R1-R6), read-only snapshots, side-effect attribution, evidence ledger,
  hash-chained trace, Markdown and JSON reports.
- ProofLoop's tool-calling agent on connected SQLite sources and the practice store; backups and undo.
- Demo SQL, REST and file sandboxes with nine fault modes; reproducible benchmark.
- Local model through Ollama with interpretation guard rails; opt-in NVIDIA NIM cloud model behind an admin
  consent flow.
- Accounts, roles, lockout, audit log; Streamlit dashboard; FastAPI backend.
