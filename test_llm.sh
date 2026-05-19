#!/bin/bash
API=http://tower.local:8000
EMAIL=teste@teste.com
PASS=123456

echo "=== 1. Login ==="
RES=$(curl -s -X POST $API/auth/login \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}")
TOKEN=$(echo $RES | sed 's/.*"token":"\([^"]*\)".*/\1/')
echo "OK (token: ${TOKEN:0:20}...)"

THREAD=""

chat() {
  echo -e "\n🤔 $1"
  BODY="{\"message\":\"$1\"}"
  [ -n "$THREAD" ] && BODY="{\"message\":\"$1\",\"thread_id\":\"$THREAD\"}"
  RES=$(curl -s -X POST $API/chat \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "$BODY")
  THREAD=$(echo $RES | sed 's/.*"thread_id":"\([^"]*\)".*/\1/')
  echo "🤖 $(echo $RES | sed 's/.*"reply":"\([^"]*\)".*/\1/')"
}

echo -e "\n=== 2. Conversando com o assistente ==="

chat "Bom dia! Gostaria de agendar um corte de cabelo para amanhã às 15h"
chat "Pode ser para o Thiago"
chat "Qual o valor?"
chat "Ok, confirma pra mim por favor"
chat "Obrigado! Agora preciso cancelar esse agendamento"
chat "Sim, pode cancelar"
chat "Quero marcar outro para sexta às 10h"
chat "Perfeito, confirma"

echo -e "\n=== 3. Agendamentos finais ==="
curl -s -H "Authorization: Bearer $TOKEN" $API/appointments | \
  python3 -c "import sys,json; [print(f\"  #{a['id']} {a['title']} - {a['start_time'][:16]} ({a['status']})\") for a in json.load(sys.stdin)]" 2>/dev/null

echo -e "\n✅ Done"