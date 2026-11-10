# Patinho — recepção de WhatsApp com agenda, para vender

Produto próprio para barbearias, salões, estúdios e clínicas: o cliente conversa no WhatsApp, a IA
entende serviço/dia/hora/nome e marca na agenda. O que se vende é o **painel + o simulador**, não o bot.

> Isto **não** é um fork do `Gedankenn/pato`. Aquele repositório é PolyForm Shield com cláusula de
> não-concorrência: replicar o código e vender compete com o produto do licenciante. Este projeto é
> reescrito do zero, com as mesmas ideias de produto e nenhuma linha copiada. Ver `NOTES.md`.

## O que tem aqui

| peça | o que faz |
|---|---|
| `app/core.py` | SQLite: clientes (tenants), serviços, horários, agendamentos, conversas, uso por mês |
| `app/agent.py` | cérebro: LLM via API OpenAI-compatível com *tool calling* + motor de regras de reserva como rede de segurança |
| `app/main.py` | painel `/`, simulador `/sim/<slug>`, API `/api/...` |
| `tests/` | 5 testes: reserva, conflito, fluxo completo, contadores, isolamento entre clientes |

### Limites (o que segura a margem)

- **Plano** (`tenants.msg_limit`): teto mensal de mensagens por cliente. Estourou → o app responde
  com oferta de upgrade, não com a IA.
- **Demo** (`tenants.demo_limit`): teto por sessão de simulador, para vendedor não queimar o plano
  do cliente em demonstração.
- Contador por mês em `usage`, incrementado no servidor a cada mensagem respondida.

### Simulador de venda

`/sim/barbearia-demo` é uma tela de WhatsApp falsa: o prospect digita como cliente, vê a resposta e o
agendamento cai na agenda real do tenant. Fecha sem precisar de número de WhatsApp e sem custo por
mensagem além do LLM.

## Rodar

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/uvicorn app.main:app --port 8090
# painel: http://127.0.0.1:8090/   simulador: http://127.0.0.1:8090/sim/barbearia-demo
./.venv/bin/python -m pytest -q     # 5 passed
```

## LLM

Qualquer endpoint OpenAI-compatível. Variáveis:

```
PATINHO_LLM_BASE=https://opencode.ai/zen/v1
PATINHO_LLM_KEY=<chave OpenCode Zen>
PATINHO_LLM_MODEL=qwen3.8-flash
```

Sem chave (ou com a chave sem saldo), o **motor de regras** assume: ele entende "cortar cabelo amanhã
às 15h", pede o nome, confere a agenda e reserva. É o modo que roda no demo de venda, custo zero.

### Modelo recomendado para este trabalho

O trabalho é curto e específico: responder WhatsApp em PT-BR, tom humano, e chamar 3 ferramentas.
Não precisa de raciocínio longo; precisa de **latência baixa, custo por mensagem baixo e tool calling
confiável**. Ordem sugerida (trocar com uma variável, sem tocar em código):

1. `qwen3.8-flash` — padrão: US$ 0,15/1M in e 0,47/1M out, rápido, PT-BR bom, tool calling ok.
2. `gemini-3.8-flash` — alternativa com JSON mais estável, custo parecido.
3. `claude-haiku-4-5` — quando o cliente paga por “atendimento premium” e o tom pesa mais que o custo.

`deepseek-v4.1-flash` e `qwen3.8-flash` são alternativas de custo; a decisão final deve sair de uma
bateria com conversas reais do nicho (o script está em `bench/`), não de benchmark genérico.

**Estado da chave agora**: Bearer passa na autenticação e para em `402 Insufficient balance` — conta
pré-paga sem saldo. `x-api-key` não funciona nesse endpoint (401). Modelo free não roda fora do cliente
OpenCode (429). Billing, custo por conversa e teto de gasto em `BILLING.md`; LGPD em `PRIVACIDADE.md`.

## O que ainda falta para vender

1. **WhatsApp real**: hoje só simulador. A ponte recomendada é a Cloud API oficial (webhook + token);
   `whatsapp-web.js` funciona, mas viola os termos e cai quando o celular desconecta.
2. **Login do painel**: hoje é aberto em `127.0.0.1`. Antes de expor, precisa de sessão por tenant
   (o `/sim` pode ficar público com token de demo).
3. **Cobrança**: plano → limite é manual no banco; falta Stripe/Mercado Pago e fatura.
4. **Lembretes**: job diário 17h no dia anterior (a peça existe em `core`, falta o agendador).
5. **Multi-profissional**: hoje a agenda é por estabelecimento; o `pato` original tinha staff.

## Licença

Proprietária — ver `LICENSE` (titular: Fabio Slika Stella). O repo de referência (`Gedankenn/pato`)
está em PolyForm Shield com noncompete; como o histórico é 100% do mesmo autor, relicenciar é decisão
do titular (ver `NOTES.md`).
