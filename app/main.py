"""Patinho — FastAPI app: dashboard, WhatsApp-style simulator, quotas, API."""
import os
import time

BUDGET_USD = float(os.environ.get("PATINHO_BUDGET_USD", "0"))

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app import agent, core

app = FastAPI(title="Patinho", docs_url="/api/docs")
core.init()
core.seed()


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


@app.get("/", response_class=HTMLResponse)
def dashboard():
    rows = []
    with core.connect() as con:
        tenants = con.execute("select * from tenants order by id").fetchall()
        for t in tenants:
            u = core.usage(t["id"])
            appts = con.execute(
                "select count(*) c from appointments where tenant_id=? and status='scheduled' and start_ts>?",
                (t["id"], int(time.time()))).fetchone()["c"]
            full = core.usage_full(t["id"])
            rows.append({
                "slug": t["slug"], "name": t["name"], "niche": t["niche"], "plan": t["plan"],
                "used": u, "limit": t["msg_limit"], "pct": min(100, round(u * 100 / max(1, t["msg_limit"]))),
                "appts": appts, "demo_limit": t["demo_limit"],
                "tok_in": full["tokens_in"], "tok_out": full["tokens_out"],
                "cost": full["cost_micros"] / 1_000_000,
            })
    cards = "".join(
        f"""<a class="card" href="/sim/{r['slug']}">
          <div class="row"><b>{r['name']}</b><span class="pill">{r['plan']}</span></div>
          <div class="muted">{r['niche']}</div>
          <div class="bar"><i style="width:{r['pct']}%"></i></div>
          <div class="row small"><span>{r['used']}/{r['limit']} mensagens no mês</span>
          <span>{r['appts']} agendados</span></div>
          <div class="row small"><span>{r['tok_in']+r['tok_out']} tokens</span>
          <span>US$ {r['cost']:.4f} no mês</span></div>
        </a>""" for r in rows)
    return f"""<!doctype html><html lang="pt-BR"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Patinho — painel</title>
<style>
:root{{--bg:#0b0f14;--panel:#121820;--line:#1e2834;--ink:#e6f0fa;--dim:#8aa0b4;--cy:#2fd6ff;--lime:#7dff8a;--amber:#ffc24a}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 ui-monospace,Menlo,monospace}}
header{{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--line)}}
h1{{font-size:16px;margin:0;letter-spacing:.5px}} .muted{{color:var(--dim);font-size:12px}}
main{{padding:20px;display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}}
.card{{display:block;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;color:inherit;text-decoration:none}}
.card:hover{{border-color:#2f4a63}} .row{{display:flex;justify-content:space-between;align-items:center;gap:8px}}
.small{{font-size:11px;margin-top:6px;color:var(--dim)}} .pill{{font-size:10px;border:1px solid var(--line);border-radius:20px;padding:2px 8px;color:var(--cy)}}
.bar{{height:6px;background:#0a121a;border-radius:4px;margin:10px 0 2px;overflow:hidden}} .bar i{{display:block;height:100%;background:linear-gradient(90deg,var(--cy),var(--lime))}}
footer{{padding:10px 20px;color:var(--dim);font-size:11px;border-top:1px solid var(--line)}}
</style>
<header><h1>PATINHO</h1><span class="muted">recepção automática no WhatsApp · painel de controle</span>
<span style="flex:1"></span><span class="muted">{time.strftime('%d/%m %H:%M')}</span></header>
<main>{cards}</main>
<footer>Clique num cliente para abrir o simulador de venda. Limites do plano são aplicados no servidor.</footer>
</html>"""


@app.get("/sim/{slug}", response_class=HTMLResponse)
def simulator(slug: str):
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
let session = null;
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
  if (d.blocked) {{ say(d.reply, 'sys'); document.getElementById('b').disabled = true; return; }}
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
fetch('/api/sim/' + SLUG + '/history').then(r => r.json()).then(h => {{
  if (!h.messages || !h.messages.length) return;
  session = h.session;
  document.getElementById('log').innerHTML = '';
  h.messages.forEach(m => say(m.content, m.role === 'user' ? 'me' : 'them'));
  say('sessão anterior · continua de onde parou', 'sys');
}});
</script>
</html>"""


@app.post("/api/sim/{slug}/message")
def sim_message(slug: str, body: SendIn):
    t = tenant_or_404(slug)
    st = limit_state(t)
    if st["blocked"]:
        return {"session": body.session, "reply": "Limite de mensagens do plano atingido. "
                "Este é o corte do plano trial — no plano pago o atendimento continua.",
                "blocked": True, "used": st["used"], "limit": st["limit"], "engine": "quota"}
    cid = body.session or core.conversation(t["id"])
    # per-session cap keeps a demo from burning a whole plan
    used_session = core.user_messages(cid)
    if used_session >= t["demo_limit"]:
        return {"session": cid, "reply": f"Esta demonstração tem limite de {t['demo_limit']} mensagens. "
                "Reinicie para testar de novo.", "blocked": True, "used": st["used"],
                "limit": st["limit"], "engine": "quota"}
    core.add_message(cid, "user", body.text)
    msgs = core.history(cid, 16)
    reply, engine, meta = agent.turn(t["id"], t, msgs, session=cid)
    core.add_message(cid, "assistant", reply)
    used = core.bump_usage(t["id"], meta["usage"]["in"], meta["usage"]["out"], meta["cost_micros"])
    return {"session": cid, "reply": reply, "engine": engine, "tools": meta["tools"],
            "used": used, "limit": t["msg_limit"], "tokens": meta["usage"]}


@app.get("/api/sim/{slug}/history")
def sim_history(slug: str):
    t = tenant_or_404(slug)
    with core.connect() as con:
        row = con.execute(
            "select id from conversations where tenant_id=? and kind='demo' order by created_at desc limit 1",
            (t["id"],)).fetchone()
        if not row:
            return {"session": None, "messages": []}
        msgs = con.execute(
            "select role,content from messages where conversation_id=? order by id", (row["id"],)).fetchall()
    return {"session": row["id"], "messages": [dict(m) for m in msgs]}


@app.post("/api/sim/{slug}/reset")
def sim_reset(slug: str):
    tenant_or_404(slug)
    return {"session": None}


@app.get("/api/tenants")
def api_tenants():
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
def lgpd_export(slug: str, phone: str):
    t = tenant_or_404(slug)
    return core.subject_data(t["id"], phone)


class LgpdDeleteIn(BaseModel):
    phone: str


@app.post("/api/{slug}/lgpd/delete")
def lgpd_delete(slug: str, body: LgpdDeleteIn):
    t = tenant_or_404(slug)
    return core.delete_subject(t["id"], body.phone)


@app.post("/api/{slug}/lgpd/purge")
def lgpd_purge(slug: str):
    t = tenant_or_404(slug)
    return {"conversations_removed": core.purge(t["id"], core.retention_days(t))}


@app.get("/api/{slug}/usage")
def api_usage(slug: str):
    t = tenant_or_404(slug)
    u = core.usage_full(t["id"])
    u["plan_limit"] = t["msg_limit"]
    u["retention_days"] = core.retention_days(t)
    u["cost_usd"] = round(u["cost_micros"] / 1_000_000, 4)
    return u


@app.get("/api/{slug}/appointments")
def api_appointments(slug: str):
    t = tenant_or_404(slug)
    return [{"id": a["id"], "servico": a["service"], "cliente": a["client_name"],
             "quando": time.strftime("%d/%m %H:%M", time.localtime(a["start_ts"])),
             "status": a["status"]} for a in core.appointments(t["id"], since=int(time.time()) - 86400)]


@app.get("/health")
def health():
    return {"ok": True, "tenants": len(core.connect().execute("select 1 from tenants").fetchall())}
