#!/usr/bin/env python3
"""Estimativa de custo de energia elétrica para rodar o PatoAgenda."""

# ── Configurações (ajuste conforme sua realidade) ──

# Tarifa de energia (R$/kWh) — Paraná/Copel 2026
KWH_PRICE = 0.85

# Servidor (ex: NUC, mini PC, VPS local)
SERVER_IDLE_W = 15      # consumo em idle (watts)
SERVER_LOAD_W = 25      # consumo sob carga (watts)

# GPU para LLM (se usar Ollama local com GPU)
GPU_IDLE_W = 10         # consumo em idle
GPU_INFERENCE_W = 80    # consumo durante inferência (ex: RTX 3060 ~ 170W max, metade em uso)
HAS_GPU = True          # True se usa GPU local, False se usa API externa

# Uso estimado
MESSAGES_PER_DAY = 50   # mensagens WhatsApp processadas por dia
AVG_INFERENCE_SEC = 3   # segundos por inferência da LLM

# Horas por mês
HOURS_MONTH = 24 * 30   # 720h se ligado 24/7

# ═══════════════════════════════════════════════
# Cálculos
# ═══════════════════════════════════════════════

def fmt_real(v):
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

print("=" * 55)
print("  ⚡ PatoAgenda — Estimativa de Custo Energético")
print("=" * 55)
print(f"  Tarifa: R$ {KWH_PRICE:.2f}/kWh")
print()

# 1. Servidor base (24/7)
server_avg_w = (SERVER_IDLE_W * 0.7 + SERVER_LOAD_W * 0.3)  # 70% idle, 30% load
server_kwh_month = (server_avg_w * HOURS_MONTH) / 1000
server_cost = server_kwh_month * KWH_PRICE

print(f"  🖥️  Servidor ({SERVER_IDLE_W}W idle / {SERVER_LOAD_W}W load):")
print(f"     Média: {server_avg_w:.0f}W × {HOURS_MONTH}h = {server_kwh_month:.1f} kWh/mês")
print(f"     Custo: {fmt_real(server_cost)}/mês")
print()

# 2. LLM (inferências)
inference_seconds_day = MESSAGES_PER_DAY * AVG_INFERENCE_SEC
inference_hours_month = (inference_seconds_day * 30) / 3600
if HAS_GPU:
    gpu_idle_kwh = (GPU_IDLE_W * (HOURS_MONTH - inference_hours_month)) / 1000
    gpu_inf_kwh = (GPU_INFERENCE_W * inference_hours_month) / 1000
    gpu_total_kwh = gpu_idle_kwh + gpu_inf_kwh
    gpu_cost = gpu_total_kwh * KWH_PRICE
    print(f"  🎮 GPU LLM local ({GPU_IDLE_W}W idle / {GPU_INFERENCE_W}W inferência):")
    print(f"     Mensagens/dia: {MESSAGES_PER_DAY} × {AVG_INFERENCE_SEC}s = {inference_seconds_day:.0f}s/dia")
    print(f"     Inferência: {inference_hours_month:.1f}h/mês")
    print(f"     Idle: {GPU_IDLE_W}W × {(HOURS_MONTH - inference_hours_month):.0f}h = {gpu_idle_kwh:.1f} kWh")
    print(f"     Inferência: {GPU_INFERENCE_W}W × {inference_hours_month:.1f}h = {gpu_inf_kwh:.1f} kWh")
    print(f"     Custo: {fmt_real(gpu_cost)}/mês")
    print()
else:
    gpu_cost = 0
    print(f"  ☁️  LLM via API externa (sem GPU local):")
    print(f"     Custo via API: ~R$ 0-15/mês (depende do provider)")
    print()

# 3. Total
total_cost = server_cost + gpu_cost
print(f"  💰 TOTAL ESTIMADO: {fmt_real(total_cost)}/mês")
print()

# ── Cenários ──
print("─" * 55)
print("  📊 Cenários de uso:")
print("─" * 55)

scenarios = [
    ("🥱 Baixo (20 msg/dia, server leve)",  20, 15, 20),
    ("🙂 Médio (50 msg/dia, server normal)", 50, 15, 25),
    ("🔥 Alto (200 msg/dia, server pesado)", 200, 30, 50),
    ("🚀 Extreme (1000 msg/dia, server full)", 1000, 50, 80),
]

for label, msgs, idle_w, load_w in scenarios:
    srv_avg = idle_w * 0.7 + load_w * 0.3
    srv_kwh = (srv_avg * HOURS_MONTH) / 1000
    inf_h = (msgs * AVG_INFERENCE_SEC * 30) / 3600
    gpu_kwh = ((GPU_IDLE_W * (HOURS_MONTH - inf_h)) + (GPU_INFERENCE_W * inf_h)) / 1000 if HAS_GPU else 0
    total = (srv_kwh + gpu_kwh) * KWH_PRICE
    print(f"  {label}:")
    print(f"     {fmt_real(total)}/mês — {fmt_real(total/msgs)}/msg")
print()

# ── Quanto cobrar? ──
print("─" * 55)
print("  💸 Sugestão de precificação mínima:")
print("─" * 55)
overhead = 2.0   # fator de overhead (hospedagem, domínio, manutenção)
min_month = total_cost * overhead
customers = [1, 3, 5, 10, 20]
for n in customers:
    per_customer = min_month / n
    print(f"  {n:2d} clientes: {fmt_real(per_customer)}/mês cada (mínimo)")
print()
print(f"  💡 Margem sugerida: 3-5× o custo base")
print(f"     Ex: {fmt_real(min_month*3)} com 5 clientes = {fmt_real(min_month*3/5)}/mês cada")
print()
print(f"  ⚠️  Isso cobre SÓ energia. Adicione:")
print(f"     - Hospedagem/Domínio (~R$ 50/mês)")
print(f"     - Seu tempo de desenvolvimento e suporte")
print(f"     - Lucro")
