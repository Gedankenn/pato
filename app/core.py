"""Patinho — SQLite, quotas and appointment slots. Stdlib only, no ORM."""
import os
import sqlite3
import time
import uuid

DB_PATH = os.environ.get("PATINHO_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "patinho.db"))

SCHEMA = """
create table if not exists tenants(
  id integer primary key autoincrement,
  slug text unique not null,
  name text not null,
  niche text not null default 'barbearia',
  plan text not null default 'trial',
  msg_limit integer not null default 300,
  demo_limit integer not null default 25,
  retention_days integer not null default 180,
  tz text not null default 'America/Sao_Paulo',
  created_at integer not null,
  active integer not null default 1
);
create table if not exists services(
  id integer primary key autoincrement,
  tenant_id integer not null references tenants(id) on delete cascade,
  name text not null,
  minutes integer not null default 30,
  price_cents integer not null default 0
);
create table if not exists hours(
  tenant_id integer not null references tenants(id) on delete cascade,
  weekday integer not null,
  open_min integer not null default 540,
  close_min integer not null default 1140,
  primary key(tenant_id, weekday)
);
create table if not exists appointments(
  id integer primary key autoincrement,
  tenant_id integer not null references tenants(id) on delete cascade,
  service text not null,
  client_name text not null,
  start_ts integer not null,
  end_ts integer not null,
  status text not null default 'scheduled',
  source text not null default 'demo',
  created_at integer not null
);
create index if not exists appt_tenant_start on appointments(tenant_id, start_ts);
create table if not exists conversations(
  id text primary key,
  tenant_id integer not null references tenants(id) on delete cascade,
  kind text not null default 'demo',
  created_at integer not null
);
create table if not exists messages(
  id integer primary key autoincrement,
  conversation_id text not null references conversations(id) on delete cascade,
  role text not null,
  content text not null,
  ts integer not null
);
create table if not exists usage(
  tenant_id integer not null references tenants(id) on delete cascade,
  period text not null,
  messages integer not null default 0,
  tokens_in integer not null default 0,
  tokens_out integer not null default 0,
  cost_micros integer not null default 0,
  primary key(tenant_id, period)
);
"""


def connect():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("pragma foreign_keys=on")
    return con


def init():
    with connect() as con:
        con.executescript(SCHEMA)
        tcols = {r["name"] for r in con.execute("pragma table_info(tenants)")}
        if "retention_days" not in tcols:
            con.execute("alter table tenants add column retention_days integer not null default 180")
        cols = {r["name"] for r in con.execute("pragma table_info(usage)")}
        for name, ddl in (("tokens_in", "integer not null default 0"),
                          ("tokens_out", "integer not null default 0"),
                          ("cost_micros", "integer not null default 0")):
            if name not in cols:
                con.execute(f"alter table usage add column {name} {ddl}")


def period(ts=None):
    return time.strftime("%Y-%m", time.localtime(ts or time.time()))


def seed():
    tenants = [
        ("barbearia-demo", "Barbearia do Zé", "barbearia", "pro", 2000, 25,
         [("Corte", 30, 4000), ("Barba", 20, 2500), ("Corte + barba", 50, 6000)]),
        ("salao-demo", "Studio Bella", "salão de beleza", "trial", 300, 15,
         [("Escova", 45, 7000), ("Manicure", 40, 4500), ("Coloração", 120, 18000)]),
    ]
    with connect() as con:
        for slug, name, niche, plan, limit, demo, svcs in tenants:
            if con.execute("select 1 from tenants where slug=?", (slug,)).fetchone():
                continue
            cur = con.execute(
                "insert into tenants(slug,name,niche,plan,msg_limit,demo_limit,created_at) values(?,?,?,?,?,?,?)",
                (slug, name, niche, plan, limit, demo, int(time.time())))
            tid = cur.lastrowid
            for sv, mins, price in svcs:
                con.execute("insert into services(tenant_id,name,minutes,price_cents) values(?,?,?,?)",
                            (tid, sv, mins, price))
            for wd in range(6):
                con.execute("insert into hours(tenant_id,weekday,open_min,close_min) values(?,?,?,?)",
                            (tid, wd, 540, 1140))


def tenant(slug):
    with connect() as con:
        return con.execute("select * from tenants where slug=?", (slug,)).fetchone()


def tenant_id(slug):
    t = tenant(slug)
    return t["id"] if t else None


def services(tid):
    with connect() as con:
        return con.execute("select * from services where tenant_id=? order by id", (tid,)).fetchall()


def hours(tid):
    with connect() as con:
        return {r["weekday"]: (r["open_min"], r["close_min"])
                for r in con.execute("select * from hours where tenant_id=?", (tid,))}


def appointments(tid, since=None, limit=200):
    q = "select * from appointments where tenant_id=?"
    args = [tid]
    if since:
        q += " and start_ts>=?"
        args.append(since)
    q += " order by start_ts limit ?"
    args.append(limit)
    with connect() as con:
        return con.execute(q, args).fetchall()


def conversation(tid, kind="demo"):
    cid = uuid.uuid4().hex[:12]
    with connect() as con:
        con.execute("insert into conversations(id,tenant_id,kind,created_at) values(?,?,?,?)",
                    (cid, tid, kind, int(time.time())))
    return cid


def add_message(cid, role, content):
    with connect() as con:
        con.execute("insert into messages(conversation_id,role,content,ts) values(?,?,?,?)",
                    (cid, role, content, int(time.time())))


def history(cid, limit=24):
    with connect() as con:
        rows = con.execute(
            "select role,content from messages where conversation_id=? order by id desc limit ?",
            (cid, limit)).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def user_messages(cid):
    with connect() as con:
        return con.execute("select count(*) c from messages where conversation_id=? and role='user'",
                           (cid,)).fetchone()["c"]


def bump_usage(tid, tokens_in=0, tokens_out=0, cost_micros=0):
    p = period()
    with connect() as con:
        con.execute("insert into usage(tenant_id,period,messages,tokens_in,tokens_out,cost_micros) "
                    "values(?,?,1,?,?,?) on conflict(tenant_id,period) do update set "
                    "messages=messages+1, tokens_in=tokens_in+excluded.tokens_in, "
                    "tokens_out=tokens_out+excluded.tokens_out, "
                    "cost_micros=cost_micros+excluded.cost_micros",
                    (tid, p, tokens_in, tokens_out, cost_micros))
        r = con.execute("select messages from usage where tenant_id=? and period=?", (tid, p)).fetchone()
        return r["messages"]


def usage_full(tid):
    with connect() as con:
        r = con.execute("select * from usage where tenant_id=? and period=?", (tid, period())).fetchone()
    return dict(r) if r else {"messages": 0, "tokens_in": 0, "tokens_out": 0, "cost_micros": 0}


def usage(tid):
    with connect() as con:
        r = con.execute("select messages from usage where tenant_id=? and period=?",
                        (tid, period())).fetchone()
    return r["messages"] if r else 0


def book(tid, service, client, start_ts, end_ts, source="demo"):
    """Returns appointment id, or None when the slot is taken."""
    with connect() as con:
        clash = con.execute(
            "select id from appointments where tenant_id=? and status='scheduled' "
            "and start_ts < ? and ? < end_ts", (tid, end_ts, start_ts)).fetchone()
        if clash:
            return None
        return con.execute(
            "insert into appointments(tenant_id,service,client_name,start_ts,end_ts,source,created_at) "
            "values(?,?,?,?,?,?,?)",
            (tid, service, client, start_ts, end_ts, source, int(time.time()))).lastrowid


def free_slots(tid, day_ts, service_minutes=30):
    """Open 30-min-aligned starts for a given day, minus booked ranges."""
    day = time.localtime(day_ts)
    wd = day.tm_wday
    hrs = hours(tid)
    if wd not in hrs:
        return []
    open_m, close_m = hrs[wd]
    midnight = int(time.mktime((day.tm_year, day.tm_mon, day.tm_mday, 0, 0, 0, 0, 0, -1)))
    booked = [(r["start_ts"], r["end_ts"]) for r in appointments(tid, since=midnight)
              if r["status"] == "scheduled" and r["start_ts"] < midnight + 86400]
    slots = []
    t = midnight + open_m * 60
    end_day = midnight + close_m * 60
    while t + service_minutes * 60 <= end_day:
        if not any(s < t + service_minutes * 60 and t < e for s, e in booked):
            slots.append(t)
        t += 1800
    return slots


# ------------------------------------------------------------------ LGPD
def retention_days(t):
    return int(t["retention_days"]) if "retention_days" in t.keys() else 180


def purge(tid, days=None):
    """Drops conversation content past the retention window; keeps aggregate usage."""
    days = days or 180
    cutoff = int(time.time()) - days * 86400
    with connect() as con:
        old = [r["id"] for r in con.execute(
            "select id from conversations where tenant_id=? and created_at < ?", (tid, cutoff))]
        if old:
            q = "delete from messages where conversation_id in (%s)" % ",".join("?" * len(old))
            con.execute(q, old)
            con.execute("delete from conversations where id in (%s)" % ",".join("?" * len(old)), old)
        con.execute("update appointments set client_name='(removido)' where tenant_id=? and start_ts < ?",
                    (tid, cutoff))
        return len(old)


def subject_data(tid, phone_marker):
    """Everything we hold for one contact, keyed by the conversation token."""
    with connect() as con:
        convs = con.execute(
            "select c.id, m.role, m.content, m.ts from conversations c "
            "join messages m on m.conversation_id=c.id "
            "where c.tenant_id=? and c.id like ? order by m.id", (tid, f"%{phone_marker}%")).fetchall()
        appts = con.execute(
            "select id, service, client_name, start_ts, status from appointments "
            "where tenant_id=? and (client_name=? or id in (select 0))", (tid, phone_marker)).fetchall()
    return {"messages": [dict(r) for r in convs], "appointments": [dict(r) for r in appts]}


def delete_subject(tid, phone_marker):
    with connect() as con:
        convs = [r["id"] for r in con.execute(
            "select id from conversations where tenant_id=? and id like ?", (tid, f"%{phone_marker}%"))]
        n = 0
        for cid in convs:
            n += con.execute("select count(*) c from messages where conversation_id=?", (cid,)).fetchone()["c"]
            con.execute("delete from messages where conversation_id=?", (cid,))
            con.execute("delete from conversations where id=?", (cid,))
        a = con.execute("update appointments set client_name='(removido)' where tenant_id=? and client_name=?",
                        (tid, phone_marker)).rowcount
    return {"messages_deleted": n, "appointments_anonymized": a}
