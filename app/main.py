import json
import os
import re
from datetime import datetime
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
)
from app import database as db
from app.llm import SYSTEM_PROMPT
from app.auth import create_token, get_current_barbershop_id, get_barbershop_id_from_request

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
WHATSAPP_DATA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "whatsapp", "data"
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


def execute_action(action: str, params: dict, barbershop_id: int) -> str | None:
    if action == "create_appointment":
        appt_id = db.create_appointment(
            barbershop_id=barbershop_id,
            title=params["title"],
            description=params.get("description", ""),
            start_time=params["start_time"],
            end_time=params["end_time"],
        )
        a = db.get_appointment(barbershop_id, appt_id)
        return f"Created: {format_appointment(a)}"

    elif action == "list_appointments":
        appointments = db.list_appointments(barbershop_id, status=params.get("status"))
        if not appointments:
            return "No appointments found."
        return "\n".join(format_appointment(a) for a in appointments)

    elif action == "get_appointment":
        a = db.get_appointment(barbershop_id, params["appointment_id"])
        if not a:
            return f"Appointment #{params['appointment_id']} not found."
        return (
            f"Appointment #{a['id']}: {a['title']}\n"
            f"  Description: {a['description']}\n"
            f"  Start: {a['start_time']}\n"
            f"  End: {a['end_time']}\n"
            f"  Status: {a['status']}"
        )

    elif action == "reschedule_appointment":
        success = db.reschedule_appointment(
            barbershop_id,
            params["appointment_id"],
            params["new_start_time"],
            params["new_end_time"],
        )
        if not success:
            return f"Appointment #{params['appointment_id']} not found."
        a = db.get_appointment(barbershop_id, params["appointment_id"])
        return f"Rescheduled: {format_appointment(a)}"

    elif action == "cancel_appointment":
        success = db.cancel_appointment(barbershop_id, params["appointment_id"])
        if not success:
            return f"Appointment #{params['appointment_id']} not found."
        a = db.get_appointment(barbershop_id, params["appointment_id"])
        return f"Cancelled: {format_appointment(a)}"

    elif action == "reply":
        return None

    return f"Unknown action: {action}"


def extract_json(text: str) -> dict | None:
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
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

@app.post("/chat", response_model=MessageResponse)
def chat(
    request: MessageRequest,
    barbershop_id: int = Depends(get_current_barbershop_id),
):
    thread_id = request.thread_id or f"thread_{datetime.utcnow().timestamp()}"

    history = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": request.message},
    ]

    for _ in range(5):
        raw = call_llm(history)
        parsed = extract_json(raw)

        if not parsed or not isinstance(parsed, dict):
            return MessageResponse(
                reply="I couldn't process that request. Could you rephrase?",
                thread_id=thread_id,
            )

        action = parsed.get("action", "reply")
        params = parsed.get("parameters", {})
        msg = parsed.get("message", "")

        result = execute_action(action, params, barbershop_id)

        if action == "reply" or result is None:
            return MessageResponse(reply=msg, thread_id=thread_id)

        history.append({"role": "assistant", "content": raw})
        history.append({
            "role": "user",
            "content": f"The action was executed. Result:\n{result}",
        })

    return MessageResponse(
        reply="I've processed your request.",
        thread_id=thread_id,
    )


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
<div class="header"><img src="/static/logo.png" class="logo" alt="PatoAgenda AI"><h1>PatoAgenda AI</h1><p>{shop['name']} <a href="/logout" class="logout">sair</a></p></div>
<div class="container">
<div class="card"><h2>🤖 WhatsApp</h2><span class="badge b-{wa_status}">{wa_status}</span></div>
{qr_block}
<div class="card"><h2>📋 Agendamentos</h2>
{"<table><thead><tr><th>#</th><th>Serviço</th><th>Início</th><th>Fim</th><th>Status</th></tr></thead><tbody>" + app_rows + "</tbody></table>" if appointments else '<p class="empty">Nenhum agendamento</p>'}
</div></div>
<div class="ftr">PatoAgenda AI v1.0 — Agendamentos Inteligentes</div>
<script>setTimeout(()=>location.reload(),15000)</script>
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
<form id="loginForm" onsubmit="return submitForm('login')">
<input type="email" id="loginEmail" placeholder="Email" required>
<input type="password" id="loginPass" placeholder="Senha" required>
<button type="submit">Entrar</button>
<div class="error" id="loginError"></div>
</form>
<form id="registerForm" class="hidden" onsubmit="return submitForm('register')">
<input type="text" id="regName" placeholder="Nome da empresa" required>
<input type="email" id="regEmail" placeholder="Email" required>
<input type="password" id="regPass" placeholder="Senha" required>
<button type="submit">Cadastrar</button>
<div class="error" id="regError"></div>
</form>
</div>
<script>
function showTab(t){document.getElementById('loginForm').classList.toggle('hidden',t!='login');document.getElementById('registerForm').classList.toggle('hidden',t!='register');document.getElementById('tabLogin').classList.toggle('active',t=='login');document.getElementById('tabReg').classList.toggle('active',t=='register')}
async function submitForm(t){const form=t=='login'?{email:loginEmail.value,password:loginPass.value}:{name:regName.value,email:regEmail.value,password:regPass.value};const res=await fetch('/auth/'+t,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(form)});const data=await res.json();if(!res.ok){document.getElementById(t+'Error').textContent=data.detail||'Erro';document.getElementById(t+'Error').style.display='block';return false}
localStorage.setItem('token',data.token);localStorage.setItem('name',data.name);window.location.href='/dashboard';return false}
if(localStorage.getItem('token')){window.location.href='/dashboard'}
</script>
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

    history = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]

    for _ in range(5):
        raw = call_llm(history)
        parsed = extract_json(raw)
        if not parsed:
            return {"reply": "Desculpe, não entendi. Pode repetir?", "barbershop_id": barbershop_id}

        action = parsed.get("action", "reply")
        params = parsed.get("parameters", {})
        msg = parsed.get("message", "")
        result = execute_action(action, params, barbershop_id)

        if action == "reply" or result is None:
            return {"reply": msg, "barbershop_id": barbershop_id}

        history.append({"role": "assistant", "content": raw})
        history.append({"role": "user", "content": f"Result: {result}"})

    return {"reply": "Processado.", "barbershop_id": barbershop_id}


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
