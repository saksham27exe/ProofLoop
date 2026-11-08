"""The ProofLoop verification engine.

Inputs are *only* observable artefacts: the validated plan, independently
captured before/after snapshots and the execution trace. The engine never
receives the agent object, its fault mode or any other harness knowledge.

Pipeline::

    evidence ledger -> trace assessment -> state diff -> target resolution
    -> change classification (side effects) -> requirement checks
    -> direct-evidence enforcement -> verdict policy -> diagnosis/report data
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.collector.event_schema import ExecutionTrace
from app.environments.base import Environment
from app.interpretation.verification_plan import VerificationPlan
from app.utils.hashing import hash_obj
from app.utils.timeutil import Stopwatch
from app.verification.checks import CheckContext, expected_keys, resolve, run_check
from app.verification.evidence_validator import (
    EvidenceLedger,
    assess_trace,
    enforce_direct_evidence,
)
from app.verification.models import (
    ChangeKind,
    CheckResult,
    CheckStatus,
    EvidenceCategory,
    TraceAssessment,
    Verdict,
    VerdictDecision,
    VerificationOutcome,
)
from app.verification.side_effect_detector import classify_changes
from app.verification.state_comparator import (
    compact_record,
    diff_snapshots,
    values_equal,
)
from app.verification.state_inspector import SnapshotResult
from app.verification.verdict_engine import decide_verdict

D, IND, AG, DER, MIS, CON = (
    EvidenceCategory.DIRECT,
    EvidenceCategory.INDIRECT,
    EvidenceCategory.AGENT_REPORTED,
    EvidenceCategory.DERIVED,
    EvidenceCategory.MISSING,
    EvidenceCategory.CONTRADICTORY,
)

NEXT_STEPS = {
    "POSTCONDITION_FAILED": "Do not rely on the agent's success message. Inspect its tool calls in the Execution Monitor, fix the cause and re-run the task.",
    "TARGET_NOT_FOUND": "Confirm the target identifier with the requester; the record does not exist.",
    "DUPLICATE_OPERATION": "Remove the duplicate records and make the agent's create step idempotent (check before insert).",
    "SIDE_EFFECT_DETECTED": "Review and revert the unintended changes listed under Side effects, and narrow the agent's write permissions.",
    "CONSTRAINT_VIOLATED": "Restore the overwritten data from a backup and require explicit overwrite permission in the task.",
    "STATE_UNAVAILABLE": "Restore read access to the system, then re-run the task and verification together.",
    "AMBIGUOUS_TARGET": "Rephrase the task with a unique identifier (for example the record id) and run it again.",
    "NO_MATCHING_RECORDS": "Check the condition in the task - no record matched it before the run, so there was nothing to verify.",
    "AMBIGUOUS_TASK": "Clarify the instruction (operation, target and values) before running the agent.",
    "CONTRADICTORY_EVIDENCE": "Investigate the conflicting log entries; treat the outcome as unproven until a human confirms it.",
    "TRACE_INTEGRITY_FAILURE": "Treat the trace as tampered or corrupted and investigate before trusting this execution.",
    "MISSING_EXECUTION_EVIDENCE": "Enable tool-level tracing for this agent (see the adapter guide) so state changes can be attributed to it.",
    "UNCERTAIN_SIDE_EFFECT": "Have a human review the unattributed changes before accepting the result.",
    "ENVIRONMENT_WINDOW_VIOLATED": "Verify immediately after each run, or re-run this task in isolation.",
    "INSUFFICIENT_EVIDENCE": "Collect more independent evidence (state access, complete traces) and re-run.",
    "ALL_CHECKS_PASSED": "No action required. Keep this report as the audit record of the change.",
}


@dataclass
class VerificationInput:
    execution_id: str
    plan: VerificationPlan
    environment: Environment
    before: SnapshotResult
    after: SnapshotResult
    trace: ExecutionTrace
    window_intact: bool = True
    window_detail: str = ""


class VerificationEngine:
    def verify(self, inp: VerificationInput) -> VerificationOutcome:
        timer = Stopwatch()
        env, plan = inp.environment, inp.plan
        schema = env.schema
        ledger = EvidenceLedger(inp.execution_id)
        ev: dict[str, Any] = {}

        # 1. Plan ---------------------------------------------------------
        ev["plan"] = ledger.add(
            "verification_plan", DER, f"proofloop:interpreter:{plan.interpreter}", f"Verification plan: {plan.headline()}",
            resource=plan.collection or "", data={
                "operation": plan.operation, "target": plan.target.model_dump() if plan.target else None,
                "expected_state": plan.expected_state, "requirements": [r.id for r in plan.requirements],
                "plan_hash": hash_obj(plan.model_dump(mode="json")),
            },
        ).id

        # 2. Independent snapshots -----------------------------------------------
        for phase, snap in (("before", inp.before), ("after", inp.after)):
            if snap.available:
                counts = ", ".join(f"{k}: {v}" for k, v in snap.record_counts().items())
                ev[phase] = ledger.add(
                    "state_snapshot", D, snap.source,
                    f"{phase.capitalize()}-execution snapshot of {snap.resource_identifier} ({counts})",
                    resource=snap.resource_identifier, result="supports",
                    data={"phase": phase, "state_hash": snap.state_hash, "record_counts": snap.record_counts(),
                          "captured_at": snap.captured_at.isoformat()},
                ).id
            else:
                ev[phase] = ledger.add(
                    "state_snapshot", MIS, snap.source, f"{phase.capitalize()}-execution snapshot unavailable: {snap.error}",
                    resource=snap.resource_identifier, result="unavailable", data={"phase": phase, "error": snap.error},
                ).id

        # 3. Execution trace (indirect evidence) ----------------------------------------
        ta = assess_trace(inp.trace.events, inp.trace.integrity, env.trace_refs, schema.mutating_tools)
        tool_evidence = self._tool_evidence(inp, ledger, schema.mutating_tools)
        ev["tools"] = list(tool_evidence.values())
        if ta.tool_calls == 0 and ta.tool_responses == 0:
            ev["missing_trace"] = ledger.add(
                "execution_trace", MIS, "trace:agent", "No tool invocation events were recorded for this execution.",
                result="unavailable", data={"total_events": ta.total_events},
            ).id
        ev["integrity"] = ledger.add(
            "trace_integrity", DER if ta.chain_valid else CON, "proofloop:trace-verifier",
            f"Hash-chain verification over {ta.total_events} event(s): {ta.chain_detail}",
            result="supports" if ta.chain_valid else "contradicts",
            data={"valid": ta.chain_valid, "events": ta.total_events, "detail": ta.chain_detail},
        ).id
        ev["contradictions"] = [
            ledger.add("log_contradiction", CON, "proofloop:trace-analyzer", text, result="contradicts").id
            for text in ta.contradictions
        ]
        if ta.claim_present:
            ev["claim"] = ledger.add(
                "agent_claim", AG, "agent", ta.claim_text or "", result="neutral",
                data={"claimed_status": ta.claimed_status, "note": "An agent's claim is never treated as proof."},
            ).id
        else:
            ev["claim"] = ledger.add("agent_claim", MIS, "agent", "The agent did not report an outcome.", result="unavailable").id

        # 4. Before/after comparison ------------------------------------------------
        diff = None
        if inp.before.available and inp.after.available:
            diff = diff_snapshots(inp.before.state or {}, inp.after.state or {})
            ev["diff"] = ledger.add(
                "state_diff", D, "verifier:state-comparator",
                f"Before/after comparison found {len(diff.changes)} changed record(s).",
                resource=env.resource_identifier, result="supports",
                data={"summary": diff.summary(), "changes": [
                    {"collection": c.collection, "key": c.key, "type": c.change_type,
                     "fields": {f: d.model_dump() for f, d in c.changed_fields.items()} if c.change_type == "modified" else None}
                    for c in diff.changes[:60]
                ]},
            ).id

        entity = schema.entity(plan.entity) if plan.entity else None
        checks: list[CheckResult] = []
        ctx: CheckContext | None = None
        if plan.verifiable and entity is not None:
            ctx = CheckContext(plan=plan, schema=schema, entity=entity, before=inp.before, after=inp.after, diff=diff,
                               trace=ta, label=env.label, ev=ev, tool_evidence=tool_evidence)
            resolve(ctx)
            self._record_evidence(ctx, ledger, ev, env)
            if diff is not None:
                candidates = (
                    {(ctx.collection, k) for k in ctx.target_candidates} if ctx.target_status == "ambiguous" else set()
                )
                ctx.changes = classify_changes(
                    diff, plan, schema, expected_keys(ctx), {(ctx.collection, k) for k in ctx.duplicate_keys}, ta,
                    env.label, candidate_keys=candidates,
                )
                ev["side_effects"] = [
                    ledger.add(
                        "side_effect", D, "verifier:side-effect-detector", f"{c.label}: {c.reason}.", resource=c.label,
                        result="contradicts",
                        data={"change_type": c.change_type, "kind": c.kind.value,
                              "fields": {f: d.model_dump() for f, d in list(c.fields.items())[:20]},
                              "attribution": c.attribution, "calls": c.attributed_calls},
                    ).id
                    for c in ctx.changes if c.kind in (ChangeKind.SIDE_EFFECT, ChangeKind.UNCERTAIN)
                ]
            checks = [run_check(ctx, r.id, r.description) for r in plan.requirements]
            enforce_direct_evidence(checks, ledger)

        window_ev: list[str] = []
        if not inp.window_intact:
            window_ev.append(ledger.add("execution_window", CON, "proofloop:orchestrator", inp.window_detail,
                                        result="contradicts").id)
        decision = decide_verdict(plan, checks, window_intact=inp.window_intact, window_evidence=window_ev)

        if ctx is not None:
            checks.extend(self._consistency_checks(ctx, checks, decision, ta, ev))

        bad = ledger.verify_integrity()
        if bad:  # pragma: no cover - defensive; ledger is in-memory
            raise RuntimeError(f"Evidence integrity failure: {bad}")

        diagnosis = self._diagnose(ctx, plan, checks, decision, ta, inp)
        summary = diagnosis[0] if diagnosis else decision.reasons[0].message
        next_steps = []
        for r in decision.reasons:
            step = NEXT_STEPS.get(r.code)
            if step and step not in next_steps:
                next_steps.append(step)
        if decision.verdict == Verdict.FAILED and ta.claimed_status == "success":
            next_steps.append("Flag this agent: it reported success for a task that did not succeed.")

        return VerificationOutcome(
            execution_id=inp.execution_id,
            verdict=decision.verdict,
            rule=decision.rule,
            reasons=decision.reasons,
            summary=summary,
            diagnosis=diagnosis,
            checks=checks,
            evidence=ledger.items,
            changes=ctx.changes if ctx else [],
            trace=ta,
            expected_state=self._expected_view(plan),
            actual_state=self._actual_view(ctx, inp),
            missing_evidence=[e.description for e in ledger.items if e.category == MIS],
            next_steps=next_steps,
            diff_summary=diff.summary() if diff else {},
            duration_ms=timer.ms,
        )

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _tool_evidence(inp: VerificationInput, ledger: EvidenceLedger, mutating: set[str]) -> dict[str, str]:
        calls = {e.call_id: e for e in inp.trace.events if e.event_type == "tool_call" and e.call_id}
        responses: dict[str, Any] = {}
        for e in inp.trace.events:
            if e.event_type == "tool_response" and e.call_id:
                responses.setdefault(e.call_id, e)
        out: dict[str, str] = {}
        for call_id, call in list(calls.items())[:80]:
            resp = responses.get(call_id)
            refs = inp.environment.trace_refs(call.tool_name, call.input, resp.output if resp else None)
            status = resp.status if resp else "no response"
            out[call_id] = ledger.add(
                "tool_invocation", IND, f"trace:{call.source}",
                f"{call_id}: {call.tool_name} -> {status}" + (f" (HTTP {resp.status_code})" if resp and resp.status_code else ""),
                resource=", ".join(f"{c}:{k}" for c, k in refs),
                result="neutral",
                data={"call_id": call_id, "tool": call.tool_name, "mutating": (call.tool_name or "") in mutating,
                      "args": call.input, "status": status, "status_code": resp.status_code if resp else None,
                      "output": resp.output if resp else None},
            ).id
        return out

    @staticmethod
    def _record_evidence(ctx: CheckContext, ledger: EvidenceLedger, ev: dict[str, Any], env: Environment) -> None:
        before_rows, after_rows = ctx.rows(ctx.before), ctx.rows(ctx.after)
        if ctx.bulk:
            if ctx.before.available:
                cond = ctx.plan.target.describe(ctx.noun()) if ctx.plan.target else ""
                ev["selection"] = ledger.add(
                    "record_selection", D, ctx.before.source,
                    f"{len(ctx.selected_keys)} record(s) matched {cond} in the before-state", resource=ctx.collection,
                    result="supports", data={"keys": ctx.selected_keys[:500],
                                             "rows": {k: compact_record(before_rows[k]) for k in ctx.selected_keys[:25]}},
                ).id
            if ctx.after.available and ctx.selected_keys:
                ev["selection_after"] = ledger.add(
                    "record_inspection", D, ctx.after.source,
                    f"Selected {ctx.noun()} records after execution", resource=ctx.collection, result="supports",
                    data={k: compact_record(after_rows.get(k)) for k in ctx.selected_keys[:25]},
                ).id
            return
        key = ctx.created_key if ctx.plan.operation == "CREATE" else ctx.target_key
        if not key:
            return
        lbl = env.label(ctx.collection, key)
        if ctx.before.available and key in before_rows:
            ev["target_before"] = ledger.add("record_inspection", D, ctx.before.source, f"{lbl} before execution",
                                             resource=lbl, result="supports", data=compact_record(before_rows[key])).id
        if ctx.after.available:
            if key in after_rows:
                name = "created" if ctx.plan.operation == "CREATE" else "target_after"
                ev[name] = ledger.add("record_inspection", D, ctx.after.source, f"{lbl} after execution",
                                      resource=lbl, result="supports", data=compact_record(after_rows[key])).id
                if ctx.entity.key_type == "path":
                    rec = after_rows[key]
                    ev["checksum"] = ledger.add("file_checksum", D, ctx.after.source,
                                                f"SHA-256 of '{key}' = {rec.get('sha256')} ({rec.get('size')} bytes)",
                                                resource=lbl, result="supports",
                                                data={"path": key, "sha256": rec.get("sha256"), "size": rec.get("size")}).id
            else:
                ev["target_after"] = ledger.add("record_inspection", D, ctx.after.source, f"{lbl} is absent after execution",
                                                resource=lbl, result="supports", data=None).id
        if ctx.duplicate_keys:
            ev["duplicates"] = ledger.add(
                "duplicate_records", D, "verifier:state-comparator",
                f"Duplicate {ctx.noun()} record(s) observed: {', '.join(ctx.duplicate_keys)}", resource=ctx.collection,
                result="contradicts", data={k: compact_record(after_rows.get(k)) for k in ctx.duplicate_keys},
            ).id

    @staticmethod
    def _consistency_checks(
        ctx: CheckContext, checks: list[CheckResult], decision: VerdictDecision, ta: TraceAssessment, ev: dict[str, Any]
    ) -> list[CheckResult]:
        out: list[CheckResult] = []
        post = [c for c in checks if c.category == "postcondition" and c.status in (CheckStatus.PASSED, CheckStatus.FAILED)]
        post_failed = any(c.status == CheckStatus.FAILED for c in post)
        if ta.mutating_calls:
            if ta.successful_mutations and post_failed:
                st, msg = CheckStatus.FAILED, "Tool responses reported success, but the requested change is not reflected in the observed state."
            elif ta.successful_mutations == 0 and post and not post_failed:
                st, msg = CheckStatus.FAILED, "Tool responses reported failure, yet the observed state shows the change."
            else:
                st, msg = CheckStatus.PASSED, "Tool responses are consistent with the observed state."
            out.append(CheckResult(id="tool_responses_match_state", name="Tool responses agree with observed state",
                                   category="consistency", decisive=False, status=st, explanation=msg,
                                   evidence_ids=list(ev.get("tools", []))[:10] + ([ev["diff"]] if ev.get("diff") else [])))
        claimed = ta.claimed_status
        if claimed in ("success", "failure"):
            if decision.verdict == Verdict.INCONCLUSIVE:
                st, msg = CheckStatus.SKIPPED, f"The agent claimed {claimed}; the outcome could not be established either way."
            elif (claimed == "success") == (decision.verdict == Verdict.VERIFIED):
                st, msg = CheckStatus.PASSED, f"The agent's claim ({claimed}) matches the verified outcome."
            elif claimed == "success":
                st, msg = CheckStatus.FAILED, "False success claim: the agent reported success, but verification established failure."
            else:
                st, msg = CheckStatus.FAILED, "The agent reported failure, but the requested outcome was verified."
            out.append(CheckResult(id="agent_claim_consistent", name="Agent claim matches verified outcome",
                                   category="consistency", decisive=False, status=st, explanation=msg,
                                   evidence_ids=[ev["claim"]] if ev.get("claim") else [],
                                   expected=decision.verdict.value, actual=claimed))
        return out

    @staticmethod
    def _diagnose(
        ctx: CheckContext | None, plan: VerificationPlan, checks: list[CheckResult], decision: VerdictDecision,
        ta: TraceAssessment, inp: VerificationInput,
    ) -> list[str]:
        msgs: list[str] = []
        by_id = {c.id: c for c in checks}
        codes = {r.code for r in decision.reasons}
        if decision.verdict == Verdict.VERIFIED and ctx is not None and ctx.bulk:
            n = len([c for c in checks if c.decisive and c.status == CheckStatus.PASSED])
            verb = {"UPDATE": "update", "DELETE": "deletion", "READ": "read"}.get(plan.operation or "", "change")
            what = plan.target.describe(ctx.noun()) if plan.target else ctx.noun()
            msgs.append(f"The {verb} of {len(ctx.selected_keys)} {ctx.noun()} record(s) ({what}) was independently verified: "
                        f"all {n} decisive requirements hold and no side effects were found.")
            return msgs
        if decision.verdict == Verdict.VERIFIED and ctx is not None:
            n = len([c for c in checks if c.decisive and c.status == CheckStatus.PASSED])
            subject = {
                "CREATE": f"The creation of {ctx.tlabel(ctx.created_key)}",
                "UPDATE": f"The update of {ctx.tlabel()}",
                "DELETE": f"The deletion of {ctx.tlabel()}",
                "READ": f"The read of {ctx.tlabel()}",
            }.get(plan.operation or "", plan.headline())
            msgs.append(f"{subject} was independently verified: all {n} decisive requirements hold and no side effects were found.")
            return msgs
        if ctx is None:
            if "ENVIRONMENT_WINDOW_VIOLATED" in codes:
                msgs.append(inp.window_detail or "The environment changed between execution and verification.")
            else:
                msgs.append("The task could not be verified because it is ambiguous: " + " ".join(plan.ambiguities))
            return msgs

        field_checks = [c for c in checks if c.id.startswith("field_matches:")]
        f_pass = [c for c in field_checks if c.status == CheckStatus.PASSED]
        f_fail = [c for c in field_checks if c.status == CheckStatus.FAILED]
        side = [c for c in ctx.changes if c.kind == ChangeKind.SIDE_EFFECT]
        target_changed = any((c.collection, c.key) == (ctx.collection, ctx.target_key) for c in ctx.changes
                             if c.kind in (ChangeKind.EXPECTED, ChangeKind.SIDE_EFFECT))
        explained: set[str] = set()

        if decision.verdict == Verdict.FAILED:
            op = plan.operation
            for c in checks:
                if (c.id.startswith("bulk_field_matches:") or c.id == "all_targets_removed") and c.status == CheckStatus.FAILED:
                    msgs.append(c.explanation)
            if op == "UPDATE" and f_fail and ctx.target_key:
                for c in side:
                    hits = [f for f in plan.expected_state if f in c.fields]
                    if (c.collection == ctx.collection and c.key != ctx.target_key and c.change_type == "modified" and hits
                            and all(values_equal(c.fields[f].after, plan.expected_state[f], ctx.specs.get(f)) for f in hits)):
                        msgs.append(f"The agent modified {c.label} instead of {ctx.tlabel()}.")
                        explained.add(c.label)
            if op == "DELETE" and by_id.get("target_removed") and by_id["target_removed"].status == CheckStatus.FAILED:
                others = [c for c in side if c.collection == ctx.collection and c.change_type == "removed"]
                if others:
                    msgs.append(f"The agent deleted {others[0].label} instead of {ctx.tlabel()}.")
                    explained.add(others[0].label)
                else:
                    msgs.append(f"{ctx.tlabel()[:1].upper() + ctx.tlabel()[1:]} was not deleted.")
            if op == "CREATE" and ctx.entity.key_type == "path" and ctx.misplaced_keys:
                msgs.append(f"The file was written to '{ctx.misplaced_keys[0]}' instead of '{ctx.target_key}'.")
                explained.update(ctx.label(ctx.collection, k) for k in ctx.misplaced_keys)
            if f_pass and f_fail:
                ok = ", ".join(c.id.split(":", 1)[1] for c in f_pass)
                bad = ", ".join(c.id.split(":", 1)[1] for c in f_fail)
                msgs.append(f"Only part of the requested change was applied: {ok} correct, but {bad} not.")
            elif f_fail and not msgs:
                truncated = [c for c in f_fail if isinstance(c.actual, str) and isinstance(c.expected, str)
                             and c.actual and c.expected.startswith(c.actual) and c.actual != c.expected]
                if truncated:
                    c = truncated[0]
                    msgs.append(f"The change was only partially applied: {c.id.split(':', 1)[1]} is {c.actual!r} instead of {c.expected!r}.")
                elif op == "UPDATE" and not target_changed:
                    msgs.append(f"The requested change was not applied: {ctx.tlabel()} is unchanged.")
                elif op == "CREATE":
                    msgs.append(f"The created {ctx.noun()} does not have the requested values.")
            sha = by_id.get("content_sha256_matches")
            if sha and sha.status == CheckStatus.FAILED:
                rec = ctx.rows(ctx.after).get(ctx.target_key or "", {})
                content = rec.get("content")
                want = str(plan.expected_state.get("content", ""))
                if isinstance(content, str) and content and want.startswith(content):
                    msgs.append(f"The file content was only partially written ({len(content.encode())} of {len(want.encode())} bytes).")
                elif plan.operation == "UPDATE" and not target_changed:
                    msgs.append(f"The requested change was not applied: '{ctx.target_key}' is unchanged.")
                else:
                    msgs.append(f"'{ctx.target_key}' does not contain the requested content.")
            rc = by_id.get("record_created")
            if rc and rc.status == CheckStatus.FAILED:
                msgs.append(f"No new {ctx.noun()} record was created.")
            fe = by_id.get("file_exists")
            if fe and fe.status == CheckStatus.FAILED and not ctx.misplaced_keys:
                msgs.append(f"The file '{ctx.target_key}' was not created.")
            dup = by_id.get("exactly_one_new_record")
            if dup and dup.status == CheckStatus.FAILED:
                msgs.append(f"The operation was applied more than once: {dup.explanation}")
            ti = by_id.get("target_identified")
            if ti and ti.status == CheckStatus.FAILED:
                msgs.append(ti.explanation)
            ow = by_id.get("no_accidental_overwrite")
            if ow and ow.status == CheckStatus.FAILED:
                msgs.append(ow.explanation)
            leftovers = [c for c in side if c.label not in explained]
            if leftovers:
                parts = []
                for c in leftovers[:4]:
                    if c.change_type == "modified":
                        owner = ctx.schema.entity_for_collection(c.collection)
                        managed = set(owner.managed_fields) if owner else set()
                        shown = [(f, d) for f, d in c.fields.items() if f not in managed] or list(c.fields.items())
                        changed = ", ".join(f"{f}: {_short(d.before)} -> {_short(d.after)}" for f, d in shown[:3])
                        parts.append(f"{c.label} ({changed})")
                    else:
                        parts.append(f"{c.label} ({c.change_type})")
                msgs.append("Unintended change(s) outside the task scope: " + "; ".join(parts) + ".")
            if ta.claimed_status == "success":
                if ta.claim_contradicted_by_tools:
                    msgs.append("The agent claimed success even though its own tool calls returned errors.")
                else:
                    msgs.append("The agent claimed success, but the independently observed state contradicts that claim.")
            return msgs or [decision.reasons[0].message]

        # INCONCLUSIVE ----------------------------------------------------------
        decisive_post_ok = all(c.status == CheckStatus.PASSED for c in checks
                               if c.decisive and c.category in ("postcondition", "side_effect", "constraint", "precondition"))
        for r in decision.reasons:
            if r.code == "MISSING_EXECUTION_EVIDENCE":
                lead = "The requested state is present, but" if decisive_post_ok else "Additionally,"
                msgs.append(f"{lead} no tool-level execution evidence attributes the change to this agent.")
            elif r.code == "CONTRADICTORY_EVIDENCE":
                lead = "The requested state is present, but" if decisive_post_ok else "Additionally,"
                msgs.append(f"{lead} the execution logs contradict each other: " + " ".join(ta.contradictions[:2]))
            elif r.code == "STATE_UNAVAILABLE":
                err = inp.after.error if not inp.after.available else inp.before.error
                msgs.append(f"The system state could not be independently observed ({err}), so the outcome cannot be established.")
            elif r.code == "AMBIGUOUS_TARGET":
                ti = by_id.get("target_identified")
                msgs.append(ti.explanation if ti else r.message)
            elif r.code == "TRACE_INTEGRITY_FAILURE":
                msgs.append(f"The execution trace failed its integrity check ({ta.chain_detail}).")
            elif r.code == "UNCERTAIN_SIDE_EFFECT":
                msgs.append("Changes outside the task scope were observed but could not be attributed with certainty.")
            elif r.code == "ENVIRONMENT_WINDOW_VIOLATED":
                msgs.append(inp.window_detail or r.message)
            else:
                msgs.append(r.message)
        return msgs

    @staticmethod
    def _expected_view(plan: VerificationPlan) -> dict[str, Any]:
        return {
            "operation": plan.operation,
            "entity": plan.entity,
            "target": plan.target.model_dump() if plan.target else None,
            "values": plan.expected_state,
            "constraints": plan.constraints.model_dump(),
        }

    @staticmethod
    def _actual_view(ctx: CheckContext | None, inp: VerificationInput) -> dict[str, Any]:
        if ctx is None:
            return {"available": inp.after.available}
        if ctx.bulk:
            after_rows = ctx.rows(ctx.after)
            return {"available": inp.after.available, "selected_keys": ctx.selected_keys[:200],
                    "selected_count": len(ctx.selected_keys),
                    "after": {k: compact_record(after_rows.get(k)) for k in ctx.selected_keys[:10]} if inp.after.available else None}
        key = ctx.created_key if ctx.plan.operation == "CREATE" else ctx.target_key
        view: dict[str, Any] = {
            "available": inp.after.available,
            "record_key": key,
            "before": compact_record(ctx.rows(ctx.before).get(key or "")) if inp.before.available else None,
            "after": compact_record(ctx.rows(ctx.after).get(key or "")) if inp.after.available else None,
        }
        if ctx.target_candidates and len(ctx.target_candidates) > 1:
            view["candidates"] = ctx.target_candidates
        if not inp.after.available:
            view["error"] = inp.after.error
        return view


def _short(value: Any, limit: int = 40) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 4] + "...'"
