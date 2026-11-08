"""Execution orchestrator: interpret -> (approve) -> run agent -> verify -> report.

Separation of duties:

* The agent receives only its environment toolbox and a trace collector.
* Snapshots are captured here through the read-only inspector, *outside*
  the agent's control.
* The verification engine receives only observable artefacts.

Fault-injection harness duties (e.g. simulating an outage at verification
time) live in clearly marked methods and never leak into the engine.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from app.agent.base_agent import AGENT_CONFIGS
from app.agent.fault_injection import FAULTS, FaultMode, is_applicable
from app.agent.sample_agent import LLMPlanner, RulePlanner, SampleAgent
from app.agent.tool_agent import ToolCallingAgent
from app.collector.trace_collector import TraceCollector
from app.collector.trace_storage import TraceStorage
from app.config import Settings
from app.database import repositories as repo
from app.database.connection import Database
from app.database.models import (
    EvidenceRecord,
    Execution,
    StateSnapshot,
    User,
    VerificationCheckRecord,
    VerificationPlanRecord,
    VerificationReportRecord,
)
from app.environments.base import Environment
from app.environments.registry import EnvironmentRegistry
from app.interpretation.task_parser import TaskInterpreter
from app.interpretation.verification_plan import VerificationPlan
from app.llm.base import LLMError
from app.llm.model_manager import ModelManager
from app.llm.prompts import NARRATIVE_SYSTEM, narrative_prompt
from app.reporting.report_generator import build_report
from app.services.policies import PolicyToolbox, load_policy
from app.services.runtime_settings import RuntimeSettings, load_runtime
from app.utils.hashing import hash_obj
from app.utils.logging import get_logger
from app.utils.timeutil import Stopwatch, utcnow
from app.verification.engine import VerificationEngine, VerificationInput
from app.verification.state_comparator import find_by_conditions, find_by_match
from app.verification.state_inspector import SnapshotResult, capture_snapshot

if TYPE_CHECKING:
    from app.services.data_sources import DataSourceService

log = get_logger("executions")
MAX_INSTRUCTION_LENGTH = 2000


class ExecutionError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


EXTERNAL_SESSION_TTL = timedelta(minutes=60)


def active_external_session(session: Session, env: str, *, include_expired: bool = False) -> dict[str, Any] | None:
    """The external-agent session currently holding ``env`` (see app.services.external_sessions), if any.

    Sessions older than EXTERNAL_SESSION_TTL no longer block the environment."""
    active = repo.get_setting(session, f"external_active:{env}", None) or None
    if active and not include_expired:
        try:
            age = utcnow() - datetime.fromisoformat(str(active.get("started_at")).rstrip("Z"))
        except (ValueError, TypeError):
            return active
        if age >= EXTERNAL_SESSION_TTL:
            return None
    return active


def load_scenarios(sample_dir: Path) -> list[dict[str, Any]]:
    path = sample_dir / "sample_scenarios.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


class ExecutionService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        registry: EnvironmentRegistry,
        model_manager: ModelManager,
        runtime_override: RuntimeSettings | None = None,
        sources: DataSourceService | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.registry = registry
        self.models = model_manager
        self.sources = sources
        self.storage = TraceStorage(db)
        self.engine = VerificationEngine()
        self._runtime_override = runtime_override

    # ------------------------------------------------------------- settings
    def runtime(self) -> RuntimeSettings:
        if self._runtime_override is not None:
            return self._runtime_override
        with self.db.session() as s:
            return load_runtime(s, self.settings)

    def _interpreter(self, rt: RuntimeSettings) -> TaskInterpreter:
        return TaskInterpreter(lambda: self.models.for_runtime(rt), rt.interpreter_mode, rt.llm_enabled)

    # -------------------------------------------------------- env versioning
    def env_version(self, session: Session, env: str) -> int:
        return int(repo.get_setting(session, f"env_version:{env}", 0))

    def bump_env_version(self, env: str) -> int:
        with self.db.session() as s:
            v = self.env_version(s, env) + 1
            repo.set_setting(s, f"env_version:{env}", v)
            return v

    def reset_sandbox(self, env_name: str, actor: str) -> None:
        env = self.registry.get(env_name)
        with env.lock:
            env.reset()
            self.bump_env_version(env_name)
        with self.db.session() as s:
            repo.audit(s, actor, "sandbox_reset", env_name)

    # ---------------------------------------------------------------- create
    def create_execution(
        self,
        *,
        instruction: str,
        environment: str,
        agent_config: str = "scripted",
        fault_mode: str = "none",
        scenario_id: str | None = None,
        reset_sandbox: bool = False,
        user: User | None = None,
    ) -> str:
        instruction = (instruction or "").strip()
        if not instruction:
            raise ExecutionError("invalid_instruction", "The instruction is empty.", 422)
        if len(instruction) > MAX_INSTRUCTION_LENGTH:
            raise ExecutionError("invalid_instruction", f"Instructions are limited to {MAX_INSTRUCTION_LENGTH} characters.", 422)
        try:
            env = self.registry.get(environment)
        except KeyError as exc:
            raise ExecutionError("invalid_environment", str(exc), 422) from exc
        if agent_config not in AGENT_CONFIGS:
            raise ExecutionError("invalid_agent", f"Unknown agent configuration {agent_config!r}.", 422)
        try:
            fault = FaultMode(fault_mode)
        except ValueError as exc:
            raise ExecutionError("invalid_fault", f"Unknown fault mode {fault_mode!r}.", 422) from exc
        is_source = not self.registry.is_builtin(environment)
        wanted = "sources" if is_source else "sandbox"
        if AGENT_CONFIGS[agent_config].environments not in (wanted, "any"):
            allowed = [a.name for a in AGENT_CONFIGS.values() if a.environments in (wanted, "any")]
            raise ExecutionError("invalid_agent", f"{AGENT_CONFIGS[agent_config].name} cannot run on "
                                 f"{'a connected data source' if is_source else 'a built-in sandbox'}. Use: {', '.join(allowed)}.", 422)
        if agent_config == "external" and fault != FaultMode.NONE:
            raise ExecutionError("invalid_fault", "Fault injection does not apply to your own agent: it runs exactly as it is.", 422)
        if is_source and fault != FaultMode.NONE:
            raise ExecutionError("invalid_fault", "Fault injection is only available in the built-in sandboxes; "
                                 "on connected data the agent runs exactly as it is.", 422)
        if is_source:
            reset_sandbox, scenario_id = False, None

        rt = self.runtime()
        if AGENT_CONFIGS[agent_config].requires_llm and not rt.llm_enabled:
            raise ExecutionError("llm_unavailable", "This agent needs the local LLM, which is disabled in Settings.", 503)
        timer = Stopwatch()
        try:
            result = self._interpreter(rt).interpret(instruction, env.schema)
        except LLMError as exc:
            raise ExecutionError("llm_unavailable", str(exc), 503) from exc
        plan = result.plan
        interpretation_ms = timer.ms

        if fault != FaultMode.NONE and not is_applicable(fault, environment, plan.operation):
            raise ExecutionError(
                "fault_not_applicable",
                f"Fault '{FAULTS[fault].label}' does not apply to {plan.operation or 'this'} operations in the {environment} environment.",
                422,
            )
        self._preview_target(env, plan)

        exec_id = f"exec_{secrets.token_hex(6)}"
        with self.db.session() as s:
            s.add(
                Execution(
                    id=exec_id,
                    task_description=instruction,
                    environment=environment,
                    agent_name={"ai_agent": ToolCallingAgent.name, "external": "external-agent"}.get(agent_config, SampleAgent.name),
                    agent_framework={"ai_agent": ToolCallingAgent.framework, "external": "external"}.get(
                        agent_config, SampleAgent.framework),
                    agent_config=agent_config,
                    fault_mode=fault.value,
                    scenario_id=scenario_id,
                    reset_sandbox=reset_sandbox,
                    execution_status="interpreted" if plan.verifiable else "needs_clarification",
                    interpreter=result.interpreter,
                    created_by=user.id if user else None,
                    interpretation_ms=interpretation_ms,
                )
            )
            s.flush()
            s.add(
                VerificationPlanRecord(
                    execution_id=exec_id,
                    environment=environment,
                    entity=plan.entity,
                    expected_operation=plan.operation,
                    target=plan.target.model_dump() if plan.target else None,
                    expected_state=plan.expected_state,
                    verification_conditions=[r.model_dump() for r in plan.requirements],
                    constraints=plan.constraints.model_dump(),
                    ambiguities=plan.ambiguities,
                    requires_approval=plan.requires_approval,
                    interpreter=result.interpreter,
                    plan_data=plan.model_dump(mode="json"),
                    plan_hash=hash_obj(plan.model_dump(mode="json")),
                )
            )
        log.info("Execution %s created (%s, %s)", exec_id, environment, plan.headline())
        return exec_id

    def _preview_target(self, env: Environment, plan: VerificationPlan) -> None:
        """Best-effort plan-time warning when a field-based target matches 0 or >1 records."""
        if plan.target and plan.target.bulk and plan.collection and plan.verifiable:
            snap = capture_snapshot(env, "preview")
            if not snap.available:
                return
            entity = env.schema.entity(plan.entity)
            specs = {f.name: f for f in entity.fields} if entity else {}
            n = len(find_by_conditions((snap.state or {}).get(plan.collection, {}), plan.target.conditions, specs))
            plan.warnings.append(
                f"{n} {plan.entity_display.lower() if plan.entity_display else 'record'}(s) currently match "
                f"{' and '.join(c.describe() for c in plan.target.conditions)}." if n
                else "No record currently matches the condition; the result will be INCONCLUSIVE.")
            return
        if not plan.target or plan.target.key is not None or not plan.target.match or not plan.collection:
            return
        snap = capture_snapshot(env, "preview")
        if not snap.available:
            return
        entity = env.schema.entity(plan.entity)
        specs = {f.name: f for f in entity.fields} if entity else {}
        matches = find_by_match((snap.state or {}).get(plan.collection, {}), plan.target.match, specs)
        if len(matches) > 1:
            plan.warnings.append(
                f"{len(matches)} records currently match {plan.target.match} ({', '.join(matches)}). "
                "The task is ambiguous; verification will be INCONCLUSIVE unless a unique id is used."
            )
        elif not matches:
            plan.warnings.append(f"No record currently matches {plan.target.match}.")

    # ------------------------------------------------------------------- run
    def run_execution(self, exec_id: str, *, approved: bool = False, force: bool = False, user: User | None = None) -> None:
        with self.db.session() as s:
            ex = s.get(Execution, exec_id)
            if ex is None:
                raise ExecutionError("not_found", "Execution not found.", 404)
            if ex.execution_status not in ("interpreted", "needs_clarification"):
                raise ExecutionError("invalid_state", f"Execution is already {ex.execution_status}.", 409)
            plan_rec = repo.get_plan(s, exec_id)
            plan = VerificationPlan.model_validate(plan_rec.plan_data)  # type: ignore[union-attr]
            if ex.execution_status == "needs_clarification" and not force:
                raise ExecutionError(
                    "clarification_required",
                    "The task is ambiguous: " + " ".join(plan.ambiguities) + " Rephrase it, or run anyway to see an INCONCLUSIVE verdict.",
                    409,
                )
            if ex.agent_config == "external":
                raise ExecutionError("external_agent", "This execution is for your own agent: start a verification session "
                                     "(POST /executions/{id}/session/start) instead of running ProofLoop's agent.", 409)
            if plan.requires_approval and not approved:
                raise ExecutionError("approval_required", plan.approval_reason or "This operation requires approval.", 409)
            active = active_external_session(s, ex.environment)
            if active:
                raise ExecutionError("session_active", f"An external agent session ({active['execution_id']}) is running on "
                                     "this environment. Finish or abort it first.", 409)
            if plan.requires_approval:
                ex.approved_by = user.username if user else "system"
            ex.execution_status = "running"
            ex.started_at = utcnow()
            env_name, instruction, fault, agent_config, reset = (
                ex.environment, ex.task_description, ex.fault_mode, ex.agent_config, ex.reset_sandbox
            )

        env = self.registry.get(env_name)
        rt = self.runtime()
        collector = TraceCollector(self.storage, exec_id)
        timer = Stopwatch()
        is_source = not self.registry.is_builtin(env_name)
        with env.lock:
            if reset and not is_source:
                env.reset()
                self.bump_env_version(env_name)
            backup_note = None
            if is_source and self.sources is not None:
                try:
                    bk = self.sources.backup(env, exec_id)
                    backup_note = f"Backup taken before the run ({bk.size_bytes} bytes) - 'Undo run' can restore it."
                except Exception as exc:  # noqa: BLE001 - never run on real data without a backup
                    log.exception("Backup failed for %s", exec_id)
                    with self.db.session() as s:
                        ex = s.get(Execution, exec_id)
                        ex.execution_status = "interpreted"  # type: ignore[union-attr]
                        ex.started_at = None  # type: ignore[union-attr]
                    raise ExecutionError("backup_failed", f"The safety backup could not be created, so the agent was not "
                                         f"started: {exc}", 500) from exc
            before = capture_snapshot(env, "before")
            self._store_snapshot(exec_id, before)
            agent_name = ToolCallingAgent.name if agent_config == "ai_agent" else SampleAgent.name
            collector.started(
                f"Execution started by {user.username if user else 'system'}",
                {"instruction": instruction, "environment": env_name, "agent": agent_name, "configuration": agent_config,
                 "sandbox_reset": reset and not is_source, "backup": backup_note},
            )
            if agent_config == "ai_agent":
                with self.db.session() as s:
                    policy = load_policy(s, env_name)
                if not policy.is_empty:
                    collector.annotate("Write policy enforced: " + "; ".join(policy.describe()), policy.model_dump())
                agent = ToolCallingAgent(env, self.models.for_runtime(rt), toolbox=PolicyToolbox(env.toolbox(), policy))
            else:
                planner = LLMPlanner(self.models.for_runtime(rt)) if agent_config == "llm" else RulePlanner()
                agent = SampleAgent(env, planner, fault)
            try:
                result = agent.run(instruction, collector)
                claim, claimed_status, error = result.claim, result.claimed_status, result.error
            except Exception as exc:  # noqa: BLE001 - agent faults must not crash the platform
                log.exception("Agent crashed during %s", exec_id)
                collector.error(f"Agent crashed: {type(exc).__name__}: {exc}")
                claim, claimed_status, error = f"Agent crashed: {exc}", "failure", str(exc)
            duration = timer.ms
            collector.completed("Agent run finished", {"duration_ms": duration})
            version = self.bump_env_version(env_name)

        with self.db.session() as s:
            ex = s.get(Execution, exec_id)
            ex.agent_claim = claim  # type: ignore[union-attr]
            ex.agent_claimed_status = claimed_status  # type: ignore[union-attr]
            ex.agent_error = error  # type: ignore[union-attr]
            ex.execution_status = "completed"  # type: ignore[union-attr]
            ex.completed_at = utcnow()  # type: ignore[union-attr]
            ex.execution_ms = duration  # type: ignore[union-attr]
            ex.env_version_after_run = version  # type: ignore[union-attr]

    def _store_snapshot(self, exec_id: str, snap: SnapshotResult) -> None:
        with self.db.session() as s:
            s.add(
                StateSnapshot(
                    execution_id=exec_id, phase=snap.phase, resource_type=snap.resource_type,
                    resource_identifier=snap.resource_identifier, available=snap.available, error=snap.error,
                    state=snap.state, state_hash=snap.state_hash, source=snap.source, captured_at=snap.captured_at,
                )
            )

    @staticmethod
    def _snapshot_from_record(rec: StateSnapshot | None, env: Environment, phase: str) -> SnapshotResult:
        if rec is None:
            return SnapshotResult(phase=phase, available=False, error=f"No {phase}-execution snapshot was captured",
                                  source=env.inspector().source, resource_type=env.name,
                                  resource_identifier=env.resource_identifier, captured_at=utcnow())
        snap = SnapshotResult(phase=phase, available=rec.available, state=rec.state, state_hash=rec.state_hash,
                              error=rec.error, source=rec.source, resource_type=rec.resource_type,
                              resource_identifier=rec.resource_identifier, captured_at=rec.captured_at)
        if snap.available and snap.state is not None and hash_obj(snap.state) != snap.state_hash:
            snap.available = False
            snap.error = "Stored snapshot failed its integrity check"
        return snap

    # ---------------------------------------------------------------- verify
    def verify_execution(self, exec_id: str, *, user: User | None = None) -> dict[str, Any]:
        with self.db.session() as s:
            ex = s.get(Execution, exec_id)
            if ex is None:
                raise ExecutionError("not_found", "Execution not found.", 404)
            if ex.execution_status == "verified":
                raise ExecutionError("already_verified", "This execution has already been verified; reports are immutable.", 409)
            if ex.execution_status != "completed":
                raise ExecutionError("invalid_state", f"Execution must be completed before verification (status: {ex.execution_status}).", 409)
            plan = VerificationPlan.model_validate(repo.get_plan(s, exec_id).plan_data)  # type: ignore[union-attr]
            env_name, fault, run_version = ex.environment, FaultMode(ex.fault_mode), ex.env_version_after_run

        env = self.registry.get(env_name)
        timer = Stopwatch()
        with env.lock:
            with self.db.session() as s:
                current_version = self.env_version(s, env_name)
            window_intact = current_version == run_version
            window_detail = (
                "" if window_intact else
                f"The {env_name} sandbox was modified {current_version - (run_version or 0)} time(s) by other runs or resets "
                "after this execution finished, so its after-state no longer reflects this execution alone."
            )
            if self._harness_outage_requested(fault, env):
                with env.simulate_outage():
                    after = capture_snapshot(env, "after")
            else:
                after = capture_snapshot(env, "after")
        self._store_snapshot(exec_id, after)

        with self.db.session() as s:
            before = self._snapshot_from_record(repo.get_snapshot(s, exec_id, "before"), env, "before")
        trace = self.storage.load(exec_id)
        outcome = self.engine.verify(
            VerificationInput(execution_id=exec_id, plan=plan, environment=env, before=before, after=after,
                              trace=trace, window_intact=window_intact, window_detail=window_detail)
        )
        narrative = self._narrative(outcome)
        report_id = f"rpt_{exec_id.split('_', 1)[1]}"

        with self.db.session() as s:
            ex = s.get(Execution, exec_id)
            ex.verification_ms = timer.ms  # type: ignore[union-attr]
            report = build_report(report_id=report_id, execution=ex, plan=plan, outcome=outcome, trace=trace, narrative=narrative)
            for e in outcome.evidence:
                s.add(EvidenceRecord(id=e.id, execution_id=exec_id, evidence_type=e.evidence_type, category=e.category.value,
                                     source=e.source, resource=e.resource[:256], result=e.result, description=e.description,
                                     evidence_data=report_safe(e.data), integrity_hash=e.integrity_hash, collected_at=e.collected_at))
            for c in outcome.checks:
                s.add(VerificationCheckRecord(execution_id=exec_id, check_id=c.id, check_name=c.name, category=c.category,
                                              decisive=c.decisive, expected_value=report_safe(c.expected),
                                              actual_value=report_safe(c.actual), status=c.status.value,
                                              explanation=c.explanation, evidence_ids=c.evidence_ids))
            s.add(VerificationReportRecord(id=report_id, execution_id=exec_id, verdict=outcome.verdict.value,
                                           summary=outcome.summary, report_data=report,
                                           report_hash=report["integrity"]["report_hash"]))
            ex.verdict = outcome.verdict.value  # type: ignore[union-attr]
            ex.execution_status = "verified"  # type: ignore[union-attr]
            ex.verified_at = utcnow()  # type: ignore[union-attr]
            repo.audit(s, user.username if user else "system", "execution_verified", f"{exec_id} -> {outcome.verdict.value}")
        log.info("Execution %s verified: %s", exec_id, outcome.verdict.value)
        return report

    @staticmethod
    def _harness_outage_requested(fault: FaultMode, env: Environment) -> bool:
        """TEST HARNESS ONLY: simulate an environment outage during verification."""
        return fault == FaultMode.INSPECTOR_UNAVAILABLE and env.supports_outage

    def _narrative(self, outcome) -> dict[str, Any] | None:
        rt = self.runtime()
        if not (rt.llm_enabled and rt.llm_narrative_enabled):
            return None
        facts = {
            "verdict": outcome.verdict.value,
            "summary": outcome.summary,
            "diagnosis": outcome.diagnosis,
            "failed_checks": [c.explanation for c in outcome.checks if c.status.value == "failed"],
            "inconclusive_checks": [c.explanation for c in outcome.checks if c.status.value == "inconclusive"],
        }
        try:
            res = self.models.for_runtime(rt).generate(narrative_prompt(facts), system=NARRATIVE_SYSTEM)
        except LLMError as exc:
            log.info("Narrative skipped: %s", exc)
            return None
        return {
            "text": res.text.strip()[:2000],
            "model": res.model,
            "latency_ms": res.latency_ms,
            "disclaimer": ("Generated by a cloud language model" if rt.llm_backend == "cloud" else "Generated by a local language model") + " from the facts above. It is not evidence and does not affect the verdict.",
        }

    # ------------------------------------------------------------------ undo
    def undo_execution(self, exec_id: str, *, user: User | None = None) -> dict[str, Any]:
        """Restore a connected source to the backup taken right before ``exec_id`` ran.

        Only the most recent run on a source can be undone, so later runs are never silently reverted.
        """
        if self.sources is None:
            raise ExecutionError("not_supported", "Undo is not available.", 409)
        bk = self.sources.backup_for(exec_id)
        if bk is None:
            raise ExecutionError("no_backup", "This execution has no backup (only runs on connected data sources do).", 404)
        if bk.restored_at is not None:
            raise ExecutionError("already_undone", f"This run was already undone by {bk.restored_by}.", 409)
        latest = self.sources.latest_backup(bk.environment)
        if latest is None or latest.execution_id != exec_id:
            raise ExecutionError("not_latest", "Only the most recent run on a data source can be undone; a later run "
                                 f"({latest.execution_id if latest else '?'}) changed it afterwards.", 409)
        try:
            env = self.registry.get(bk.environment)
        except KeyError as exc:
            raise ExecutionError("source_unavailable", "The data source is no longer connected.", 409) from exc
        with env.lock:
            env.restore(Path(bk.path))  # type: ignore[attr-defined]
            self.bump_env_version(bk.environment)
        actor = user.username if user else "system"
        self.sources.mark_restored(exec_id, actor)
        log.info("Execution %s undone by %s", exec_id, actor)
        return {"execution_id": exec_id, "restored": True, "environment": bk.environment}

    # --------------------------------------------------------------- combined
    def run_and_verify(self, exec_id: str, *, approved: bool = False, force: bool = False, user: User | None = None) -> dict[str, Any]:
        self.run_execution(exec_id, approved=approved, force=force, user=user)
        return self.verify_execution(exec_id, user=user)


def report_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)
