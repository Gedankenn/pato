# Billing do OpenCode — como usar e como medir

Testado nesta máquina em 10/11, contra `https://opencode.ai/zen/v1` (dialeto OpenAI).

## 1. Como o gateway autentica (e o que não funciona)

| header | resultado |
|---|---|
| `Authorization: Bearer <key>` | passa na autenticação; chega no saldo (**402 Insufficient balance**) |
| `x-api-key: <key>` | **401 `Missing API key`** — não use |

O produto usa Bearer (`app/agent.py`). Base:

```
PATINHO_LLM_BASE=https://opencode.ai/zen/v1
PATINHO_LLM_KEY=<chave do Console>
PATINHO_LLM_MODEL=qwen3.8-flash
```

## 2. Por que a chamada para em 402

A conta é **pré-paga**: cada requisição debita o saldo. Sem crédito, o gateway autentica e recusa com
`402 Insufficient balance` — não é permissão nem modelo. Modelos `-free` respondem
`429 FreeUsageLimitError` fora do cliente OpenCode, então **não servem para um app próprio**.

Para ligar:

1. Console → **Billing** → adicionar crédito (pré-pago). A recarga tem taxa por transação
   (relato do time: 4,6 % + US$ 0,31 — **confirmar no Console**, não medi).
2. Criar a chave com **Models: Request** (a “read-only” lista modelo e não chama).
3. Guardar só no `.env` do servidor. Chave colada em chat = revogar.

## 3. Quanto custa responder WhatsApp

`qwen3.8-flash` neste Console: US$ 0,15 / 1M entrada, US$ 0,47 / 1M saída (preços de terceiro; conferir
na tabela do Console antes de fechar preço de venda).

Medida do prompt atual: ~1,4k tokens de entrada + ~100 de saída por turno.

```
turno  = 1400 × 0,15/1e6 + 100 × 0,47/1e6 ≈ US$ 0,00026
6 turnos (1 conversa)                    ≈ US$ 0,0015  ≈ R$ 0,008
1.000 conversas/mês                      ≈ US$ 1,5     ≈ R$ 8
```

O custo por conversa é irrelevante; o custo que assusta é suporte e WhatsApp oficial.

## 4. Como o produto mede o gasto

Não existe API de “quanto gastei” usando a chave de inferência. O produto lê o campo `usage` de cada
resposta (`prompt_tokens`, `completion_tokens`), grava por tenant e por mês e calcula o custo com
`PATINHO_COST_IN` / `PATINHO_COST_OUT`.

- Painel: tokens e US$ do mês por cliente.
- API: `GET /api/<loja>/usage` → `{messages, tokens_in, tokens_out, cost_usd, plan_limit, retention_days}`.
- **Teto de custo**: `PATINHO_BUDGET_USD=20` — ao bater, a IA para para aquele tenant e o agendamento
  segue no motor de regras (custo zero). Sem a env, sem teto.

## 5. Decisão de modelo

`qwen3.8-flash` é o padrão: barato, rápido, PT-BR bom e aceita tool calling. Alternativas medidas pelo
time: `gemini-3.8-flash` (JSON mais estável), `claude-haiku-4-5` (tom melhor, 2–3× o custo),
`glm-5.3-flash` (reserva). A escolha final sai de `bench/bench.py` com crédito na conta — hoje nenhum
modelo pago roda, então não há número de qualidade para apresentar.
