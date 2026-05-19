#!/usr/bin/env python3
"""Seed PatoBarba demo barbershop with services."""
import os
import sys
import sqlite3

DB_PATH = os.environ.get("PATO_DB_PATH", "/data/pato.db")

DEMO_EMAIL = "demo@patobarba.com"
DEMO_PASS = "patobarba123"

SERVICES = [
    ("Corte de Cabelo", 45, 50.0),
    ("Barba", 20, 30.0),
    ("Corte + Barba", 60, 80.0),
    ("Hidratação", 30, 40.0),
    ("Sobrancelha", 15, 20.0),
    ("Pigmentação Capilar", 60, 120.0),
]


def seed():
    if not os.path.exists(DB_PATH):
        print(f"DB not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Check if demo already exists
    cur.execute("SELECT id FROM barbershops WHERE email = ?", (DEMO_EMAIL,))
    row = cur.fetchone()
    if row:
        print(f"PatoBarba already exists (id={row['id']}), updating services...")
        bid = row["id"]
        cur.execute("DELETE FROM services WHERE barbershop_id = ?", (bid,))
    else:
        import bcrypt
        pw_hash = bcrypt.hashpw(DEMO_PASS.encode(), bcrypt.gensalt()).decode()
        from datetime import datetime
        now = datetime.utcnow().isoformat()
        cur.execute(
            "INSERT INTO barbershops (name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
            ("PatoBarba", DEMO_EMAIL, pw_hash, now),
        )
        bid = cur.lastrowid
        print(f"PatoBarba created (id={bid})")

    for name, dur, price in SERVICES:
        from datetime import datetime
        cur.execute(
            "INSERT INTO services (barbershop_id, name, duration_minutes, price, created_at) VALUES (?, ?, ?, ?, ?)",
            (bid, name, dur, price, datetime.utcnow().isoformat()),
        )
        print(f"  + {name}: R${price:.2f} ({dur}min)")

    conn.commit()
    conn.close()
    print(f"\nDone! Login with:\n  Email: {DEMO_EMAIL}\n  Pass:  {DEMO_PASS}")


if __name__ == "__main__":
    seed()
