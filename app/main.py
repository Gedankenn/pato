import json
import os
import re
from datetime import datetime, timedelta
import httpx
from dotenv import load_dotenv
from openai import OpenAI

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
)
from app import database as db
from app.llm import SYSTEM_PROMPT
from app.auth import create_token, get_current_barbershop_id, get_barbershop_id_from_request, require_admin

app = FastAPI(title="PatoAgenda AI — Agendamentos Inteligentes")

app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

WEBHOOK_VERIFY_TOKEN = os.environ.get("WEBHOOK_VERIFY_TOKEN", "pato123")
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
    return OpenAI(**kwargs)


LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")


@app.on_event("startup")
def startup():
    db.init_db()


def format_appointment(a: dict) -> str:
    return (
        f"[#{a['id']}] {a['title']} - {a['start_time']} to {a['end_time']} "
        f"({a['status']})"
    )


def execute_action(action: str, params: dict, barbershop_id: int) -> tuple[str | None, int | None]:
    if action == "create_appointment":
        title = params.get("title", "").strip()
        if not title:
            return "Title is required. Ask the customer what service they want.", None
        start = params.get("start_time")
        end = params.get("end_time")
        if not start or not end:
            return "start_time and end_time are required. Ask the customer when they want to book.", None
        appt_id = db.create_appointment(
            barbershop_id=barbershop_id,
            title=title,
            description=params.get("description", ""),
            start_time=start,
            end_time=end,
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


def call_llm(messages: list) -> str:
    response = get_openai().chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        temperature=0.1,
    )
    return response.choices[0].message.content or ""


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
    resp.set_cookie(key="token", value=token, httponly=False, max_age=2592000, path="/")
    return resp


@app.post("/auth/register")
def register(body: RegisterRequest):
    shop = db.create_barbershop(body.name, body.email, body.password)
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
    resp.set_cookie(key="token", value=token, httponly=False, max_age=2592000, path="/")
    return resp


# ── Chat ────────────────────────────────────────────────────────

_last_appointment: dict[str, int] = {}

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
    services = db.list_services(barbershop_id)
    base = SYSTEM_PROMPT
    if services:
        lines = "\n".join(
            f"  - {s['name']}: R$ {s['price']:.2f} ({s['duration_minutes']}min)"
            for s in services
        )
        base += f"\n\nSERVIÇOS DA BARBEARIA:\n{lines}\n\nUse esta lista para informar preços e durações quando o cliente perguntar."
    if thread_id and thread_id in _last_appointment:
        a = db.get_appointment(barbershop_id, _last_appointment[thread_id])
        if a and a["status"] == "scheduled":
            base += f"\n\núltimo agendamento deste cliente: #{a['id']} ({a['title']} às {a['start_time'][:16]})."
    return base


@app.post("/chat", response_model=MessageResponse)
def chat(
    request: MessageRequest,
    barbershop_id: int = Depends(get_current_barbershop_id),
):
    thread_id = request.thread_id or f"thread_{datetime.utcnow().timestamp()}"

    resolved = _resolve_dates(request.message)
    db.save_message(thread_id, "user", resolved)

    prior = db.get_conversation(thread_id, limit=12)
    prompt = _build_prompt(barbershop_id, thread_id)
    history = [{"role": "system", "content": prompt}] + prior

    action_executed = False

    for iteration in range(3):
        raw = call_llm(history)
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
    appt_id = db.create_appointment(
        barbershop_id=barbershop_id,
        title=body.title,
        description=body.description,
        start_time=body.start_time,
        end_time=body.end_time,
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
        return JSONResponse(json.load(open(status_path)))
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
        return JSONResponse(json.load(open(status_path)))
    return JSONResponse({"status": "inactive"})


@app.get("/admin/whatsapp/qrcode/{barbershop_id}")
def admin_whatsapp_qrcode(barbershop_id: int, _=Depends(require_admin)):
    qr_path = os.path.join(get_wa_status_dir(barbershop_id), "qrcode.png")
    if os.path.exists(qr_path):
        return FileResponse(qr_path, media_type="image/png")
    return JSONResponse({"error": "QR code not available"}, status_code=404)


# ── Dashboard (scoped) ──────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    barbershop_id = get_barbershop_id_from_request(request)
    if not barbershop_id:
        return RedirectResponse(url="/login")
    shop = db.get_barbershop(barbershop_id)
    appointments = db.list_appointments(barbershop_id)

    status_path = os.path.join(get_wa_status_dir(barbershop_id), "status.json")
    wa_status = "inactive"
    if os.path.exists(status_path):
        wa_status = json.load(open(status_path)).get("status", "inactive")

    app_rows = "".join(
        f"<tr><td>#{a['id']}</td><td>{a['title']}</td><td>{a['start_time']}</td><td>{a['end_time']}</td><td class='s-{a['status']}'>{a['status']}</td></tr>"
        for a in appointments
    )

    qr_block = ""
    if wa_status == "awaiting_scan":
        qr_block = f"""<div class="card"><h2>📱 Conectar WhatsApp</h2><p>Escaneie o QR code com o WhatsApp da empresa:</p><p class="hint">WhatsApp > ⋮ > Aparelhos conectados > Conectar</p><img class="qr" src="/whatsapp/qrcode" alt="QR Code"></div>"""

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>PatoAgenda AI - {shop['name']}</title><link rel="icon" type="image/png" href="/static/logo.png">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333}}
.header{{background:#1a73e8;color:#fff;padding:20px;text-align:center}}
.header .logo{{width:100px;height:100px;border-radius:50%;object-fit:cover;background:#fff;padding:6px;margin-bottom:8px;box-shadow:0 2px 8px rgba(0,0,0,.15)}}
.header h1{{font-size:24px;margin-top:4px}}
.container{{max-width:900px;margin:20px auto;padding:0 16px}}
.card{{background:#fff;border-radius:12px;padding:20px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
.card h2{{margin-bottom:12px;font-size:18px}}
.badge{{display:inline-block;padding:4px 12px;border-radius:20px;font-size:13px;font-weight:600}}
.b-connected{{background:#e6f4ea;color:#1e7e34}}
.b-awaiting_scan{{background:#fef7e0;color:#e37400}}
.b-inactive,.b-unknown{{background:#f1f3f4;color:#5f6368}}
.b-disconnected{{background:#fce8e6;color:#c5221f}}
.hint{{color:#666;font-size:13px;margin:8px 0}}
table{{width:100%;border-collapse:collapse}}
th,td{{padding:10px 8px;text-align:left;border-bottom:1px solid #eee;font-size:14px}}
th{{color:#666;font-weight:600}}
.s-scheduled{{color:#1e7e34}}
.s-rescheduled{{color:#e37400}}
.s-cancelled{{color:#c5221f;text-decoration:line-through}}
img.qr{{display:block;margin:12px auto;width:260px;image-rendering:pixelated}}

.empty{{text-align:center;color:#999;padding:30px}}
.ftr{{text-align:center;padding:20px;color:#999;font-size:13px}}
.logout{{float:right;color:#fff;text-decoration:none;font-size:14px;opacity:.8}}
</style></head>
<body>
<div class="header"><img src="/static/logo.png" class="logo" alt="PatoAgenda AI"><h1>PatoAgenda AI</h1>
<p>
  <a href="/dashboard" style="color:#fff;text-decoration:none;font-weight:700">📋 Agenda</a>
  &nbsp;·&nbsp;
  <a href="/config" style="color:#fff;text-decoration:none">⚙️ Config</a>
  {' · <a href="/admin" style="color:#fff;text-decoration:none">Admin</a>' if shop.get('is_admin') else ''}
  <a href="/logout" class="logout">sair</a>
</p></div>
<div class="container">
<div class="card"><h2>🤖 WhatsApp</h2><span class="badge b-{wa_status}">{wa_status}</span></div>
{qr_block}
<div class="card"><h2>📋 Agendamentos</h2>
{"<table><thead><tr><th>#</th><th>Serviço</th><th>Início</th><th>Fim</th><th>Status</th></tr></thead><tbody>" + app_rows + "</tbody></table>" if appointments else '<p class="empty">Nenhum agendamento</p>'}
</div></div>
<div class="ftr">PatoAgenda AI v1.0 — Agendamentos Inteligentes — <a href="mailto:fabiostella@gmail.com" style="color:#999;text-decoration:none">fabiostella@gmail.com</a></div>
<script>setTimeout(()=>location.reload(),15000)</script>
</body></html>""")


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

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Configuração - PatoAgenda AI</title><link rel="icon" type="image/png" href="/static/logo.png">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#333}}
.header{{background:#1a73e8;color:#fff;padding:20px;text-align:center}}
.header .logo{{width:100px;height:100px;border-radius:50%;object-fit:cover;background:#fff;padding:6px;margin-bottom:8px;box-shadow:0 2px 8px rgba(0,0,0,.15)}}
.container{{max-width:900px;margin:20px auto;padding:0 16px}}
.card{{background:#fff;border-radius:12px;padding:20px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
.card h2{{margin-bottom:12px;font-size:18px}}
.badge{{display:inline-block;padding:4px 12px;border-radius:20px;font-size:13px;font-weight:600}}
.b-connected{{background:#e6f4ea;color:#1e7e34}}
.b-awaiting_scan{{background:#fef7e0;color:#e37400}}
.b-inactive,.b-unknown{{background:#f1f3f4;color:#5f6368}}
.b-disconnected{{background:#fce8e6;color:#c5221f}}
img.qr{{display:block;margin:12px auto;width:260px;image-rendering:pixelated}}
table{{width:100%;border-collapse:collapse}}
th,td{{padding:10px 8px;text-align:left;border-bottom:1px solid #eee;font-size:14px}}
th{{color:#666;font-weight:600}}
input,select{{padding:8px;border:1px solid #ddd;border-radius:6px;font-size:14px;width:100%;margin-bottom:8px}}
.btn{{display:inline-block;padding:10px 20px;border:none;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer}}
.btn-primary{{background:#1a73e8;color:#fff}}
.btn-primary:hover{{background:#1557b0}}
.btn-danger{{background:#c5221f;color:#fff}}
.btn-sm{{padding:4px 8px;border:none;border-radius:6px;cursor:pointer;font-size:14px}}
.ftr{{text-align:center;padding:20px;color:#999;font-size:13px}}
.form-row{{display:flex;gap:12px;align-items:end;flex-wrap:wrap}}
.form-row .field{{flex:1;min-width:140px}}
.form-row .field label{{display:block;font-size:13px;color:#666;margin-bottom:4px}}
.empty{{text-align:center;color:#999;padding:30px}}
.msg{{padding:10px;border-radius:6px;margin-bottom:12px;display:none}}
.msg-success{{background:#e6f4ea;color:#1e7e34;display:block}}
.msg-error{{background:#fce8e6;color:#c5221f;display:block}}
.logout{{float:right;color:#fff;text-decoration:none;font-size:14px;opacity:.8}}
.nav a{{color:#fff;text-decoration:none}}
.nav a:hover{{text-decoration:underline}}
</style></head>
<body>
<div class="header"><img src="/static/logo.png" class="logo" alt="PatoAgenda AI"><h1>PatoAgenda AI</h1>
<p class="nav">
  <a href="/dashboard">📋 Agenda</a>
  &nbsp;·&nbsp;
  <a href="/config" style="font-weight:700">⚙️ Config</a>
  <a href="/logout" class="logout">sair</a>
</p></div>
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
  <h2>📱 WhatsApp</h2>
  <p>Status: <span class="badge b-{wa_status}">{wa_status}</span></p>
  {qr_block}
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
</script>
</body></html>""")



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

    shop_rows = "".join(
        f"<tr><td>#{s['id']}</td><td>{s['name']}</td><td>{s['email']}</td>"
        f"<td>{s['whatsapp_number'] or '-'}</td>"
        f"<td>{'<span class=\"badge b-admin\">Admin</span>' if s['is_admin'] else '<span class=\"badge b-shop\">Loja</span>'}</td>"
        f"<td><span id=\"wa-status-{s['id']}\">…</span></td>"
        f"<td><button class=\"btn-sm\" onclick=\"generateQr({s['id']})\">📱 QR</button></td>"
        f"<td>{s['created_at'][:10]}</td></tr>"
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
{"<table><thead><tr><th>#</th><th>Nome</th><th>Email</th><th>WhatsApp</th><th>Tipo</th><th>WA Status</th><th></th><th>Criado</th></tr></thead><tbody>" + shop_rows + "</tbody></table>" if shops else '<p style="color:#888">Nenhuma empresa</p>'}
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
if(localStorage.getItem('token')){window.location.href='/dashboard'}
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
    return {"id": shop["id"], "name": shop["name"], "email": shop["email"], "whatsapp_number": shop.get("whatsapp_number")}


# ── Webhook (WhatsApp Manager → Backend) ───────────────────────

@app.post("/webhook/wa-message")
def wa_message_webhook(request: Request):
    """Recebe mensagens do WhatsApp Manager (Node.js)"""
    body = request.json()
    wa_number = body.get("from")
    text = body.get("text", "")
    barbershop_id = body.get("barbershop_id")

    if not barbershop_id or not text:
        return {"error": "missing fields"}

    thread_id = f"wa_{barbershop_id}_{wa_number}"

    resolved = _resolve_dates(text)
    db.save_message(thread_id, "user", resolved)

    prior = db.get_conversation(thread_id, limit=12)
    prompt = _build_prompt(barbershop_id, thread_id)
    history = [{"role": "system", "content": prompt}] + prior

    action_executed = False

    for iteration in range(3):
        raw = call_llm(history)
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
            result, _ = execute_action(action, params, barbershop_id)
            reply = result or msg or "Concluído."
            db.save_message(thread_id, "assistant", reply)
            return {"reply": reply, "barbershop_id": barbershop_id}

        result, created_id = execute_action(action, params, barbershop_id)

        if action == "reply" or result is None or iteration == 2:
            reply = msg or result or "Processado."
            db.save_message(thread_id, "assistant", reply)
            return {"reply": reply, "barbershop_id": barbershop_id}

        action_executed = True

        if created_id:
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
def demo_login():
    shop = db.verify_password(DEMO_EMAIL, "patobarba123")
    if not shop:
        shop = db.create_barbershop("PatoBarba", DEMO_EMAIL, "patobarba123")
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
.chat .dt{text-align:center;font-size:12px;color:#888;margin:8px 0 4px;background:#e1f3fb;display:inline-block;padding:4px 12px;border-radius:6px;align-self:center}
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
.sc{display:flex;align-items:center;justify-content:center;height:100%;color:#999;flex-direction:column;gap:10px;text-align:center;padding:20px}
.sc svg{width:60px;height:60px;fill:#ddd}
</style>
</head>
<body>
<div id="app">
<div class="hdr">
<img src="/static/logo.png" alt="P">
<div class="nm"><b>PatoBarba</b><small>online</small></div>
<div class="icn">
<svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>
<svg viewBox="0 0 24 24"><path d="M12 8c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zm0 2c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm0 6c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2z"/></svg>
</div>
</div>
<div class="chat" id="chat"><div class="sc"><svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H5.17L4 17.17V4h16v12z"/><path d="M7 9h2v2H7zm4 0h2v2h-2zm4 0h2v2h-2z"/></svg>Conectando...</div></div>
<div class="inp">
<input id="inp" placeholder="Digite sua mensagem" autofocus>
<button id="btn" onclick="send()"><svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor"><path d="M1.101 21.757L23.8 12.028 1.101 2.3l.011 7.912 13.623 1.816-13.623 1.817-.011 7.912z"/></svg></button>
</div>
</div>
<script>
let tok=localStorage.getItem('patobarba_token'),tid=localStorage.getItem('patobarba_tid'),chatEl=document.getElementById('chat'),inp=document.getElementById('inp'),btn=document.getElementById('btn');
function esc(s){const d=document.createElement('div');d.appendChild(document.createTextNode(s));return d.innerHTML}
function tm(){const d=new Date();return d.getHours().toString().padStart(2,'0')+':'+d.getMinutes().toString().padStart(2,'0')}
function addMsg(text,role){
const d=document.createElement('div');d.className='bbl '+role;
d.innerHTML='<p>'+esc(text)+'</p><div class="tm">'+tm()+(role==='user'?'<span class="dd">✓✓</span>':'')+'</div>';
chatEl.appendChild(d);d.scrollIntoView({behavior:'smooth',block:'end'});
localStorage.setItem('patobarba_msgs',JSON.stringify([...document.querySelectorAll('.bbl')].map(m=>({t:m.querySelector('p').textContent,r:m.classList.contains('user')?'user':'ai',h:m.querySelector('.tm').textContent.trim()}))));
}
function typing(on){const e=chatEl.querySelector('.typing');if(on&&!e){const d=document.createElement('div');d.className='typing';d.innerHTML='<span></span><span></span><span></span>';chatEl.appendChild(d);d.scrollIntoView({behavior:'smooth',block:'end'})}else if(!on&&e)e.remove()}
function loadMsgs(){try{const m=JSON.parse(localStorage.getItem('patobarba_msgs')||'[]');m.forEach(x=>{const d=document.createElement('div');d.className='bbl '+x.r;d.innerHTML='<p>'+esc(x.t)+'</p><div class="tm">'+esc(x.h)+'</div>';chatEl.appendChild(d)});document.querySelector('.sc')?.remove()}catch(e){}}
loadMsgs();
async function send(){
const text=inp.value.trim();if(!text||btn.disabled)return;
inp.value='';btn.disabled=true;
addMsg(text,'user');typing(true);
try{
const res=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},body:JSON.stringify({message:text,thread_id:tid||null})});
const d=await res.json();
if(d.thread_id){tid=d.thread_id;localStorage.setItem('patobarba_tid',tid)}
typing(false);
if(d.reply)addMsg(d.reply,'ai');
else addMsg('(sem resposta)','ai');
}catch(e){typing(false);addMsg('Erro de conexão.','ai')}
btn.disabled=false;inp.focus();
}
inp.addEventListener('keydown',e=>{if(e.key==='Enter')send()});
if(!tok){
fetch('/demo/login',{method:'POST'}).then(r=>r.json()).then(d=>{tok=d.token;localStorage.setItem('patobarba_token',tok);document.querySelector('.sc')&&(document.querySelector('.sc').innerHTML='<svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H5.17L4 17.17V4h16v12z"/><path d="M7 9h2v2H7zm4 0h2v2h-2zm4 0h2v2h-2z"/></svg>Olá! Como posso ajudar? 💈'});
}
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
        return int(hub_challenge) if hub_challenge else "ok"
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
