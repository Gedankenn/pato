# Notas de avaliação — `Gedankenn/pato` → Patinho

## 1. Por que não é um fork

`pato` está sob **PolyForm Shield 1.0.0**, e a licença tem cláusula de **não-concorrência**:

> "Any purpose is a permitted purpose, except for providing any product that competes with the software
> or any product the licensor or any of its affiliates provides using the software."

Vender um SaaS de recepção de WhatsApp feito a partir daquele código é exatamente o caso proibido.
Também não adianta "mudar o suficiente": new works ficam sob a mesma licença. Então este repositório é
**reescrita limpa**: mesmas ideias de produto, nenhuma linha copiada. O `pato` serve como referência de
escopo e de UX, não como base de código.

## 2. O que o `pato` faz bem (e vale copiar como ideia)

- Escopo certo: barbearia/salão/estúdio, agendamento por conversa, PT-BR.
- Multi-tenant desde o começo (cada loja com serviços, equipe, agenda e conexão próprios).
- Calendário semanal, relatórios de 30 dias, lembrete no dia anterior.
- Validação de horário comercial e bloqueio de duplo agendamento no mesmo profissional.
- 19 tipos de negócio com prompt adaptado — bom argumento de venda.

## 3. O que eu não repetiria

| ponto | por quê |
|---|---|
| Prompt que exige **JSON no texto** (`{"action": ...}`) e parsing manual | quebra em resposta criativa, sem retry tipado; o certo é *tool calling* nativo (é o que o Patinho usa) |
| `JWT_SECRET` com default `change-this-in-production-REPLACE-ME` | esquece a env e o token é forjável |
| `whatsapp-web.js` (WhatsApp Web não oficial) | viola os termos e cai quando o celular desconecta; para vender, Cloud API |
| SQLite num único processo, sem fila | ok para 1 loja, frágil com 50; precisa WAL + worker |
| Sem quota, sem demo, sem limite de uso | é justamente o que segura a margem e o que o vendedor precisa |
| Testes em shell (`test_*.sh`) sem CI | não roda em PR, não mede regressão |
| Prompt de 2 KB com 9 regras numeradas | funciona, mas envelhece mal; ferramentas + validação no servidor são mais estáveis |

## 4. O que o Patinho entrega

- **Painel** (`/`): clientes, plano, uso do mês contra o teto, agendamentos futuros.
- **Simulador de venda** (`/sim/<slug>`): WhatsApp falso, sem número e sem custo por mensagem além do LLM;
  limite por sessão (`demo_limit`) e por plano (`msg_limit`), aplicados no servidor.
- **Agente** com *tool calling* (`horarios_livres`, `agendar`, `listar_servicos`) e **motor de regras**
  como plano B — se a chave do LLM falhar ou estiver sem saldo, a demo continua funcionando.
- **Agenda real** com slots de 30 min, horário comercial por dia da semana, bloqueio de conflito.

## 5. LLM: escolha e estado

Trabalho: responder WhatsApp curto em PT-BR e chamar 3 ferramentas. Precisa de latência baixa, custo por
mensagem baixo e tool calling confiável — não de raciocínio longo.

Sugestão de ordem (troca por env, sem tocar em código):
1. `gpt-5.4-mini` — produção.
2. `gemini-3.5-flash-lite` — volume/baixo custo.
3. `claude-haiku-4-5` — quando o tom “humano” é o diferencial vendido.

**Bloqueio real hoje:** a chave autentica (Bearer) e para em `402 Insufficient balance` — conta
pré-paga sem crédito. `x-api-key` é recusado (401). Modelo free dá 429 fora do cliente. O produto roda
no motor de regras até houver saldo. Ver `BILLING.md`.

## 6. Próximos passos para vender

1. Login por tenant no painel (hoje aberto em loopback).
2. WhatsApp Cloud API: webhook + token por tenant, `kind='whatsapp'` já previsto na tabela `conversations`.
3. Cobrança (Stripe/Mercado Pago) ligada a `tenants.plan`.
4. Agendador do lembrete (17h do dia anterior).
5. Múltiplos profissionais por loja.
6. Bateria de conversas reais do nicho para escolher o modelo com dado, não com intuição.
