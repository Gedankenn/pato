# LGPD no Patinho — o que está implementado

Base legal: execução de contrato (art. 7º, V) para atender e agendar; legítimo interesse para
segurança. Dados tratados: telefone, nome informado, serviço, horário, texto das mensagens.
**Não** tratamos documento, cartão, saúde, biometria ou localização.

## Retenção

- `tenants.retention_days` (padrão **180**).
- `POST /api/<loja>/lgpd/purge` apaga o conteúdo das conversas mais antigas que a janela e troca o nome
  do agendamento antigo por `(removido)`.
- Contadores de uso (mensagens/tokens/custo) ficam: são agregados, sem conteúdo pessoal.
- Rodar 1×/dia em cron: `0 4 * * * curl -X POST .../lgpd/purge`.

## Direitos do titular

| direito | endpoint |
|---|---|
| acesso / portabilidade | `GET /api/<loja>/lgpd/export?phone=<telefone>` |
| eliminação | `POST /api/<loja>/lgpd/delete` `{"phone":"..."}` |
| informação sobre retenção | `GET /api/<loja>/usage` → `retention_days` |

A exclusão apaga mensagens e a conversa daquele contato e anonimiza o nome nos agendamentos; o
histórico de uso agregado permanece (não identifica pessoa).

## Transparência

Página pública `/privacidade` explica base legal, retenção, suboperador (provedor de modelo) e como
exercer os direitos. O aviso deve estar no primeiro contato do WhatsApp (“posso anotar seu nome e
horário? usamos isso só para o agendamento”).

## Suboperador (modelo de IA)

O texto da conversa é enviado ao provedor do modelo (OpenCode Zen / modelo escolhido pela loja) para
gerar a resposta. O titular aceitou que o provedor possa usar os dados conforme os termos dele; isso
deve constar no aviso de privacidade da loja e no contrato. **Nenhum dado é vendido.**

## O que ainda falta para auditoria

1. Registro de consentimento por contato (data/hora do aceite) — hoje é implícito na conversa.
2. Criptografia em repouso: SQLite fica em disco comum no omaserver; usar volume cifrado (LUKS) ou
   SQLCipher. Volume cifrado é o caminho mais simples.
3. Trilha de acesso (quem exportou/apagou e quando) — hoje o endpoint não registra auditoria.
4. Encarregado (DPO) nomeado e canal de contato publicado; hoje a página cita “suporte do contrato”.
5. Backup com o mesmo prazo de retenção — backup antigo pode ressuscitar dado excluído.
