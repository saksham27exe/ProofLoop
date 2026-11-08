# The verification engine

`app/verification/engine.py` is the core of ProofLoop. Its only inputs are:

```python
VerificationInput(execution_id, plan, environment, before, after, trace, window_intact, window_detail)
```

No agent object, no fault mode, no network. A test asserts this.

## Pipeline

1. **Evidence ledger** – every artefact becomes an `EvidenceItem` with id, category, source, resource,
   result, data and a SHA-256 over its canonical JSON.
2. **Snapshots** – before/after state captured by the read-only inspector (DIRECT evidence, or MISSING
   when unavailable). Stored snapshots are re-hashed before use.
3. **Trace assessment** – hash chain re-verified; per call id: responses, conflicting statuses, success
   followed by an error event for the same call, orphan responses, calls without responses; which records
   each call references (`Environment.trace_refs`); the agent's claimed status.
4. **Diff** – record-level added/removed/modified with per-field deltas.
5. **Target resolution** – by key or by field match against the BEFORE state (0 = not found, >1 = ambiguous).
   For creates: added records fully matching the expected values (first = created, rest = duplicates).
6. **Change classification** – `expected`, `permitted_dependency`, `duplicate`, `side_effect`, `uncertain`.
7. **Requirement checks** – each plan requirement id maps to one deterministic function (`checks.py`).
8. **Direct-evidence enforcement** – a passing state check that cites no DIRECT evidence is downgraded to INCONCLUSIVE.
9. **Verdict policy** – ordered rules R1–R6 (`verdict_engine.py`).
10. **Consistency checks** (informational) – do tool responses agree with the state? does the claim match the verdict?
11. **Diagnosis** – plain-language statements derived from the checks (wrong target, partial, no effect,
    duplicates, side effects, false claims) and next steps per reason code.

## Requirements by operation

| Operation | Requirements |
|---|---|
| CREATE record | `record_created`, `field_matches:<f>`…, `exactly_one_new_record`, `no_changes_to_other_records` |
| UPDATE record | `target_identified`, `field_matches:<f>`…, `no_unrequested_target_changes`, `no_changes_to_other_records` |
| DELETE record | `target_identified`, `target_removed`, `no_changes_to_other_records` |
| READ | `target_identified`, `no_state_change` |
| CREATE file | `file_exists`, `content_sha256_matches`, `size_matches`, [`no_accidental_overwrite`], `exactly_one_new_record`, `no_changes_to_other_records` |
| UPDATE file | `target_identified`, `file_exists`, `content_sha256_matches`, `size_matches`, `no_changes_to_other_records` |
| any mutation | `execution_evidence_present`, `trace_consistent` |

## Comparison rules

* `None` equals only `None`; integers compare numerically.
* E-mail and enum fields: trimmed, case-insensitive. Enum inputs are normalised at plan time (`in progress` → `in_progress`).
* File content: byte-exact via SHA-256 and size (UTF-8, no newline translation).
* Everything else: trimmed, case-sensitive string equality.
* An UPDATE whose target already had the requested value passes with a note ("precondition already satisfied").

## Side-effect rules

* Changes on the target limited to requested fields → expected; system-managed fields (`updated_at`, `size`, `sha256`) → permitted dependency.
* Other fields on the target → side effect ("field changed although not requested").
* Rows in dependency collections (e.g. `audit_log`) are permitted when they reference a changed record;
  otherwise a side effect.
* Any other change → side effect when the environment was held exclusively during the run window
  (all built-in sandboxes), otherwise *uncertain* (INCONCLUSIVE rather than invented certainty).
* When the target is ambiguous, changes to the candidate records are *uncertain*.
* Each change lists the tool calls that referenced the record (attribution).

## Verdict policy

| Rule | Condition | Verdict |
|---|---|---|
| R1 | Environment modified after the run, before verification | INCONCLUSIVE |
| R2 | Plan ambiguous / not verifiable | INCONCLUSIVE |
| R3 | Any decisive check FAILED | FAILED |
| R4 | Any decisive check INCONCLUSIVE | INCONCLUSIVE |
| R5 | No decisive check passed | INCONCLUSIVE |
| R6 | All decisive checks passed | VERIFIED |

Design consequence: **FAILED needs an established violation; VERIFIED needs complete, consistent evidence.**
A correct state with no trace, a contradictory trace or a broken hash chain is INCONCLUSIVE – the change
cannot be attributed to the agent. A wrong state is FAILED even if the trace is missing.

## Reason codes

`POSTCONDITION_FAILED`, `TARGET_NOT_FOUND`, `DUPLICATE_OPERATION`, `SIDE_EFFECT_DETECTED`,
`CONSTRAINT_VIOLATED`, `STATE_UNAVAILABLE`, `AMBIGUOUS_TARGET`, `AMBIGUOUS_TASK`, `CONTRADICTORY_EVIDENCE`,
`TRACE_INTEGRITY_FAILURE`, `MISSING_EXECUTION_EVIDENCE`, `UNCERTAIN_SIDE_EFFECT`,
`ENVIRONMENT_WINDOW_VIOLATED`, `INSUFFICIENT_EVIDENCE`, `ALL_CHECKS_PASSED`.
