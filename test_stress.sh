#!/bin/bash
API=http://tower.local:8000
EMAIL=teste@teste.com
PASS=123456

echo "=== 1. Login ==="
RES=$(curl -s -X POST $API/auth/login \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}")
TOKEN=$(echo $RES | sed 's/.*"token":"\([^"]*\)".*/\1/')
echo "OK"

chat() {
  local LABEL=$1 MSG=$2 THREAD=$3
  BODY="{\"message\":\"$MSG\"}"
  [ -n "$THREAD" ] && BODY="{\"message\":\"$MSG\",\"thread_id\":\"$THREAD\"}"
  RES=$(curl -s -X POST $API/chat \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "$BODY")
  TID=$(echo $RES | sed 's/.*"thread_id":"\([^"]*\)".*/\1/')
  REPLY=$(echo $RES | sed 's/.*"reply":"\([^"]*\)".*/\1/')
  echo "[$LABEL] $MSG"
  echo "[$LABEL] $REPLY"
  echo "---"
}

echo -e "\n=== 2. Cinco clientes marcam ===\n"

echo "--- Cliente 1: Corte de Cabelo às 09:00 ---"
chat "#1" "Quero agendar corte de cabelo para amanhã às 9h"
T1="$TID"

echo "--- Cliente 2: Barba às 10:00 ---"
chat "#2" "Quero marcar barba para amanhã às 10h"
T2="$TID"

echo "--- Cliente 3: Corte de Cabelo às 11:00 ---"
chat "#3" "Quero cortar o cabelo amanhã às 11h"
T3="$TID"

echo "--- Cliente 4: Hidratação às 14:00 ---"
chat "#4" "Quero agendar hidratação amanhã às 14h"
T4="$TID"

echo "--- Cliente 5: Corte + Barba às 16:00 ---"
chat "#5" "Quero corte + barba amanhã às 16h"
T5="$TID"

echo -e "\n=== 3. Clientes confirmam ===\n"
# Send confirmation to each thread
for t in "$T1" "$T2" "$T3" "$T4" "$T5"; do
  chat "ok" "Sim, pode confirmar" "$t"
done

echo -e "\n=== 4. Agendamentos ativos ==="
curl -s -H "Authorization: Bearer $TOKEN" $API/appointments | \
  python3 -c "import sys,json; [print(f\"  #{a['id']} {a['title']} - {a['start_time'][:16]} ({a['status']})\") for a in json.load(sys.stdin) if a['status']=='scheduled']" 2>/dev/null

echo -e "\n=== 5. Cliente 4 (14h) cancela via thread T4 ==="
chat "#4c" "Cancelar" "$T4"
chat "#4c" "Sim" "$T4"

echo -e "\n=== 6. Agendamentos após cancelamento ==="
curl -s -H "Authorization: Bearer $TOKEN" $API/appointments | \
  python3 -c "import sys,json; [print(f\"  #{a['id']} {a['title']} - {a['start_time'][:16]} ({a['status']})\") for a in json.load(sys.stdin) if a['status']=='scheduled']" 2>/dev/null

echo -e "\n✅ Done"
