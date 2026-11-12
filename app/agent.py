"""Patinho agent: one brain, two engines.

- LLM engine: any OpenAI-compatible endpoint (OpenCode Zen by default), native tool calling.
- Rule engine: deterministic PT-BR receptionist used when the key is missing, out of balance
  or the network is down, so a sales demo never shows an error to a prospect.
"""
import json
import os
import re
import time
import uuid

import httpx

from app import core
from app import profile as profilelib

BASE = os.environ.get("PATINHO_LLM_BASE", "https://opencode.ai/zen/v1").rstrip("/")
KEY = os.environ.get("PATINHO_LLM_KEY", "")
MODEL = os.environ.get("PATINHO_LLM_MODEL", "qwen3.8-flash")
TIMEOUT = float(os.environ.get("PATINHO_LLM_TIMEOUT", "20"))
# Modelo local pequeno erra data: o motor de regras agenda (exato, instantâneo) e o LLM conversa.
HYBRID = os.environ.get("PATINHO_HYBRID", "1") not in ("0", "false", "no")
# USD por 1M de tokens (qwen3.8-flash). Override por env se o modelo mudar.
COST_IN = float(os.environ.get("PATINHO_COST_IN", "0.15"))
COST_OUT = float(os.environ.get("PATINHO_COST_OUT", "0.47"))
UA = os.environ.get("PATINHO_LLM_UA", "patinho/1.0 (+OpenBot)")
MAX_TOKENS = int(os.environ.get("PATINHO_MAX_TOKENS", "800"))

SYSTEM = """Você é o atendente virtual de {name} no WhatsApp. Fala português do Brasil:
mensagens curtas, tom humano, sem emoji exagerado, nunca diz que é IA.

O bloco "COMO A CASA FUNCIONA" (quando existir) é a descrição do próprio dono: é a verdade sobre o
negócio — o que faz, quem trabalha, o que vende e por quanto, horário. Responda a partir dele.
Não invente serviço, preço, pessoa ou horário que não esteja nesse bloco nem nas ferramentas.
Se o negócio não for de hora marcada (restaurante, escritório, loja), atenda a dúvida e siga a
descrição em vez de forçar um agendamento.

Objetivo: atender como a casa atende — responder dúvidas, dizer o que é vendido e por quanto, e
marcar horário quando fizer sentido.

Regras:
- Sempre confirme os dados antes de agendar, em uma frase ("Corte amanhã 15h no nome de Carlos, pode confirmar?").
- Nunca invente horário livre: use a ferramenta horarios_livres.
- Se o horário estiver ocupado, ofereça 2 ou 3 alternativas.
- Se a casa recebe reserva ou pedido com horário (mesa, banho, sessão, consulta), trate como
  agendamento: use a ferramenta agendar com o serviço descrito (ex. "Mesa para 4").
- Quando o cliente confirmar ("pode", "sim", "confirma"), chame agendar NA HORA com os dados que já
  foram combinados na conversa. Não peça os dados de novo nem diga que não achou o pedido.
- Se faltar informação, pergunte só o que falta.
- Serviços cadastrados: {services}.
- Hoje é {today}. Responder em no máximo 2 frases.
- Se o bloco "Agendamentos desta conversa" existir, esses agendamentos SÃO REAIS: nunca diga
  que não marcou, nem que "não deu certo". Se o cliente perguntar, confirme com esses dados.
- Um horário da própria conversa não é "ocupado": já é do cliente.
"""

TOOLS = [
    {"type": "function", "function": {
        "name": "listar_servicos",
        "description": "Lista os serviços do estabelecimento.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "horarios_livres",
        "description": "Horários livres para um dia.",
        "parameters": {"type": "object", "properties": {
            "data": {"type": "string", "description": "YYYY-MM-DD"}},
            "required": ["data"]}}},
    {"type": "function", "function": {
        "name": "meus_agendamentos",
        "description": "Agendamentos já marcados nesta conversa (para confirmar ao cliente).",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "agendar",
        "description": "Cria o agendamento depois da confirmação do cliente.",
        "parameters": {"type": "object", "properties": {
            "servico": {"type": "string"},
            "inicio": {"type": "string", "description": "YYYY-MM-DDTHH:MM"},
            "cliente": {"type": "string"}},
            "required": ["servico", "inicio", "cliente"]}}},
]


def _tools_for(tid):
    svcs = core.services(tid)
    return ", ".join(f"{s['name']} ({s['minutes']} min)" for s in svcs) or "a combinar"


def _fmt_ts(ts):
    return time.strftime("%d/%m às %H:%M", time.localtime(ts))


def _system(tid, t, cid=None):
    base = SYSTEM.format(name=t["name"], niche=t["niche"], services=_tools_for(tid),
                         today=time.strftime("%Y-%m-%d"))
    prof = core.profile(tid)
    if (prof.get("text") or "").strip():
        base += ("\nCOMO A CASA FUNCIONA (descrição do dono, use como verdade):\n"
                 + prof["text"].strip()[:4000] + "\n")
        resumo = profilelib.summary_text(prof.get("data") or {})
        if resumo:
            base += "Resumo estruturado: " + resumo + "\n"
    rows = core.conversation_appointments(tid, cid) if cid else []
    if rows:
        linhas = "; ".join(f"#{r['id']} {r['service']} {_fmt_ts(r['start_ts'])} "
                           f"({r['client_name']})" for r in rows)
        base += ("\nAgendamentos desta conversa (já gravados no sistema, status agendado): "
                 f"{linhas}.\n")
    return base


def _exec_tool(tid, name, args, cid=None):
    if name == "meus_agendamentos":
        return {"agendamentos": [{"id": r["id"], "servico": r["service"],
                                  "quando": _fmt_ts(r["start_ts"]), "cliente": r["client_name"]}
                                 for r in core.conversation_appointments(tid, cid)]}
    if name == "listar_servicos":
        return [{"servico": s["name"], "minutos": s["minutes"], "preco": s["price_cents"] / 100}
                for s in core.services(tid)]
    if name == "horarios_livres":
        try:
            day = time.mktime(time.strptime(args["data"], "%Y-%m-%d"))
        except Exception:
            return {"erro": "data inválida, use YYYY-MM-DD"}
        slots = core.free_slots(tid, day, ignore_conversation=cid)
        return {"data": args["data"], "livres": [time.strftime("%H:%M", time.localtime(s)) for s in slots[:24]]}
    if name == "agendar":
        svc = args.get("servico", "")
        try:
            raw = str(args.get("inicio", "")).strip().replace(" ", "T")
            start = int(time.mktime(time.strptime(raw, "%Y-%m-%dT%H:%M")))
        except Exception:
            return {"erro": "horário inválido, use YYYY-MM-DDTHH:MM"}
        match = next((s for s in core.services(tid) if s["name"].lower() == svc.lower()), None)
        minutes = match["minutes"] if match else 30
        aid = core.book(tid, match["name"] if match else svc, args.get("cliente", "Cliente"),
                        start, start + minutes * 60, conversation_id=cid)
        if not aid:
            return {"erro": "horário ocupado"}
        return {"agendamento": aid, "servico": match["name"] if match else svc,
                "cliente": args.get("cliente", "Cliente"),
                "quando": time.strftime("%d/%m %H:%M", time.localtime(start)),
                "ok": True}
    return {"erro": f"ferramenta desconhecida: {name}"}


def _usage_acc(usage, data):
    u = data.get("usage") or {}
    usage["in"] += int(u.get("prompt_tokens") or 0)
    usage["out"] += int(u.get("completion_tokens") or 0)
    usage["cost_usd"] = usage.get("cost_usd", 0.0) + float((data.get("opencode") or {}).get("list_cost") or 0)


def llm_turn(tid, t, msgs, cid=None):
    """Returns (reply, events, usage) or raises when the endpoint refuses."""
    # 400 estourava o JSON de ferramenta no meio; 800 cabe o turno inteiro.
    payload = {"model": MODEL, "messages": [{"role": "system", "content": _system(tid, t, cid)}] + msgs,
               "tools": TOOLS, "tool_choice": "auto", "temperature": 0.4, "max_tokens": MAX_TOKENS}
    # OpenCode Go exige sessao estavel por conversa e recusa UA de SDK/urllib.
    session = str(uuid.uuid5(uuid.NAMESPACE_URL, str(tid)))
    headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
               "User-Agent": UA, "x-opencode-session": session}
    events = []
    usage = {"in": 0, "out": 0, "cost_usd": 0.0}
    with httpx.Client(timeout=TIMEOUT) as c:
        for _ in range(5):
            r = c.post(f"{BASE}/chat/completions", json=payload, headers=headers)
            if r.status_code >= 400:
                raise RuntimeError(f"{r.status_code} {r.text[:120]}")
            data = r.json()
            _usage_acc(usage, data)
            msg = data["choices"][0]["message"]
            payload["messages"].append(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                text = (msg.get("content") or "").strip()
                if data["choices"][0].get("finish_reason") == "length":
                    # resposta cortada no teto de tokens: refaz uma vez, com teto maior e pedindo curto
                    payload["messages"].append({"role": "user", "content":
                                                "Sua resposta anterior foi cortada. Responda de novo, "
                                                "em no máximo duas frases curtas."})
                    payload["max_tokens"] = max(MAX_TOKENS, 800)
                    r2 = c.post(f"{BASE}/chat/completions", json=payload, headers=headers)
                    payload["max_tokens"] = MAX_TOKENS
                    if r2.status_code < 400:
                        d2 = r2.json()
                        _usage_acc(usage, d2)
                        ch2 = d2["choices"][0]
                        alt = (ch2["message"].get("content") or "").strip()
                        if alt and ch2.get("finish_reason") != "length":
                            return alt, events, usage
                return text, events, usage
            for call in calls:
                fn = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                out = _exec_tool(tid, fn, args, cid)
                events.append({"tool": fn, "args": args, "out": out})
                payload["messages"].append({"role": "tool", "tool_call_id": call["id"],
                                            "content": json.dumps(out, ensure_ascii=False)})
        # Se o modelo passou o limite só chamando ferramenta, força uma resposta em texto.
        payload.pop("tools", None)
        payload["messages"].append({"role": "user", "content": "Agora responda ao cliente em uma frase."})
        r = c.post(f"{BASE.rstrip('/')}/chat/completions", json=payload, headers=headers)
        if r.status_code < 400:
            data = r.json()
            _usage_acc(usage, data)
            return (data["choices"][0]["message"].get("content") or "").strip(), events, usage
    return ("", events, usage)


# ---------------------------------------------------------------- rule engine
WD = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]

# "cortar cabelo" -> "Corte": sem isso o motor de regras pergunta o serviço de novo.
HINTS = {
    "corte": ["corte", "cortar", "cabelo", "tesoura"],
    "barba": ["barba", "barbear", "aparar"],
    "escova": ["escova", "escovar", "secar"],
    "manicure": ["manicure", "unha", "unhas"],
    "coloração": ["coloracao", "coloração", "pintar", "tinta", "mechas"],
}


def _parse_when(text):
    """Very small PT-BR date/time reader: hoje, amanhã, dd/mm, 15h, 15:30."""
    now = time.localtime()
    base = None
    low = text.lower()
    if "amanhã" in low or "amanha" in low:
        base = time.mktime((now.tm_year, now.tm_mon, now.tm_mday + 1, 0, 0, 0, 0, 0, -1))
    elif "hoje" in low:
        base = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
    else:
        m = re.search(r"\b(\d{1,2})/(\d{1,2})\b", text)
        if m:
            base = time.mktime((now.tm_year, int(m.group(2)), int(m.group(1)), 0, 0, 0, 0, 0, -1))
    hour = None
    m = re.search(r"\b(\d{1,2})\s*(?:h|:)\s*(\d{2})?\b", low)
    if m:
        hour = (int(m.group(1)), int(m.group(2) or 0))
    return base, hour


def _fmt(ts):
    return time.strftime("%d/%m às %H:%M", time.localtime(ts))


def rule_turn(tid, t, msgs, cid=None):
    """Deterministic small-talk + booking. Good enough for a demo, honest about scope."""
    text = " ".join(m["content"] for m in msgs if m["role"] == "user")
    last = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
    svcs = [s["name"] for s in core.services(tid)]
    low = text.lower()
    # "cortar cabelo" must match "Corte": compare the first 4 letters of each service word.
    svc = next((s for s in svcs
                if s.lower() in low or any(w[:4] in low for w in s.lower().split() if len(w) >= 4)), None)
    day, hour = _parse_when(text)
    name = None
    m = re.search(r"(?:meu nome é|me chamo|sou o|sou a|nome[:\s]+)\s*([A-Za-zÀ-ÿ]{2,20})", low)
    if m:
        name = m.group(1).capitalize()

    if not svc:
        return (f"Oi! Aqui é a recepção da {t['name']} 😊 Qual serviço você quer? "
                f"Temos {', '.join(svcs)}.", [])
    if not day or not hour:
        slots = core.free_slots(tid, day or time.time())
        amostra = ", ".join(time.strftime("%H:%M", time.localtime(s)) for s in slots[:3]) or "sem vaga"
        return (f"Perfeito, {svc}. Para quando você quer? Tenho {amostra} — "
                f"me diz o dia e o horário.", [])
    start = int(time.mktime(time.strptime(time.strftime("%Y-%m-%d", time.localtime(day)), "%Y-%m-%d"))) \
        + hour[0] * 3600 + hour[1] * 60
    if not name:
        return (f"Fechado: {svc} {_fmt(start)}. Só me diz seu nome para eu reservar.", [])
    aid = core.book(tid, svc, name, start, start + 1800, conversation_id=cid)
    if not aid:
        slots = core.free_slots(tid, start)
        alt = ", ".join(time.strftime("%H:%M", time.localtime(s)) for s in slots[:3])
        return (f"Esse horário já está ocupado. Tenho {alt} — qual fica melhor?", [])
    return (f"Pronto, {name}! {svc} {_fmt(start)} reservado. "
            f"Se precisar remarcar, me chama aqui.", [{"tool": "agendar",
              "args": {"servico": svc, "inicio": time.strftime("%Y-%m-%dT%H:%M", time.localtime(start)),
                       "cliente": name}, "out": {"agendamento": aid}}])


def booking_intent(tid, text):
    low = text.lower()
    svcs = [s["name"] for s in core.services(tid)]
    has_svc = any(s.lower() in low for s in svcs) or any(
        w in low for s in svcs for w in HINTS.get(s.lower(), []))
    day, hour = _parse_when(text)
    return bool(has_svc and day and hour)


def turn(tid, t, msgs, cid=None):
    """LLM first; rule engine as the safety net. Returns (reply, engine, events)."""
    last_user = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
    if KEY and HYBRID and booking_intent(tid, last_user):
        reply, events = rule_turn(tid, t, msgs, cid)
        return reply, "rule (agendamento exato)", {"tools": events, "usage": {"in": 0, "out": 0},
                                                   "cost_micros": 0}
    if KEY:
        try:
            reply, events, usage = llm_turn(tid, t, msgs, cid)
            if reply:
                cost = round(usage.get("cost_usd", 0) * 1_000_000) or \
                       round(usage["in"] * COST_IN + usage["out"] * COST_OUT)
                return reply, f"llm:{MODEL}", {"tools": events, "usage": usage, "cost_micros": cost}
        except Exception as e:
            reply, events = rule_turn(tid, t, msgs, cid)
            return reply, f"rule (llm falhou: {str(e)[:60]})", {"tools": events, "usage": {"in": 0, "out": 0},
                                                                "error": str(e)[:120], "cost_micros": 0}
    reply, events = rule_turn(tid, t, msgs, cid)
    return reply, "rule", {"tools": events, "usage": {"in": 0, "out": 0}, "cost_micros": 0}
