#!/bin/sh
# Grava a key do OpenCode no .env do patinho e testa. Nao mostra a key.
set -e
cd /home/server/docker/patinho
printf "Cole a key (nao aparece na tela): "
stty -echo 2>/dev/null || true
read -r KEY
stty echo 2>/dev/null || true
echo
KEY=$(printf "%s" "$KEY" | tr -d "[:space:]")
if [ ${#KEY} -lt 20 ]; then echo "key curta demais (${#KEY} chars) — copie com o botao Copy do Console"; exit 1; fi
cp .env ".env.bak-$(date +%Y%m%d-%H%M%S)"
sed -i "s|^PATINHO_LLM_KEY=.*|PATINHO_LLM_KEY=$KEY|" .env
grep -q "^PATINHO_LLM_KEY=" .env || echo "PATINHO_LLM_KEY=$KEY" >> .env
docker compose up -d --force-recreate patinho >/dev/null
sleep 6
CODE=$(curl -s -o /tmp/keytest.json -w "%{http_code}" -m 40 \
  https://opencode.ai/inference/go/openai/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -H "User-Agent: patinho/1.0" -H "x-opencode-session: setkey-$(date +%s)" \
  -d "{\"model\":\"qwen3.8-flash\",\"messages\":[{\"role\":\"user\",\"content\":\"ok\"}],\"max_tokens\":4}")
echo "teste HTTP $CODE"
case "$CODE" in
  200) echo "OK: key valida e com franquia. Produto ligado no LLM." ;;
  402) echo "key valida, mas SEM SALDO nesse caminho. No Go use o endpoint /inference/go/openai/v1 (ja configurado)." ;;
  401) echo "401: key invalida/expirada — copie de novo do Console." ;;
  403) echo "403: bloqueio de cliente (UA) — avise o ManNager." ;;
  429) echo "429: franquia/limite do modelo estourou agora." ;;
  *) echo "resposta inesperada, veja /tmp/keytest.json" ;;
esac
rm -f /tmp/keytest.json
