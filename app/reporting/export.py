"""Report export: JSON (machine-readable) and Markdown (human-readable)."""

from __future__ import annotations

import json
from typing import Any

_ICON = {"passed": "PASS", "failed": "FAIL", "inconclusive": "????", "skipped": "skip"}


def to_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False)


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text.replace("|", "\\|").replace("\n", " ")[:120]


def to_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    add(f"# ProofLoop Verification Report - {report['verdict']}")
    add("")
    add(f"**Execution:** `{report['execution_id']}`  ")
    add(f"**Report:** `{report['report_id']}` (generated {report['generated_at']})  ")
    add(f"**Environment:** {report['environment']}  ")
    add(f"**Agent:** {report['agent']['name']} ({report['agent']['configuration']})  ")
    add(f"**Decision rule:** {report['decision_rule']}")
    add("")
    add("## Instruction")
    add(f"> {report['instruction']}")
    add("")
    add("## Summary")
    add(report["summary"])
    for d in report.get("diagnosis", [])[1:]:
        add(f"- {d}")
    add("")
    add("## Agent claim (agent-reported, not proof)")
    claim = report["agent_claim"]
    add(f"> {claim.get('text') or '(none)'}  ")
    add(f"Claimed status: **{claim.get('claimed_status') or 'unknown'}**")
    add("")
    add("## Expected vs actual")
    add("```json")
    add(json.dumps({"expected": report["expected_state"], "actual": report["actual_state"]}, indent=2, ensure_ascii=False))
    add("```")
    add("")
    add("## Checks")
    add("| Result | Check | Expected | Actual | Explanation |")
    add("|---|---|---|---|---|")
    for c in report["checks"]:
        add(f"| {_ICON.get(c['status'], c['status'])} | {c['name']} | {_fmt(c.get('expected'))} | {_fmt(c.get('actual'))} | {_fmt(c.get('explanation'))} |")
    add("")
    add("## Side effects")
    if report["side_effects"]:
        for s in report["side_effects"]:
            add(f"- **{s['label']}** ({s['change_type']}, {s['kind']}): {s['reason']}. {s.get('attribution', '')}")
    else:
        add("None detected.")
    add("")
    add("## Evidence")
    add("| Id | Category | Type | Source | Description | SHA-256 |")
    add("|---|---|---|---|---|---|")
    for e in report["evidence"]:
        add(f"| `{e['id']}` | {e['category']} | {e['evidence_type']} | {e['source']} | {_fmt(e['description'])} | `{e['integrity_hash'][:12]}` |")
    add("")
    if report["missing_evidence"]:
        add("## Missing evidence")
        for m in report["missing_evidence"]:
            add(f"- {m}")
        add("")
    add("## Recommended next steps")
    for s in report["next_steps"]:
        add(f"- {s}")
    add("")
    narrative = report["facts_vs_model"].get("model_generated_narrative")
    if narrative:
        add("## Model-generated narrative (not evidence)")
        add(f"_{narrative.get('disclaimer', '')}_")
        add("")
        add(narrative.get("text", ""))
        add("")
    add("---")
    d = report["durations_ms"]
    add(f"Interpretation {d.get('interpretation')} ms - execution {d.get('execution')} ms - verification {d.get('verification')} ms.  ")
    add(f"Trace chain valid: {report['integrity']['trace_chain_valid']} - report hash `{report['integrity'].get('report_hash', '')}`")
    return "\n".join(lines) + "\n"
