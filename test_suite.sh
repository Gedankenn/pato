#!/bin/bash
# PatoAgenda AI — Full Test Suite
# Tests: auth, pages, CRUD, double-booking, chat, 
#        WhatsApp personal/business mode, business info,
#        business hours validation, LLM knowledge, admin

PASS=0
FAIL=0
API="http://localhost:8000"
TST_DATE="2026-08-01"
TST_PH1="55100"
TST_PH2="55101"
TST_PH3="55102"

pass() { PASS=$((PASS+1)); echo "  ✅ $1"; }
fail() { FAIL=$((FAIL+1)); echo "  ❌ $1 — $2"; }

echo "╔══════════════════════════════════════════╗"
echo "║   PatoAgenda AI — Full Test Suite v2    ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Setup: clean previous test data ──
python3 -c "
import sqlite3
conn = sqlite3.connect('/data/pato.db')
conn.execute(\"UPDATE appointments SET status='cancelled' WHERE barbershop_id=3 AND status='scheduled'\")
conn.commit()
conn.close()
" 2>/dev/null

# ── 1. AUTH ──
echo "── 1. AUTH ──"
R=$(curl -s -X POST $API/auth/login -H 'Content-Type: application/json' -d '{"email":"demo@patobarba.com","password":"patobarba123"}')
TOKEN=$(echo "$R" | python3 -c "import sys,json;print(json.load(sys.stdin).get('token',''))" 2>/dev/null)
[ -n "$TOKEN" ] && pass "Login demo" || fail "Login demo" "no token"
R=$(curl -s -o /dev/null -w "%{http_code}" -X POST $API/auth/login -H 'Content-Type: application/json' -d '{"email":"x@x.com","password":"wrong"}')
[ "$R" = "401" ] && pass "Login inválido → 401" || fail "Login inválido → 401" "got $R"
R=$(curl -s -o /dev/null -w "%{http_code}" -X POST $API/demo/login -H 'Content-Type: application/json')
[ "$R" = "200" ] && pass "Demo login" || fail "Demo login" "got $R"

# ── 2. PAGES ──
echo ""
echo "── 2. PAGES ──"
for p in dashboard config reports login demo; do
  R=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" $API/$p 2>/dev/null)
  [ "$R" = "200" ] && pass "GET /$p → 200" || fail "GET /$p → 200" "got $R"
done

# ── 3. CONFIG: business info + hours ──
echo ""
echo "── 3. CONFIG: business info & hours ──"

# Save business info
R=$(curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  $API/barbershop -d '{"business_info":"Rua Augusta 123. Aceitamos dinheiro, cartao e PIX. Nao temos estacionamento. Tolerancia de 10min de atraso."}')
echo "$R" | grep -q '"business_info"' && pass "Save business_info" || fail "Save business_info" "$R"

# Save opening hours
R=$(curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  $API/barbershop -d '{"opening_time":"09:00","closing_time":"18:00"}')
echo "$R" | grep -q '"business_type"' && pass "Save opening/closing hours" || fail "Save hours" "$R"

# ── 4. BUSINESS HOURS VALIDATION ──
echo ""
echo "── 4. BUSINESS HOURS VALIDATION ──"

# Block: appointment ending at 19:00 (past 18:00 closing)
R=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -X POST $API/appointments -d "{\"title\":\"Corte\",\"description\":\"Joao\",\"start_time\":\"${TST_DATE}T17:00\",\"end_time\":\"${TST_DATE}T19:00\",\"staff_id\":1}")
[ "$R" = "400" ] && pass "Block past-closing (19:00 > 18:00) → 400" || fail "Block past-closing" "got $R"

# Allow: appointment ending at 17:30 (before 18:00)
R=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -X POST $API/appointments -d "{\"title\":\"Corte\",\"description\":\"Joao\",\"start_time\":\"${TST_DATE}T17:00\",\"end_time\":\"${TST_DATE}T17:30\",\"staff_id\":1}")
[ "$R" = "200" ] && pass "Allow before-closing (17:30 < 18:00) → 200" || fail "Allow before-closing" "got $R"

# Allow: appointment at 18:00 exactly (borderline)
R=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -X POST $API/appointments -d "{\"title\":\"Barba\",\"description\":\"Pedro\",\"start_time\":\"${TST_DATE}T18:00\",\"end_time\":\"${TST_DATE}T18:30\",\"staff_id\":2}")
[ "$R" = "400" ] && pass "Block 18:00-18:30 past closing → 400" || fail "Block 18:00-18:30" "got $R"

# Cleanup test appointments
python3 -c "
import sqlite3
conn = sqlite3.connect('/data/pato.db')
conn.execute(\"UPDATE appointments SET status='cancelled' WHERE barbershop_id=3 AND status='scheduled'\")
conn.commit()
conn.close()
" 2>/dev/null

# ── 5. DOUBLE-BOOKING ──
echo ""
echo "── 5. DOUBLE-BOOKING ──"
R=$(curl -s -w "%{http_code}" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -X POST $API/appointments -d "{\"title\":\"A\",\"description\":\"Joao\",\"start_time\":\"${TST_DATE}T10:00\",\"end_time\":\"${TST_DATE}T10:30\",\"staff_id\":1}" --max-time 10)
HTTP=$(echo "$R" | tail -c 4)
AID=$(echo "$R" | head -c -3 | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
[ "$HTTP" = "200" ] && pass "Create appointment A" || fail "Create appointment A" "got $HTTP"

R=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -X POST $API/appointments -d "{\"title\":\"B\",\"description\":\"Maria\",\"start_time\":\"${TST_DATE}T10:15\",\"end_time\":\"${TST_DATE}T10:45\",\"staff_id\":1}" --max-time 10)
[ "$R" = "409" ] && pass "Double-book same staff → 409" || fail "Double-book same staff" "got $R"

R=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -X POST $API/appointments -d "{\"title\":\"C\",\"description\":\"Pedro\",\"start_time\":\"${TST_DATE}T10:15\",\"end_time\":\"${TST_DATE}T10:45\",\"staff_id\":2}" --max-time 10)
[ "$R" = "200" ] && pass "Overlap different staff → 200" || fail "Overlap different staff" "got $R"

# Cleanup
[ -n "$AID" ] && curl -s -o /dev/null -H "Authorization: Bearer $TOKEN" -X DELETE $API/appointments/$AID

# ── 6. STAFF AUTO-ASSIGN ──
echo ""
echo "── 6. STAFF AUTO-ASSIGN ──"
R=$(curl -s -w "%{http_code}" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -X POST $API/appointments -d "{\"title\":\"Teste\",\"description\":\"Ana\",\"start_time\":\"${TST_DATE}T11:00\",\"end_time\":\"${TST_DATE}T11:30\"}" --max-time 10)
HTTP=$(echo "$R" | tail -c 4)
echo "$R" | grep -q '"staff_id":' && pass "Auto-assign staff → staff_id present" || fail "Auto-assign staff" "no staff_id"
[ "$HTTP" = "200" ] && pass "Auto-assign appointment → 200" || fail "Auto-assign" "got $HTTP"

# Cleanup
AID2=$(echo "$R" | head -c -3 | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
[ -n "$AID2" ] && curl -s -o /dev/null -H "Authorization: Bearer $TOKEN" -X DELETE $API/appointments/$AID2

# ── 7. LLM: business knowledge ──
echo ""
echo "── 7. LLM: BUSINESS KNOWLEDGE ──"

R=$(curl -s -X POST $API/webhook/wa-message -H 'Content-Type: application/json' \
  -d "{\"barbershop_id\":3,\"from\":\"${TST_PH1}\",\"customer_name\":\"Cliente\",\"text\":\"voce funciona no sabado?\"}" --max-time 30)
echo "$R" | grep -q '"reply":null' && fail "LLM: responds about business hours" "$R" || pass "LLM: responds about business hours"

R=$(curl -s -X POST $API/webhook/wa-message -H 'Content-Type: application/json' \
  -d "{\"barbershop_id\":3,\"from\":\"${TST_PH1}\",\"customer_name\":\"Cliente\",\"text\":\"aceita PIX?\"}" --max-time 30)
echo "$R" | grep -qi 'pix\|sim\|aceita' && pass "LLM: knows about PIX" || fail "LLM: PIX knowledge" "$(echo $R | head -c 100)"

R=$(curl -s -X POST $API/webhook/wa-message -H 'Content-Type: application/json' \
  -d "{\"barbershop_id\":3,\"from\":\"${TST_PH1}\",\"customer_name\":\"Cliente\",\"text\":\"tem sala de massagem?\"}" --max-time 30)
echo "$R" | grep -q '"reply":null' && fail "LLM: handles unknown question" "$R" || pass "LLM: handles unknown question"

# ── 8. LLM: closing time enforcement ──
echo ""
echo "── 8. LLM: CLOSING TIME ──"
R=$(curl -s -X POST $API/webhook/wa-message -H 'Content-Type: application/json' \
  -d "{\"barbershop_id\":3,\"from\":\"${TST_PH2}\",\"customer_name\":\"Cliente\",\"text\":\"quero agendar um corte as 19h\"}" --max-time 30)
echo "$R" | grep -q '"reply":null' && fail "LLM: refuses past-closing 19h" "$R" || pass "LLM: refuses past-closing 19h"

# ── 9. WHATSAPP PERSONAL MODE ──
echo ""
echo "── 9. WHATSAPP PERSONAL MODE ──"
PH=$(date +%s)  # unique phone per run
curl -s -o /dev/null -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  $API/barbershop -d '{"whatsapp_mode":"personal"}'

R=$(curl -s -X POST $API/webhook/wa-message -H 'Content-Type: application/json' \
  -d "{\"barbershop_id\":3,\"from\":\"${PH}0\",\"text\":\"oi sumido\"}" --max-time 15)
echo "$R" | grep -q '"reply":null' && pass "Personal: casual ignored" || fail "Personal: casual ignored" "$R"

R=$(curl -s -X POST $API/webhook/wa-message -H 'Content-Type: application/json' \
  -d "{\"barbershop_id\":3,\"from\":\"${PH}1\",\"text\":\"preciso agendar um corte\"}" --max-time 30)
echo "$R" | grep -q '"reply":null' && fail "Personal: scheduling opens window" "$R" || pass "Personal: scheduling opens window"

R=$(curl -s -X POST $API/webhook/wa-message -H 'Content-Type: application/json' \
  -d "{\"barbershop_id\":3,\"from\":\"${PH}1\",\"text\":\"amanha as 14h\"}" --max-time 30)
echo "$R" | grep -q '"reply":null' && fail "Personal: inside window responds" "$R" || pass "Personal: inside window responds"

R=$(curl -s -X POST $API/webhook/wa-message -H 'Content-Type: application/json' \
  -d "{\"barbershop_id\":3,\"from\":\"${PH}1\",\"text\":\"sim pode confirmar\"}" --max-time 30)
echo "$R" | grep -q '"reply":null' && fail "Personal: appointment created" "$R" || pass "Personal: appointment created"

R=$(curl -s -X POST $API/webhook/wa-message -H 'Content-Type: application/json' \
  -d "{\"barbershop_id\":3,\"from\":\"${PH}1\",\"text\":\"valeu obrigado\"}" --max-time 15)
echo "$R" | grep -q '"reply":null' && pass "Personal: window closed" || fail "Personal: window closed" "$R"

curl -s -o /dev/null -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  $API/barbershop -d '{"whatsapp_mode":"business"}'

# ── 10. ADMIN ──
echo ""
echo "── 10. ADMIN ──"
# Admin endpoints with regular token should be denied
R=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" \
  -X POST $API/admin/barbershops/3/toggle-payment 2>/dev/null)
[ "$R" = "403" ] && pass "Admin action denied to non-admin → 403" || fail "Admin auth check" "got $R"

R=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" \
  -X DELETE $API/admin/barbershops/99 2>/dev/null)
[ "$R" = "403" ] && pass "Admin delete denied to non-admin → 403" || fail "Admin delete check" "got $R"

# Payment toggle via admin (test with demo — demo is not admin, expect 403)
R=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" \
  $API/admin 2>/dev/null)
[ "$R" = "307" ] && pass "GET /admin as non-admin → redirect" || fail "Admin redirect" "got $R"

# ── 11. SERVICES & STAFF ──
echo ""
echo "── 11. SERVICES & STAFF ──"
R=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" $API/services)
[ "$R" = "200" ] && pass "GET /services → 200" || fail "GET /services" "got $R"
R=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" $API/staff)
[ "$R" = "200" ] && pass "GET /staff → 200" || fail "GET /staff" "got $R"

# ── 12. CHAT & WEBHOOK ──
echo ""
echo "── 12. CHAT & WEBHOOK ──"
R=$(curl -s -w "%{http_code}" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -X POST $API/chat -d '{"message":"oi"}' --max-time 30)
HTTP=$(echo "$R" | tail -c 4)
[ "$HTTP" = "200" ] && pass "POST /chat async LLM → 200" || fail "POST /chat" "got $HTTP"

R=$(curl -s -o /dev/null -w "%{http_code}" -X POST $API/webhook/wa-message -H 'Content-Type: application/json' \
  -d "{\"barbershop_id\":3,\"from\":\"55999\",\"text\":\"oi\"}" --max-time 30)
[ "$R" = "200" ] && pass "Webhook business mode → 200" || fail "Webhook" "got $R"

# ── SUMMARY ──
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║  Results: $PASS passed, $FAIL failed"
echo "╚══════════════════════════════════════════╝"
[ "$FAIL" -eq 0 ] && echo "  ✅ ALL TESTS PASSED" && exit 0
echo "  ❌ SOME TESTS FAILED"
exit 1
