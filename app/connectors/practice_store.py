"""Generate the "Practice store" - a realistic, fictional shop database.

It is an ordinary SQLite file connected like any user database, so the real AI
agent and the verifier are exercised exactly as on your own data. Everything is
deterministic (fixed seed) and fictional: names are invented, e-mail addresses
use the reserved ``.example`` domain, phone numbers the 555-01xx range.
"""

from __future__ import annotations

import random
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

FIRST = ["Maria", "Liam", "Aisha", "Noah", "Chen", "Sofia", "Mateo", "Hana", "Lucas", "Amara", "Elias", "Yuki", "Omar",
         "Ines", "Ravi", "Freya", "Tomas", "Leila", "Jonas", "Priya", "Diego", "Mila", "Kofi", "Anya", "Felix", "Zara",
         "Arjun", "Nora", "Hugo", "Lea"]
LAST = ["Garcia", "Okafor", "Novak", "Silva", "Tanaka", "Muller", "Rossi", "Haddad", "Kowalski", "Dubois", "Nakamura",
        "Andersen", "Costa", "Ivanova", "Mensah", "Fischer", "Patel", "Laurent", "Moreno", "Berg"]
CITIES = [("Berlin", "Germany"), ("Munich", "Germany"), ("Paris", "France"), ("Lyon", "France"), ("Madrid", "Spain"),
          ("Lisbon", "Portugal"), ("Rome", "Italy"), ("Vienna", "Austria"), ("Amsterdam", "Netherlands"),
          ("Warsaw", "Poland"), ("Oslo", "Norway"), ("Dublin", "Ireland")]
CATEGORIES = {
    "Office": ["Ergonomic Chair", "Standing Desk", "Desk Lamp", "Monitor Arm", "Cable Tray", "Notebook Set"],
    "Electronics": ["USB-C Hub", "Wireless Mouse", "Mechanical Keyboard", "27in Monitor", "Webcam HD", "Noise-Cancelling Headset",
                    "Portable SSD 1TB", "Docking Station"],
    "Kitchen": ["Coffee Grinder", "Espresso Cups (6)", "Electric Kettle", "Water Filter Jug", "Tea Sampler"],
    "Outdoor": ["Trail Backpack", "Insulated Bottle", "Camping Lantern", "Rain Jacket", "Folding Stool"],
    "Books": ["Data Pipelines Handbook", "Practical Statistics", "The Calm Office", "Writing Clear Specs"],
}
DEPARTMENTS = ["Sales", "Support", "Warehouse", "Finance", "Marketing", "Engineering"]
STATUSES = ["pending", "paid", "shipped", "delivered", "cancelled"]

SCHEMA_SQL = """
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE,
    phone TEXT,
    city TEXT,
    country TEXT,
    tier TEXT NOT NULL DEFAULT 'standard' CHECK (tier IN ('standard', 'gold', 'platinum')),
    marketing_opt_in INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    price REAL NOT NULL,
    stock INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'paid', 'shipped', 'delivered', 'cancelled')),
    order_date TEXT NOT NULL,
    total REAL NOT NULL,
    notes TEXT,
    updated_at TEXT
);
CREATE TABLE order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL,
    unit_price REAL NOT NULL
);
CREATE TABLE employees (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE,
    department TEXT NOT NULL,
    title TEXT,
    manager_id INTEGER REFERENCES employees(id),
    active INTEGER NOT NULL DEFAULT 1
);
"""


def generate(path: Path, seed: int = 7) -> dict[str, int]:
    """(Re)create the practice store at ``path``. Returns row counts per table."""
    rng = random.Random(seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    with closing(sqlite3.connect(tmp)) as con:
        con.executescript(SCHEMA_SQL)
        base = date(2026, 1, 5)

        used_emails: set[str] = set()
        customers = []
        for i in range(1, 121):
            first, last = rng.choice(FIRST), rng.choice(LAST)
            if i in (17, 64):  # two customers deliberately share a name (ambiguity test)
                first, last = "Maria", "Garcia"
            city, country = CITIES[(i * 7 + rng.randrange(3)) % len(CITIES)]
            email = f"{first}.{last}{i}@mail.example".lower()
            used_emails.add(email)
            tier = rng.choices(["standard", "gold", "platinum"], weights=[70, 22, 8])[0]
            created = base + timedelta(days=rng.randrange(0, 200))
            customers.append((i, f"{first} {last}", email, f"+1-555-01{rng.randrange(0, 100):02d}", city, country, tier,
                              rng.randrange(2), created.isoformat(), None))
        con.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?,?,?)", customers)

        products = []
        pid = 1
        for cat, names in CATEGORIES.items():
            for n in names:
                price = round(rng.uniform(6, 420), 2)
                products.append((pid, f"{cat[:3].upper()}-{1000 + pid}", n, cat, price, rng.randrange(0, 180),
                                 0 if pid in (9, 22) else 1))
                pid += 1
        con.executemany("INSERT INTO products VALUES (?,?,?,?,?,?,?)", products)

        items = []
        orders = []
        item_id = 1
        for oid in range(1, 301):
            cust = rng.randrange(1, 121)
            day = base + timedelta(days=rng.randrange(0, 300))
            status = rng.choices(STATUSES, weights=[14, 18, 22, 38, 8])[0]
            total = 0.0
            for _ in range(rng.randrange(1, 4)):
                p = products[rng.randrange(len(products))]
                qty = rng.randrange(1, 4)
                items.append((item_id, oid, p[0], qty, p[4]))
                total += qty * p[4]
                item_id += 1
            orders.append((oid, cust, status, day.isoformat(), round(total, 2), None, None))
        con.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", orders)
        con.executemany("INSERT INTO order_items VALUES (?,?,?,?,?)", items)

        employees = []
        for eid in range(1, 26):
            first, last = rng.choice(FIRST), rng.choice(LAST)
            dept = DEPARTMENTS[eid % len(DEPARTMENTS)]
            manager = None if eid <= len(DEPARTMENTS) else (eid % len(DEPARTMENTS)) or len(DEPARTMENTS)
            title = "Head of " + dept if manager is None else rng.choice(["Specialist", "Associate", "Coordinator", "Analyst"])
            employees.append((eid, f"{first} {last}", f"{first}.{last}.{eid}@shop.example".lower(), dept, title, manager,
                              0 if eid in (13, 21) else 1))
        con.executemany("INSERT INTO employees VALUES (?,?,?,?,?,?,?)", employees)
        con.commit()
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("customers", "products", "orders", "order_items", "employees")}
    tmp.replace(path)
    return counts
