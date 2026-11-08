"""Regenerate the deterministic synthetic sample data in ``sample_data/``.

All people, companies, e-mail addresses and phone numbers are fictional:
e-mail domains use the reserved ``.example`` TLD and phone numbers use the
fictional 555-01xx range. Run with:  python scripts/generate_sample_data.py
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "sample_data"
SEED = 20261107

FIRST = ["Aarav", "Maya", "Liam", "Sofia", "Noah", "Priya", "Ethan", "Chloe", "Ravi", "Hana", "Lucas", "Amara",
         "Diego", "Mei", "Omar", "Isla", "Arjun", "Zara", "Felix", "Nora", "Kenji", "Leah", "Mateo", "Ava",
         "Samir", "Elena", "Jonas", "Tara", "Victor", "Yuki"]
LAST = ["Sharma", "Patel", "Nguyen", "Garcia", "Okafor", "Schmidt", "Rossi", "Haddad", "Kowalski", "Tanaka",
        "Silva", "Mensah", "Johansson", "Dubois", "Reyes", "Ali", "Fischer", "Costa", "Novak", "Larsen"]
DEPARTMENTS = ["Engineering", "Sales", "Marketing", "Finance", "Human Resources", "Operations", "Support", "Legal"]
TITLES = {
    "Engineering": ["Software Engineer", "Senior Engineer", "QA Engineer", "Engineering Manager"],
    "Sales": ["Account Executive", "Sales Manager", "Sales Associate"],
    "Marketing": ["Marketing Specialist", "Content Lead", "Brand Manager"],
    "Finance": ["Financial Analyst", "Accountant", "Controller"],
    "Human Resources": ["HR Generalist", "Recruiter", "HR Manager"],
    "Operations": ["Operations Analyst", "Logistics Coordinator", "Operations Manager"],
    "Support": ["Support Specialist", "Support Lead"],
    "Legal": ["Paralegal", "Counsel"],
}
STREETS = ["Maple Avenue", "Harbor Road", "Cedar Lane", "Sunset Boulevard", "Elm Street", "Riverside Drive",
           "Oak Court", "Hillcrest Way", "Lakeview Terrace", "Market Street"]
CITIES = ["Springfield", "Riverton", "Lakeside", "Fairview", "Brookfield", "Greenville"]
COMPANY_A = ["Blue", "Bright", "North", "Silver", "Rapid", "Green", "Prime", "Urban", "Summit", "Clear"]
COMPANY_B = ["Harbor", "Leaf", "Peak", "Stone", "Wave", "Field", "Forge", "Bridge", "Grove", "Line"]
COMPANY_C = ["Labs", "Logistics", "Retail", "Foods", "Studios", "Systems", "Health", "Partners"]


def main() -> int:
    rng = random.Random(SEED)
    OUT.mkdir(exist_ok=True)

    employees = []
    used = set()
    for emp_id in range(1001, 1061):
        while True:
            first, last = rng.choice(FIRST), rng.choice(LAST)
            if (first, last) not in used:
                used.add((first, last))
                break
        dept = rng.choice(DEPARTMENTS)
        employees.append({
            "id": emp_id,
            "name": f"{first} {last}",
            "email": f"{first.lower()}.{last.lower()}@acme.example",
            "department": dept,
            "title": rng.choice(TITLES[dept]),
            "phone": f"+1-555-01{rng.randint(0, 99):02d}",
        })
    # Two employees deliberately share a name to demonstrate ambiguity handling.
    for emp_id, dept in ((1015, "Engineering"), (1048, "Sales")):
        e = employees[emp_id - 1001]
        e.update(name="Alex Kim", email=f"alex.kim{emp_id}@acme.example", department=dept, title=TITLES[dept][0])

    customers = []
    for cust_id in range(101, 131):
        name = f"{rng.choice(COMPANY_A)}{rng.choice(COMPANY_B)} {rng.choice(COMPANY_C)}"
        slug = name.split()[0].lower()
        customers.append({
            "id": cust_id,
            "name": name,
            "email": f"contact@{slug}{cust_id}.example",
            "phone": f"+1-555-01{rng.randint(0, 99):02d}",
            "address": f"{rng.randint(1, 999)} {rng.choice(STREETS)}, {rng.choice(CITIES)}",
            "tier": rng.choice(["standard", "standard", "gold", "platinum"]),
        })

    ticket_titles = [
        "VPN disconnects every hour", "Printer on floor 3 is jammed", "Cannot access shared drive",
        "Laptop battery drains quickly", "Request for second monitor", "Email sync delayed on mobile",
        "Password reset not received", "Conference room display flickers", "Software license renewal",
        "Slow Wi-Fi in east wing", "Expense tool returns error 500", "New hire onboarding checklist",
        "Calendar invites show wrong time zone", "Backup job failed overnight", "Request access to analytics dashboard",
        "Keyboard keys sticking", "CRM export missing columns", "Update antivirus definitions",
        "Projector remote missing", "Badge reader offline at entrance",
    ]
    tickets = []
    for tid, title in enumerate(ticket_titles, start=1):
        tickets.append({
            "id": tid,
            "title": title,
            "description": f"Synthetic ticket #{tid}: {title.lower()}.",
            "priority": rng.choice(["low", "medium", "medium", "high", "critical"]),
            "status": rng.choice(["open", "open", "in_progress", "resolved"]),
            "assignee": rng.choice([None, "Priya Sharma", "Liam Nguyen", "Hana Tanaka", "Omar Haddad"]),
        })

    (OUT / "synthetic_employees.json").write_text(json.dumps(employees, indent=2) + "\n", encoding="utf-8")
    (OUT / "synthetic_customers.json").write_text(json.dumps(customers, indent=2) + "\n", encoding="utf-8")
    (OUT / "synthetic_tickets.json").write_text(json.dumps(tickets, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(employees)} employees, {len(customers)} customers, {len(tickets)} tickets to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
