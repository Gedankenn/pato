import sqlite3
import os
from datetime import datetime, timedelta, timezone

def _now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
import bcrypt as _bcrypt

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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_thread ON conversations(thread_id)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                barbershop_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL DEFAULT 60,
                price REAL NOT NULL DEFAULT 0.0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY (barbershop_id) REFERENCES barbershops(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS staff (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                barbershop_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY (barbershop_id) REFERENCES barbershops(id)
            )
        """)
        _migrate(conn)
        _ensure_admin(conn)


def _migrate(conn):
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(barbershops)").fetchall()]
    if "is_admin" not in cols:
        conn.execute("ALTER TABLE barbershops ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
    svc_cols = [r["name"] for r in conn.execute("PRAGMA table_info(services)").fetchall()]
    if "price_cents" in svc_cols and "price" not in svc_cols:
        conn.execute("ALTER TABLE services ADD COLUMN price REAL NOT NULL DEFAULT 0.0")
        conn.execute("UPDATE services SET price = price_cents / 100.0 WHERE price_cents > 0")
    app_cols = [r["name"] for r in conn.execute("PRAGMA table_info(appointments)").fetchall()]
    if "customer_phone" not in app_cols:
        conn.execute("ALTER TABLE appointments ADD COLUMN customer_phone TEXT DEFAULT ''")
    if "reminder_sent" not in app_cols:
        conn.execute("ALTER TABLE appointments ADD COLUMN reminder_sent INTEGER NOT NULL DEFAULT 0")
    if "staff_id" not in app_cols:
        conn.execute("ALTER TABLE appointments ADD COLUMN staff_id INTEGER DEFAULT NULL REFERENCES staff(id)")
    shop_cols = [r["name"] for r in conn.execute("PRAGMA table_info(barbershops)").fetchall()]
    if "business_type" not in shop_cols:
        conn.execute("ALTER TABLE barbershops ADD COLUMN business_type TEXT NOT NULL DEFAULT 'barbearia'")
    if "paid_until" not in shop_cols:
        conn.execute("ALTER TABLE barbershops ADD COLUMN paid_until TEXT DEFAULT NULL")
    if "whatsapp_mode" not in shop_cols:
        conn.execute("ALTER TABLE barbershops ADD COLUMN whatsapp_mode TEXT NOT NULL DEFAULT 'business'")
    if "business_info" not in shop_cols:
        conn.execute("ALTER TABLE barbershops ADD COLUMN business_info TEXT NOT NULL DEFAULT ''")


def _ensure_admin(conn):
    email = os.environ.get("ADMIN_EMAIL")
    password = os.environ.get("ADMIN_PASSWORD")
    if not email or not password:
        return  # no admin env vars set — skip admin creation
    existing = conn.execute(
        "SELECT id FROM barbershops WHERE email = ?", (email,)
    ).fetchone()
    if existing:
        conn.execute("UPDATE barbershops SET is_admin = 1 WHERE id = ?", (existing["id"],))
    else:
        now = _now_iso()
        password_hash = _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()
        conn.execute(
            "INSERT INTO barbershops (name, email, password_hash, is_admin, created_at) VALUES (?, ?, ?, 1, ?)",
            ("Administrador", email, password_hash, now),
        )


# ── Barbershops ──────────────────────────────────────────────

def create_barbershop(name: str, email: str, password: str, business_type: str = "barbearia") -> dict | None:
    now = _now_iso()
    password_hash = _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()
    try:
        with get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO barbershops (name, email, password_hash, business_type, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, email, password_hash, business_type, now),
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


def update_barbershop(barbershop_id: int, name: str | None = None, business_type: str | None = None):
    parts = []
    vals = []
    if name is not None:
        parts.append("name = ?")
        vals.append(name)
    if business_type is not None:
        parts.append("business_type = ?")
        vals.append(business_type)
    if not parts:
        return False
    vals.append(barbershop_id)
    with get_connection() as conn:
        cur = conn.execute(f"UPDATE barbershops SET {', '.join(parts)} WHERE id = ?", vals)
        return cur.rowcount > 0


def toggle_payment(barbershop_id: int):
    """Toggle paid status: if unpaid, mark paid for 30 days. If paid, mark unpaid."""
    from datetime import datetime as dt, timedelta as td, timezone as tz
    with get_connection() as conn:
        row = conn.execute("SELECT paid_until FROM barbershops WHERE id = ?", (barbershop_id,)).fetchone()
        if not row:
            return False
        paid = row["paid_until"]
        now = dt.now(tz.utc).replace(tzinfo=None)
        if paid and paid > now.strftime("%Y-%m-%d"):
            # Currently paid → mark unpaid
            conn.execute("UPDATE barbershops SET paid_until = NULL WHERE id = ?", (barbershop_id,))
        else:
            # Mark paid for 30 days
            new_date = (now + td(days=30)).strftime("%Y-%m-%d")
            conn.execute("UPDATE barbershops SET paid_until = ? WHERE id = ?", (new_date, barbershop_id,))
        return True


# ── Appointments (scoped by barbershop) ─────────────────────

def create_appointment(barbershop_id, title, description, start_time, end_time, customer_phone="", staff_id=None):
    now = _now_iso()
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO appointments (barbershop_id, title, description, start_time, end_time, customer_phone, staff_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (barbershop_id, title, description, start_time, end_time, customer_phone, staff_id, now, now),
        )
        return cursor.lastrowid


def list_appointments(barbershop_id, status=None):
    with get_connection() as conn:
        query = """
            SELECT a.*, s.name AS staff_name
            FROM appointments a
            LEFT JOIN staff s ON a.staff_id = s.id
            WHERE a.barbershop_id = ?
        """
        params = [barbershop_id]
        if status:
            query += " AND a.status = ?"
            params.append(status)
        query += " ORDER BY a.start_time"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_appointment(barbershop_id, appointment_id):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT a.*, s.name AS staff_name FROM appointments a LEFT JOIN staff s ON a.staff_id = s.id WHERE a.id = ? AND a.barbershop_id = ?",
            (appointment_id, barbershop_id),
        ).fetchone()
        return dict(row) if row else None


def reschedule_appointment(barbershop_id, appointment_id, new_start_time, new_end_time):
    now = _now_iso()
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE appointments SET start_time = ?, end_time = ?, status = 'rescheduled', updated_at = ? "
            "WHERE id = ? AND barbershop_id = ?",
            (new_start_time, new_end_time, now, appointment_id, barbershop_id),
        )
        return cursor.rowcount > 0


def cancel_appointment(barbershop_id, appointment_id):
    now = _now_iso()
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE appointments SET status = 'cancelled', updated_at = ? WHERE id = ? AND barbershop_id = ?",
            (now, appointment_id, barbershop_id),
        )
        return cursor.rowcount > 0


# ── Conversations ─────────────────────────────────────────────

def save_message(thread_id: str, role: str, content: str):
    now = _now_iso()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO conversations (thread_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (thread_id, role, content, now),
        )


def get_conversation(thread_id: str, limit: int = 10):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT role, content FROM conversations WHERE thread_id = ? ORDER BY created_at DESC LIMIT ?",
            (thread_id, limit),
        ).fetchall()
        result = [dict(r) for r in rows]
        result.reverse()
        return result


def update_appointment(barbershop_id, appointment_id, title=None, description=None):
    now = _now_iso()
    parts = ["updated_at = ?"]
    vals = [now]
    if title is not None:
        parts.append("title = ?")
        vals.append(title)
    if description is not None:
        parts.append("description = ?")
        vals.append(description)
    vals += [appointment_id, barbershop_id]
    with get_connection() as conn:
        cursor = conn.execute(
            f"UPDATE appointments SET {', '.join(parts)} WHERE id = ? AND barbershop_id = ?",
            vals,
        )
        return cursor.rowcount > 0


# ── Admin ─────────────────────────────────────────────────────

def list_all_barbershops():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, name, email, whatsapp_number, is_admin, business_type, paid_until, created_at FROM barbershops ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def get_tomorrow_appointments():
    """Returns appointments for tomorrow that haven't been reminded yet."""
    tomorrow = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)).strftime("%Y-%m-%d")
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT a.*, b.whatsapp_number FROM appointments a JOIN barbershops b ON a.barbershop_id = b.id "
            "WHERE a.status = 'scheduled' AND a.reminder_sent = 0 "
            "AND a.customer_phone != '' AND DATE(a.start_time) = ?",
            (tomorrow,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_reminder_sent(appointment_id: int):
    with get_connection() as conn:
        conn.execute("UPDATE appointments SET reminder_sent = 1, updated_at = ? WHERE id = ?",
                     (_now_iso(), appointment_id))


def list_all_appointments():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT a.*, b.name AS barbershop_name
            FROM appointments a
            LEFT JOIN barbershops b ON a.barbershop_id = b.id
            ORDER BY a.created_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


# ── Services ──────────────────────────────────────────────

def list_services(barbershop_id: int, active_only: bool = True):
    with get_connection() as conn:
        query = "SELECT * FROM services WHERE barbershop_id = ?"
        params = [barbershop_id]
        if active_only:
            query += " AND active = 1"
        query += " ORDER BY name"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def create_service(barbershop_id: int, name: str, duration_minutes: int, price: float):
    now = _now_iso()
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO services (barbershop_id, name, duration_minutes, price, created_at) VALUES (?, ?, ?, ?, ?)",
            (barbershop_id, name, duration_minutes, price, now),
        )
        return cursor.lastrowid


def update_service(service_id: int, barbershop_id: int, name: str | None = None,
                   duration_minutes: int | None = None, price: float | None = None,
                   active: bool | None = None):
    parts = []
    vals = []
    if name is not None:
        parts.append("name = ?")
        vals.append(name)
    if duration_minutes is not None:
        parts.append("duration_minutes = ?")
        vals.append(duration_minutes)
    if price is not None:
        parts.append("price = ?")
        vals.append(price)
    if active is not None:
        parts.append("active = ?")
        vals.append(1 if active else 0)
    if not parts:
        return False
    vals += [service_id, barbershop_id]
    with get_connection() as conn:
        cursor = conn.execute(
            f"UPDATE services SET {', '.join(parts)} WHERE id = ? AND barbershop_id = ?",
            vals,
        )
        return cursor.rowcount > 0


def delete_service(service_id: int, barbershop_id: int):
    with get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM services WHERE id = ? AND barbershop_id = ?",
            (service_id, barbershop_id),
        )
        return cursor.rowcount > 0


def delete_barbershop(barbershop_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM appointments WHERE barbershop_id = ?", (barbershop_id,))
        conn.execute("DELETE FROM services WHERE barbershop_id = ?", (barbershop_id,))
        conn.execute("DELETE FROM staff WHERE barbershop_id = ?", (barbershop_id,))
        conn.execute("DELETE FROM conversations WHERE thread_id IN (SELECT thread_id FROM conversations WHERE thread_id LIKE ?)", (f"%_{barbershop_id}",))
        cursor = conn.execute("DELETE FROM barbershops WHERE id = ?", (barbershop_id,))
        return cursor.rowcount > 0


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


def get_barbershop_stats(barbershop_id: int, days: int = 30):
    with get_connection() as conn:
        since = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).isoformat()
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM appointments WHERE barbershop_id = ? AND created_at >= ?",
            (barbershop_id, since),
        ).fetchone()["c"]
        scheduled = conn.execute(
            "SELECT COUNT(*) AS c FROM appointments WHERE barbershop_id = ? AND status = 'scheduled' AND created_at >= ?",
            (barbershop_id, since),
        ).fetchone()["c"]
        cancelled = conn.execute(
            "SELECT COUNT(*) AS c FROM appointments WHERE barbershop_id = ? AND status = 'cancelled' AND created_at >= ?",
            (barbershop_id, since),
        ).fetchone()["c"]
        by_service = conn.execute(
            "SELECT title AS service, COUNT(*) AS count FROM appointments WHERE barbershop_id = ? AND created_at >= ? GROUP BY title ORDER BY count DESC",
            (barbershop_id, since),
        ).fetchall()
        by_day = conn.execute(
            "SELECT DATE(start_time) AS day, COUNT(*) AS count FROM appointments WHERE barbershop_id = ? AND status = 'scheduled' AND start_time >= ? GROUP BY day ORDER BY day",
            (barbershop_id, since),
        ).fetchall()
        # revenue by service: join with services table
        revenue = conn.execute(
            "SELECT a.title AS service, COUNT(*) AS count, COALESCE(s.price, 0) AS price FROM appointments a LEFT JOIN services s ON a.barbershop_id = s.barbershop_id AND LOWER(a.title) = LOWER(s.name) AND s.active = 1 WHERE a.barbershop_id = ? AND a.status = 'scheduled' AND a.start_time >= ? GROUP BY a.title ORDER BY count DESC",
            (barbershop_id, since),
        ).fetchall()
        return {
            "total": total,
            "scheduled": scheduled,
            "cancelled": cancelled,
            "by_service": [dict(r) for r in by_service],
            "by_day": [dict(r) for r in by_day],
            "revenue": [{"service": r["service"], "count": r["count"], "price": r["price"], "total": r["count"] * r["price"]} for r in revenue],
        }


# ── Staff ──────────────────────────────────────────────────

def list_staff(barbershop_id: int, active_only: bool = True):
    with get_connection() as conn:
        query = "SELECT * FROM staff WHERE barbershop_id = ?"
        params = [barbershop_id]
        if active_only:
            query += " AND active = 1"
        query += " ORDER BY name"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def create_staff(barbershop_id: int, name: str):
    now = _now_iso()
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO staff (barbershop_id, name, created_at) VALUES (?, ?, ?)",
            (barbershop_id, name, now),
        )
        return cursor.lastrowid


def update_staff(staff_id: int, barbershop_id: int, name: str | None = None, active: bool | None = None):
    parts = []
    vals = []
    if name is not None:
        parts.append("name = ?")
        vals.append(name)
    if active is not None:
        parts.append("active = ?")
        vals.append(1 if active else 0)
    if not parts:
        return False
    vals += [staff_id, barbershop_id]
    with get_connection() as conn:
        cursor = conn.execute(
            f"UPDATE staff SET {', '.join(parts)} WHERE id = ? AND barbershop_id = ?", vals
        )
        return cursor.rowcount > 0


def delete_staff(staff_id: int, barbershop_id: int):
    with get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM staff WHERE id = ? AND barbershop_id = ?", (staff_id, barbershop_id)
        )
        return cursor.rowcount > 0
