"""Reproducible synthetic benchmark dataset.

Cases are generated from the deterministic seed data with a fixed RNG seed:
each *task template instance* is combined with every fault mode applicable to
its operation. Ground-truth labels come from the fault-injection harness
(what was actually done), never from ProofLoop's own output.
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path

from app.agent.fault_injection import FAULTS, FaultMode, applicable_faults

DEPARTMENTS = ["Engineering", "Sales", "Marketing", "Finance", "Human Resources", "Operations", "Support", "Legal"]
TITLES = ["Software Engineer", "Account Executive", "Brand Manager", "Controller", "Recruiter", "Operations Analyst"]
FIRST = ["Noor", "Kai", "Lena", "Mason", "Aiko", "Ravi", "Sara", "Tomas", "Ines", "Oliver"]
LAST = ["Brooks", "Iyer", "Moreau", "Bauer", "Sato", "Lindqvist", "Okoro", "Varga"]
PRIORITIES = ["low", "medium", "high", "critical"]
ASSIGNEES = ["Priya Sharma", "Liam Nguyen", "Hana Tanaka", "Omar Haddad", "Elena Rossi"]
TICKET_TITLES = ["Docking station not detected", "Shared calendar out of sync", "Request VPN token",
                 "Monitor flickers after update", "Cannot print to floor 2", "Access to finance share"]


@dataclass
class BenchmarkCase:
    case_id: str
    environment: str
    operation: str
    template: str
    instruction: str
    fault_mode: str
    expected_verdict: str
    ground_truth: str  # success | failure | indeterminate
    unintended_change: bool


def _load(sample_dir: Path, name: str) -> list[dict]:
    return json.loads((sample_dir / name).read_text(encoding="utf-8"))


def _task_instances(sample_dir: Path, rng: random.Random, variants: int) -> list[tuple[str, str, str, str]]:
    """Return (environment, operation, template_id, instruction)."""
    employees = [e for e in _load(sample_dir, "synthetic_employees.json") if e["name"] != "Alex Kim"]
    customers = _load(sample_dir, "synthetic_customers.json")
    tickets = _load(sample_dir, "synthetic_tickets.json")
    files = sorted(p.relative_to(sample_dir / "sandbox_files").as_posix()
                   for p in (sample_dir / "sandbox_files").rglob("*") if p.is_file())
    tasks: list[tuple[str, str, str, str]] = []

    for i in range(variants):
        e = rng.choice(employees)
        field, phrase = rng.choice([("email", "email address"), ("phone", "phone number"), ("department", "department"), ("title", "job title")])
        if field == "email":
            value = f"{e['name'].split()[0].lower()}.{rng.randint(10, 99)}@acme.example"
        elif field == "phone":
            value = f"+1-555-01{rng.randint(0, 99):02d}"
            while value == e["phone"]:
                value = f"+1-555-01{rng.randint(0, 99):02d}"
        elif field == "department":
            value = rng.choice([d for d in DEPARTMENTS if d != e["department"]])
        else:
            value = rng.choice([t for t in TITLES if t != e["title"]])
        phrasing = [
            f"Update the {phrase} of employee {e['id']} to {value}",
            f"Set employee {e['id']}'s {phrase} to {value}",
            f"Change the {phrase} for employee ID {e['id']} to {value}",
        ][i % 3]
        tasks.append(("database", "UPDATE", "db.update.employee", phrasing))

    for _ in range(max(1, variants - 1)):
        c = rng.choice(customers)
        phone = f"+1-555-01{rng.randint(0, 99):02d}"
        tier = rng.choice([t for t in ("standard", "gold", "platinum") if t != c["tier"]])
        tasks.append(("database", "UPDATE", "db.update.customer.2fields",
                      f"Update customer {c['id']} phone to {phone} and tier to {tier}"))

    for _ in range(max(1, variants - 1)):
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        tasks.append(("database", "CREATE", "db.create.employee",
                      f"Create a new employee record with name {name} and department {rng.choice(DEPARTMENTS)}"))
    company = f"{rng.choice(['Nova', 'Kite', 'Ember', 'Atlas'])}{rng.choice(['Works', 'Labs', 'Foods'])}"
    tasks.append(("database", "CREATE", "db.create.customer",
                  f"Add a customer named {company} with email hello@{company.lower()}.example and tier gold"))
    for _ in range(max(1, variants - 1)):
        if rng.random() < 0.5:
            tasks.append(("database", "DELETE", "db.delete.customer", f"Delete customer {rng.choice(customers)['id']}"))
        else:
            tasks.append(("database", "DELETE", "db.delete.employee", f"Remove employee {rng.choice(employees)['id']}"))

    for i in range(variants):
        t = rng.choice(tickets)
        kind = i % 3
        if kind == 0:
            p = rng.choice([x for x in PRIORITIES if x != t["priority"]])
            tasks.append(("api", "UPDATE", "api.update.priority", f"Set the priority of ticket {t['id']} to {p}"))
        elif kind == 1:
            open_ticket = rng.choice([x for x in tickets if x["status"] != "closed"])
            tasks.append(("api", "UPDATE", "api.update.close", f"Close ticket {open_ticket['id']}"))
        else:
            a = rng.choice([x for x in ASSIGNEES if x != t["assignee"]])
            tasks.append(("api", "UPDATE", "api.update.assign", f"Assign ticket {t['id']} to {a}"))
    for _ in range(max(1, variants - 1)):
        tasks.append(("api", "CREATE", "api.create",
                      # Never the service default ("medium"): a partially applied create must be observable.
                      f"Create a ticket titled '{rng.choice(TICKET_TITLES)}' with priority {rng.choice(['low', 'high', 'critical'])}"))
    tasks.append(("api", "DELETE", "api.delete", f"Delete ticket {rng.choice(tickets)['id']}"))

    for i in range(max(1, variants - 1)):
        name = f"{rng.choice(['reports', 'notes'])}/bench_{i}_{rng.randint(100, 999)}.txt"
        content = rng.choice(["Quarterly numbers approved.", "Migration finished without errors.", "Retro notes: ship smaller."])
        tasks.append(("file", "CREATE", "file.create", f"Create a file {name} with content '{content}'"))
    for _ in range(max(1, variants - 1)):
        target = rng.choice(files)
        tasks.append(("file", "UPDATE", "file.overwrite", f"Overwrite the file {target} with 'Rewritten by benchmark {rng.randint(1000, 9999)}.'"))
    tasks.append(("file", "DELETE", "file.delete", f"Delete the file {rng.choice(files)}"))
    return tasks


def generate_cases(sample_dir: Path, seed: int = 42, variants: int = 3) -> list[BenchmarkCase]:
    rng = random.Random(seed)
    cases: list[BenchmarkCase] = []
    for idx, (env, op, template, instruction) in enumerate(_task_instances(sample_dir, rng, variants), start=1):
        for fault in applicable_faults(env, op):
            info = FAULTS[fault]
            cases.append(BenchmarkCase(
                case_id=f"c{idx:03d}-{fault.value}",
                environment=env, operation=op, template=template, instruction=instruction,
                fault_mode=fault.value, expected_verdict=info.expected_verdict, ground_truth=info.ground_truth,
                unintended_change=info.unintended_change,
            ))
    # Ambiguous-target tasks (two employees share the name "Alex Kim").
    for idx, instruction in enumerate([
        "Update the email of employee named Alex Kim to alex.kim@acme.example",
        "Update the department of employee named Alex Kim to Marketing",
    ], start=1):
        cases.append(BenchmarkCase(
            case_id=f"amb{idx:02d}-none", environment="database", operation="UPDATE", template="db.update.ambiguous",
            instruction=instruction, fault_mode=FaultMode.NONE.value, expected_verdict="INCONCLUSIVE",
            ground_truth="indeterminate", unintended_change=False,
        ))
    return cases


def describe(cases: list[BenchmarkCase]) -> dict:
    by_fault: dict[str, int] = {}
    by_env: dict[str, int] = {}
    by_gt: dict[str, int] = {}
    for c in cases:
        by_fault[c.fault_mode] = by_fault.get(c.fault_mode, 0) + 1
        by_env[c.environment] = by_env.get(c.environment, 0) + 1
        by_gt[c.ground_truth] = by_gt.get(c.ground_truth, 0) + 1
    return {"size": len(cases), "by_fault": by_fault, "by_environment": by_env, "by_ground_truth": by_gt}


def as_dicts(cases: list[BenchmarkCase]) -> list[dict]:
    return [asdict(c) for c in cases]
