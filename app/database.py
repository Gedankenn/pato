import sqlite3
import os
from datetime import datetime
import bcrypt as _bcrypt

DB_PATH = os.environ.get("PATO_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "pato.db"))


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@patoagenda.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")


def init_db():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS barbershops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                whatsapp_number TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                barbershop_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK (status IN ('scheduled', 'rescheduled', 'cancelled')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (barbershop_id) REFERENCES barbershops(id)
            )
        """)
        _migrate(conn)
        _ensure_admin(conn)


def _migrate(conn):
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(barbershops)").fetchall()]
    if "is_admin" not in cols:
        conn.execute("ALTER TABLE barbershops ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")


def _ensure_admin(conn):
    existing = conn.execute(
        "SELECT id FROM barbershops WHERE email = ?", (ADMIN_EMAIL,)
    ).fetchone()
    if existing:
        conn.execute("UPDATE barbershops SET is_admin = 1 WHERE id = ?", (existing["id"],))
    else:
        now = datetime.utcnow().isoformat()
        password_hash = _bcrypt.hashpw(ADMIN_PASSWORD.encode(), _bcrypt.gensalt()).decode()
        conn.execute(
            "INSERT INTO barbershops (name, email, password_hash, is_admin, created_at) VALUES (?, ?, ?, 1, ?)",
            ("Administrador", ADMIN_EMAIL, password_hash, now),
        )


# ── Barbershops ──────────────────────────────────────────────

def create_barbershop(name: str, email: str, password: str) -> dict | None:
    now = datetime.utcnow().isoformat()
    password_hash = _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()
    try:
        with get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO barbershops (name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (name, email, password_hash, now),
            )
            row = conn.execute("SELECT * FROM barbershops WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return dict(row) if row else None
    except sqlite3.IntegrityError:
        return None


def get_barbershop(barbershop_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM barbershops WHERE id = ?", (barbershop_id,)).fetchone()
        return dict(row) if row else None


def get_barbershop_by_email(email: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM barbershops WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None


def verify_password(email: str, password: str) -> dict | None:
    shop = get_barbershop_by_email(email)
    if shop and _bcrypt.checkpw(password.encode(), shop["password_hash"].encode()):
        return shop
    return None


def set_whatsapp_number(barbershop_id: int, number: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE barbershops SET whatsapp_number = ? WHERE id = ?",
            (number, barbershop_id),
        )


# ── Appointments (scoped by barbershop) ─────────────────────

def create_appointment(barbershop_id, title, description, start_time, end_time):
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO appointments (barbershop_id, title, description, start_time, end_time, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (barbershop_id, title, description, start_time, end_time, now, now),
        )
        return cursor.lastrowid


def list_appointments(barbershop_id, status=None):
    with get_connection() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM appointments WHERE barbershop_id = ? AND status = ? ORDER BY start_time",
                (barbershop_id, status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM appointments WHERE barbershop_id = ? ORDER BY start_time",
                (barbershop_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_appointment(barbershop_id, appointment_id):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM appointments WHERE id = ? AND barbershop_id = ?",
            (appointment_id, barbershop_id),
        ).fetchone()
        return dict(row) if row else None


def reschedule_appointment(barbershop_id, appointment_id, new_start_time, new_end_time):
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE appointments SET start_time = ?, end_time = ?, status = 'rescheduled', updated_at = ? "
            "WHERE id = ? AND barbershop_id = ?",
            (new_start_time, new_end_time, now, appointment_id, barbershop_id),
        )
        return cursor.rowcount > 0


def cancel_appointment(barbershop_id, appointment_id):
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE appointments SET status = 'cancelled', updated_at = ? WHERE id = ? AND barbershop_id = ?",
            (now, appointment_id, barbershop_id),
        )
        return cursor.rowcount > 0


# ── Admin ─────────────────────────────────────────────────────

def list_all_barbershops():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, name, email, whatsapp_number, is_admin, created_at FROM barbershops ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def list_all_appointments():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT a.*, b.name AS barbershop_name
            FROM appointments a
            LEFT JOIN barbershops b ON a.barbershop_id = b.id
            ORDER BY a.created_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


def get_stats():
    with get_connection() as conn:
        shops = conn.execute("SELECT COUNT(*) AS c FROM barbershops").fetchone()["c"]
        appointments = conn.execute("SELECT COUNT(*) AS c FROM appointments").fetchone()["c"]
        scheduled = conn.execute("SELECT COUNT(*) AS c FROM appointments WHERE status = 'scheduled'").fetchone()["c"]
        cancelled = conn.execute("SELECT COUNT(*) AS c FROM appointments WHERE status = 'cancelled'").fetchone()["c"]
        return {
            "barbershops": shops,
            "appointments": appointments,
            "scheduled": scheduled,
            "cancelled": cancelled,
        }
