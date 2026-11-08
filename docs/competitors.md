# How ProofLoop compares

Researched 2026-11-08 from public product pages, vendor comparisons and papers. Vendor-written comparisons are
marked as such; treat their verdicts with caution.

## The landscape

| Category | Examples | What they do | Gap ProofLoop addresses |
|---|---|---|---|
| Observability & evaluation platforms | LangSmith, Langfuse, Arize Phoenix, Braintrust, AgentOps, Patronus AI, Galileo, Maxim | Record traces/spans of LLM and tool calls; score outputs and trajectories, mostly with LLM-as-judge; CI gates (Braintrust) | They judge what the agent **said and logged**. None inspects the real system the agent changed, so a wrong-record update with a "200 OK" and a confident summary looks green. LLM judges are non-deterministic. Several are cloud-only or proprietary; LangSmith is strongest inside LangChain. |
| Guardrails / runtime security | Invariant Labs Guardrails & Gateway (reported acquired by Snyk), NeMo Guardrails | Rule-based policies over traces; a proxy that blocks disallowed tool calls | Rules see the *call*, not the *outcome*: a policy-compliant call that hits the wrong row passes. |
| State-based benchmarks (research) | τ-bench / τ²-bench, AppWorld, STAGE-Claw, Proxy State-Based Evaluation (ACL 2026) | Compare the final database state with an annotated goal state | Closest in spirit, but they are **benchmarks** with purpose-built environments, not a tool for your agent on your data. Proxy State-Based Evaluation even replaces the real state with an LLM's inference of it. |

Sources: [LangSmith alternatives (Latitude, vendor)](https://latitude.so/blog/langsmith-alternatives-for-ai-agents),
[Galileo (vendor)](https://galileo.ai/blog/best-langsmith-alternatives),
[Arize (vendor)](https://arize.com/?p=32038),
[Braintrust alternatives (Latitude, vendor)](https://latitude.so/blog/best-braintrust-alternatives-ai-agent-evaluation),
[agent eval infrastructure 2026](https://www.agenticwire.news/article/agent-eval-infrastructure-2026),
[Invariant docs](https://explorer.invariantlabs.ai/docs),
[Invariant on PyPI](https://pypi.org/project/invariant-ai/),
[AppWorld](https://arxiv.org/pdf/2407.18901),
[τ-bench review](https://beancount.io/bean-labs/research-logs/2026/06/12/tau-bench-tool-agent-user-interaction-real-world-domains),
[Proxy State-Based Evaluation](https://arxiv.org/abs/2602.16246v2),
[STAGE-Claw](https://arxiv.org/pdf/2606.10394).

## Where ProofLoop stands

**Unique:** independent before/after snapshots through a read-only channel, deterministic verdict rules
(no LLM decides), side-effect detection with attribution to logged tool calls, hash-chained tamper-evident
traces, three-state verdicts that refuse to guess, local-first.

**Weaknesses found, and what was done:**

| Weakness (vs. competitors) | Status |
|---|---|
| Could only verify its *own* built-in agent - every competitor works with any framework | **Added:** verification sessions + single-file Python SDK; any agent or language over HTTP (`docs/own_agents.md`) |
| No preventive guardrails (Invariant blocks calls) | **Added:** per-source write policies - read-only tables/paths, protected columns, no-delete / no-insert - enforced before a call reaches the data |
| Free cloud tiers fail under rate limits | **Added:** automatic retry with `Retry-After` / backoff on HTTP 429 |
| No CI regression gate (Braintrust) | Open - next candidate: a suite file of tasks + expected verdicts, run from CI with a non-zero exit code |
| Only SQLite and folders (competitors are data-agnostic because they never look at data) | Open - PostgreSQL/MySQL and spreadsheets are the next connectors |
| No OpenTelemetry trace import (Phoenix, Langfuse) | Open - import OTel spans as reported tool calls |
