"""Compara modelos do gateway para o trabalho real: 3 turnos de WhatsApp + 1 agendamento.

Roda só com saldo na conta (sem saldo o gateway responde 402). Uso:
    PATINHO_LLM_KEY=... ./.venv/bin/python bench/bench.py gpt-5.4-mini gemini-3.5-flash-lite
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("PATINHO_LLM_BASE", "https://opencode.ai/zen/v1").rstrip("/")
KEY = os.environ.get("PATINHO_LLM_KEY", "")
MODELS = sys.argv[1:] or ["gpt-5.4-mini", "gemini-3.5-flash-lite", "claude-haiku-4-5"]

TOOLS = [{"type": "function", "function": {
    "name": "agendar",
    "description": "Cria agendamento",
    "parameters": {"type": "object",
                   "properties": {"servico": {"type": "string"},
                                  "inicio": {"type": "string"},
                                  "cliente": {"type": "string"}},
                   "required": ["servico", "inicio", "cliente"]}}}]

TURNS = [
    "oi, queria cortar o cabelo amanhã as 15h",
    "meu nome é Carlos",
    "beleza, confirmo",
]


def call(model, msgs):
    body = {"model": model, "messages": msgs, "tools": TOOLS, "tool_choice": "auto",
            "temperature": 0.4, "max_tokens": 300}
    req = urllib.request.Request(f"{BASE}/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=40) as r:
        d = json.load(r)
    return time.time() - t, d["choices"][0]["message"], d.get("usage", {})


def run(model):
    msgs = [{"role": "system", "content": "Você é recepcionista de barbearia no WhatsApp. "
                                          "Português do Brasil, curto e humano. Use a ferramenta para marcar."}]
    lat, tools, text = [], 0, ""
    for user in TURNS:
        msgs.append({"role": "user", "content": user})
        dt, msg, usage = call(model, msgs)
        lat.append(dt)
        msgs.append(msg)
        if msg.get("tool_calls"):
            tools += 1
            for c in msg["tool_calls"]:
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": "{\"ok\":true}"})
        text = msg.get("content") or text
    return {"model": model, "lat_avg": round(sum(lat) / len(lat), 2), "lat_max": round(max(lat), 2),
            "tool_calls": tools, "last": text[:90]}


if __name__ == "__main__":
    for m in MODELS:
        try:
            print(json.dumps(run(m), ensure_ascii=False))
        except urllib.error.HTTPError as e:
            print(json.dumps({"model": m, "error": e.code, "body": e.read()[:120].decode(errors="replace")}))
