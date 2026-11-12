"""profile.py — compila o bloco de texto do dono em configuração estruturada.

O cliente descreve a casa em UM campo livre (o que faz, quem trabalha, o que vende,
horário). Aqui isso vira JSON: serviços, horário, equipe, tom. LLM quando há chave;
senão um leitor determinístico para o essencial.
"""
import json
import os
import re

import httpx

BASE = os.environ.get("PATINHO_LLM_BASE", "").rstrip("/")
KEY = os.environ.get("PATINHO_LLM_KEY", "")
MODEL = os.environ.get("PATINHO_LLM_MODEL", "")
UA = os.environ.get("PATINHO_LLM_UA", "patinho/1.0 (+OpenBot)")
TIMEOUT = float(os.environ.get("PATINHO_LLM_TIMEOUT", "20"))

DIAS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]

EXTRACT_PROMPT = """Você lê a descrição de um negócio e devolve SÓ JSON, sem texto em volta.
Formato exato:
{"vertical":"<tipo: barbearia|salao|restaurante|clinica|escritorio|massagem|outro>",
 "tone":"<como a casa fala: formal|descontraido|acolhedor|direto>",
 "services":[{"name":"<nome curto>","minutes":<int>,"price_cents":<int>}],
 "hours":[{"weekday":<0=segunda..6=domingo>,"open_min":<int>,"close_min":<int>}],
 "staff":[{"name":"<nome>","role":"<função>"}],
 "notes":"<1 frase com o que é essencial saber>"}
Regras: não invente serviço que não está no texto; preço em centavos (0 se não citado);
minutes padrão 30; se o horário não for citado use 540-1140 de segunda a sábado;
weekday 0 = segunda. Responda só o JSON."""


def _llm(text, name, niche):
    if not (BASE and KEY):
        return None
    payload = {"model": MODEL, "temperature": 0.1, "max_tokens": 900,
               "messages": [{"role": "system", "content": EXTRACT_PROMPT},
                            {"role": "user", "content": f"Nome: {name}\nTipo informado: {niche}\n\n{text}"}]}
    headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
               "User-Agent": UA, "x-opencode-session": "patinho-profile"}
    try:
        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.post(f"{BASE}/chat/completions", json=payload, headers=headers)
            if r.status_code >= 400:
                return None
            raw = (r.json()["choices"][0]["message"].get("content") or "").strip()
        m = re.search(r"\{.*\}", raw, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


def _money(s):
    m = re.search(r"(\d{1,4})(?:[.,](\d{2}))?", s)
    if not m:
        return 0
    return int(m.group(1)) * 100 + int(m.group(2) or 0)


def _fallback(text):
    """Leitor determinístico: serviços com preço, horário e nomes em 'quem trabalha'."""
    services, hours, staff = [], [], []
    for line in text.splitlines():
        l = line.strip(" -•\t")
        if not l:
            continue
        low = l.lower()
        if re.search(r"r\$|\d+\s*(reais|conto)", low):
            nome = re.split(r"[-–—:rR]\s*R?\$?", l)[0].strip(" -:–—")[:40]
            if nome and len(nome) > 2:
                services.append({"name": nome[:1].upper() + nome[1:], "minutes": 30,
                                 "price_cents": _money(low)})
    m = re.search(r"(\d{1,2})(?:[:h](\d{2}))?\s*(?:h|hs|horas)?\s*(?:às|a|–|-|até)\s*(\d{1,2})(?:[:h](\d{2}))?", text, re.I)
    if m:
        o = int(m.group(1)) * 60 + int(m.group(2) or 0)
        c = int(m.group(3)) * 60 + int(m.group(4) or 0)
        if 0 <= o < c <= 24 * 60:
            days = range(0, 6)
            if "sáb" in text.lower() or "sab" in text.lower():
                days = range(0, 6)
            if "domingo" in text.lower():
                days = range(0, 7)
            hours = [{"weekday": d, "open_min": o, "close_min": c} for d in days]
    for line in text.splitlines():
        if re.search(r"quem trabalha|equipe|funcion|profissional", line, re.I):
            for nome in re.findall(r"\b([A-ZÁÉÍÓÚÂÊÔÃÕÇ][a-záéíóúâêôãõç]{2,15})\b", line):
                if nome.lower() not in ("quem", "equipe", "funciona", "profissional"):
                    staff.append({"name": nome, "role": ""})
    return {"vertical": "", "tone": "", "services": services[:12], "hours": hours,
            "staff": staff[:8], "notes": ""}


def compile_profile(text, name="", niche=""):
    """Devolve o dicionário normalizado (sempre com as chaves)."""
    data = _llm(text, name, niche) or _fallback(text)
    out = {"vertical": str(data.get("vertical") or niche or "outro")[:24],
           "tone": str(data.get("tone") or "")[:24],
           "notes": str(data.get("notes") or "")[:200],
           "services": [], "hours": [], "staff": []}
    for s in (data.get("services") or [])[:20]:
        if not isinstance(s, dict):
            continue
        n = str(s.get("name") or "").strip()[:40]
        if not n:
            continue
        try:
            mins = int(s.get("minutes") or 30)
        except Exception:
            mins = 30
        try:
            price = int(s.get("price_cents") or 0)
        except Exception:
            price = 0
        out["services"].append({"name": n, "minutes": max(5, min(mins, 480)),
                                "price_cents": max(0, min(price, 10_000_00))})
    for h in (data.get("hours") or [])[:14]:
        if not isinstance(h, dict):
            continue
        try:
            wd = int(h.get("weekday"))
            o = int(h.get("open_min"))
            c = int(h.get("close_min"))
        except Exception:
            continue
        if 0 <= wd <= 6 and 0 <= o < c <= 24 * 60:
            out["hours"].append({"weekday": wd, "open_min": o, "close_min": c})
    if not out["hours"]:
        out["hours"] = [{"weekday": d, "open_min": 540, "close_min": 1140} for d in range(0, 6)]
    for p in (data.get("staff") or [])[:12]:
        if isinstance(p, dict) and str(p.get("name") or "").strip():
            out["staff"].append({"name": str(p["name"]).strip()[:24],
                                 "role": str(p.get("role") or "").strip()[:24]})
    return out


def summary_text(data):
    """Resumo legível para mostrar ao cliente e para o prompt do agente."""
    parts = []
    if data.get("services"):
        parts.append("serviços: " + ", ".join(
            f"{s['name']} ({s['minutes']}min" + (f", R$ {s['price_cents']/100:.2f}" if s["price_cents"] else "") + ")"
            for s in data["services"]))
    if data.get("hours"):
        parts.append("horário: " + ", ".join(
            f"{DIAS[h['weekday']]} {h['open_min']//60:02d}:{h['open_min']%60:02d}-{h['close_min']//60:02d}:{h['close_min']%60:02d}"
            for h in data["hours"]))
    if data.get("staff"):
        parts.append("equipe: " + ", ".join(
            (p["name"] + (f" ({p['role']})" if p["role"] else "")) for p in data["staff"]))
    return "; ".join(parts)
