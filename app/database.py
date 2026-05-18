import sqlite3
import os
from contextlib import contextmanager
from datetime import datetime

DB_PATH = os.environ.get("PATO_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "pato.db"))


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK (status IN ('scheduled', 'rescheduled', 'cancelled')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)


def create_appointment(title, description, start_time, end_time):
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO appointments (title, description, start_time, end_time, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (title, description, start_time, end_time, now, now),
        )
        return cursor.lastrowid


def list_appointments(status=None):
    with get_connection() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM appointments WHERE status = ? ORDER BY start_time",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM appointments ORDER BY start_time"
            ).fetchall()
        return [dict(r) for r in rows]


def get_appointment(appointment_id):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM appointments WHERE id = ?", (appointment_id,)
        ).fetchone()
        return dict(row) if row else None


def reschedule_appointment(appointment_id, new_start_time, new_end_time):
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE appointments SET start_time = ?, end_time = ?, status = 'rescheduled', updated_at = ? "
            "WHERE id = ?",
            (new_start_time, new_end_time, now, appointment_id),
        )
        return cursor.rowcount > 0


def cancel_appointment(appointment_id):
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE appointments SET status = 'cancelled', updated_at = ? WHERE id = ?",
            (now, appointment_id),
        )
        return cursor.rowcount > 0
