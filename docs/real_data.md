# Real data and the AI agent

ProofLoop's demo sandboxes use a scripted agent and injected faults. **Real-data mode** is the opposite: a
genuine AI agent works on data you connect, nothing is scripted or injected, and ProofLoop verifies what really
happened.

## 1. Connecting data

**Data Sources** (dashboard, administrators) or `POST /sources`:

| Type | What is connected | What the agent can do |
|---|---|---|
| SQLite file (`.db`, `.sqlite`, ...) | Every table (minus the ones you hide) | `list_tables`, `describe_table`, `find_rows`, `count_rows`, `insert_row`, `update_rows`, `delete_rows` |
| Folder | Text files in the folder and its sub-folders (hidden/tool folders such as `.git`, `node_modules` skipped) | `list_files`, `read_file`, `write_file`, `delete_file` |

The **Practice store** (`data/practice/store.db`) is connected out of the box: 120 customers, 28 products,
300 orders, ~600 order items and 25 employees - all fictional (`.example` e-mail addresses, 555-01xx phone
numbers), with CHECK constraints, foreign keys (`order_items -> orders ON DELETE CASCADE`) and deliberate traps
(three customers named *Maria Garcia*). **Reset practice data** regenerates it.

Before a source is saved, ProofLoop takes a read-only snapshot to prove access works. It refuses relative paths,
non-SQLite files, and ProofLoop's own data folder.

### Structure discovery (SQLite)

Tables, primary keys (or SQLite row ids), column types, `NOT NULL`, defaults, `CHECK (col IN (...))` value
lists and foreign keys are read from the database itself. Column names like `updated_at`/`created_at` are treated
as system-managed. Tables written by triggers (audit logs) are hidden automatically, because every change would
otherwise create "side effects" in them. **Re-read structure** after you change the schema.

## 2. Safety model

| Risk | Protection |
|---|---|
| The agent damages data | A **backup before every run** (SQLite online-backup API / zip of the folder); **Undo this run** restores it (most recent run per source). Bulk changes and deletions require **approval**. |
| The agent changes too much | Updates and deletes **require a condition** (never "the whole table"); one call may change at most *N* rows (default 200, per source). |
| SQL injection / path traversal | The agent never sends SQL: table and column names are validated against the discovered schema, values are bound parameters. Paths are confined to the folder. |
| The verifier is fooled | ProofLoop reads through a **separate read-only connection** (`mode=ro`, `PRAGMA query_only`). The agent's claims and tool responses are recorded but never treated as proof. |
| Huge sources | Snapshots above the row limit (default 50,000) are **refused - INCONCLUSIVE**, never checked partially. |
| Other apps write at the same time | Mark the source *shared* (untick "Nothing else writes to this data"): unexplained changes become *uncertain* (INCONCLUSIVE) - unless the agent's own tool call touched that record, which attributes it to the agent. |

**Start with a copy.** ProofLoop is not designed or audited for live production systems.

## 3. Tasks it understands

* **One record:** create, update, delete or read - by id (*"Set the stock of product 3 to 75"*) or by a unique
  field (*"... the customer named Ana Silva ..."*; several matches -> INCONCLUSIVE, never a guess).
* **Every record matching a condition:** *"Give every customer in Oslo the gold tier"*, *"Delete all cancelled
  orders with a total under 100"*. Conditions support `= != < <= > >= contains`.

In `auto` mode the strict rule grammar handles documented phrasing instantly
(*"Set the tier of all customers where city is Oslo to gold"*); everything else is interpreted by the local
LLM under the guard rails described in the README (grounding, injection flags, reconciliation).

Not yet: several operations in one task, appends, PostgreSQL/MySQL, spreadsheets, web APIs.

## 4. How a bulk task is verified

1. The verifier computes the **selection itself** from the independent *before* snapshot - never from the
   agent's queries. A wrong filter by the agent therefore shows up as missed or extra records.
2. `targets_selected` - at least one record matched (none -> INCONCLUSIVE: nothing to verify).
3. `bulk_field_matches:<field>` - every selected record has the requested value afterwards (lists the ones
   that do not); for deletions `all_targets_removed`.
4. `no_unrequested_target_changes` / `no_changes_to_other_records` - nothing else changed. Foreign-key
   consequences declared in the database (`ON DELETE CASCADE` / `SET NULL`) of an *expected* deletion are
   recognised as permitted dependencies; anything else is a side effect.
5. `execution_evidence_present` - successful tool calls in the hash-chained trace reference every changed record.

## 5. The AI agent

`app/agent/tool_agent.py` runs the local model with Ollama's native tool calling: it receives your task, the
source's tables/columns and the tools above, calls one tool at a time, sees each result and ends with a
`DONE:` / `FAILED:` report. It is deliberately **not** given ProofLoop's verification plan.

Robustness measures, each added after watching `qwen2.5:3b` fail in a real run:

* The tools accept the shapes small models produce (`where={"total": ">900"}`, `{"column", "op", "value"}`
  objects, HTML-escaped operators); values that start with an operator (`"=paid"`) are refused with a hint.
* When the runtime drops a tool call it cannot parse (empty reply), the agent asks once in text mode and parses
  the JSON call itself.
* Identical calls repeated more than twice are refused; at most 10 steps.

The agent still makes real mistakes - ProofLoop's job is to catch them.

## 6. Results with a real model

Measured on 2026-11-08 with `qwen2.5:3b` on a CPU-only Windows laptop, Practice store, `auto` interpretation,
through the REST API (`real_e2e` run; every run verified, then the last one undone):

| Task | Understood as | What the AI agent actually did | Verdict |
|---|---|---|---|
| Change the email of customer 5 to jonas.m@newmail.example | rules: customer 5, email | `update_rows where id=5` (after the text-mode recovery of a dropped tool call) | **VERIFIED** |
| Give every customer in Oslo the gold tier | LLM: every customer where city = 'Oslo' | updated the 9 Oslo customers | **VERIFIED** |
| Mark all pending orders with a total above 900 as paid | LLM: status = 'pending' and total > 900 | first sent `"status": "=paid"` (refused by the tool with a hint), then updated the 15 matching orders | **VERIFIED** |
| Set the stock of product 3 to 75 | rules: product 3, stock | `update_rows` with a condition object | **VERIFIED** |
| Delete all cancelled orders with a total under 100 | LLM: every order where status = 'cancelled' and total < 100 | deleted 2 orders; the database cascaded to their order lines | **VERIFIED** (cascade recognised) |
| Update the email of the customer named Maria Garcia to ... | rules: name = 'Maria Garcia' - 3 matches | tried to update all three; the database's UNIQUE rule blocked it | **INCONCLUSIVE** (ambiguous target) |

Each task took 15-50 s (interpretation + agent + verification) on this machine. The same cascade-delete task
was also run end-to-end from the dashboard (approve -> run -> VERIFIED -> **Undo this run**).

**What the earlier runs taught us** (each item is now covered by a test in `tests/verification/test_real_data.py`):

* A correct bulk delete was reported **FAILED** because the database's `ON DELETE CASCADE` removed the order
  lines - the verifier now reads declared cascades. An unrelated child deletion is still a side effect.
* Twice the agent "ended without a report": Ollama dropped a tool call it could not parse. Fixed by the
  text-mode fallback.
* The agent passed condition objects where plain values were expected and wrote `">900"` / `"=pending"` into
  filters - the tools now accept these shapes.
* The interpreter echoed the condition into the new values (`"total": "*gt;900"`) and invented a column
  (`status_change_date`) - both are now removed by deterministic repairs, never by trusting the model.

Across all runs ProofLoop **never accepted a wrong outcome**: every FAILED or INCONCLUSIVE matched the data;
the one wrong verdict (the cascade false alarm) was too strict, not too lenient, and is fixed.

Reproduce: start the app, open **New Execution -> Real data · AI agent**, and run the example tasks; or call the
API as shown in the README. Results depend on the model and hardware; a 3B model on a CPU is the low end.
