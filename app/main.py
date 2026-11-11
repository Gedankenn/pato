"""Patinho — FastAPI app: dashboard, WhatsApp-style simulator, quotas, API."""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import parse_qs

BUDGET_USD = float(os.environ.get("PATINHO_BUDGET_USD", "0"))

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from app import agent, core

app = FastAPI(title="Patinho", docs_url="/api/docs")
core.init()
core.seed()


# Dois hostnames publicos: o app (painel) e a demo (simulador). Um processo so,
# entao o roteamento por Host e feito aqui.
APP_HOST = os.environ.get("PATINHO_APP_HOST", "patopro.stellacorp.com.br").lower()
DEMO_HOST = os.environ.get("PATINHO_DEMO_HOST", "patoprodemo.stellacorp.com.br").lower()
DEMO_BASE = os.environ.get("PATINHO_DEMO_BASE", "https://" + DEMO_HOST)
DEMO_SLUG = os.environ.get("PATINHO_DEMO_SLUG", "barbearia-demo")


def req_host(request):
    return (request.headers.get("host") or "").split(":")[0].strip().lower()


# --------------------------------------------------- limite de uso da demo publica
# A demo nao tem login. Sem teto por IP, um script queima o plano e o custo do LLM.
DEMO_MAX_5MIN = int(os.environ.get("PATINHO_DEMO_MAX_5MIN", "12"))
DEMO_MAX_24H = int(os.environ.get("PATINHO_DEMO_MAX_24H", "60"))
_DEMO_HITS = {}      # ip -> timestamps das mensagens
_DEMO_LOCK = threading.Lock()


def client_ip(request):
    ip = request.client.host if request.client else "?"
    if ip in ("127.0.0.1", "::1"):
        # via Cloudflare o cliente real vem no header; tunnel conecta do loopback
        ip = (request.headers.get("cf-connecting-ip") or ip).split(",")[0].strip()
    return ip or "?"


def demo_allowed(ip):
    now = time.time()
    with _DEMO_LOCK:
        hits = [t for t in _DEMO_HITS.get(ip, []) if now - t < 86400]
        if len(hits) >= DEMO_MAX_24H:
            _DEMO_HITS[ip] = hits
            return False
        if sum(1 for t in hits if now - t < 300) >= DEMO_MAX_5MIN:
            _DEMO_HITS[ip] = hits
            return False
        hits.append(now)
        _DEMO_HITS[ip] = hits
        if len(_DEMO_HITS) > 5000:
            for k in [k for k, v in _DEMO_HITS.items() if not v or now - v[-1] > 86400]:
                _DEMO_HITS.pop(k, None)
        return True


def owned_conversation(tid, cid):
    """So aceita sessao que existe, e da loja certa, e nasceu como demo."""
    if not isinstance(cid, str) or not cid.strip():
        return None
    with core.connect() as con:
        row = con.execute(
            "select id from conversations where id=? and tenant_id=? and kind='demo'",
            (cid.strip(), tid)).fetchone()
    return row["id"] if row else None


class SendIn(BaseModel):
    session: str | None = None
    text: str


def tenant_or_404(slug):
    t = core.tenant(slug)
    if not t:
        raise HTTPException(404, "tenant não encontrado")
    return t


def limit_state(t):
    used = core.usage(t["id"])
    return {"used": used, "limit": t["msg_limit"], "left": max(0, t["msg_limit"] - used),
            "blocked": used >= t["msg_limit"]}


# ---------------------------------------------------------------- contas e login
# Sem dependencia nova: pbkdf2 na stdlib, sessao em cookie assinado com HMAC.
DATA_DIR = os.path.dirname(os.path.abspath(core.DB_PATH))
SESSION_COOKIE = "patinho_session"
SESSION_TTL = int(float(os.environ.get("PATINHO_SESSION_HOURS", "12")) * 3600)
PBKDF2_ROUNDS = 120_000
ADMIN_LOGIN = os.environ.get("PATINHO_ADMIN_USER", "admin").strip().lower()
_ADMIN = {}
_SECRET = {}
_FAILS = {}          # ip -> (contagem, janela)


def _data_file(name):
    return os.path.join(DATA_DIR, name)


def _write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def _salt():
    return secrets.token_hex(16)


def _hash(password, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), PBKDF2_ROUNDS).hex()


def _new_password():
    return secrets.token_urlsafe(9)


def _admin_credential():
    """Senha do dono do produto: env ganha; senao admin.json; senao gera e grava em
    data/admin-password.txt (600) para o dono ler."""
    if _ADMIN:
        return _ADMIN
    env_pw = os.environ.get("PATINHO_ADMIN_PASSWORD", "").strip()
    path = _data_file("admin.json")
    if env_pw:
        salt = "0" * 32
        _ADMIN.update(salt=salt, hash=_hash(env_pw, salt))
        return _ADMIN
    try:
        with open(path) as f:
            d = json.load(f)
        _ADMIN.update(salt=d["salt"], hash=d["hash"])
        return _ADMIN
    except Exception:
        pass
    pw = _new_password()
    salt = _salt()
    _ADMIN.update(salt=salt, hash=_hash(pw, salt))
    try:
        _write_private(path, json.dumps({"user": ADMIN_LOGIN, "salt": salt, "hash": _ADMIN["hash"]}))
        _write_private(_data_file("admin-password.txt"), pw + "\n")
    except Exception:
        pass
    return _ADMIN


def _secret():
    if _SECRET:
        return _SECRET["k"]
    path = _data_file("session.key")
    try:
        with open(path) as f:
            k = f.read().strip()
        if len(k) >= 32:
            _SECRET["k"] = k
            return k
    except Exception:
        pass
    k = secrets.token_hex(32)
    try:
        _write_private(path, k)
    except Exception:
        pass
    _SECRET["k"] = k
    return k


def _session_token(uid):
    ts = str(int(time.time()))
    sig = hmac.new(_secret().encode(), f"{ts}|{uid}".encode(), hashlib.sha256).hexdigest()[:40]
    return f"{ts}.{uid}.{sig}"


def current_user(request):
    raw = request.cookies.get(SESSION_COOKIE, "")
    try:
        ts, uid, sig = raw.split(".", 2)
        if int(time.time()) - int(ts) > SESSION_TTL:
            return None
        want = hmac.new(_secret().encode(), f"{ts}|{uid}".encode(), hashlib.sha256).hexdigest()[:40]
        if not hmac.compare_digest(sig, want):
            return None
        with core.connect() as con:
            u = con.execute("select * from users where id=? and active=1", (int(uid),)).fetchone()
        return dict(u) if u else None
    except Exception:
        return None


def check_login(email, password):
    u = core.user_by_email(email)
    if not u or not u["active"]:
        return None
    return dict(u) if hmac.compare_digest(_hash(password, u["salt"]), u["hash"]) else None


def _client_ip(request):
    return (request.headers.get("cf-connecting-ip") or request.client.host or "").strip()


def _throttled(ip):
    now = time.time()
    n, start = _FAILS.get(ip, (0, now))
    if now - start > 60:
        n, start = 0, now
    return n, start


def _fail(ip):
    n, start = _throttled(ip)
    _FAILS[ip] = (n + 1, start)


def bootstrap_admin():
    """Garante a conta do dono do produto na tabela de usuarios."""
    if core.user_by_email(ADMIN_LOGIN):
        return
    c = _admin_credential()
    core.create_user(None, ADMIN_LOGIN, c["salt"], c["hash"], "admin")


bootstrap_admin()


PANEL_CSS = """
:root{--bg:#0b0f14;--panel:#121820;--line:#1e2834;--ink:#e6f0fa;--dim:#8aa0b4;--cy:#2fd6ff;--lime:#7dff8a;--amber:#ffc24a;--red:#ff3b54}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 ui-monospace,Menlo,monospace}
a{color:var(--cy)}
header{display:flex;align-items:center;gap:12px;padding:12px 20px;border-bottom:1px solid var(--line);flex-wrap:wrap}
h1{font-size:15px;margin:0;letter-spacing:.5px}
h2{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);margin:26px 0 10px}
.muted{color:var(--dim);font-size:12px}
main{padding:20px;max-width:1080px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.card{display:block;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;color:inherit;text-decoration:none}
.card:hover{border-color:#2f4a63}
.row{display:flex;justify-content:space-between;align-items:center;gap:8px}
.small{font-size:11px;margin-top:6px;color:var(--dim)}
.pill{font-size:10px;border:1px solid var(--line);border-radius:20px;padding:2px 8px;color:var(--cy)}
.pill.off{color:var(--red);border-color:#40222c}
.bar{height:6px;background:#0a121a;border-radius:4px;margin:10px 0 2px;overflow:hidden}
.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--cy),var(--lime))}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);font-size:12px}
th{color:var(--dim);font-weight:400;font-size:11px;text-transform:uppercase;letter-spacing:.06em}
tr:last-child td{border-bottom:0}
form.inline{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin:0}
label{display:block;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);margin-bottom:4px}
input,select{background:#0a121a;border:1px solid var(--line);border-radius:6px;color:var(--ink);font:inherit;font-size:12px;padding:7px 9px}
input:focus,select:focus{outline:none;border-color:var(--cy)}
button{background:var(--cy);color:#04121a;border:0;border-radius:6px;font:inherit;font-size:12px;font-weight:700;padding:8px 12px;cursor:pointer}
button.ghost{background:none;border:1px solid var(--line);color:var(--dim);font-weight:400}
button.ghost:hover{color:var(--ink);border-color:#2f4a63}
.box{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
.ok{color:var(--lime)} .warn{color:var(--amber)} .err{color:var(--red)}
footer{padding:10px 20px;color:var(--dim);font-size:11px;border-top:1px solid var(--line);margin-top:24px}
"""


def page(title, user, body, extra_head=""):
    quem = ""
    if user:
        quem = (f'<span class="muted">{user["email"]}'
                + (' · dono' if user["role"] == "admin" else '')
                + '</span><form method="post" action="/logout" style="margin:0">'
                  '<button class="ghost" type="submit">sair</button></form>')
    return (f'<!doctype html><html lang="pt-BR"><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{title} — Patinho</title><style>{PANEL_CSS}</style>{extra_head}'
            f'<header><h1>PATINHO</h1><span class="muted">recepção automática no WhatsApp</span>'
            f'<span style="flex:1"></span>{quem}</header>'
            f'<main>{body}</main>'
            f'<footer>Patinho · painel</footer></html>')


LOGIN_HTML = """<!doctype html><html lang="pt-BR"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Entrar — Patinho</title>
<style>
:root{--bg:#0b0f14;--panel:#121820;--line:#1e2834;--ink:#e6f0fa;--dim:#8aa0b4;--cy:#2fd6ff}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);
font:14px/1.5 ui-monospace,Menlo,monospace}
form{width:min(340px,92vw);background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:22px}
h1{font-size:16px;margin:0 0 2px;letter-spacing:.5px}
p.sub{margin:0 0 18px;color:var(--dim);font-size:12px}
label{display:block;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);margin:12px 0 4px}
input{width:100%;background:#0a121a;border:1px solid var(--line);border-radius:6px;color:var(--ink);font:inherit;padding:9px 10px}
input:focus{outline:none;border-color:var(--cy)}
button{margin-top:18px;width:100%;background:var(--cy);color:#04121a;border:0;border-radius:6px;font:inherit;font-weight:700;padding:10px;cursor:pointer}
.erro{margin-top:14px;color:#ff3b54;font-size:12px}
.foot{margin-top:16px;color:var(--dim);font-size:11px}
</style>
<form method="post" action="/login">
  <h1>PATINHO</h1>
  <p class="sub">painel de controle · acesso restrito</p>
  <label for="email">e-mail ou usuário</label>
  <input id="email" name="email" autocomplete="username" autocapitalize="none" spellcheck="false" value="__EMAIL__" autofocus required>
  <label for="senha">senha</label>
  <input id="senha" name="senha" type="password" autocomplete="current-password" required>
  <input type="hidden" name="next" value="__NEXT__">
  <button type="submit">Entrar</button>
  __ERRO__
  __TROCAR__
  <div class="foot">Sessão de __HORAS__h. Sem acesso? fale com quem administra sua conta.</div>
</form>
</html>"""


def login_page(next_url="/", erro="", email=""):
    erro_html = f'<div class="erro">{erro}</div>' if erro else ""
    if next_url.startswith("/app"):
        trocar = '<div class="foot" style="margin-top:10px"><a href="/login" style="color:#8aa0b4">sou o dono do produto</a></div>'
    else:
        trocar = '<div class="foot" style="margin-top:10px"><a href="/entrar" style="color:#8aa0b4">sou cliente de uma loja</a></div>'
    return (LOGIN_HTML.replace("__TROCAR__", trocar).replace("__EMAIL__", email)
            .replace("__NEXT__", next_url.replace('"', "%22"))
            .replace("__HORAS__", str(SESSION_TTL // 3600))
            .replace("__ERRO__", erro_html))


def _safe_next(next_url):
    next_url = (next_url or "").strip()
    return next_url if next_url.startswith("/") and not next_url.startswith("//") else ""


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = ""):
    u = current_user(request)
    if u:
        return RedirectResponse(_safe_next(next) or ("/admin" if u["role"] == "admin" else "/app"),
                                status_code=303)
    return login_page(_safe_next(next))


@app.post("/login")
async def login_submit(request: Request):
    ip = _client_ip(request)
    n, _ = _throttled(ip)
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    email = (form.get("email") or [""])[0].strip()
    next_url = _safe_next((form.get("next") or [""])[0])
    if n >= 8:
        return HTMLResponse(login_page(next_url, "muitas tentativas, espere 1 minuto", email), status_code=429)
    u = check_login(email, (form.get("senha") or [""])[0])
    if not u:
        _fail(ip)
        return HTMLResponse(login_page(next_url, "e-mail ou senha inválidos", email), status_code=401)
    _FAILS.pop(ip, None)
    dest = next_url or ("/admin" if u["role"] == "admin" else "/app")
    resp = RedirectResponse(dest, status_code=303)
    resp.set_cookie(SESSION_COOKIE, _session_token(u["id"]), max_age=SESSION_TTL,
                    httponly=True, samesite="lax",
                    secure=(request.headers.get("x-forwarded-proto", "") == "https"))
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


def require_login(request):
    if current_user(request) is None:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    return None


def _need_admin(request):
    u = current_user(request)
    if not u:
        return None, RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    if u["role"] != "admin":
        return None, RedirectResponse("/app", status_code=303)
    return u, None


def _need_client(request):
    u = current_user(request)
    if not u:
        return None, RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    if u["role"] == "admin" or not u["tenant_id"]:
        return None, RedirectResponse("/admin", status_code=303)
    return u, None


# ---------------------------------------------------------------- painel do dono
NICHE_SERVICES = {
    "barbearia": [("Corte", 30, 4000), ("Barba", 20, 2500), ("Corte + barba", 50, 6000)],
    "salao": [("Escova", 45, 7000), ("Manicure", 40, 4500), ("Coloração", 120, 18000)],
    "consultorio": [("Consulta", 30, 0), ("Retorno", 20, 0), ("Avaliação", 45, 0)],
    "outro": [("Atendimento", 30, 0)],
}


def _tenant_stats(tid):
    with core.connect() as con:
        appts = con.execute(
            "select count(*) c from appointments where tenant_id=? and status='scheduled' and start_ts>?",
            (tid, int(time.time()))).fetchone()["c"]
    return {"used": core.usage(tid), "appts": appts, "users": len(core.tenant_users(tid)),
            "full": core.usage_full(tid)}


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    host = req_host(request)
    if host == DEMO_HOST:
        return RedirectResponse(f"/sim/{DEMO_SLUG}", status_code=302)
    u = current_user(request)
    if not u:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/admin" if u["role"] == "admin" else "/app", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    u, redir = _need_admin(request)
    if redir:
        return redir
    with core.connect() as con:
        rows = con.execute("select * from tenants order by id").fetchall()
    cards = []
    for t in rows:
        st = _tenant_stats(t["id"])
        pct = min(100, round(st["used"] * 100 / max(1, t["msg_limit"])))
        cards.append(
            f'<a class="card" href="/admin/t/{t["slug"]}">'
            f'<div class="row"><b>{t["name"]}</b>'
            f'<span class="pill{"" if t["active"] else " off"}">{t["plan"]} · {t["status"] or "trial"}'
            f'{"" if t["active"] else " · inativo"}</span></div>'
            f'<div class="muted">{t["niche"]} · {t["slug"]}</div>'
            f'<div class="bar"><i style="width:{pct}%"></i></div>'
            f'<div class="row small"><span>{st["used"]}/{t["msg_limit"]} mensagens no mês</span>'
            f'<span>{st["appts"]} agendados</span></div>'
            f'<div class="row small"><span>{st["users"]} acesso(s)</span>'
            f'<span>{st["full"]["tokens_in"] + st["full"]["tokens_out"]} tokens</span></div></a>')
    novo = (
        '<div class="box"><form class="inline" method="post" action="/admin/novo">'
        '<div><label>nome da loja</label><input name="nome" placeholder="Barbearia do Zé" required style="min-width:220px"></div>'
        '<div><label>tipo</label><select name="nicho">'
        '<option value="barbearia">barbearia</option><option value="salao">salão de beleza</option>'
        '<option value="consultorio">consultório / clínica</option><option value="outro">outro</option>'
        '</select></div>'
        '<div><label>plano</label><select name="plano"><option value="trial">trial</option>'
        '<option value="pro">pro</option></select></div>'
        '<div><label>mensagens/mês</label><input name="limite" value="300" size="6"></div>'
        '<div><label>e-mail do cliente</label><input name="email" type="email" placeholder="dono@loja.com" required style="min-width:220px"></div>'
        '<button type="submit">Criar conta</button></form>'
        '<div class="small">A senha do cliente é gerada e mostrada uma vez, logo depois de criar.</div></div>')
    body = ('<h2>Clientes</h2><div class="grid">' + "".join(cards) + '</div>'
            '<h2>Novo cliente</h2>' + novo)
    return page("Admin", u, body)


@app.post("/admin/novo")
async def admin_create(request: Request):
    u, redir = _need_admin(request)
    if redir:
        return redir
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    g = lambda k, d="": (form.get(k) or [d])[0].strip()
    nome, nicho, plano = g("nome"), g("nicho", "outro"), g("plano", "trial")
    email = g("email").lower()
    try:
        limite = int(g("limite", "300") or 300)
    except ValueError:
        limite = 300
    if not nome or "@" not in email:
        return HTMLResponse(page("Admin", u, '<p class="err">nome e e-mail válidos são obrigatórios.</p>'
                                          '<p><a href="/admin">voltar</a></p>'), status_code=400)
    if core.user_by_email(email):
        return HTMLResponse(page("Admin", u, f'<p class="err">já existe conta com {email}.</p>'
                                          '<p><a href="/admin">voltar</a></p>'), status_code=400)
    tid = core.create_tenant(nome, nicho, plano, limite, 25, NICHE_SERVICES.get(nicho, NICHE_SERVICES["outro"]))
    pw = _new_password()
    salt = _salt()
    core.create_user(tid, email, salt, _hash(pw, salt), "client")
    t = core.tenant_by_id(tid)
    body = (f'<h2>Conta criada</h2><div class="box">'
            f'<p><b>{nome}</b> · {t["slug"]} · plano {plano} · {limite} mensagens/mês</p>'
            f'<p>login: <b>{email}</b><br>senha: <b>{pw}</b></p>'
            f'<p class="small warn">Copie a senha agora — ela não é mostrada de novo. Dá para gerar outra na página da loja.</p>'
            f'<p><a href="/admin/t/{t["slug"]}">abrir a loja</a> · <a href="/admin">voltar ao painel</a></p></div>')
    return page("Conta criada", u, body)


@app.get("/admin/t/{slug}", response_class=HTMLResponse)
def admin_tenant(slug: str, request: Request):
    u, redir = _need_admin(request)
    if redir:
        return redir
    t = core.tenant(slug)
    if not t:
        raise HTTPException(404, "loja não encontrada")
    st = _tenant_stats(t["id"])
    with core.connect() as con:
        appts = con.execute("select * from appointments where tenant_id=? and status='scheduled' "
                            "and start_ts>? order by start_ts limit 20", (t["id"], int(time.time()) - 86400)).fetchall()
        svcs = con.execute("select * from services where tenant_id=? order by id", (t["id"],)).fetchall()
        hrs = con.execute("select * from hours where tenant_id=? order by weekday", (t["id"],)).fetchall()
    users = core.tenant_users(t["id"])
    agenda = "".join(
        f'<tr><td>{time.strftime("%d/%m %H:%M", time.localtime(a["start_ts"]))}</td>'
        f'<td>{a["service"]}</td><td>{a["client_name"]}</td></tr>' for a in appts) or \
        '<tr><td colspan="3" class="muted">nenhum agendamento futuro</td></tr>'
    servicos = ", ".join(f'{s["name"]} ({s["minutes"]} min · R$ {s["price_cents"] / 100:.2f})' for s in svcs)
    dias = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
    horarios = ", ".join(f'{dias[h["weekday"]]} {h["open_min"] // 60:02d}:{h["open_min"] % 60:02d}-'
                         f'{h["close_min"] // 60:02d}:{h["close_min"] % 60:02d}' for h in hrs) or "sem horário"
    convs = core.invites_of(t["id"])
    convites = "".join(
        f'<tr><td>{c["email"]}</td><td>{time.strftime("%d/%m %H:%M", time.localtime(c["created_at"]))}</td>'
        f'<td>{"usado" if c["used_at"] else ("expirado" if c["expires_at"] < time.time() else "pendente")}</td>'
        f'<td><a href="{_invite_link(c["token"])}">link</a></td></tr>' for c in convs) or \
        '<tr><td colspan="4" class="muted">nenhum convite</td></tr>'
    invs = core.invoices_of(t["id"])
    faturas = "".join(
        f'<tr><td>#{i["id"]}</td><td>{PLANOS.get(i["plan"], {}).get("label", i["plan"])}</td>'
        f'<td>R$ {i["amount_cents"] / 100:.2f}</td>'
        f'<td>{time.strftime("%d/%m %H:%M", time.localtime(i["created_at"]))}</td>'
        f'<td>{"pago" if i["status"] == "paid" else i["status"]}</td>'
        f'<td>' + ('<form method="post" action="/admin/fatura/%d/pago" style="margin:0">'
                   '<button class="ghost" type="submit">confirmar pagamento</button></form>' % i["id"]
                   if i["status"] == "open" else "—") + '</td></tr>'
        for i in invs) or '<tr><td colspan="6" class="muted">nenhuma fatura</td></tr>'
    linhas_users = "".join(
        f'<tr><td>{x["email"]}</td><td>{x["role"]}</td><td>{"ativo" if x["active"] else "inativo"}</td>'
        f'<td><form method="post" action="/admin/t/{slug}/senha" style="margin:0">'
        f'<input type="hidden" name="uid" value="{x["id"]}">'
        f'<button class="ghost" type="submit">nova senha</button></form></td></tr>' for x in users)
    body = (
        f'<h2>{t["name"]}</h2>'
        f'<div class="box"><div class="row"><span class="muted">{t["slug"]} · {t["niche"]} · plano {t["plan"]}'
        f'{"" if t["active"] else " · INATIVO"}</span>'
        f'<span><a href="/admin">← clientes</a></span></div>'
        f'<div class="small">{st["used"]}/{t["msg_limit"]} mensagens no mês · '
        f'{st["full"]["tokens_in"] + st["full"]["tokens_out"]} tokens · '
        f'US$ {st["full"]["cost_micros"] / 1_000_000:.4f} · {st["appts"]} agendados</div></div>'
        f'<h2>Agenda</h2><table><tr><th>quando</th><th>serviço</th><th>cliente</th></tr>{agenda}</table>'
        f'<h2>Serviços</h2><div class="box">{servicos}</div>'
        f'<h2>Horário</h2><div class="box">{horarios}</div>'
        f'<h2>Faturas</h2><table><tr><th>#</th><th>plano</th><th>valor</th><th>criada</th>'
        f'<th>status</th><th></th></tr>{faturas}</table>'
        f'<h2>Convites</h2><table><tr><th>e-mail</th><th>criado</th><th>status</th><th>link</th></tr>{convites}</table>'
        f'<div class="box" style="margin-top:10px"><form class="inline" method="post" action="/admin/t/{slug}/convite">'
        f'<div><label>convidar por e-mail</label><input name="email" type="email" placeholder="dono@loja.com" required style="min-width:240px"></div>'
        f'<button type="submit">Gerar convite</button></form>'
        f'<div class="small">Sem SMTP configurado, o link aparece para você enviar no WhatsApp.</div></div>'
        f'<h2>Acessos</h2><table><tr><th>e-mail</th><th>papel</th><th>status</th><th></th></tr>{linhas_users}</table>'
        f'<div class="box" style="margin-top:10px"><form class="inline" method="post" action="/admin/t/{slug}/usuario">'
        f'<div><label>novo acesso (e-mail)</label><input name="email" type="email" placeholder="socio@loja.com" required style="min-width:240px"></div>'
        f'<button type="submit">Criar acesso</button></form></div>'
        f'<h2>Ajustes</h2><div class="box"><form class="inline" method="post" action="/admin/t/{slug}/loja">'
        f'<div><label>plano</label><select name="plano">'
        f'<option value="trial"{" selected" if t["plan"] == "trial" else ""}>trial</option>'
        f'<option value="pro"{" selected" if t["plan"] == "pro" else ""}>pro</option></select></div>'
        f'<div><label>mensagens/mês</label><input name="limite" value="{t["msg_limit"]}" size="6"></div>'
        f'<div><label>situação</label><select name="ativo">'
        f'<option value="1"{" selected" if t["active"] else ""}>ativa</option>'
        f'<option value="0"{"" if t["active"] else " selected"}>inativa</option></select></div>'
                f'<div><label>assinatura</label><select name="status">'
        + "".join(f'<option value="{v}"{" selected" if (t["status"] or "trial") == v else ""}>{v}</option>'
                  for v in ("trial", "active", "suspensa"))
        + f'<button type="submit">Salvar</button></form></div>')
    return page(t["name"], u, body)


@app.post("/admin/fatura/{iid}/pago")
async def admin_invoice_paid(iid: int, request: Request):
    u, redir = _need_admin(request)
    if redir:
        return redir
    inv = core.invoice_by_id(iid)
    if not inv:
        raise HTTPException(404, "fatura não encontrada")
    core.invoice_pay(iid, PLANOS.get(inv["plan"], {}).get("msg"))
    t = core.tenant_by_id(inv["tenant_id"])
    return RedirectResponse(f"/admin/t/{t['slug']}", status_code=303)


@app.post("/admin/t/{slug}/loja")
async def admin_update(slug: str, request: Request):
    u, redir = _need_admin(request)
    if redir:
        return redir
    t = core.tenant(slug)
    if not t:
        raise HTTPException(404, "loja não encontrada")
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    g = lambda k, d="": (form.get(k) or [d])[0].strip()
    try:
        limite = int(g("limite", str(t["msg_limit"])) or t["msg_limit"])
    except ValueError:
        limite = t["msg_limit"]
    core.set_tenant(t["id"], plan=g("plano", t["plan"]), msg_limit=limite, active=1 if g("ativo", "1") == "1" else 0)
    return RedirectResponse(f"/admin/t/{slug}", status_code=303)


@app.post("/admin/t/{slug}/usuario")
async def admin_new_user(slug: str, request: Request):
    u, redir = _need_admin(request)
    if redir:
        return redir
    t = core.tenant(slug)
    if not t:
        raise HTTPException(404, "loja não encontrada")
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    email = ((form.get("email") or [""])[0] or "").strip().lower()
    if "@" not in email:
        return HTMLResponse(page("Acesso", u, '<p class="err">e-mail inválido.</p>'
                                           f'<p><a href="/admin/t/{slug}">voltar</a></p>'), status_code=400)
    if core.user_by_email(email):
        return HTMLResponse(page("Acesso", u, f'<p class="err">{email} já tem conta.</p>'
                                           f'<p><a href="/admin/t/{slug}">voltar</a></p>'), status_code=400)
    pw = _new_password()
    salt = _salt()
    core.create_user(t["id"], email, salt, _hash(pw, salt), "client")
    body = (f'<h2>Acesso criado</h2><div class="box"><p>login: <b>{email}</b><br>senha: <b>{pw}</b></p>'
            f'<p class="small warn">Copie agora — não é mostrada de novo.</p>'
            f'<p><a href="/admin/t/{slug}">voltar para a loja</a></p></div>')
    return page("Acesso criado", u, body)


@app.post("/admin/t/{slug}/senha")
async def admin_reset(slug: str, request: Request):
    u, redir = _need_admin(request)
    if redir:
        return redir
    t = core.tenant(slug)
    if not t:
        raise HTTPException(404, "loja não encontrada")
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    uid = int((form.get("uid") or ["0"])[0])
    if uid not in [x["id"] for x in core.tenant_users(t["id"])]:
        raise HTTPException(404, "acesso não encontrado")
    pw = _new_password()
    salt = _salt()
    core.set_user_password(uid, salt, _hash(pw, salt))
    body = (f'<h2>Nova senha</h2><div class="box"><p>senha: <b>{pw}</b></p>'
            f'<p class="small warn">Copie agora — não é mostrada de novo.</p>'
            f'<p><a href="/admin/t/{slug}">voltar para a loja</a></p></div>')
    return page("Nova senha", u, body)


# ---------------------------------------------------------------- painel do cliente
@app.get("/app", response_class=HTMLResponse)
def client_home(request: Request):
    u, redir = _need_client(request)
    if redir:
        return redir
    with core.connect() as con:
        t = con.execute("select * from tenants where id=?", (u["tenant_id"],)).fetchone()
        if not t:
            raise HTTPException(404, "loja não encontrada")
        appts = con.execute("select * from appointments where tenant_id=? and status='scheduled' "
                            "and start_ts>? order by start_ts limit 20", (t["id"], int(time.time()) - 86400)).fetchall()
        svcs = con.execute("select * from services where tenant_id=? order by id", (t["id"],)).fetchall()
        hrs = con.execute("select * from hours where tenant_id=? order by weekday", (t["id"],)).fetchall()
    full = core.usage_full(t["id"])
    used = core.usage(t["id"])
    pct = min(100, round(used * 100 / max(1, t["msg_limit"])))
    agenda = "".join(
        f'<tr><td>{time.strftime("%d/%m %H:%M", time.localtime(a["start_ts"]))}</td>'
        f'<td>{a["service"]}</td><td>{a["client_name"]}</td></tr>' for a in appts) or \
        '<tr><td colspan="3" class="muted">nenhum agendamento futuro</td></tr>'
    dias = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
    horarios = ", ".join(f'{dias[h["weekday"]]} {h["open_min"] // 60:02d}:{h["open_min"] % 60:02d}-'
                         f'{h["close_min"] // 60:02d}:{h["close_min"] % 60:02d}' for h in hrs) or "sem horário"
    servicos = "".join(f'<div class="row small"><span>{s["name"]} · {s["minutes"]} min</span>'
                       f'<span>R$ {s["price_cents"] / 100:.2f}</span></div>' for s in svcs)
    st = tenant_state(t)
    aviso = "" if st["ok"] else (f'<div class="box" style="border-color:#40222c">'
                                 f'<b class="err">Atendimento suspenso</b>'
                                 f'<div class="small">{st["motivo"]} — regularize em '
                                 f'<a href="/app/assinatura">Assinatura</a>.</div></div>')
    trial = (f' · trial termina em {st["dias"]} dia(s)' if st["dias"] is not None else "")
    body = (
        f'<h2>{t["name"]}</h2>{aviso}'
        f'<div class="box"><div class="row"><span class="muted">{t["niche"]} · plano {t["plan"]}{trial}</span>'
        f'<span class="pill">{used}/{t["msg_limit"]} mensagens</span></div>'
        f'<div class="bar"><i style="width:{pct}%"></i></div>'
        f'<div class="small">{full["tokens_in"] + full["tokens_out"]} tokens no mês · '
        f'US$ {full["cost_micros"] / 1_000_000:.4f} · '
        f'<a href="/app/assinatura">assinatura e faturas</a></div></div>'
        f'<h2>Agenda</h2><table><tr><th>quando</th><th>serviço</th><th>cliente</th></tr>{agenda}</table>'
        f'<h2>Serviços</h2><div class="box">{servicos}</div>'
        f'<h2>Horário de funcionamento</h2><div class="box">{horarios}</div>'
        f'<h2>Testar o atendimento</h2><div class="box"><p class="small">O simulador mostra como o cliente '
        f'conversa com o bot. É a mesma demonstração pública, sem os seus dados.</p>'
        f'<p><a href="{DEMO_BASE}/sim/{DEMO_SLUG}" target="_blank" rel="noopener">abrir simulador →</a></p></div>'
        f'<h2>Senha</h2><div class="box"><form class="inline" method="post" action="/app/senha">'
        f'<div><label>senha atual</label><input name="atual" type="password" required></div>'
        f'<div><label>nova senha (8+)</label><input name="nova" type="password" required minlength="8"></div>'
        f'<button class="ghost" type="submit">Trocar senha</button></form></div>')
    return page(t["name"], u, body)


# ---------------------------------------------------------------- planos e assinatura
PLANOS = {
    "trial":     {"label": "Trial",     "msg": 150,  "preco": 0,     "dias": 14},
    "essencial": {"label": "Essencial", "msg": 1000, "preco": 9700,  "dias": 30},
    "pro":       {"label": "Pro",       "msg": 3000, "preco": 19700, "dias": 30},
}
DEMO_SLUGS = {"barbearia-demo", "salao-demo"}


def pix_payload(chave, nome, cidade, valor_centavos, txid):
    """BR Code (EMV) do Pix copia-e-cola — sem dependencia, sem PSP."""
    def emv(i, v):
        return f"{i}{len(v):02d}{v}"

    gui = emv("00", "br.gov.bcb.pix") + emv("01", chave)
    txid = re.sub(r"[^A-Za-z0-9]", "", txid or "")[:25] or "***"
    corpo = (emv("00", "01") + emv("26", gui) + emv("52", "0000") + emv("53", "986")
             + emv("54", f"{valor_centavos / 100:.2f}") + emv("58", "BR")
             + emv("59", (nome or "PATINHO")[:25]) + emv("60", (cidade or "CURITIBA")[:15])
             + emv("62", emv("05", txid)) + "6304")
    crc = 0xFFFF
    for b in corpo.encode():
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return corpo + f"{crc:04X}"


def tenant_state(t):
    """Assinatura da loja: trial vencido, suspensa ou ativa."""
    if not t["active"]:
        return {"ok": False, "motivo": "loja desativada pelo suporte", "dias": None}
    status = t["status"] or "trial"
    if status == "active":
        return {"ok": True, "motivo": "", "dias": None}
    if status in ("suspensa", "cancelada"):
        return {"ok": False, "motivo": f"assinatura {status}", "dias": None}
    fim = t["trial_ends"] or 0
    dias = max(0, (fim - int(time.time())) // 86400)
    if fim and fim < int(time.time()):
        return {"ok": False, "motivo": "trial vencido", "dias": 0}
    return {"ok": True, "motivo": "", "dias": dias}


@app.get("/entrar", response_class=HTMLResponse)
def client_login(request: Request):
    """Tela de login do cliente (lojista). O dono do produto usa /login."""
    u = current_user(request)
    if u:
        return RedirectResponse("/admin" if u["role"] == "admin" else "/app", status_code=303)
    return login_page("/app")


@app.get("/app/assinatura", response_class=HTMLResponse)
def client_billing(request: Request, erro: str = ""):
    u, redir = _need_client(request)
    if redir:
        return redir
    t = core.tenant_by_id(u["tenant_id"])
    st = tenant_state(t)
    usados = core.usage(t["id"])
    invs = core.invoices_of(t["id"])
    chave = os.environ.get("PATINHO_PIX_KEY", "").strip()
    nome = os.environ.get("PATINHO_PIX_NOME", "PATINHO").strip()
    cidade = os.environ.get("PATINHO_PIX_CIDADE", "CURITIBA").strip()

    estado = (f'<div class="box"><div class="row"><b>Plano {PLANOS.get(t["plan"], {}).get("label", t["plan"])}</b>'
              f'<span class="pill{"" if st["ok"] else " off"}">{t["status"] or "trial"}</span></div>'
              f'<div class="small">{usados}/{t["msg_limit"]} mensagens no mês'
              + (f' · trial termina em {st["dias"]} dia(s)' if st["dias"] is not None else "")
              + ("" if st["ok"] else f' · <span class="err">{st["motivo"]}</span>')
              + '</div></div>')

    cards = ""
    for slug, p in PLANOS.items():
        if slug == "trial":
            continue
        cards += (f'<div class="box"><div class="row"><b>{p["label"]}</b>'
                  f'<span class="pill">R$ {p["preco"] / 100:.2f}/mês</span></div>'
                  f'<div class="small">{p["msg"]} mensagens por mês · 30 dias</div>'
                  f'<form method="post" action="/app/assinatura" style="margin-top:10px">'
                  f'<input type="hidden" name="plano" value="{slug}">'
                  f'<button type="submit">Assinar {p["label"]}</button></form></div>')

    abertas = [i for i in invs if i["status"] == "open"]
    pix_html = ""
    if abertas:
        i = abertas[0]
        p = PLANOS.get(i["plan"], {})
        if chave:
            brcode = pix_payload(chave, nome, cidade, i["amount_cents"], f"PATINHO{i['id']}")
            pix_html = (f'<h2>Cobrança aberta</h2><div class="box">'
                        f'<p>{p.get("label", i["plan"])} · <b>R$ {i["amount_cents"] / 100:.2f}</b> · '
                        f'fatura #{i["id"]}</p>'
                        f'<p class="small">Pix copia e cola (pague no app do banco):</p>'
                        f'<textarea readonly rows="5" style="width:100%;background:#0a121a;color:#e6f0fa;'
                        f'border:1px solid #1e2834;border-radius:6px;font:11px/1.4 ui-monospace;padding:8px">'
                        f'{brcode}</textarea>'
                        f'<p class="small">A liberação é manual: o pagamento é conferido e a conta é ativada.</p>'
                        f'</div>')
        else:
            pix_html = (f'<h2>Cobrança aberta</h2><div class="box"><p>{p.get("label", i["plan"])} · '
                        f'<b>R$ {i["amount_cents"] / 100:.2f}</b> · fatura #{i["id"]}</p>'
                        f'<p class="small warn">Pix ainda não configurado no servidor '
                        f'(PATINHO_PIX_KEY). Fale com o suporte para pagar.</p></div>')

    hist = "".join(
        f'<tr><td>#{i["id"]}</td><td>{PLANOS.get(i["plan"], {}).get("label", i["plan"])}</td>'
        f'<td>R$ {i["amount_cents"] / 100:.2f}</td>'
        f'<td>{time.strftime("%d/%m %H:%M", time.localtime(i["created_at"]))}</td>'
        f'<td>{"pago" if i["status"] == "paid" else i["status"]}</td></tr>' for i in invs) or \
        '<tr><td colspan="5" class="muted">nenhuma fatura ainda</td></tr>'

    body = (f'<h2>Assinatura</h2>{estado}'
            + (f'<p class="err">{erro}</p>' if erro else "")
            + f'<h2>Planos</h2><div class="grid">{cards}</div>{pix_html}'
            f'<h2>Histórico</h2><table><tr><th>#</th><th>plano</th><th>valor</th><th>criada</th>'
            f'<th>status</th></tr>{hist}</table>')
    return page("Assinatura", u, body)


@app.post("/app/assinatura")
async def client_subscribe(request: Request):
    u, redir = _need_client(request)
    if redir:
        return redir
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    slug = (form.get("plano") or [""])[0].strip()
    p = PLANOS.get(slug)
    if not p or slug == "trial":
        return RedirectResponse("/app/assinatura?erro=plano+invalido", status_code=303)
    t = core.tenant_by_id(u["tenant_id"])
    aberta = [i for i in core.invoices_of(t["id"]) if i["status"] == "open"]
    if not aberta:
        core.invoice_create(t["id"], slug, p["preco"])
    return RedirectResponse("/app/assinatura", status_code=303)


@app.post("/app/senha")
async def client_password(request: Request):
    u, redir = _need_client(request)
    if redir:
        return redir
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    atual = (form.get("atual") or [""])[0]
    nova = (form.get("nova") or [""])[0]
    if len(nova) < 8:
        return RedirectResponse("/app?senha=curta", status_code=303)
    row = core.user_by_email(u["email"])
    if not row or not hmac.compare_digest(_hash(atual, row["salt"]), row["hash"]):
        return RedirectResponse("/app?senha=errada", status_code=303)
    salt = _salt()
    core.set_user_password(row["id"], salt, _hash(nova, salt))
    return RedirectResponse("/app?senha=ok", status_code=303)



# ---------------------------------------------------------------- convite por e-mail
SMTP_HOST = os.environ.get("PATINHO_SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("PATINHO_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("PATINHO_SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("PATINHO_SMTP_PASS", "").strip()
SMTP_FROM = os.environ.get("PATINHO_SMTP_FROM", SMTP_USER or "no-reply@stellacorp.com.br").strip()
PUBLIC_BASE = os.environ.get("PATINHO_PUBLIC_BASE", "https://patopro.stellacorp.com.br").rstrip("/")


def send_email(to, subject, text):
    """Envia se o SMTP estiver configurado. Devolve True/False, nunca levanta."""
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        return False
    try:
        import smtplib
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["From"] = SMTP_FROM
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(text)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as sm:
            sm.starttls()
            sm.login(SMTP_USER, SMTP_PASS)
            sm.send_message(msg)
        return True
    except Exception as e:
        print("smtp:", e, flush=True)
        return False


def _invite_link(token):
    return f"{PUBLIC_BASE}/convite/{token}"


@app.post("/admin/t/{slug}/convite")
async def admin_invite(slug: str, request: Request):
    u, redir = _need_admin(request)
    if redir:
        return redir
    t = core.tenant(slug)
    if not t:
        raise HTTPException(404, "loja não encontrada")
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    email = ((form.get("email") or [""])[0] or "").strip().lower()
    if "@" not in email:
        return HTMLResponse(page("Convite", u, '<p class="err">e-mail inválido.</p>'
                                            f'<p><a href="/admin/t/{slug}">voltar</a></p>'), status_code=400)
    token = secrets.token_urlsafe(24)
    core.invite_create(t["id"], email, token)
    link = _invite_link(token)
    enviado = send_email(
        email, f"Acesso ao painel {t['name']}",
        f"Olá!\n\nSua conta no painel {t['name']} está pronta.\n"
        f"Defina sua senha neste link (vale 7 dias):\n{link}\n\nSe não foi você, ignore esta mensagem.\n")
    status = ("E-mail enviado para <b>%s</b>." % email if enviado
              else "SMTP não configurado — copie o link e envie você mesmo:")
    body = (f'<h2>Convite criado</h2><div class="box"><p>{status}</p>'
            f'<p><textarea readonly rows="2" style="width:100%;background:#0a121a;color:#e6f0fa;'
            f'border:1px solid #1e2834;border-radius:6px;font:11px/1.4 ui-monospace;padding:8px">{link}</textarea></p>'
            f'<p class="small">O cliente define a senha e cai direto no painel dele. Vale 7 dias.</p>'
            f'<p><a href="/admin/t/{slug}">voltar para a loja</a></p></div>')
    return page("Convite", u, body)


@app.get("/convite/{token}", response_class=HTMLResponse)
def invite_page(token: str, erro: str = ""):
    inv = core.invite_by_token(token)
    if not inv or inv["used_at"]:
        return HTMLResponse(page("Convite", None,
                                 '<h2>Convite inválido</h2><p class="small">Este link já foi usado ou expirou. '
                                 'Peça um novo para a loja.</p>'), status_code=410)
    if inv["expires_at"] < time.time():
        return HTMLResponse(page("Convite", None,
                                 '<h2>Convite expirado</h2><p class="small">Peça um novo link para a loja.</p>'),
                            status_code=410)
    t = core.tenant_by_id(inv["tenant_id"])
    erro_html = f'<p class="err">{erro}</p>' if erro else ""
    body = (f'<h2>Definir senha</h2><div class="box"><p class="muted">{t["name"] if t else ""} · {inv["email"]}</p>'
            f'{erro_html}<form method="post" action="/convite/{token}" class="inline">'
            f'<div><label>nova senha (8+)</label><input name="senha" type="password" required minlength="8" autofocus></div>'
            f'<button type="submit">Salvar senha e entrar</button></form></div>')
    return page("Convite", None, body)


@app.post("/convite/{token}")
async def invite_accept(token: str, request: Request):
    inv = core.invite_by_token(token)
    if not inv or inv["used_at"] or inv["expires_at"] < time.time():
        return RedirectResponse("/entrar", status_code=303)
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    senha = (form.get("senha") or [""])[0]
    if len(senha) < 8:
        return RedirectResponse(f"/convite/{token}?erro=senha+curta", status_code=303)
    row = core.user_by_email(inv["email"])
    salt = _salt()
    if row:
        core.set_user_password(row["id"], salt, _hash(senha, salt))
        core.set_user_active(row["id"], 1)
    else:
        core.create_user(inv["tenant_id"], inv["email"], salt, _hash(senha, salt), "client")
    core.invite_use(token)
    resp = RedirectResponse("/app", status_code=303)
    row = core.user_by_email(inv["email"])
    resp.set_cookie(SESSION_COOKIE, _session_token(row["id"]), max_age=SESSION_TTL,
                    httponly=True, samesite="lax",
                    secure=(request.headers.get("x-forwarded-proto", "") == "https"))
    return resp



@app.get("/sim/{slug}", response_class=HTMLResponse)
def simulator(slug: str, request: Request):
    if req_host(request) == APP_HOST:
        return RedirectResponse(f"{DEMO_BASE}/sim/{slug}", status_code=302)
    t = tenant_or_404(slug)
    st = limit_state(t)
    return f"""<!doctype html><html lang="pt-BR"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{t['name']} — demo</title>
<style>
:root{{--bg:#0b141a;--panel:#111b21;--me:#005c4b;--them:#202c33;--ink:#e9edef;--dim:#8696a0;--line:#222d34}}
*{{box-sizing:border-box}}body{{margin:0;height:100vh;display:flex;flex-direction:column;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
header{{display:flex;align-items:center;gap:10px;padding:10px 14px;background:var(--panel);border-bottom:1px solid var(--line)}}
.avatar{{width:34px;height:34px;border-radius:50%;background:#2a3942;display:grid;place-items:center;font-size:15px}}
.name{{font-weight:600}} .sub{{font-size:11px;color:var(--dim)}}
.spacer{{flex:1}} .quota{{font-size:11px;color:var(--dim)}}
#log{{flex:1;overflow:auto;padding:16px;display:flex;flex-direction:column;gap:8px}}
.msg{{max-width:76%;padding:8px 10px;border-radius:10px;white-space:pre-wrap}}
.me{{align-self:flex-end;background:var(--me)}} .them{{align-self:flex-start;background:var(--them)}}
.sys{{align-self:center;background:#182229;color:var(--dim);font-size:11px;border-radius:6px;padding:4px 8px}}
form{{display:flex;gap:8px;padding:10px;background:var(--panel);border-top:1px solid var(--line)}}
input{{flex:1;background:#2a3942;border:0;border-radius:20px;padding:10px 14px;color:var(--ink);font:inherit;outline:none}}
button{{background:#00a884;color:#04140f;border:0;border-radius:20px;padding:0 18px;font-weight:700;cursor:pointer}}
button:disabled{{opacity:.5;cursor:not-allowed}}
</style>
<header><div class="avatar">🦆</div><div><div class="name">{t['name']}</div>
<div class="sub" id="sub">online · demo de venda</div></div><div class="spacer"></div>
<div class="quota" id="quota">{st['used']}/{st['limit']} msgs do plano</div></header>
<div id="log"></div>
<form id="f"><input id="q" placeholder="Escreva como se fosse um cliente…" autocomplete="off" autofocus>
<button id="b">Enviar</button></form>
<script>
const SLUG = {slug!r};
const SKEY = 'patinho_session_' + SLUG;
let session = sessionStorage.getItem(SKEY);
const log = document.getElementById('log');
function say(text, cls) {{
  const d = document.createElement('div'); d.className = 'msg ' + cls; d.textContent = text;
  log.appendChild(d); log.scrollTop = log.scrollHeight;
}}
async function send(text) {{
  if (!text.trim()) return;
  say(text, 'me');
  const r = await fetch('/api/sim/' + SLUG + '/message', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{session, text}})
  }});
  const d = await r.json();
  session = d.session;
  if (session) sessionStorage.setItem(SKEY, session);
  if (d.blocked) {{ say(d.reply, 'sys'); if (d.engine !== 'rate') document.getElementById('b').disabled = true; return; }}
  say(d.reply, 'them');
  document.getElementById('quota').textContent = d.used + '/' + d.limit + ' msgs do plano';
  document.getElementById('sub').textContent = 'online · ' + (d.engine.startsWith('llm') ? 'IA' : 'modo demonstração');
}}
document.getElementById('f').addEventListener('submit', e => {{
  e.preventDefault();
  const q = document.getElementById('q'); const t = q.value; q.value = '';
  send(t);
}});
say('Demonstração: você é o cliente. Tente "queria cortar cabelo amanhã às 15h".', 'sys');
fetch('/api/sim/' + SLUG + '/history' + (session ? '?session=' + encodeURIComponent(session) : '')).then(r => r.json()).then(h => {{
  if (!h.messages || !h.messages.length) return;
  session = h.session;
  document.getElementById('log').innerHTML = '';
  h.messages.forEach(m => say(m.content, m.role === 'user' ? 'me' : 'them'));
  say('sessão anterior · continua de onde parou', 'sys');
}});
</script>
</html>"""


@app.post("/api/sim/{slug}/message")
def sim_message(slug: str, body: SendIn, request: Request):
    t = tenant_or_404(slug)
    if not demo_allowed(client_ip(request)):
        return JSONResponse(
            {"session": body.session, "reply": "Muitas mensagens deste acesso. "
             "Tente de novo em alguns minutos.", "blocked": True, "engine": "rate"},
            status_code=429, headers={"Retry-After": "300"})
    if t["slug"] not in DEMO_SLUGS:
        ts = tenant_state(t)
        if not ts["ok"]:
            return {"session": body.session, "reply": "Atendimento temporariamente suspenso. "
                    "A loja precisa regularizar a assinatura.", "blocked": True, "engine": "assinatura"}
    st = limit_state(t)
    if st["blocked"]:
        return {"session": body.session, "reply": "Limite de mensagens do plano atingido. "
                "Este é o corte do plano trial — no plano pago o atendimento continua.",
                "blocked": True, "used": st["used"], "limit": st["limit"], "engine": "quota"}
    cid = owned_conversation(t["id"], body.session) or core.conversation(t["id"])
    # per-session cap keeps a demo from burning a whole plan
    used_session = core.user_messages(cid)
    if used_session >= t["demo_limit"]:
        return {"session": cid, "reply": f"Esta demonstração tem limite de {t['demo_limit']} mensagens. "
                "Reinicie para testar de novo.", "blocked": True, "used": st["used"],
                "limit": st["limit"], "engine": "quota"}
    core.add_message(cid, "user", body.text)
    msgs = core.history(cid, 16)
    reply, engine, meta = agent.turn(t["id"], t, msgs, cid)
    core.add_message(cid, "assistant", reply)
    used = core.bump_usage(t["id"], meta["usage"]["in"], meta["usage"]["out"], meta["cost_micros"])
    return {"session": cid, "reply": reply, "engine": engine, "tools": meta["tools"],
            "used": used, "limit": t["msg_limit"], "tokens": meta["usage"]}


@app.get("/api/sim/{slug}/history")
def sim_history(slug: str, session: str | None = None):
    t = tenant_or_404(slug)
    cid = owned_conversation(t["id"], session)
    if not cid:
        # sem sessao do proprio cliente: nao devolve conversa de terceiro
        return {"session": None, "messages": []}
    with core.connect() as con:
        msgs = con.execute(
            "select role,content from messages where conversation_id=? order by id", (cid,)).fetchall()
    return {"session": cid, "messages": [dict(m) for m in msgs]}


@app.post("/api/sim/{slug}/reset")
def sim_reset(slug: str):
    tenant_or_404(slug)
    return {"session": None}


@app.get("/api/tenants")
def api_tenants(request: Request):
    if current_user(request) is None:
        raise HTTPException(401, "login necessario")
    with core.connect() as con:
        rows = con.execute("select slug,name,plan,msg_limit,demo_limit from tenants").fetchall()
    return [dict(r) | {"used": core.usage(r["id"]) if "id" in r.keys() else None} for r in rows]


@app.get("/privacidade", response_class=HTMLResponse)
def privacy():
    return """<!doctype html><html lang="pt-BR"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Privacidade — Patinho</title>
<style>body{margin:0;background:#0b0f14;color:#e6f0fa;font:15px/1.6 system-ui,sans-serif;padding:40px;max-width:760px}
h1{font-size:20px}h2{font-size:14px;letter-spacing:.08em;text-transform:uppercase;color:#8aa0b4;margin-top:26px}
code{background:#121820;padding:1px 5px;border-radius:4px}</style>
<h1>Privacidade e LGPD</h1>
<p>O Patinho guarda o mínimo para o atendimento funcionar: telefone, nome informado, serviço, horário e o texto das
mensagens trocadas no WhatsApp. Não guardamos senha, documento, cartão nem localização.</p>
<h2>Base legal</h2><p>Execução de contrato (art. 7º, V) para atender e agendar; legítimo interesse para segurança e
prevenção de fraude. O cliente pode pedir informação, correção, portabilidade ou exclusão a qualquer momento.</p>
<h2>Retenção</h2><p>Cada loja define o prazo (padrão <strong>180 dias</strong>). Depois disso, o texto das conversas é
apagado e o nome do agendamento vira <code>(removido)</code>; os contadores agregados (quantidade de mensagens e
tokens) ficam, sem conteúdo pessoal.</p>
<h2>Seus direitos, por aqui</h2>
<ul><li>Exportar: <code>GET /api/&lt;loja&gt;/lgpd/export?phone=&lt;telefone&gt;</code></li>
<li>Excluir: <code>POST /api/&lt;loja&gt;/lgpd/delete</code> com <code>{"phone": "..."}</code></li>
<li>Expurgo por prazo: <code>POST /api/&lt;loja&gt;/lgpd/purge</code> (roda também 1x/dia no start)</li></ul>
<h2>Suboperadores</h2><p>Provedor de modelo (OpenCode Zen) recebe o texto da conversa para gerar a resposta. A loja
escolhe o modelo; o provedor pode usar os dados conforme os termos dele. Nenhum dado é vendido.</p>
<h2>Contato</h2><p>O responsável pelo tratamento é o dono da loja cadastrada; o encarregado do produto responde pelo
e-mail de suporte informado no contrato.</p></html>"""


@app.get("/api/{slug}/lgpd/export")
def lgpd_export(slug: str, phone: str, request: Request):
    if current_user(request) is None:
        raise HTTPException(401, "login necessario")
    t = tenant_or_404(slug)
    return core.subject_data(t["id"], phone)


class LgpdDeleteIn(BaseModel):
    phone: str


@app.post("/api/{slug}/lgpd/delete")
def lgpd_delete(slug: str, body: LgpdDeleteIn, request: Request):
    if current_user(request) is None:
        raise HTTPException(401, "login necessario")
    t = tenant_or_404(slug)
    return core.delete_subject(t["id"], body.phone)


@app.post("/api/{slug}/lgpd/purge")
def lgpd_purge(slug: str, request: Request):
    if current_user(request) is None:
        raise HTTPException(401, "login necessario")
    t = tenant_or_404(slug)
    return {"conversations_removed": core.purge(t["id"], core.retention_days(t))}


@app.get("/api/{slug}/usage")
def api_usage(slug: str, request: Request):
    if current_user(request) is None:
        raise HTTPException(401, "login necessario")
    t = tenant_or_404(slug)
    u = core.usage_full(t["id"])
    u["plan_limit"] = t["msg_limit"]
    u["retention_days"] = core.retention_days(t)
    u["cost_usd"] = round(u["cost_micros"] / 1_000_000, 4)
    return u


@app.get("/api/{slug}/appointments")
def api_appointments(slug: str, request: Request):
    if current_user(request) is None:
        raise HTTPException(401, "login necessario")
    t = tenant_or_404(slug)
    return [{"id": a["id"], "servico": a["service"], "cliente": a["client_name"],
             "quando": time.strftime("%d/%m %H:%M", time.localtime(a["start_ts"])),
             "status": a["status"]} for a in core.appointments(t["id"], since=int(time.time()) - 86400)]


@app.get("/health")
def health():
    return {"ok": True, "tenants": len(core.connect().execute("select 1 from tenants").fetchall())}
