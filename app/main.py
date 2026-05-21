import json
import os
import re
from datetime import datetime, timedelta, timezone
import httpx
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

from fastapi import FastAPI, HTTPException, Request, Query, Depends, Cookie
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.schemas import (
    AppointmentCreate,
    AppointmentReschedule,
    AppointmentResponse,
    MessageRequest,
    MessageResponse,
    RegisterRequest,
    LoginRequest,
    AuthResponse,
    ServiceCreate,
    ServiceUpdate,
    ServiceResponse,
    StaffCreate,
    StaffUpdate,
    StaffResponse,
)
from pydantic import BaseModel
from app import database as db
from app.llm import build_system_prompt
from app.auth import create_token, get_current_barbershop_id, get_barbershop_id_from_request, require_admin

app = FastAPI(title="PatoAgenda AI — Agendamentos Inteligentes")

app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

WEBHOOK_VERIFY_TOKEN = os.environ.get("WEBHOOK_VERIFY_TOKEN") or os.urandom(16).hex()
if not os.environ.get("WEBHOOK_VERIFY_TOKEN"):
    print(f"[webhook] WEBHOOK_VERIFY_TOKEN not set — generated random: {WEBHOOK_VERIFY_TOKEN}")
WHATSAPP_DATA_DIR = os.environ.get(
    "WHATSAPP_DATA_DIR",
    os.path.join(os.path.dirname(__file__), "..", "whatsapp", "data"),
)


def get_openai():
    base_url = os.environ.get("OPENAI_BASE_URL")
    api_key = os.environ.get("OPENAI_API_KEY", "not-needed")
    kwargs = {"api_key": api_key, "timeout": 300}
    if base_url:
        kwargs["base_url"] = base_url
    return AsyncOpenAI(**kwargs)


LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")


@app.on_event("startup")
def startup():
    db.init_db()
    import threading
    t = threading.Thread(target=_reminder_loop, daemon=True)
    t.start()


def _reminder_loop():
    """Envia lembretes WhatsApp 1 dia antes, sempre as 17:00."""
    import time
    while True:
        now = datetime.now()
        target = now.replace(hour=17, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        sleep_secs = (target - now).total_seconds()
        time.sleep(sleep_secs)

        try:
            appts = db.get_tomorrow_appointments()
            by_shop_phone: dict[tuple[int, str], list[dict]] = {}
            for a in appts:
                phone = a.get("customer_phone")
                if not phone:
                    continue
                key = (a["barbershop_id"], phone)
                by_shop_phone.setdefault(key, []).append(a)

            for (shop_id, phone), items in by_shop_phone.items():
                name = items[0].get("description") or "Cliente"
                lines = [f"Ola {name}! Lembrete dos seus horarios de amanha:"]
                for a in items:
                    lines.append(f"- {a['title']} as {a['start_time'][11:16]}")
                lines.append("\nAguardamos voce! 🐱")
                msg = "\n".join(lines)
                try:
                    r = httpx.post(
                        f"{WHATSAPP_MANAGER_URL}/manager/send",
                        json={"barbershop_id": shop_id, "to": phone, "text": msg},
                        timeout=10,
                    )
                    if r.status_code == 200:
                        for a in items:
                            db.mark_reminder_sent(a["id"])
                except Exception:
                    pass
        except Exception:
            pass


def format_appointment(a: dict) -> str:
    name = f" ({a['description']})" if a.get('description') else ""
    staff = f" com {a['staff_name']}" if a.get('staff_name') else ""
    return (
        f"[#{a['id']}] {a['title']}{name}{staff} - {a['start_time']} to {a['end_time']} "
        f"({a['status']})"
    )


def execute_action(action: str, params: dict, barbershop_id: int, customer_phone: str = "") -> tuple[str | None, int | None]:
    if action == "create_appointment":
        title = params.get("title", "").strip()
        if not title:
            return "Title is required. Ask the customer what service they want.", None
        start = params.get("start_time")
        end = params.get("end_time")
        if not start or not end:
            return "start_time and end_time are required. Ask the customer when they want to book.", None
        if "2025" in start or "AAAA" in start or "YYYY" in start:
            return "Invalid date — you copied the example. Use the actual date and time the customer requested (format: 2026-05-20T13:00).", None
        staff_id = params.get("staff_id")
        if not staff_id:
            active_staff = db.list_staff(barbershop_id, active_only=True)
            if active_staff:
                import random
                staff_id = random.choice(active_staff)["id"]
        description = params.get("description", "").strip()
        if not description:
            return "description (nome do cliente) is required. Ask who the appointment is for.", None

        # Double-booking check: same staff, overlapping time
        existing = db.list_appointments(barbershop_id, status="scheduled")
        for a in existing:
            if a["start_time"] < end and start < a["end_time"]:
                if staff_id and a.get("staff_id") == staff_id:
                    return f"Horário já reservado para {a['staff_name'] or 'este funcionário'} nesse período ({a['start_time'][11:16]}-{a['end_time'][11:16]}). Escolha outro horário.", None
                if not staff_id and not a.get("staff_id"):
                    return f"Horário já reservado ({a['start_time'][11:16]}-{a['end_time'][11:16]}). Escolha outro horário.", None

        appt_id = db.create_appointment(
            barbershop_id=barbershop_id,
            title=title,
            description=description,
            start_time=start,
            end_time=end,
            customer_phone=customer_phone,
            staff_id=staff_id,
        )
        a = db.get_appointment(barbershop_id, appt_id)
        return f"Created: {format_appointment(a)}", appt_id

    elif action == "list_appointments":
        appointments = db.list_appointments(barbershop_id, status=params.get("status"))
        if not appointments:
            return "No appointments found.", None
        return "\n".join(format_appointment(a) for a in appointments), None

    elif action == "get_appointment":
        aid = params.get("appointment_id")
        if not aid:
            return "appointment_id is required.", None
        a = db.get_appointment(barbershop_id, aid)
        if not a:
            return f"Appointment #{aid} not found.", None
        return (
            f"Appointment #{a['id']}: {a['title']}\n"
            f"  Description: {a['description']}\n"
            f"  Start: {a['start_time']}\n"
            f"  End: {a['end_time']}\n"
            f"  Status: {a['status']}"
        ), None

    elif action == "reschedule_appointment":
        aid = params.get("appointment_id")
        new_start = params.get("new_start_time")
        new_end = params.get("new_end_time")
        if not aid or not new_start or not new_end:
            return "appointment_id, new_start_time and new_end_time are required.", None
        success = db.reschedule_appointment(barbershop_id, aid, new_start, new_end)
        if not success:
            return f"Appointment #{aid} not found.", None
        a = db.get_appointment(barbershop_id, aid)
        return f"Rescheduled: {format_appointment(a)}", None

    elif action == "cancel_appointment":
        aid = params.get("appointment_id")
        if not aid:
            return "appointment_id is required. Ask which appointment to cancel.", None
        success = db.cancel_appointment(barbershop_id, aid)
        if not success:
            return f"Appointment #{aid} not found.", None
        a = db.get_appointment(barbershop_id, aid)
        cancelled_start = a["start_time"]
        nearby = db.list_appointments(barbershop_id, status="scheduled")
        suggestions = []
        for na in nearby:
            if abs((datetime.fromisoformat(na["start_time"]) - datetime.fromisoformat(cancelled_start)).total_seconds()) < 7200:
                suggestions.append(f"- {na['title']} #{na['id']} em {na['start_time'][:16]}")
        result = f"Cancelled: {format_appointment(a)}"
        if suggestions:
            result += "\n\nHorários próximos que ainda estão ocupados:\n" + "\n".join(suggestions) + "\n\nPergunte ao cliente se ele quer avisar alguém ou se há interesse em reagendar um desses para o horário liberado."
        return result, None

    elif action == "update_appointment":
        aid = params.get("appointment_id")
        if not aid:
            return "appointment_id is required.", None
        success = db.update_appointment(
            barbershop_id,
            aid,
            title=params.get("title"),
            description=params.get("description"),
        )
        if not success:
            return f"Appointment #{aid} not found.", None
        a = db.get_appointment(barbershop_id, aid)
        return f"Updated: {format_appointment(a)}", None

    return None, None


def extract_json(text: str) -> dict | None:
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # If no valid JSON, treat entire text as a reply message
    if text:
        return {"action": "reply", "parameters": {}, "message": text}
    return None


async def call_llm(messages: list) -> str:
    try:
        response = await get_openai().chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            temperature=0.1,
        )
        return response.choices[0].message.content or ""
    except Exception:
        return ""


def get_wa_status_dir(barbershop_id: int) -> str:
    return os.path.join(WHATSAPP_DATA_DIR, str(barbershop_id))


# ── Auth ────────────────────────────────────────────────────────

@app.post("/auth/login")
def login(body: LoginRequest):
    shop = db.verify_password(body.email, body.password)
    if not shop:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = create_token(shop["id"])
    from fastapi.responses import JSONResponse
    resp = JSONResponse(content={
        "token": token,
        "barbershop_id": shop["id"],
        "name": shop["name"],
        "email": shop["email"],
        "whatsapp_number": shop.get("whatsapp_number"),
        "is_admin": shop.get("is_admin", 0),
    })
    resp.set_cookie(key="token", value=token, httponly=True, max_age=2592000, path="/")
    return resp


@app.post("/auth/register")
def register(body: RegisterRequest):
    shop = db.create_barbershop(body.name, body.email, body.password, business_type=body.business_type)
    if not shop:
        raise HTTPException(status_code=409, detail="Email already registered")
    token = create_token(shop["id"])
    from fastapi.responses import JSONResponse
    resp = JSONResponse(content={
        "token": token,
        "barbershop_id": shop["id"],
        "name": shop["name"],
        "email": shop["email"],
    })
    resp.set_cookie(key="token", value=token, httponly=True, max_age=2592000, path="/")
    return resp


# ── Chat ────────────────────────────────────────────────────────

import threading
_last_appointment: dict[str, int] = {}
_last_lock = threading.Lock()
_scheduling_window: dict[str, bool] = {}  # thread_id -> True when in active scheduling conversation


def _load_json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _header_html(shop_name: str, page: str, is_admin: bool = False) -> str:
    pages = [
        ("dashboard", "📋 Agenda"),
        ("config", "⚙️ Config"),
        ("reports", "📊 Relat\u00f3rios"),
    ]
    links = "".join(
        f'<a href="/{k}" class="nav-item{" active" if k == page else ""}">{v}</a>'
        for k, v in pages
    )
    admin = f'<a href="/admin" class="nav-item">🛡️ Admin</a>' if is_admin else ""
    return f"""<div class="topbar"><div class="topbar-inner">
<a href="/dashboard" class="topbar-brand"><img src="/static/logo.png" class="topbar-logo" alt=""> <span>{shop_name}</span></a>
<div class="topbar-nav">{links}{admin}</div>
<a href="/logout" class="topbar-logout">sair</a>
</div></div>"""


_WEEKDAYS = {"segunda": 0, "terça": 1, "terca": 1, "quarta": 2, "quinta": 3, "sexta": 4, "sábado": 5, "sabado": 5, "domingo": 6}


def _resolve_dates(text: str) -> str:
    today = datetime.now()
    result = text

    # Replace relative day references
    def _next_weekday(target_wd: int) -> str:
        days_ahead = target_wd - today.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        return (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    def _this_weekday(target_wd: int) -> str:
        days_ahead = target_wd - today.weekday()
        if days_ahead < 0:
            days_ahead += 7
        return (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    # hoje
    result = re.sub(r'\bhoje\b', today.strftime("%Y-%m-%d"), result, flags=re.IGNORECASE)
    # amanhã
    result = re.sub(r'\bamanh[ãa]\b', (today + timedelta(days=1)).strftime("%Y-%m-%d"), result, flags=re.IGNORECASE)
    # depois de amanhã
    result = re.sub(r'\bdepois de amanh[ãa]\b', (today + timedelta(days=2)).strftime("%Y-%m-%d"), result, flags=re.IGNORECASE)

    # "próxima [weekday]" → next week
    for name, wd in _WEEKDAYS.items():
        result = re.sub(
            rf'\bpr[oó]xim[ao]\s+{name}\b',
            (today + timedelta(days=(7 + wd - today.weekday()) % 7 or 7)).strftime("%Y-%m-%d"),
            result, flags=re.IGNORECASE,
        )

    # "esta [weekday]", "[weekday]" (alone) → this/next occurrence
    for name, wd in _WEEKDAYS.items():
        # "esta [weekday]"
        result = re.sub(
            rf'\besta\s+{name}\b',
            _this_weekday(wd),
            result, flags=re.IGNORECASE,
        )
        # standalone weekday with word boundaries, not preceded by "próxima" or "esta"
        result = re.sub(
            rf'(?<!pr[oó]xim[ao]\s)(?<!esta\s)\b{name}\b(?!-feira)',
            _this_weekday(wd),
            result, flags=re.IGNORECASE,
        )

    # Time patterns: "15h", "15:00", "15 horas" → keep as "15:00"
    result = re.sub(r'\b(\d{1,2})h(?:\s*(\d{2}))?\b', lambda m: f"{int(m.group(1)):02d}:{m.group(2) or '00'}", result)

    return result


def _build_prompt(barbershop_id: int, thread_id: str | None = None) -> str:
    shop = db.get_barbershop(barbershop_id)
    biz_type = shop.get("business_type", "barbearia") if shop else "barbearia"
    base = build_system_prompt(biz_type)
    services = db.list_services(barbershop_id)
    if services:
        lines = "\n".join(
            f"  - {s['name']}: R$ {s['price']:.2f} ({s['duration_minutes']}min)"
            for s in services
        )
        base += f"\n\nSERVIÇOS OFERECIDOS:\n{lines}\n\nUse esta lista para informar preços e durações quando o cliente perguntar."
    staff_list = db.list_staff(barbershop_id, active_only=True)
    if staff_list:
        names = ", ".join(s["name"] for s in staff_list)
        ids = ", ".join(f'{s["name"]} (id={s["id"]})' for s in staff_list)
        base += f"\n\nFUNCIONÁRIOS DISPONÍVEIS: {ids}\n\nSEMPRE inclua staff_id no create_appointment. Pergunte ao cliente qual funcionário prefere. Se o cliente não tiver preferência, escolha um aleatório e informe qual foi. NUNCA crie agendamento sem staff_id."
    if thread_id:
        with _last_lock:
            aid = _last_appointment.get(thread_id)
        if aid:
            a = db.get_appointment(barbershop_id, aid)
            if a and a["status"] == "scheduled":
                base += f"\n\núltimo agendamento deste cliente: #{a['id']} ({a['title']} às {a['start_time'][:16]})."
    return base


@app.post("/chat", response_model=MessageResponse)
async def chat(
    request: MessageRequest,
    barbershop_id: int = Depends(get_current_barbershop_id),
):
    thread_id = request.thread_id or f"thread_{datetime.now(timezone.utc).replace(tzinfo=None).timestamp()}"

    resolved = _resolve_dates(request.message)
    db.save_message(thread_id, "user", resolved)

    prior = db.get_conversation(thread_id, limit=12)
    prompt = _build_prompt(barbershop_id, thread_id)
    history = [{"role": "system", "content": prompt}] + prior

    action_executed = False

    for iteration in range(3):
        raw = await call_llm(history)
        parsed = extract_json(raw)

        if not parsed or not isinstance(parsed, dict):
            db.save_message(thread_id, "assistant", "Desculpe, não entendi. Pode repetir?")
            return MessageResponse(
                reply="Desculpe, não entendi. Pode repetir?",
                thread_id=thread_id,
            )

        action = parsed.get("action", "reply")
        params = parsed.get("parameters", {})
        msg = parsed.get("message", "")

        # Hallucination guard: only on first turn, before any real action
        if action == "reply" and not action_executed:
            lower_msg = msg.lower()
            hallucination_keywords = [
                "foi cancelado", "cancelado com sucesso", "cancelado!",
                "foi criado", "criado com sucesso",
                "foi remarcado", "remarcado com sucesso",
                "foi reagendado",
            ]
            if any(w in lower_msg for w in hallucination_keywords):
                history.append({"role": "assistant", "content": raw})
                history.append({
                    "role": "user",
                    "content": "Você disse que realizou uma ação mas usou 'reply'. Você PRECISA usar a action correta (create_appointment, cancel_appointment, etc) para executar a ação. NÃO finja que executou. Responda APENAS com JSON contendo a action correta."
                })
                continue

        # If already executed an action and LLM tries another, block it
        if action_executed and action != "reply":
            result, _ = execute_action(action, params, barbershop_id)
            reply = result or msg or "Concluído."
            db.save_message(thread_id, "assistant", reply)
            return MessageResponse(reply=reply, thread_id=thread_id)

        result, created_id = execute_action(action, params, barbershop_id)

        if result is None:
            db.save_message(thread_id, "assistant", msg or "Desculpe, não entendi. Pode repetir?")
            return MessageResponse(reply=msg or "Desculpe, não entendi. Pode repetir?", thread_id=thread_id)

        if action == "reply" or iteration == 2:
            reply = msg or result or "Desculpe, não entendi. Pode repetir?"
            db.save_message(thread_id, "assistant", reply)
            return MessageResponse(reply=reply, thread_id=thread_id)

        action_executed = True

        if created_id:
            with _last_lock:
                _last_appointment[thread_id] = created_id
            a = db.get_appointment(barbershop_id, created_id)
            history.append({
                "role": "system",
                "content": f"Appointment #{created_id} ({a['title']}) created for this client."
            })

        # Action succeeded — let the LLM craft a natural reply
        history.append({"role": "assistant", "content": raw})
        history.append({
            "role": "user",
            "content": f"Ação executada. Resultado:\n{result}\nAgora responda ao cliente em português natural. Use APENAS a ação reply."
        })
    # Fallback if loop exhausts without returning
    db.save_message(thread_id, "assistant", "Desculpe, não consegui processar. Pode repetir?")
    return MessageResponse(reply="Desculpe, não consegui processar. Pode repetir?", thread_id=thread_id)


# ── Appointment endpoints (scoped) ──────────────────────────────

@app.get("/appointments", response_model=list[AppointmentResponse])
def list_appointments(
    status: str | None = None,
    barbershop_id: int = Depends(get_current_barbershop_id),
):
    return db.list_appointments(barbershop_id, status=status)


@app.get("/appointments/{appointment_id}", response_model=AppointmentResponse)
def get_appointment(
    appointment_id: int,
    barbershop_id: int = Depends(get_current_barbershop_id),
):
    a = db.get_appointment(barbershop_id, appointment_id)
    if not a:
        raise HTTPException(status_code=404, detail="Appointment not found")
    return a


@app.post("/appointments", response_model=AppointmentResponse)
def create_appointment(
    body: AppointmentCreate,
    barbershop_id: int = Depends(get_current_barbershop_id),
):
    staff_id = body.staff_id
    if not staff_id:
        active_staff = db.list_staff(barbershop_id, active_only=True)
        if active_staff:
            import random
            staff_id = random.choice(active_staff)["id"]

    # Double-booking check
    existing = db.list_appointments(barbershop_id, status="scheduled")
    for a in existing:
        if a["start_time"] < body.end_time and body.start_time < a["end_time"]:
            if staff_id and a.get("staff_id") == staff_id:
                raise HTTPException(status_code=409, detail=f"Horário já reservado para {a['staff_name'] or 'este funcionário'} ({a['start_time'][11:16]}-{a['end_time'][11:16]})")
            if not staff_id and not a.get("staff_id"):
                raise HTTPException(status_code=409, detail=f"Horário já reservado ({a['start_time'][11:16]}-{a['end_time'][11:16]})")

    appt_id = db.create_appointment(
        barbershop_id=barbershop_id,
        title=body.title,
        description=body.description,
        start_time=body.start_time,
        end_time=body.end_time,
        staff_id=staff_id,
        customer_phone=body.customer_phone,
    )
    return db.get_appointment(barbershop_id, appt_id)


@app.put("/appointments/{appointment_id}/reschedule", response_model=AppointmentResponse)
def reschedule_appointment(
    appointment_id: int,
    body: AppointmentReschedule,
    barbershop_id: int = Depends(get_current_barbershop_id),
):
    success = db.reschedule_appointment(
        barbershop_id, appointment_id, body.new_start_time, body.new_end_time
    )
    if not success:
        raise HTTPException(status_code=404, detail="Appointment not found")
    return db.get_appointment(barbershop_id, appointment_id)


@app.delete("/appointments/{appointment_id}", response_model=AppointmentResponse)
def cancel_appointment(
    appointment_id: int,
    barbershop_id: int = Depends(get_current_barbershop_id),
):
    a = db.get_appointment(barbershop_id, appointment_id)
    if not a:
        raise HTTPException(status_code=404, detail="Appointment not found")
    db.cancel_appointment(barbershop_id, appointment_id)
    return db.get_appointment(barbershop_id, appointment_id)


# ── Services (scoped) ─────────────────────────────────────────

@app.get("/services", response_model=list[ServiceResponse])
def list_services(
    barbershop_id: int = Depends(get_current_barbershop_id),
):
    return [_row_to_svc(s) for s in db.list_services(barbershop_id)]


def _row_to_svc(row) -> dict:
    d = dict(row)
    d["active"] = bool(d["active"])
    return d


@app.post("/services", response_model=ServiceResponse)
def create_service(
    body: ServiceCreate,
    barbershop_id: int = Depends(get_current_barbershop_id),
):
    svc_id = db.create_service(barbershop_id, body.name, body.duration_minutes, body.price)
    row = db.get_connection().execute("SELECT * FROM services WHERE id = ?", (svc_id,)).fetchone()
    return _row_to_svc(row)


@app.put("/services/{service_id}", response_model=ServiceResponse)
def update_service(
    service_id: int,
    body: ServiceUpdate,
    barbershop_id: int = Depends(get_current_barbershop_id),
):
    ok = db.update_service(service_id, barbershop_id, body.name, body.duration_minutes, body.price, body.active)
    if not ok:
        raise HTTPException(status_code=404, detail="Service not found")
    row = db.get_connection().execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()
    return _row_to_svc(row)


@app.delete("/services/{service_id}")
def delete_service(
    service_id: int,
    barbershop_id: int = Depends(get_current_barbershop_id),
):
    ok = db.delete_service(service_id, barbershop_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Service not found")
    return {"ok": True}


# ── Staff (scoped) ──────────────────────────────────────────

@app.get("/staff", response_model=list[StaffResponse])
def list_staff(barbershop_id: int = Depends(get_current_barbershop_id)):
    return [{"id": s["id"], "barbershop_id": s["barbershop_id"], "name": s["name"], "active": bool(s["active"]), "created_at": s["created_at"]} for s in db.list_staff(barbershop_id, active_only=False)]


@app.post("/staff", response_model=StaffResponse)
def create_staff(body: StaffCreate, barbershop_id: int = Depends(get_current_barbershop_id)):
    sid = db.create_staff(barbershop_id, body.name)
    row = db.get_connection().execute("SELECT * FROM staff WHERE id = ?", (sid,)).fetchone()
    return {"id": row["id"], "barbershop_id": row["barbershop_id"], "name": row["name"], "active": bool(row["active"]), "created_at": row["created_at"]}


@app.put("/staff/{staff_id}", response_model=StaffResponse)
def update_staff(staff_id: int, body: StaffUpdate, barbershop_id: int = Depends(get_current_barbershop_id)):
    ok = db.update_staff(staff_id, barbershop_id, name=body.name, active=body.active)
    if not ok:
        raise HTTPException(status_code=404, detail="Staff not found")
    row = db.get_connection().execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()
    return {"id": row["id"], "barbershop_id": row["barbershop_id"], "name": row["name"], "active": bool(row["active"]), "created_at": row["created_at"]}


@app.delete("/staff/{staff_id}")
def delete_staff(staff_id: int, barbershop_id: int = Depends(get_current_barbershop_id)):
    ok = db.delete_staff(staff_id, barbershop_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Staff not found")
    return {"ok": True}


# ── WhatsApp Status (scoped) ────────────────────────────────────

@app.get("/whatsapp/qrcode")
def get_qrcode(barbershop_id: int = Depends(get_current_barbershop_id)):
    qr_path = os.path.join(get_wa_status_dir(barbershop_id), "qrcode.png")
    if os.path.exists(qr_path):
        return FileResponse(qr_path, media_type="image/png")
    return JSONResponse({"error": "QR code not available"}, status_code=404)


@app.get("/whatsapp/status")
def get_whatsapp_status(barbershop_id: int = Depends(get_current_barbershop_id)):
    status_path = os.path.join(get_wa_status_dir(barbershop_id), "status.json")
    if os.path.exists(status_path):
        return JSONResponse(_load_json(status_path))
    return JSONResponse({"status": "inactive"})


# ── Admin: WhatsApp ────────────────────────────────────────────

WHATSAPP_MANAGER_URL = os.environ.get("WHATSAPP_MANAGER_URL", "http://localhost:8001")

@app.post("/admin/start-whatsapp/{barbershop_id}")
def admin_start_whatsapp(barbershop_id: int, _=Depends(require_admin)):
    import httpx
    try:
        resp = httpx.post(f"{WHATSAPP_MANAGER_URL}/manager/start", json={"barbershop_id": barbershop_id}, timeout=10)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="WhatsApp manager error")
        return resp.json()
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Cannot reach WhatsApp manager: {e}")


@app.get("/admin/whatsapp/status/{barbershop_id}")
def admin_whatsapp_status(barbershop_id: int, _=Depends(require_admin)):
    status_path = os.path.join(get_wa_status_dir(barbershop_id), "status.json")
    if os.path.exists(status_path):
        return JSONResponse(_load_json(status_path))
    return JSONResponse({"status": "inactive"})


@app.get("/admin/whatsapp/qrcode/{barbershop_id}")
def admin_whatsapp_qrcode(barbershop_id: int, _=Depends(require_admin)):
    qr_path = os.path.join(get_wa_status_dir(barbershop_id), "qrcode.png")
    if os.path.exists(qr_path):
        return FileResponse(qr_path, media_type="image/png")
    return JSONResponse({"error": "QR code not available"}, status_code=404)


# ── Dashboard (scoped) ──────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, week: str = Query(None)):
    barbershop_id = get_barbershop_id_from_request(request)
    if not barbershop_id:
        return RedirectResponse(url="/login")
    shop = db.get_barbershop(barbershop_id)
    appointments = db.list_appointments(barbershop_id)

    status_path = os.path.join(get_wa_status_dir(barbershop_id), "status.json")
    wa_status = "inactive"
    if os.path.exists(status_path):
        wa_status = _load_json(status_path).get("status", "inactive")

    app_json = json.dumps(appointments)

    staff_list = db.list_staff(barbershop_id, active_only=True)
    staff_json = json.dumps([{"id": s["id"], "name": s["name"]} for s in staff_list])

    today = datetime.now()
    if week:
        mon = datetime.strptime(week, "%Y-%m-%d")
    else:
        mon = today - timedelta(days=today.weekday())
    week_param = mon.strftime("%Y-%m-%d")
    next_week = (mon + timedelta(days=7)).strftime("%Y-%m-%d")
    prev_week = (mon - timedelta(days=7)).strftime("%Y-%m-%d")

    DAYS = ["dom", "seg", "ter", "qua", "qui", "sex", "sáb"]

    CAL_HTML = """
<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1,user-scalable=no"><title>PatoAgenda AI - SHOP_NAME</title><link rel="icon" type="image/png" href="/static/logo.png">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333}
.topbar{background:linear-gradient(135deg,#1a73e8,#0d47a1);color:#fff;box-shadow:0 2px 12px rgba(0,0,0,.15)}
.topbar-inner{max-width:1100px;margin:0 auto;display:flex;align-items:center;gap:12px;padding:10px 16px;flex-wrap:wrap}
.topbar-brand{display:flex;align-items:center;gap:8px;color:#fff;text-decoration:none;font-size:16px;font-weight:700}
.topbar-logo{width:38px;height:38px;border-radius:50%;object-fit:cover;background:#fff;padding:3px;box-shadow:0 2px 6px rgba(0,0,0,.2)}
.topbar-nav{display:flex;gap:2px;flex:1;justify-content:center}
.nav-item{padding:6px 14px;border-radius:8px;color:#fff;text-decoration:none;font-size:14px;transition:background .15s;white-space:nowrap}
.nav-item:hover{background:rgba(255,255,255,.15)}
.nav-item.active{background:rgba(255,255,255,.2);font-weight:600}
.topbar-logout{margin-left:auto;color:rgba(255,255,255,.7);text-decoration:none;font-size:13px;transition:color .15s}
.topbar-logout:hover{color:#fff}
.container{max-width:1100px;margin:16px auto;padding:0 12px}
.card{background:#fff;border-radius:12px;padding:16px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,.1)}
.card h2{margin-bottom:8px;font-size:16px}
.badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:600}
.b-connected{background:#e6f4ea;color:#1e7e34}
.b-awaiting_scan{background:#fef7e0;color:#e37400}
.b-inactive,.b-unknown{background:#f1f3f4;color:#5f6368}
.b-disconnected{background:#fce8e6;color:#c5221f}
.hint{color:#666;font-size:13px;margin:8px 0}
img.qr{display:block;margin:12px auto;width:220px;image-rendering:pixelated}
.ftr{text-align:center;padding:16px;color:#999;font-size:12px}
.nav{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;gap:8px;flex-wrap:wrap}
.nav h2{font-size:16px}
.nav .btns{display:flex;gap:4px}
.nav button,.nav a{padding:6px 14px;border:1px solid #ddd;border-radius:6px;background:#fff;cursor:pointer;font-size:13px;text-decoration:none;color:#333}
.nav button:hover,.nav a:hover{background:#f0f0f0}
.filtro{display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap}
.filtro label{font-size:13px;color:#666;font-weight:600}
.filtro select{padding:6px 10px;border:1px solid #ddd;border-radius:6px;font-size:13px;background:#fff;cursor:pointer;flex:1;max-width:220px}
.cal{display:grid;grid-template-columns:50px repeat(7,1fr);font-size:12px;overflow-x:auto;-webkit-overflow-scrolling:touch;scroll-behavior:smooth}
.cal .ch{background:#f9f9f9;font-weight:600;text-align:center;padding:6px 2px;border-bottom:2px solid #ddd;position:sticky;top:0;z-index:2;font-size:11px}
.cal .ch.today{background:#1a73e8;color:#fff;border-radius:6px 6px 0 0}
.cal .tm{text-align:right;padding:4px 6px;color:#999;font-size:10px;border-top:1px solid #f0f0f0;height:48px}
.cal .sl{border-left:1px solid #eee;border-top:1px solid #f0f0f0;position:relative;min-height:48px;padding:2px;cursor:pointer;transition:background .15s}
.cal .sl.today{background:#f0f6ff}
.cal .sl.hl{background:#fafafa}
.cal .sl:active{background:#e8f0fe}
.appt{position:absolute;left:2px;right:2px;border-radius:4px;padding:3px 5px;font-size:11px;cursor:pointer;overflow:hidden;z-index:1;color:#fff;min-height:20px;border:1px solid rgba(0,0,0,.1);transition:transform .1s,box-shadow .1s}
.appt:hover{opacity:.9;z-index:3;transform:scale(1.02);box-shadow:0 2px 8px rgba(0,0,0,.2)}
.appt .n{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.appt .d{font-size:10px;opacity:.9;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:99;align-items:center;justify-content:center;animation:fadeIn .2s}
.modal-overlay.show{display:flex}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
@keyframes slideUp{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}
.modal{background:#fff;border-radius:16px;width:92%;max-width:400px;box-shadow:0 8px 32px rgba(0,0,0,.25);overflow:hidden;animation:slideUp .25s}
.modal .mhead{padding:18px 20px 14px;position:relative}
.modal .mhead h3{font-size:18px;margin:0;padding-right:28px}
.modal .mhead .close-x{position:absolute;top:12px;right:14px;width:30px;height:30px;border:none;border-radius:50%;background:rgba(0,0,0,.08);color:#555;font-size:20px;line-height:30px;text-align:center;cursor:pointer;transition:background .15s}
.modal .mhead .close-x:hover{background:rgba(0,0,0,.15)}
.modal .mbody{padding:0 20px 18px}
.modal .mrow{display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid #f0f0f0;font-size:14px}
.modal .mrow:last-child{border:none}
.modal .mrow .ico{width:22px;text-align:center;font-size:16px;flex-shrink:0}
.modal .mrow .val{color:#555;flex:1}
.modal .mrow .val b{color:#333}
.modal .mfoot{display:flex;gap:8px;padding:12px 20px;border-top:1px solid #eee}
.modal .mfoot button{flex:1;padding:10px;border:none;border-radius:8px;cursor:pointer;font-size:14px;font-weight:600;transition:opacity .15s}
.modal .mfoot button:active{opacity:.7}
.modal .mfoot .ccl{background:#c5221f;color:#fff}
.modal .mfoot .ok{background:#1a73e8;color:#fff}
.modal .mfoot .close{background:#f1f3f4;color:#333}
.empty{text-align:center;color:#999;padding:40px;grid-column:1/9}
@media(max-width:640px){
.cal{grid-template-columns:40px repeat(7,1fr);font-size:11px}
.cal .tm{font-size:9px;height:40px;padding:2px 4px}
.cal .sl{min-height:40px}
.appt{font-size:10px;padding:2px 3px}
.appt .d{display:none}
.nav h2{font-size:14px}
}
</style></head>
<body>
HEADER_HTML
<div class="container">
<div class="card"><h2> WhatsApp</h2><span class="badge b-WA_STATUS">WA_STATUS</span></div>
QR_BLOCK
<div class="card"><h2> Agenda Semanal</h2>
<div id="cal"></div>
</div></div>
<div class="ftr">PatoAgenda AI v1.0 — <a href="mailto:fabiostella@gmail.com" style="color:#999;text-decoration:none">fabiostella@gmail.com</a></div>
<div class="modal-overlay" id="modal"><div class="modal" id="modalBody"></div></div>
<div class="modal-overlay" id="modalCreate"><div class="modal" id="modalCreateBody"></div></div>
<script>
const APPOINTMENTS = APP_JSON;
const DAYS = DAYS_JSON;
const COLORS = ["#1a73e8","#e37400","#1e7e34","#9334e6","#c5221f","#0d7377","#e67e22","#2ecc71","#e74c3c","#3498db","#9b59b6","#f39c12","#1abc9c","#d35400","#2980b9","#8e44ad","#27ae60","#c0392b","#16a085","#f1c40f"];
const HOURS = [];
for(let h=8;h<=20;h++){HOURS.push(('0'+h).slice(-2)+':00')}
const SERVICES = SERVICES_JSON;
const STAFF = STAFF_JSON;
let WEEK_CURRENT = 'WEEK_PARAM';
let filterService = '';
let filterStaff = '';
function parseLocal(s){var p=s.split('-');return new Date(+p[0],+p[1]-1,+p[2])}
function getMon(d){d=parseLocal(d);var day=d.getDay();d.setDate(d.getDate()-(day===0?6:day-1));return d}
function fmtDate(d){var m=d.getMonth()+1;return d.getFullYear()+'-'+('0'+m).slice(-2)+'-'+('0'+d.getDate()).slice(-2)}
function fmtBr(d){return ('0'+d.getDate()).slice(-2)+'/'+('0'+(d.getMonth()+1)).slice(-2)}
function esc(s){var d=document.createElement('div');d.appendChild(document.createTextNode(s));return d.innerHTML}
function render(weekStart){
WEEK_CURRENT = weekStart;
var mon=getMon(weekStart),weekDays=[];
for(var i=0;i<7;i++){var d=new Date(mon);d.setDate(mon.getDate()+i);weekDays.push(d)}
var today=fmtDate(new Date());
var filterOpts='<option value="">Todos os servicos</option>';
SERVICES.forEach(function(s){filterOpts+='<option value="'+esc(s.name)+'"'+(filterService===s.name?' selected':'')+'>'+esc(s.name)+'</option>'});
var staffOpts='<option value="">Todos os funcionarios</option>';
STAFF.forEach(function(s){staffOpts+='<option value="'+esc(s.name)+'"'+(filterStaff===s.name?' selected':'')+'>'+esc(s.name)+'</option>'});
var html='<div class="filtro"><label>Servico:</label><select id="svcFilter" onchange="filterService=this.value;render(WEEK_CURRENT)">'+filterOpts+'</select><label>Funcionario:</label><select id="staffFilter" onchange="filterStaff=this.value;render(WEEK_CURRENT)">'+staffOpts+'</select></div>';
html+='<div class="nav"><div class="btns"><a href="/dashboard"> Hoje</a></div><h2>'+fmtBr(weekDays[0])+' - '+fmtBr(weekDays[6])+'</h2><div class="btns">';
var prev="PREV_WEEK";var next="NEXT_WEEK";
html+='<a href="?week='+prev+'"> Anterior</a>';
html+='<a href="?week='+next+'"> Proximo</a></div></div>';
html+='<div class="cal"><div class="ch"></div>';
for(var i=0;i<7;i++){var f=fmtDate(weekDays[i]);html+='<div class="ch'+(f===today?' today':'')+'">'+DAYS[weekDays[i].getDay()]+'<br>'+weekDays[i].getDate()+'</div>'}
for(var h=0;h<HOURS.length;h++){
html+='<div class="tm">'+HOURS[h]+'</div>';
for(var d=0;d<7;d++){var f=fmtDate(weekDays[d]);html+='<div class="sl'+(f===today?' today':'')+(h%2===1?' hl':'')+'" id="s-'+f+'-'+h+'"></div>'}
}
document.getElementById('cal').innerHTML=html;
var slots={};
APPOINTMENTS.forEach(function(a){
  if(a.status!=='scheduled')return;
  if(filterService && a.title!==filterService)return;
  if(filterStaff && a.staff_name!==filterStaff)return;
var s=new Date(a.start_time),e=new Date(a.end_time);
var dayIdx=weekDays.findIndex(function(wd){return fmtDate(wd)===fmtDate(s)});
if(dayIdx<0)return;
var sm=s.getHours()*60+s.getMinutes(),em=e.getHours()*60+e.getMinutes();
var sh=Math.floor(sm/60)-8,eh=Math.ceil(em/60)-8;
if(sh<0)sh=0;if(eh>HOURS.length)eh=HOURS.length;
var sid=fmtDate(s)+'-'+sh;
if(!slots[sid])slots[sid]=[];
var topPct=((sm%60)/60)*100;
var heightPx=(em-sm)/60*48;
if(heightPx<24)heightPx=24;
slots[sid].push({a:a,top:topPct,height:heightPx})
});
for(var key in slots){
var items=slots[key];
items.sort(function(x,y){return x.a.start_time<y.a.start_time?-1:1});
items.forEach(function(item){
  var el=document.getElementById('s-'+key);
  if(!el)return;
  var c=COLORS[item.a.id%COLORS.length];
  var n=item.a.description||'';
  var staffName=item.a.staff_name||'';
  var d=document.createElement('div');d.className='appt';
  d.style.cssText='background:'+c+';height:'+item.height+'px;top:'+item.top+'%';
  var startStr=item.a.start_time.slice(11,16);
  d.innerHTML='<div class="n">'+esc(item.a.title)+'</div>'+(staffName?'<div class="d">'+esc(staffName)+'</div>':'')+(n?'<div class="d">'+esc(n)+'</div>':'')+'<div class="d">'+startStr+'</div>';
  d.onclick=function(){showModal(item.a)};
  el.appendChild(d)
})
}
}
function showModal(a){
var c=COLORS[a.id%COLORS.length];
var h='<div class="mhead" style="background:'+c+'10;border-bottom:3px solid '+c+'"><h3>'+esc(a.title)+'</h3><button class="close-x" onclick="closeModal()">&times;</button></div>';
h+='<div class="mbody">';
if(a.description)h+='<div class="mrow"><span class="ico">👤</span><div class="val"><b>Cliente:</b> '+esc(a.description)+'</div></div>';
  h+='<div class="mrow"><span class="ico">📅</span><div class="val"><b>Inicio:</b> '+a.start_time.slice(0,16).replace('T',' ')+'</div></div>';
  if(a.staff_name)h+='<div class="mrow"><span class="ico">👤</span><div class="val"><b>Funcionario:</b> '+esc(a.staff_name)+'</div></div>';
h+='<div class="mrow"><span class="ico">⏰</span><div class="val"><b>Fim:</b> '+a.end_time.slice(0,16).replace('T',' ')+'</div></div>';
var dur=Math.round((new Date(a.end_time)-new Date(a.start_time))/60000);
h+='<div class="mrow"><span class="ico">⏱️</span><div class="val"><b>Duracao:</b> '+dur+'min</div></div>';
var stCls=a.status==='scheduled'?'b-connected':a.status==='cancelled'?'b-disconnected':'b-awaiting_scan';
var stTxt=a.status==='scheduled'?'Confirmado':a.status==='cancelled'?'Cancelado':'Reagendado';
h+='<div class="mrow"><span class="ico">📌</span><div class="val"><b>Status:</b> <span class="badge '+stCls+'">'+stTxt+'</span></div></div>';
h+='</div><div class="mfoot">';
if(a.status==='scheduled'){h+='<button class="ccl" onclick="cancelAppt('+a.id+')">Cancelar</button>'}
h+='<button class="close" onclick="closeModal()">Fechar</button></div>';
document.getElementById('modalBody').innerHTML=h;
document.getElementById('modal').classList.add('show')
}
function closeModal(){document.getElementById('modal').classList.remove('show')}
function closeCreateModal(){document.getElementById('modalCreate').classList.remove('show')}
document.getElementById('modalCreate').onclick=function(e){if(e.target===this)closeCreateModal()};

function openCreate(dateStr, hourIdx){
  var svcOpts='<option value=\"\">Selecione...</option>';
  SERVICES.forEach(function(s){svcOpts+='<option value=\"'+esc(s.name)+'\">'+esc(s.name)+'</option>'});
  var staffOpts='<option value=\"\">Sem preferencia</option>';
  STAFF.forEach(function(s){staffOpts+='<option value=\"'+s.id+'\">'+esc(s.name)+'</option>'});
  var h=document.getElementById('modalCreateBody');
  h.innerHTML='<div class=\"mhead\" style=\"background:#1a73e810;border-bottom:3px solid #1a73e8\"><h3>➕ Novo Agendamento</h3><button class=\"close-x\" onclick=\"closeCreateModal()\">&times;</button></div>'
    +'<div class=\"mbody\">'
    +'<div class=\"mrow\"><span class=\"ico\">💈</span><div class=\"val\"><select id=\"crSvc\" style=\"width:100%;padding:6px;border-radius:6px;border:1px solid #ddd;font-size:14px\">'+svcOpts+'</select></div></div>'
    +'<div class=\"mrow\"><span class=\"ico\">👤</span><div class=\"val\"><input id=\"crName\" placeholder=\"Nome do cliente\" style=\"width:100%;padding:6px;border-radius:6px;border:1px solid #ddd;font-size:14px\"></div></div>'
    +'<div class=\"mrow\"><span class=\"ico\">📅</span><div class=\"val\"><input type=\"date\" id=\"crDate\" value=\"'+dateStr+'\" style=\"width:100%;padding:6px;border-radius:6px;border:1px solid #ddd;font-size:14px\"></div></div>'
    +'<div class=\"mrow\"><span class=\"ico\">⏰</span><div class=\"val\"><input type=\"time\" id=\"crTime\" value=\"'+(HOURS[hourIdx]||'09:00')+'\" style=\"width:100%;padding:6px;border-radius:6px;border:1px solid #ddd;font-size:14px\"></div></div>'
    +'<div class=\"mrow\"><span class=\"ico\">👥</span><div class=\"val\"><select id=\"crStaff\" style=\"width:100%;padding:6px;border-radius:6px;border:1px solid #ddd;font-size:14px\">'+staffOpts+'</select></div></div>'
    +'<div class=\"mrow\"><span class=\"ico\">📱</span><div class=\"val\"><input id=\"crPhone\" placeholder=\"WhatsApp do cliente (opcional)\" style=\"width:100%;padding:6px;border-radius:6px;border:1px solid #ddd;font-size:14px\"></div></div>'
    +'</div><div class=\"mfoot\"><button class=\"ok\" onclick=\"submitCreate()\">Criar</button><button class=\"close\" onclick=\"closeCreateModal()\">Cancelar</button></div>';
  document.getElementById('modalCreate').classList.add('show')
}

async function submitCreate(){
  var svc=document.getElementById('crSvc').value;
  var name=document.getElementById('crName').value.trim();
  var date=document.getElementById('crDate').value;
  var time=document.getElementById('crTime').value;
  var staffId=document.getElementById('crStaff').value;
  var phone=document.getElementById('crPhone').value.trim();
  if(!svc||!name||!date||!time){alert('Preencha todos os campos');return}
  var start=date+'T'+time;
  try{
    var body={title:svc,description:name,start_time:start,end_time:start,staff_id:staffId?parseInt(staffId):null,customer_phone:phone};
    var res=await fetch('/appointments',{method:'POST',headers:{'Authorization':'Bearer '+localStorage.getItem('token'),'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(res.status===409){var d=await res.json();alert(d.detail||'Conflito de horario');return}
    if(!res.ok){var e=await res.json();alert(e.detail||'Erro ao criar');return}
    closeCreateModal();location.reload()
  }catch(e){alert('Erro de conexao')}
}

// Click on empty slot → create appointment
document.getElementById('cal').addEventListener('click',function(e){
  var sl=e.target.closest('.sl');if(!sl)return;
  if(sl.querySelector('.appt'))return; // has appointments, don't interfere
  var id=sl.id.replace('s-','');
  var parts=id.split('-');
  var dateStr=parts.slice(0,3).join('-');
  var hourIdx=parseInt(parts[3]||0);
  openCreate(dateStr,hourIdx)
});
async function cancelAppt(id){
if(!confirm('Cancelar este agendamento?'))return;
try{
var res=await fetch('/appointments/'+id,{method:'DELETE',headers:{'Authorization':'Bearer '+localStorage.getItem('token')}});
if(res.ok){closeModal();location.reload()}
else{alert('Erro ao cancelar')}
}catch(e){alert('Erro de conexao')}
}
document.getElementById('modal').onclick=function(e){if(e.target===this)closeModal()};
render('WEEK_PARAM');
</script>
</body></html>"""

    services = db.list_services(barbershop_id, active_only=True)
    services_json = json.dumps([{"name": s["name"]} for s in services])

    shop_name = shop['name']
    is_admin = bool(shop.get('is_admin'))
    qr_block2 = ""
    if wa_status == "awaiting_scan":
        qr_block2 = """<div class="card"><h2> Conectar WhatsApp</h2><p>Escaneie o QR code com o WhatsApp da empresa:</p><p class="hint">WhatsApp > ... > Aparelhos conectados > Conectar</p><img class="qr" src="/whatsapp/qrcode" alt="QR Code"></div>"""

    return HTMLResponse(
        CAL_HTML
        .replace("HEADER_HTML", _header_html(shop_name, "dashboard", is_admin))
        .replace("SHOP_NAME", shop_name)
        .replace("WA_STATUS", wa_status)
        .replace("QR_BLOCK", qr_block2)
        .replace("APP_JSON", app_json)
        .replace("DAYS_JSON", json.dumps(DAYS))
        .replace("SERVICES_JSON", services_json)
        .replace("STAFF_JSON", staff_json)
        .replace("WEEK_PARAM", week_param)
        .replace("PREV_WEEK", prev_week)
        .replace("NEXT_WEEK", next_week)
    )


# ── Config Page ────────────────────────────────────────────────

@app.get("/config", response_class=HTMLResponse)
def config_page(request: Request):
    barbershop_id = get_barbershop_id_from_request(request)
    if not barbershop_id:
        return RedirectResponse(url="/login")
    shop = db.get_barbershop(barbershop_id)
    services = db.list_services(barbershop_id, active_only=False)
    wa_status = "inactive"
    wa_status_path = os.path.join(get_wa_status_dir(barbershop_id), "status.json")
    if os.path.exists(wa_status_path):
        wa_status = json.load(open(wa_status_path)).get("status", "inactive")
    qr_block = ""
    if wa_status == "awaiting_scan":
        qr_block = f"""<div class="card"><h2>📱 Escaneie o QR Code</h2><p>Abra o WhatsApp no celular > ⋮ > Aparelhos conectados > Conectar</p><img class="qr" src="/whatsapp/qrcode" alt="QR Code"></div>"""

    svc_rows = "".join(
        f"""<tr id="svc-{s['id']}"><td>{s['name']}</td><td>{s['duration_minutes']}min</td>
        <td>R$ {s['price']:.2f}</td>
        <td><span class="badge {'b-connected' if s['active'] else 'b-inactive'}">{'ativo' if s['active'] else 'inativo'}</span></td>
        <td>
          <button class="btn-sm" onclick="editSvc({s['id']},'{s['name']}',{s['duration_minutes']},{s['price']},{int(s['active'])})">✏️</button>
          <button class="btn-sm btn-danger" onclick="delSvc({s['id']})">🗑️</button>
        </td></tr>"""
        for s in services
    )

    bt = shop.get("business_type", "barbearia")
    bt_options = "".join(f'<option value="{k}"{" selected" if k == bt else ""}>{v}</option>' for k, v in {
        "barbearia": "Barbearia",
        "salão": "Salão de Beleza",
        "cabeleireiro": "Cabeleireiro",
        "manicure": "Manicure",
        "pedicure": "Pedicure",
        "massagista": "Massagista",
        "spa": "SPA",
        "tatuador": "Tatuador",
        "esteticista": "Esteticista",
        "depilação": "Depilação",
        "maquiador": "Maquiador",
        "personal": "Personal Trainer",
        "fisioterapeuta": "Fisioterapeuta",
        "petshop": "Petshop / Estética Animal",
        "nutricionista": "Nutricionista",
        "psicólogo": "Psicólogo",
        "podólogo": "Podólogo",
        "consultório": "Consultório",
        "outro": "Outro",
    }.items())

    staff_list = db.list_staff(barbershop_id, active_only=False)
    staff_rows = "".join(
        f"""<tr id="staff-{s['id']}"><td>{s['name']}</td>
        <td><span class="badge {'b-connected' if s['active'] else 'b-inactive'}">{'ativo' if s['active'] else 'inativo'}</span></td>
        <td>
          <button class="btn-sm" onclick="editStaff({s['id']},'{s['name']}')">✏️</button>
          <button class="btn-sm btn-danger" onclick="delStaff({s['id']})">🗑️</button>
        </td></tr>"""
        for s in staff_list
    )

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Configuração - PatoAgenda AI</title><link rel="icon" type="image/png" href="/static/logo.png">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333}}
.topbar{{background:linear-gradient(135deg,#1a73e8,#0d47a1);color:#fff;box-shadow:0 2px 12px rgba(0,0,0,.15)}}
.topbar-inner{{max-width:1100px;margin:0 auto;display:flex;align-items:center;gap:12px;padding:10px 16px;flex-wrap:wrap}}
.topbar-brand{{display:flex;align-items:center;gap:8px;color:#fff;text-decoration:none;font-size:16px;font-weight:700}}
.topbar-logo{{width:38px;height:38px;border-radius:50%;object-fit:cover;background:#fff;padding:3px;box-shadow:0 2px 6px rgba(0,0,0,.2)}}
.topbar-nav{{display:flex;gap:2px;flex:1;justify-content:center}}
.nav-item{{padding:6px 14px;border-radius:8px;color:#fff;text-decoration:none;font-size:14px;transition:background .15s;white-space:nowrap}}
.nav-item:hover{{background:rgba(255,255,255,.15)}}
.nav-item.active{{background:rgba(255,255,255,.2);font-weight:600}}
.topbar-logout{{margin-left:auto;color:rgba(255,255,255,.7);text-decoration:none;font-size:13px}}
.topbar-logout:hover{{color:#fff}}
.container{{max-width:900px;margin:16px auto;padding:0 12px}}
.card{{background:#fff;border-radius:12px;padding:16px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
.card h2{{margin-bottom:8px;font-size:16px}}
.badge{{display:inline-block;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:600}}
.b-connected{{background:#e6f4ea;color:#1e7e34}}
.b-awaiting_scan{{background:#fef7e0;color:#e37400}}
.b-inactive,.b-unknown{{background:#f1f3f4;color:#5f6368}}
.b-disconnected{{background:#fce8e6;color:#c5221f}}
img.qr{{display:block;margin:12px auto;width:220px;image-rendering:pixelated}}
.ftr{{text-align:center;padding:16px;color:#999;font-size:12px}}
table{{width:100%;border-collapse:collapse}}
th,td{{padding:8px;text-align:left;border-bottom:1px solid #eee;font-size:13px}}
th{{color:#666;font-weight:600}}
input,select{{padding:8px;border:1px solid #ddd;border-radius:6px;font-size:14px;width:100%;margin-bottom:8px}}
.btn{{display:inline-block;padding:8px 16px;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}}
.btn-primary{{background:#1a73e8;color:#fff}}
.btn-primary:hover{{background:#1557b0}}
.btn-danger{{background:#c5221f;color:#fff}}
.btn-sm{{padding:4px 8px;border:none;border-radius:6px;cursor:pointer;font-size:13px}}
.form-row{{display:flex;gap:8px;align-items:end;flex-wrap:wrap}}
.form-row .field{{flex:1;min-width:120px}}
.form-row .field label{{display:block;font-size:13px;color:#666;margin-bottom:4px}}
.empty{{text-align:center;color:#999;padding:30px}}
.msg{{padding:10px;border-radius:6px;margin-bottom:12px;display:none}}
.msg-success{{background:#e6f4ea;color:#1e7e34;display:block}}
.msg-error{{background:#fce8e6;color:#c5221f;display:block}}
</style></head>
<body>
{_header_html(shop['name'], 'config', bool(shop.get('is_admin')))}
<div class="container">

<div id="msg" class="msg"></div>

<div class="card">
  <h2>➕ Novo Serviço</h2>
  <div class="form-row">
    <div class="field"><label>Nome do serviço</label><input id="svcName" placeholder="Ex: Corte de Cabelo"></div>
    <div class="field"><label>Duração (min)</label><input id="svcDuration" type="number" value="60" min="5"></div>
    <div class="field"><label>Preço (R$)</label><input id="svcPrice" type="number" value="0" min="0" step="0.01" placeholder="50,00"></div>
    <div class="field" style="flex:0"><button class="btn btn-primary" onclick="createSvc()" id="svcBtn">Adicionar</button></div>
  </div>
  <input id="editId" type="hidden" value="">
</div>

<div class="card">
  <h2>📦 Serviços</h2>
  {"<table><thead><tr><th>Nome</th><th>Duração</th><th>Preço</th><th>Status</th><th></th></tr></thead><tbody>" + svc_rows + "</tbody></table>" if services else '<p class="empty">Nenhum serviço cadastrado. Adicione acima os serviços da sua barbearia com preço e duração.</p>'}
</div>

<div class="card">
  <h2>💡 Dica</h2>
  <p>Os serviços cadastrados aqui são enviados automaticamente para a IA. Quando um cliente perguntar "quanto custa?" ou "qual o valor?", a IA consulta esta lista para responder com os preços corretos.</p>
</div>

<div class="card">
  <h2>🏪 Tipo de Negócio</h2>
  <p style="font-size:14px;color:#666;margin-bottom:8px">Isso ajuda a IA a se comportar de acordo com o seu negócio.</p>
  <div class="form-row">
    <div class="field" style="flex:3"><label>Tipo</label>
      <select id="bizType">{bt_options}</select>
    </div>
    <div class="field" style="flex:0"><button class="btn btn-primary" onclick="saveBizType()">Salvar</button></div>
  </div>
</div>

<div class="card">
  <h2>👤 Funcionários</h2>
  <div class="form-row" style="margin-bottom:8px">
    <div class="field" style="flex:3"><label>Nome</label><input id="staffName" placeholder="Ex: Carlos"></div>
    <div class="field" style="flex:0"><button class="btn btn-primary" onclick="createStaff()" id="staffBtn">Adicionar</button></div>
  </div>
  <input id="staffEditId" type="hidden" value="">
  {"<table><thead><tr><th>Nome</th><th>Status</th><th></th></tr></thead><tbody>" + staff_rows + "</tbody></table>" if staff_list else '<p class="empty">Nenhum funcionário cadastrado.</p>'}
</div>

<div class="card">
  <h2>📱 WhatsApp</h2>
  <p>Status: <span class="badge b-{wa_status}">{wa_status}</span></p>
  {qr_block}
</div>

<div class="card">
  <h2>🤖 Modo do WhatsApp</h2>
  <p style="font-size:14px;color:#666;margin-bottom:8px">Controle como o bot responde às mensagens.</p>
  <div class="form-row">
    <div class="field" style="flex:3"><label>Modo</label>
      <select id="waMode" onchange="saveWaMode()">
        <option value="business" {'selected' if shop.get('whatsapp_mode') != 'personal' else ''}>🏢 Empresa — responder todas as mensagens</option>
        <option value="personal" {'selected' if shop.get('whatsapp_mode') == 'personal' else ''}>👤 Pessoal — só responder mensagens de agendamento</option>
      </select>
    </div>
  </div>
  <p style="font-size:12px;color:#888;margin-top:6px">No modo Pessoal, o bot ignora conversas comuns e só responde quando detecta palavras como "agendar", "horário", "marcar", "preço", etc.</p>
</div>

</div>
<div class="ftr">PatoAgenda AI v1.0 — <a href="mailto:fabiostella@gmail.com" style="color:#999;text-decoration:none">fabiostella@gmail.com</a></div>

<script>
const TOKEN = localStorage.getItem('token');
function msg(text, type) {{
  const el = document.getElementById('msg');
  el.textContent = text;
  el.className = 'msg msg-' + type;
  setTimeout(()=>el.style.display='none', 4000);
}}

async function api(method, path, body) {{
  const res = await fetch(path, {{
    method,
    headers: {{'Authorization':'Bearer '+TOKEN,'Content-Type':'application/json'}},
    body: body ? JSON.stringify(body) : undefined,
  }});
  if (!res.ok) {{ const d = await res.json(); throw new Error(d.detail || 'Erro'); }}
  return res.json();
}}

async function createSvc() {{
  const name = document.getElementById('svcName').value.trim();
  const duration = parseInt(document.getElementById('svcDuration').value);
  const price = parseFloat(document.getElementById('svcPrice').value);
  const editId = document.getElementById('editId').value;
  if (!name) return msg('Informe o nome do serviço', 'error');
  try {{
    if (editId) {{
      await api('PUT', '/services/' + editId, {{name, duration_minutes: duration, price}});
      msg('Serviço atualizado!', 'success');
    }} else {{
      await api('POST', '/services', {{name, duration_minutes: duration, price}});
      msg('Serviço adicionado!', 'success');
    }}
    setTimeout(()=>location.reload(), 1000);
  }} catch(e) {{ msg(e.message, 'error'); }}
}}

function editSvc(id, name, duration, price, active) {{
  document.getElementById('svcName').value = name;
  document.getElementById('svcDuration').value = duration;
  document.getElementById('svcPrice').value = price.toFixed(2);
  document.getElementById('editId').value = id;
  document.getElementById('svcBtn').textContent = 'Salvar';
}}

async function delSvc(id) {{
  if (!confirm('Excluir este serviço?')) return;
  try {{
    await api('DELETE', '/services/' + id);
    document.getElementById('svc-'+id).remove();
    msg('Serviço excluído!', 'success');
  }} catch(e) {{ msg(e.message, 'error'); }}
}}

async function saveBizType() {{
  const val = document.getElementById('bizType').value;
  try {{
    await api('PUT', '/barbershop', {{business_type: val}});
    msg('Tipo de negócio salvo!', 'success');
  }} catch(e) {{ msg(e.message, 'error'); }}
}}

async function createStaff() {{
  const name = document.getElementById('staffName').value.trim();
  const editId = document.getElementById('staffEditId').value;
  if (!name) return msg('Informe o nome do funcionário', 'error');
  try {{
    if (editId) {{
      await api('PUT', '/staff/' + editId, {{name}});
      msg('Funcionário atualizado!', 'success');
    }} else {{
      await api('POST', '/staff', {{name}});
      msg('Funcionário adicionado!', 'success');
    }}
    setTimeout(()=>location.reload(), 1000);
  }} catch(e) {{ msg(e.message, 'error'); }}
}}

function editStaff(id, name) {{
  document.getElementById('staffName').value = name;
  document.getElementById('staffEditId').value = id;
  document.getElementById('staffBtn').textContent = 'Salvar';
}}

async function delStaff(id) {{
  if (!confirm('Excluir este funcionário?')) return;
  try {{
    await api('DELETE', '/staff/' + id);
    document.getElementById('staff-'+id).remove();
    msg('Funcionário excluído!', 'success');
  }} catch(e) {{ msg(e.message, 'error'); }}
}}
async function saveWaMode() {{
  const mode = document.getElementById('waMode').value;
  try {{
    await api('PUT', '/barbershop', {{whatsapp_mode: mode}});
    msg('Modo WhatsApp salvo!', 'success');
  }} catch(e) {{ msg(e.message, 'error'); }}
}}
</script>
</body></html>""")


# ── Reports Page ────────────────────────────────────────────────

@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request):
    barbershop_id = get_barbershop_id_from_request(request)
    if not barbershop_id:
        return RedirectResponse(url="/login")
    shop = db.get_barbershop(barbershop_id)
    stats = db.get_barbershop_stats(barbershop_id, days=30)

    def bar_pct(v: float, mx: float) -> str:
        if mx <= 0:
            return "0"
        return f"{v / mx * 100:.1f}"

    max_count = max((s["count"] for s in stats["by_service"]), default=0)
    max_day = max((d["count"] for d in stats["by_day"]), default=0)
    max_rev = max((r["total"] for r in stats["revenue"]), default=0)

    svc_rows = "".join(
        f"""<tr><td>{s["service"]}</td><td>{s["count"]}</td>
        <td><div class="bar"><div class="bfill" style="width:{bar_pct(s["count"], max_count)}%"></div></div></td></tr>"""
        for s in stats["by_service"]
    )
    day_rows = "".join(
        f"""<tr><td>{d["day"]}</td><td>{d["count"]}</td>
        <td><div class="bar"><div class="bfill" style="width:{bar_pct(d["count"], max_day)}%"></div></div></td></tr>"""
        for d in stats["by_day"]
    )
    rev_rows = "".join(
        f"""<tr><td>{r["service"]}</td><td>{r["count"]}x</td><td>R$ {r["price"]:.2f}</td>
        <td>R$ {r["total"]:.2f}</td>
        <td><div class="bar"><div class="bfill" style="width:{bar_pct(r["total"], max_rev)}%"></div></div></td></tr>"""
        for r in stats["revenue"]
    )

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Relatorios - PatoAgenda AI</title><link rel="icon" type="image/png" href="/static/logo.png">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333}}
.topbar{{background:linear-gradient(135deg,#1a73e8,#0d47a1);color:#fff;box-shadow:0 2px 12px rgba(0,0,0,.15)}}
.topbar-inner{{max-width:1100px;margin:0 auto;display:flex;align-items:center;gap:12px;padding:10px 16px;flex-wrap:wrap}}
.topbar-brand{{display:flex;align-items:center;gap:8px;color:#fff;text-decoration:none;font-size:16px;font-weight:700}}
.topbar-logo{{width:38px;height:38px;border-radius:50%;object-fit:cover;background:#fff;padding:3px;box-shadow:0 2px 6px rgba(0,0,0,.2)}}
.topbar-nav{{display:flex;gap:2px;flex:1;justify-content:center}}
.nav-item{{padding:6px 14px;border-radius:8px;color:#fff;text-decoration:none;font-size:14px;transition:background .15s;white-space:nowrap}}
.nav-item:hover{{background:rgba(255,255,255,.15)}}
.nav-item.active{{background:rgba(255,255,255,.2);font-weight:600}}
.topbar-logout{{margin-left:auto;color:rgba(255,255,255,.7);text-decoration:none;font-size:13px}}
.topbar-logout:hover{{color:#fff}}
.container{{max-width:900px;margin:16px auto;padding:0 12px}}
.card{{background:#fff;border-radius:12px;padding:16px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
.card h2{{margin-bottom:12px;font-size:16px}}
.stat-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px}}
.stat-card{{background:#f8f9fa;border-radius:8px;padding:12px;text-align:center}}
.stat-card .n{{font-size:24px;font-weight:700;color:#1a73e8}}
.stat-card .l{{font-size:12px;color:#666;margin-top:2px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:6px 8px;text-align:left;border-bottom:1px solid #eee}}
th{{color:#666;font-weight:600;font-size:12px}}
.bar{{background:#f0f0f0;border-radius:4px;height:16px;overflow:hidden;min-width:40px}}
.bfill{{height:100%;background:#1a73e8;border-radius:4px;min-width:2px;transition:width .3s}}
.ftr{{text-align:center;padding:16px;color:#999;font-size:12px}}
.nav a{{color:#fff;text-decoration:none}}
.logout{{float:right;color:#fff;text-decoration:none;font-size:13px;opacity:.8}}
</style></head>
<body>
{_header_html(shop['name'], 'reports', bool(shop.get('is_admin')))}
<div class="container">
<div class="stat-grid">
<div class="stat-card"><div class="n">{stats['total']}</div><div class="l">Total (30 dias)</div></div>
<div class="stat-card"><div class="n">{stats['scheduled']}</div><div class="l">Confirmados</div></div>
<div class="stat-card"><div class="n">{stats['cancelled']}</div><div class="l">Cancelados</div></div>
</div>

<div class="card"><h2> Agendamentos por Dia</h2>
{"<table><thead><tr><th>Dia</th><th>Qtd</th><th></th></tr></thead><tbody>" + day_rows + "</tbody></table>" if stats['by_day'] else '<p style="color:#999">Nenhum agendamento nos ultimos 30 dias.</p>'}
</div>

<div class="card"><h2> Servicos mais Populares</h2>
{"<table><thead><tr><th>Servico</th><th>Qtd</th><th></th></tr></thead><tbody>" + svc_rows + "</tbody></table>" if stats['by_service'] else '<p style="color:#999">Nenhum dado disponivel.</p>'}
</div>

<div class="card"><h2> Receita por Servico</h2>
{"<table><thead><tr><th>Servico</th><th>Vezes</th><th>Preco</th><th>Total</th><th></th></tr></thead><tbody>" + rev_rows + "</tbody></table>" if stats['revenue'] else '<p style="color:#999">Nenhum dado disponivel.</p>'}
</div>

</div>
<div class="ftr">PatoAgenda AI v1.0 — <a href="mailto:fabiostella@gmail.com" style="color:#999;text-decoration:none">fabiostella@gmail.com</a></div>
</body>
</html>""")


# ── Admin API ────────────────────────────────────────────────────

@app.get("/admin/barbershops")
def admin_list_barbershops(_=Depends(require_admin)):
    return db.list_all_barbershops()


@app.get("/admin/appointments")
def admin_list_appointments(_=Depends(require_admin)):
    return db.list_all_appointments()


@app.get("/admin/stats")
def admin_stats(_=Depends(require_admin)):
    return db.get_stats()


@app.delete("/admin/barbershops/{barbershop_id}")
def admin_delete_barbershop(barbershop_id: int, _=Depends(require_admin)):
    ok = db.delete_barbershop(barbershop_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Barbershop not found")
    return {"ok": True}


@app.post("/admin/barbershops/{barbershop_id}/toggle-payment")
def admin_toggle_payment(barbershop_id: int, _=Depends(require_admin)):
    ok = db.toggle_payment(barbershop_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Barbershop not found")
    return {"ok": True}


def _build_paid_cell(shop: dict, today_str: str) -> str:
    paid = shop.get("paid_until")
    is_paid = bool(paid and paid >= today_str)
    color = "#4caf50" if is_paid else "#f44336"
    label = f"✓ {paid}" if is_paid else "✗"
    return (
        f"<td><label class=\"toggle-switch\" id=\"paid-wrap-{shop['id']}\" style=\"cursor:pointer\">"
        f"<input type=\"checkbox\" id=\"paid-{shop['id']}\" onchange=\"togglePayment({shop['id']})\" {'checked' if is_paid else ''}>"
        f"<span class=\"slider\" style=\"background:{color}\"></span>"
        f"<small id=\"paid-label-{shop['id']}\" style=\"margin-left:8px;font-size:11px;color:{color}\">{label}</small>"
        f"</label></td>"
    )


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    token = request.cookies.get("token")
    if not token:
        return RedirectResponse(url="/login")
    try:
        from app.auth import SECRET, ALGO
        import jwt
        payload = jwt.decode(token, SECRET, algorithms=[ALGO])
        if not payload.get("is_admin"):
            return RedirectResponse(url="/dashboard")
    except Exception:
        return RedirectResponse(url="/login")

    shops = db.list_all_barbershops()
    apps = db.list_all_appointments()
    stats = db.get_stats()

    from datetime import date as _date
    _today_str = _date.today().isoformat()
    shop_rows = "".join(
        f"<tr><td>#{s['id']}</td><td>{s['name']}</td><td>{s['email']}</td>"
        f"<td>{s['whatsapp_number'] or '-'}</td>"
        f"<td>{'<span class=\"badge b-admin\">Admin</span>' if s['is_admin'] else '<span class=\"badge b-shop\">Loja</span>'}</td>"
        f"<td><span id=\"wa-status-{s['id']}\">…</span></td>"
        f"<td><button class=\"btn-sm\" onclick=\"generateQr({s['id']})\">📱 QR</button></td>"
        f"<td>{s['created_at'][:10]}</td>"
        + (f"<td><button class=\"btn-sm btn-del\" onclick=\"delShop({s['id']},'{s['name'].replace(chr(39), chr(92)+chr(39))}')\">🗑️</button></td>" if not s['is_admin'] else "<td></td>")
        + _build_paid_cell(s, _today_str)
        + "</tr>"
        for s in shops
    )

    app_rows = "".join(
        f"<tr><td>#{a['id']}</td><td>{a.get('barbershop_name','?')}</td>"
        f"<td>{a['title']}</td><td>{a['start_time']}</td>"
        f"<td>{a['end_time']}</td>"
        f"<td class='s-{a['status']}'>{a['status']}</td></tr>"
        for a in apps
    )

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Painel Admin - PatoAgenda AI</title><link rel="icon" type="image/png" href="/static/logo.png">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,sans-serif;background:#1a1a2e;color:#e0e0e0}}
.header{{background:#16213e;color:#fff;padding:20px;text-align:center;border-bottom:3px solid #0f3460}}
.header h1{{font-size:24px}}
.stats{{display:flex;gap:16px;max-width:900px;margin:20px auto;padding:0 16px}}
.stat-card{{flex:1;background:#16213e;border-radius:12px;padding:20px;text-align:center;border:1px solid #0f3460}}
.stat-card .n{{font-size:32px;font-weight:700;color:#e94560}}
.stat-card .l{{font-size:13px;color:#888;margin-top:4px}}
.container{{max-width:1000px;margin:0 auto;padding:0 16px 20px}}
.card{{background:#16213e;border-radius:12px;padding:20px;margin-bottom:16px;border:1px solid #0f3460}}
.card h2{{margin-bottom:12px;font-size:18px;color:#e94560}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:8px 6px;text-align:left;border-bottom:1px solid #0f3460}}
th{{color:#888;font-weight:600}}
.btn-sm{{padding:4px 8px;border:none;border-radius:6px;cursor:pointer;font-size:12px;background:#0f3460;color:#e0e0e0}}
.btn-del{{background:#c5221f!important;color:#fff!important}}
.toggle-switch{{position:relative;display:inline-flex;align-items:center;gap:4px}}
.toggle-switch input{{display:none}}
.toggle-switch .slider{{width:36px;height:20px;border-radius:20px;display:inline-block;position:relative;transition:background .3s}}
.toggle-switch .slider::after{{content:'';position:absolute;width:16px;height:16px;border-radius:50%;background:#fff;top:2px;left:2px;transition:transform .3s}}
.toggle-switch input:checked+.slider::after{{transform:translateX(16px)}}
.badge{{display:inline-block;padding:2px 10px;border-radius:20px;font-size:11px;font-weight:600}}
.b-admin{{background:#e94560;color:#fff}}
.b-shop{{background:#0f3460;color:#aaa}}
.b-connected{{background:#1b5e20;color:#a5d6a7}}
.b-awaiting_scan{{background:#e65100;color:#ffe0b2}}
.b-inactive{{background:#263238;color:#90a4ae}}
.s-scheduled{{color:#4ecca3}}
.s-rescheduled{{color:#ffc857}}
.s-cancelled{{color:#e94560;text-decoration:line-through}}
.ftr{{text-align:center;padding:20px;color:#555;font-size:13px}}
.logout{{float:right;color:#e94560;text-decoration:none;font-size:14px}}
</style></head>
<body>
<div class="header"><h1>🛡️ Painel Admin</h1><p style="font-size:13px"><a href="/dashboard" style="color:#aaa;text-decoration:none">📋 Agenda</a> · <a href="/config" style="color:#aaa;text-decoration:none">⚙️ Config</a> · <a href="/logout" class="logout">sair</a></p></div>
<div class="stats">
<div class="stat-card"><div class="n">{stats['barbershops']}</div><div class="l">Empresas</div></div>
<div class="stat-card"><div class="n">{stats['appointments']}</div><div class="l">Agendamentos</div></div>
<div class="stat-card"><div class="n">{stats['scheduled']}</div><div class="l">Ativos</div></div>
<div class="stat-card"><div class="n">{stats['cancelled']}</div><div class="l">Cancelados</div></div>
</div>
<div class="container">
<div class="card"><h2>🏢 Empresas</h2>
{"<table><thead><tr><th>#</th><th>Nome</th><th>Email</th><th>WhatsApp</th><th>Tipo</th><th>WA Status</th><th></th><th>Criado</th><th></th><th>Pago</th></tr></thead><tbody>" + shop_rows + "</tbody></table>" if shops else '<p style="color:#888">Nenhuma empresa</p>'}
</div>
<div class="card"><h2>📋 Todos Agendamentos</h2>
{"<table><thead><tr><th>#</th><th>Empresa</th><th>Serviço</th><th>Início</th><th>Fim</th><th>Status</th></tr></thead><tbody>" + app_rows + "</tbody></table>" if apps else '<p style="color:#888">Nenhum agendamento</p>'}
</div>
</div>
<div class="ftr">PatoAgenda AI v1.0 — <a href="mailto:fabiostella@gmail.com" style="color:#555;text-decoration:none">fabiostella@gmail.com</a></div>
<script>
const TOKEN = localStorage.getItem('token');
async function generateQr(id) {{
  if (!confirm('Gerar QR Code do WhatsApp para esta loja?')) return;
  try {{
    const r = await fetch('/admin/start-whatsapp/' + id, {{method:'POST', headers:{{'Authorization':'Bearer '+TOKEN}}}});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Erro');
    document.getElementById('wa-status-'+id).textContent = 'gerando…';
    setTimeout(()=>checkStatus(id), 2000);
  }} catch(e) {{ alert(e.message); }}
}}
async function checkStatus(id) {{
  try {{
    const r = await fetch('/admin/whatsapp/status/' + id, {{headers:{{'Authorization':'Bearer '+TOKEN}}}});
    const d = await r.json();
    const el = document.getElementById('wa-status-'+id);
    el.textContent = d.status;
    el.className = 'badge b-' + (d.status === 'connected' ? 'connected' : d.status === 'awaiting_scan' ? 'awaiting_scan' : d.status === 'inactive' ? 'inactive' : 'unknown');
  }} catch(e) {{}}
}}
(async function init() {{
  const ids = document.querySelectorAll('[id^="wa-status-"]');
  for (const el of ids) {{
    const id = el.id.replace('wa-status-', '');
    await checkStatus(parseInt(id));
  }}
}})();
async function delShop(id, name) {{
  if (!confirm('ATENCAO: Excluir ' + name + ' (#' + id + ') e TODOS os seus dados? Agendamentos, servicos, funcionarios — tudo sera perdido!')) return;
  try {{
    const r = await fetch('/admin/barbershops/' + id, {{method:'DELETE', headers:{{'Authorization':'Bearer '+TOKEN}}}});
    if (!r.ok) throw new Error((await r.json()).detail || 'Erro');
    location.reload();
  }} catch(e) {{ alert(e.message); }}
}}
async function togglePayment(id) {{
  var cb = document.getElementById('paid-'+id);
  var was = cb.checked; cb.checked = !was; // revert until confirmed
  try {{
    const r = await fetch('/admin/barbershops/' + id + '/toggle-payment', {{method:'POST', headers:{{'Authorization':'Bearer '+TOKEN}}}});
    if (!r.ok) throw new Error('Erro');
    location.reload();
  }} catch(e) {{ alert(e.message); location.reload(); }}
}}
</script>
</body></html>""")


# ── Login page ──────────────────────────────────────────────────

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>PatoAgenda AI - Login</title><link rel="icon" type="image/png" href="/static/logo.png">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,sans-serif;background:#f0f2f5;display:flex;justify-content:center;align-items:center;min-height:100vh}
.box{background:#fff;border-radius:16px;padding:40px;width:380px;box-shadow:0 2px 10px rgba(0,0,0,.1);text-align:center}
.box h1{font-size:28px;margin-bottom:4px}
.box p{color:#666;margin-bottom:24px}
input{width:100%;padding:12px;border:1px solid #ddd;border-radius:8px;font-size:15px;margin-bottom:12px}
button{width:100%;padding:12px;background:#1a73e8;color:#fff;border:none;border-radius:8px;font-size:15px;cursor:pointer;font-weight:600}
button:hover{background:#1557b0}
.error{color:#d32f2f;font-size:13px;margin-top:8px;display:none}
.tab{display:flex;margin-bottom:20px;border-radius:8px;overflow:hidden;border:1px solid #ddd}
.tab div{flex:1;padding:10px;cursor:pointer;text-align:center;font-weight:600;font-size:14px}
.tab .active{background:#1a73e8;color:#fff}
.tab div:not(.active){background:#f5f5f5;color:#666}
.hidden{display:none}
</style>
</head>
<body>
<div class="box">
<img src="/static/logo.png" style="width:140px;height:140px;border-radius:50%;object-fit:cover;background:#f0f2f5;padding:10px;box-shadow:0 2px 12px rgba(0,0,0,.12);margin-bottom:12px"><h1>PatoAgenda AI</h1><p>Agendamentos Inteligentes</p>
<div class="tab"><div id="tabLogin" class="active" onclick="showTab('login')">Entrar</div><div id="tabReg" onclick="showTab('register')">Cadastrar</div></div>
<form id="loginForm" onsubmit="submitForm(event,'login')">
<input type="email" id="loginEmail" placeholder="Email" required>
<input type="password" id="loginPass" placeholder="Senha" required>
<button type="submit">Entrar</button>
<div class="error" id="loginError"></div>
</form>
<form id="registerForm" class="hidden" onsubmit="submitForm(event,'register')">
<input type="text" id="regName" placeholder="Nome da empresa" required>
<input type="email" id="regEmail" placeholder="Email" required>
<input type="password" id="regPass" placeholder="Senha" required>
<button type="submit">Cadastrar</button>
<div class="error" id="regError"></div>
</form>
</div>
<script>
function showTab(t){document.getElementById('loginForm').classList.toggle('hidden',t!='login');document.getElementById('registerForm').classList.toggle('hidden',t!='register');document.getElementById('tabLogin').classList.toggle('active',t=='login');document.getElementById('tabReg').classList.toggle('active',t=='register')}
async function submitForm(e,t){e.preventDefault();
const form=t=='login'?{email:loginEmail.value,password:loginPass.value}:{name:regName.value,email:regEmail.value,password:regPass.value};
try{
const res=await fetch('/auth/'+t,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(form)});
const data=await res.json();
if(!res.ok){document.getElementById(t+'Error').textContent=data.detail||'Erro';document.getElementById(t+'Error').style.display='block';return}
localStorage.setItem('token',data.token);localStorage.setItem('name',data.name);window.location.href=data.is_admin?'/admin':'/dashboard'
}catch(e){document.getElementById(t+'Error').textContent='Erro de conexão com o servidor';document.getElementById(t+'Error').style.display='block'}}
</script>
<div style="text-align:center;padding:20px;color:#999;font-size:13px;margin-top:20px">PatoAgenda AI v1.0 — <a href="mailto:fabiostella@gmail.com" style="color:#999;text-decoration:none">fabiostella@gmail.com</a></div>
</body>
</html>"""


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return HTMLResponse(LOGIN_PAGE)


@app.get("/logout")
def logout():
    return HTMLResponse("""<script>localStorage.removeItem('token');localStorage.removeItem('name');window.location.href='/login'</script>""")


@app.get("/me")
def me(barbershop_id: int = Depends(get_current_barbershop_id)):
    shop = db.get_barbershop(barbershop_id)
    if not shop:
        raise HTTPException(status_code=404)
    return {"id": shop["id"], "name": shop["name"], "email": shop["email"], "whatsapp_number": shop.get("whatsapp_number"), "business_type": shop.get("business_type", "barbearia")}


class BarbershopUpdate(BaseModel):
    name: str | None = None
    business_type: str | None = None
    whatsapp_mode: str | None = None


@app.put("/barbershop")
def update_barbershop(body: BarbershopUpdate, barbershop_id: int = Depends(get_current_barbershop_id)):
    updated = False
    if body.name or body.business_type:
        if db.update_barbershop(barbershop_id, name=body.name, business_type=body.business_type):
            updated = True
    if body.whatsapp_mode is not None:
        with db.get_connection() as conn:
            conn.execute("UPDATE barbershops SET whatsapp_mode = ? WHERE id = ?", (body.whatsapp_mode, barbershop_id))
            updated = True
    if not updated:
        raise HTTPException(status_code=400, detail="Nothing to update")
    shop = db.get_barbershop(barbershop_id)
    return {"id": shop["id"], "name": shop["name"], "business_type": shop.get("business_type", "barbearia"), "whatsapp_mode": shop.get("whatsapp_mode", "business")}


# ── Webhook (WhatsApp Manager → Backend) ───────────────────────

@app.post("/webhook/wa-message")
async def wa_message_webhook(request: Request):
    """Recebe mensagens do WhatsApp Manager (Node.js)"""
    body = await request.json()
    wa_number = body.get("from")
    text = body.get("text", "")
    barbershop_id = body.get("barbershop_id")

    if not barbershop_id or not text:
        return {"error": "missing fields"}

    thread_id = f"wa_{barbershop_id}_{wa_number}"

    # Personal mode: scheduling window logic
    shop = db.get_barbershop(barbershop_id)
    if shop and shop.get("whatsapp_mode") == "personal":
        import re as _re
        keywords = r'agendar|marcar|hor[aá]rio|agendamento|consulta|sess[aã]o|corte|barba|servi[çc]o|pre[çc]o|valor|quanto|custa|dispon[íi]vel|vaga|confirmar|cancelar|reagendar|remarcar'
        in_window = _scheduling_window.get(thread_id, False)
        if not in_window:
            if _re.search(keywords, text, _re.IGNORECASE):
                _scheduling_window[thread_id] = True  # open window
            else:
                return {"reply": None, "barbershop_id": barbershop_id}  # silent ignore

    resolved = _resolve_dates(text)
    db.save_message(thread_id, "user", resolved)

    prior = db.get_conversation(thread_id, limit=12)
    prompt = _build_prompt(barbershop_id, thread_id)
    history = [{"role": "system", "content": prompt}] + prior

    action_executed = False

    for iteration in range(3):
        raw = await call_llm(history)
        parsed = extract_json(raw)
        if not parsed:
            reply = "Desculpe, não entendi. Pode repetir?"
            db.save_message(thread_id, "assistant", reply)
            return {"reply": reply, "barbershop_id": barbershop_id}

        action = parsed.get("action", "reply")
        params = parsed.get("parameters", {})
        msg = parsed.get("message", "")

        # Hallucination guard: only on first turn, before any real action
        if action == "reply" and not action_executed:
            lower_msg = msg.lower()
            hallucination_keywords = [
                "foi cancelado", "cancelado com sucesso", "cancelado!",
                "foi criado", "criado com sucesso",
                "foi remarcado", "remarcado com sucesso",
                "foi reagendado",
            ]
            if any(w in lower_msg for w in hallucination_keywords):
                history.append({"role": "assistant", "content": raw})
                history.append({
                    "role": "user",
                    "content": "Você disse que realizou uma ação mas usou 'reply'. Você PRECISA usar a action correta (create_appointment, cancel_appointment, etc) para executar a ação. NÃO finja que executou. Responda APENAS com JSON contendo a action correta."
                })
                continue

        # If already executed an action and LLM tries another, block it
        if action_executed and action != "reply":
            result, _ = execute_action(action, params, barbershop_id, wa_number)
            reply = result or msg or "Concluído."
            db.save_message(thread_id, "assistant", reply)
            return {"reply": reply, "barbershop_id": barbershop_id}

        result, created_id = execute_action(action, params, barbershop_id, wa_number)

        if action == "reply" or result is None or iteration == 2:
            reply = msg or result or "Processado."
            db.save_message(thread_id, "assistant", reply)
            return {"reply": reply, "barbershop_id": barbershop_id}

        action_executed = True

        if created_id:
            _scheduling_window.pop(thread_id, None)  # close personal mode window
            with _last_lock:
                _last_appointment[thread_id] = created_id
            a = db.get_appointment(barbershop_id, created_id)
            history.append({
                "role": "system",
                "content": f"Appointment #{created_id} ({a['title']}) created for this client."
            })

        # Action succeeded — let the LLM craft a natural reply
        history.append({"role": "assistant", "content": raw})
        history.append({
            "role": "user",
            "content": f"Ação executada. Resultado:\n{result}\nAgora responda ao cliente em português natural. Use APENAS a ação reply."
        })
    return {"reply": "Desculpe, não consegui processar. Pode repetir?", "barbershop_id": barbershop_id}


# ── Demo: PatoBarba WhatsApp Simulator ────────────────────────────

DEMO_EMAIL = "demo@patobarba.com"

@app.post("/demo/login")
def demo_login(request: Request):
    shop = db.verify_password(DEMO_EMAIL, "patobarba123")
    if not shop:
        shop = db.create_barbershop("PatoBarba", DEMO_EMAIL, "patobarba123", business_type="barbearia")
        if not shop:
            raise HTTPException(status_code=500, detail="Failed to create demo")
        for name, dur, price in [
            ("Corte de Cabelo", 45, 50.0),
            ("Barba", 20, 30.0),
            ("Corte + Barba", 60, 80.0),
            ("Hidratação", 30, 40.0),
            ("Sobrancelha", 15, 20.0),
        ]:
            db.create_service(shop["id"], name, dur, price)
    token = create_token(shop["id"])
    return {"token": token, "barbershop_id": shop["id"], "name": shop["name"]}


WA_DEMO_PAGE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>PatoBarba</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:#111;height:100vh;display:flex;align-items:center;justify-content:center}
#app{width:100%;max-width:480px;height:100vh;max-height:820px;display:flex;flex-direction:column;background:#efeae2;position:relative;overflow:hidden}
.hdr{background:#075e54;color:#fff;padding:10px 16px;display:flex;align-items:center;gap:12px;flex-shrink:0}
.hdr img{width:40px;height:40px;border-radius:50%;background:#128c7e;padding:6px}
.hdr .nm{flex:1}
.hdr .nm b{font-size:16px;display:block}
.hdr .nm small{font-size:12px;opacity:.8}
.hdr .icn{display:flex;gap:6px}
.hdr .icn svg{width:22px;height:22px;fill:#fff;opacity:.8;cursor:pointer}
.chat{flex:1;overflow-y:auto;padding:12px 16px;display:flex;flex-direction:column;gap:4px;background:#e5ddd5}
.bbl{max-width:88%;padding:8px 12px;border-radius:8px;font-size:14.5px;line-height:1.45;position:relative;word-wrap:break-word;white-space:pre-wrap;animation:fadeIn .2s}
.bbl p{margin:0}
.bbl .tm{font-size:11px;color:#999;text-align:right;margin-top:4px;display:flex;align-items:center;justify-content:flex-end;gap:3px}
.bbl.user{background:#dcf8c6;align-self:flex-end;border-bottom-right-radius:3px}
.bbl.user .tm{color:#839a7a}
.bbl.ai{background:#fff;align-self:flex-start;border-bottom-left-radius:3px}
.bbl.ai .tm{color:#999}
.bbl .dd{font-size:10px;color:#53bdeb;margin-left:2px}
.inp{background:#f0f2f5;padding:8px 10px;display:flex;gap:6px;align-items:center;flex-shrink:0}
.inp input{flex:1;padding:10px 16px;border:none;border-radius:24px;outline:none;font-size:15px;background:#fff}
.inp button{width:44px;height:44px;border:none;border-radius:50%;background:#128c7e;color:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:background .15s}
.inp button:active{background:#075e54}
.inp button:disabled{opacity:.4}
.typing{display:flex;gap:5px;padding:12px 16px;background:#fff;border-radius:8px;align-self:flex-start;margin-bottom:4px}
.typing span{width:8px;height:8px;background:#999;border-radius:50%;animation:bounce 1.4s infinite}
.typing span:nth-child(2){animation-delay:.2s}
.typing span:nth-child(3){animation-delay:.4s}
@keyframes bounce{0%,80%,100%{transform:scale(.6)}40%{transform:scale(1)}}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
.sc{display:flex;align-items:center;justify-content:center;height:100%;color:#999;flex-direction:column;gap:10px;text-align:center;padding:20px;font-size:15px}
.sc svg{width:60px;height:60px;fill:#ddd}
</style>
</head>
<body>
<div id="app">
<div class="hdr">
<img src="/static/logo.png" alt="P">
<div class="nm"><b>PatoBarba</b><small id="status">conectando...</small></div>
<div class="icn">
<svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>
<svg viewBox="0 0 24 24"><path d="M12 8c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zm0 2c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm0 6c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2z"/></svg>
</div>
</div>
<div class="chat" id="chat"><div class="sc" id="welcome"><svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H5.17L4 17.17V4h16v12z"/><path d="M7 9h2v2H7zm4 0h2v2h-2zm4 0h2v2h-2z"/></svg><span id="wlcm">Conectando...</span></div></div>
<div class="inp">
<input id="inp" placeholder="Digite sua mensagem" disabled>
<button id="btn" disabled><svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor"><path d="M1.101 21.757L23.8 12.028 1.101 2.3l.011 7.912 13.623 1.816-13.623 1.817-.011 7.912z"/></svg></button>
</div>
</div>
<script>
(function(){
var tok=localStorage.getItem('patobarba_token'),tid=localStorage.getItem('patobarba_tid');
var chat=document.getElementById('chat'),inp=document.getElementById('inp'),btn=document.getElementById('btn');
var st=document.getElementById('status'),wl=document.getElementById('wlcm');

function esc(s){var d=document.createElement('div');d.appendChild(document.createTextNode(s));return d.innerHTML}
function tm(){var d=new Date();return('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2)}

function addMsg(text,role){
var d=document.createElement('div');d.className='bbl '+role;
d.innerHTML='<p>'+esc(text)+'</p><div class="tm">'+tm()+(role==='user'?'<span class="dd">✓✓</span>':'')+'</div>';
chat.appendChild(d);d.scrollIntoView({behavior:'smooth',block:'end'});
try{localStorage.setItem('patobarba_msgs',JSON.stringify(Array.from(document.querySelectorAll('.bbl')).map(function(m){return{t:m.querySelector('p').textContent,r:m.classList.contains('user')?'user':'ai',h:m.querySelector('.tm').textContent.trim()}})))}catch(e){}
}

function typing(on){
var e=chat.querySelector('.typing');
if(on&&!e){var d=document.createElement('div');d.className='typing';d.innerHTML='<span></span><span></span><span></span>';chat.appendChild(d);d.scrollIntoView({behavior:'smooth',block:'end'})}
else if(!on&&e)e.remove()
}

function connected(){
st.textContent='online';st.style.color='#7fefa7';
wl.textContent='Ol\u00e1! Como posso ajudar?';
inp.disabled=false;btn.disabled=false;inp.focus();
}

function loadMsgs(){
try{
var m=JSON.parse(localStorage.getItem('patobarba_msgs')||'[]');
m.forEach(function(x){
var d=document.createElement('div');d.className='bbl '+x.r;
d.innerHTML='<p>'+esc(x.t)+'</p><div class="tm">'+esc(x.h)+'</div>';
chat.appendChild(d)
});
var sc=document.getElementById('welcome');if(sc)sc.remove()
}catch(e){}
}

loadMsgs();

window.send=async function(){
var text=inp.value.trim();if(!text||btn.disabled)return;
inp.value='';btn.disabled=true;
addMsg(text,'user');typing(true);
try{
var res=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},body:JSON.stringify({message:text,thread_id:tid||null})});
if(res.status===401){tok=null;localStorage.removeItem('patobarba_token');typing(false);addMsg('Sessao expirada. Recarregue a pagina.','ai');btn.disabled=false;return}
var d=await res.json();
if(d.thread_id){tid=d.thread_id;localStorage.setItem('patobarba_tid',tid)}
typing(false);
if(d.reply)addMsg(d.reply,'ai');else addMsg('(sem resposta)','ai')
}catch(e){typing(false);addMsg('Erro de conex\u00e3o.','ai')}
btn.disabled=false;inp.focus();
};

inp.addEventListener('keydown',function(e){if(e.key==='Enter')window.send()});
btn.addEventListener('click',window.send);

var wlcm=document.getElementById('welcome');
if(tok&&tok!=='null'&&tok!=='undefined'){
connected();
}else{
fetch('/demo/login',{method:'POST'}).then(function(r){
  if(!r.ok)throw new Error(r.status);
  return r.json()
}).then(function(d){
  tok=d.token;localStorage.setItem('patobarba_token',tok);
  connected();
}).catch(function(e){
  st.textContent='erro de conexao';st.style.color='#ff6b6b';
  wl.textContent='Nao foi possivel conectar. Recarregue a pagina.';
})
}
})();
</script>
</body>
</html>"""


@app.get("/demo", response_class=HTMLResponse)
def demo_page():
    return HTMLResponse(WA_DEMO_PAGE)


# ── Redirect root to login ──────────────────────────────────────

@app.get("/")
def root():
    return HTMLResponse("""<script>window.location.href='/login'</script>""")


# ── Webhook WhatsApp Cloud API (futuro) ────────────────────────

@app.get("/webhook/whatsapp")
def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_token == WEBHOOK_VERIFY_TOKEN:
        return hub_challenge or "ok"
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook/whatsapp")
async def whatsapp_cloud_webhook(request: Request):
    body = await request.json()
    try:
        entry = body["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]
        for msg in value.get("messages", []):
            if msg["type"] == "text":
                text = msg["text"]["body"]
                phone = msg["from"]
                phone_number_id = value["metadata"]["phone_number_id"]
                access_token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
                if access_token:
                    import httpx
                    async with httpx.AsyncClient() as http:
                        await http.post(
                            f"https://graph.facebook.com/v21.0/{phone_number_id}/messages",
                            headers={"Authorization": f"Bearer {access_token}"},
                            json={"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": "Obrigado!"}},
                        )
    except (KeyError, IndexError):
        pass
    return {"status": "ok"}
