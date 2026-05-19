#!/bin/bash
API=http://tower.local:8000
EMAIL=teste@teste.com
PASS=123456

echo "=== 1. Login ==="
RES=$(curl -s -X POST $API/auth/login \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}")
TOKEN=$(echo $RES | sed 's/.*"token":"\([^"]*\)".*/\1/')
echo "Token: ${TOKEN:0:20}..."
SHOP_ID=$(echo $RES | sed 's/.*"barbershop_id":\([0-9]*\).*/\1/')
echo "Loja ID: $SHOP_ID"

echo -e "\n=== 2. Listar agendamentos ==="
curl -s -H "Authorization: Bearer $TOKEN" $API/appointments

echo -e "\n\n=== 3. Criar agendamento ==="
CRIAR=$(curl -s -X POST $API/appointments \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"Corte de Cabelo","description":"Corte masculino","start_time":"2026-05-20T14:00:00","end_time":"2026-05-20T15:00:00"}')
echo "$CRIAR"
APT_ID=$(echo $CRIAR | sed 's/.*"id":\([0-9]*\).*/\1/')
echo "Criado ID: $APT_ID"

echo -e "\n=== 4. Ver agendamento ==="
curl -s -H "Authorization: Bearer $TOKEN" $API/appointments/$APT_ID

echo -e "\n\n=== 5. Reagendar ==="
curl -s -X PUT "$API/appointments/$APT_ID/reschedule" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"appointment_id":'$APT_ID',"new_start_time":"2026-05-21T10:00:00","new_end_time":"2026-05-21T11:00:00"}'

echo -e "\n\n=== 6. Cancelar ==="
curl -s -X DELETE "$API/appointments/$APT_ID" \
  -H "Authorization: Bearer $TOKEN"

echo -e "\n\n=== 7. Listar (deve mostrar cancelled/rescheduled) ==="
curl -s -H "Authorization: Bearer $TOKEN" $API/appointments | python3 -m json.tool 2>/dev/null || curl -s -H "Authorization: Bearer $TOKEN" $API/appointments

echo -e "\n\n✅ Done"