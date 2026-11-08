# User guide

Start ProofLoop with `.\run.ps1`, open <http://localhost:8501> and create the administrator account on the first
visit. Every page is reachable from the sidebar.

## 1. Real data with the AI agent

1. Open **Data Sources**. The *Practice store* - a fictional shop with 120 customers, 28 products, 300 orders and
   25 employees, including deliberate traps such as three customers named *Maria Garcia* - is connected out of the
   box. **Browse the data** shows every table read-only. Administrators can connect their own SQLite file or folder
   here (**start with a copy**) and set a **write policy**.
2. Open **New Execution → Real data · AI agent**, pick a source and describe a task, e.g.
   *"Give every customer in Oslo the gold tier"*.
3. ProofLoop shows how it understood the task - which records, which values, which checks - before anything runs.
   Bulk changes and deletions need your approval; ambiguous tasks are flagged instead of guessed.
4. **Run agent & verify**: a backup is taken, the agent works with real tool calls, and the verifier compares
   independent before/after snapshots. The result shows what the agent *claimed* next to what *actually happened*.
5. Not happy? **Undo this run** restores the source from the backup (most recent run per source).

More: [real_data.md](real_data.md) - safety model, bulk tasks, results with a real model.

## 2. Your own agent

**New Execution → Your own agent** shows a ready-to-copy snippet. Your agent (any framework or language) runs the
task inside a *verification session* and gets the same verdict. Full guide: [own_agents.md](own_agents.md).

## 3. Demo sandboxes with fault injection

1. **New Execution → Guided scenarios**, then **Run scenario** on *Wrong record modified*. ProofLoop resets the
   sandbox, the agent updates employee **1043** instead of **1042** and claims success.
2. The result dialog shows **FAILED - "The agent modified employee 1043 instead of employee 1042"** next to the
   agent's claim.
3. **Verification Results** lists every check (e.g. `field_matches:email` failed: expected `new@example.com`,
   observed the old value), the side effect on employee 1043 *attributed to tool call `call_3`*, the before/after
   table and the integrity-hashed evidence. Export JSON or Markdown.
4. **Execution Monitor** shows the hash-chained timeline: plan → `db_get` → `db_update` → claim.
5. Try *Missing execution evidence*: the state is correct but the agent emitted no tool telemetry → **INCONCLUSIVE**,
   because nothing attributes the change to the agent.
6. **Sandbox task** lets you type your own instruction, pick an environment and an agent behaviour (nine fault modes).

## Supported phrasing

With a model (local or cloud), free-form wording is accepted. Without one, the deterministic interpreter
understands a documented grammar, for example:

* *Update the phone number of customer 105 to +1-555-0199*
* *Create a new employee record with name John Doe and department Engineering*
* *Close ticket 12* · *Assign ticket 4 to Priya Sharma*
* *Create a file reports/a.txt with content 'x'* · *Delete the file logs/agent.log*
* *Set the tier of all customers where city is Oslo to gold* · *Delete order 12*

## Verdicts

| Verdict | Returned when | Example |
|---|---|---|
| **VERIFIED** | Every decisive requirement is established by direct evidence, there are no unacceptable side effects, and a consistent trace accounts for the change | Correct update with an intact trace |
| **FAILED** | Direct evidence establishes a violation: postcondition unmet, wrong or missing target, duplicate, side effect, constraint violated | API 503 + "success" claim · wrong record · truncated file |
| **INCONCLUSIVE** | Evidence is missing, contradictory or ambiguous, or the system cannot be observed - **never silently turned into FAILED or VERIFIED** | No trace · logs say "success" *and* "rolled back" · outage · two employees named *Alex Kim* |

Decision policy (first matching rule wins): **R1** environment changed between run and verification →
INCONCLUSIVE · **R2** ambiguous task → INCONCLUSIVE · **R3** any decisive check failed → FAILED · **R4** any decisive
check inconclusive → INCONCLUSIVE · **R5** nothing established → INCONCLUSIVE · **R6** all decisive checks passed →
VERIFIED. Informational checks (e.g. *"agent claim matches outcome"*) never decide. Details:
[verification_engine.md](verification_engine.md).

## Scripts

| Command | Purpose |
|---|---|
| `python scripts\check_environment.py` | Readiness check (Python, packages, ports, Ollama) |
| `python scripts\initialize_db.py --reset` | Wipe all local data (asks first) |
| `python scripts\create_user.py --username alice --role analyst` | Create a user from the command line |
| `python scripts\run_benchmark.py` | Run the benchmark |
| `python scripts\evaluate_llm_interpreter.py --mode llm` | Measure how well a model interprets 34 labelled instructions |
| `python examples\own_agent.py` | Verify a correct and a buggy agent through the SDK |
| `pytest` · `ruff check .` | Tests and lint (`setup.ps1 -Dev` installs them) |
