#!/usr/bin/env bash
set -euo pipefail

LABEL="${1:?usage: experiment.sh <baseline|after>}"
STACK="${STACK:-Helpdesk-dev}"
PASSWORD="${COGNITO_PASSWORD:?export COGNITO_PASSWORD first}"
USERNAME="${COGNITO_USERNAME:-abelhiggins1@gmail.com}"
OUT="experiment-${LABEL}.csv"

out() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue|[0]" --output text
}

API=$(out ApiUrl); API="${API%/}"
POOL=$(out UserPoolId)
CLIENT=$(out CognitoClientId)

TABLE=$(aws cloudformation list-stack-resources --stack-name "$STACK" \
  --query "StackResourceSummaries[?ResourceType=='AWS::DynamoDB::Table'].PhysicalResourceId|[0]" \
  --output text)

echo "API:   $API"
echo "Table: $TABLE"
echo

TOKEN=$(aws cognito-idp admin-initiate-auth \
  --user-pool-id "$POOL" --client-id "$CLIENT" \
  --auth-flow ADMIN_USER_PASSWORD_AUTH \
  --auth-parameters USERNAME="$USERNAME",PASSWORD="$PASSWORD" \
  --query 'AuthenticationResult.IdToken' --output text)
[ -n "$TOKEN" ] && [ "$TOKEN" != "None" ] || { echo "auth failed"; exit 1; }

declare -a IDS=() EXPECTED=() TEXTS=()
n=0
while IFS='|' read -r expected text; do
  [[ -z "${expected// }" || "$expected" == \#* ]] && continue
  body=$(python3 -c 'import json,sys; print(json.dumps({"description": sys.argv[1]}))' "$text")
  resp=$(curl -s -X POST "$API/tickets" -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' -d "$body")
  id=$(printf '%s' "$resp" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("ticketId",""))
except Exception: print("")')
  if [ -z "$id" ]; then echo "  FAILED: ${text:0:45} -> $resp"; continue; fi
  IDS+=("$id"); EXPECTED+=("$expected"); TEXTS+=("$text")
  n=$((n+1)); printf '  %2d %s\n' "$n" "${text:0:58}"
  sleep 1
done < tickets.txt

echo
echo "Submitted $n. Waiting 60s for the queue to drain..."
sleep 60

echo "ticketId,expected,actual,priority,confidence,text" > "$OUT"
for i in "${!IDS[@]}"; do
  item=$(aws dynamodb get-item --table-name "$TABLE" \
    --key "{\"ticketId\":{\"S\":\"${IDS[$i]}\"}}" --output json)
  read -r actual priority conf status <<<"$(printf '%s' "$item" | python3 -c '
import json,sys
d=json.load(sys.stdin).get("Item",{})
g=lambda k: d.get(k,{}).get("S","")
print(g("category") or "-", g("priority") or "-", g("confidence") or "-", g("status") or "-")')"
  echo "${IDS[$i]},${EXPECTED[$i]},$actual,$priority,$conf,\"$(printf '%s' "${TEXTS[$i]}" | tr ',' ';')\"" >> "$OUT"
  [ "$status" = "ROUTED" ] || echo "  not routed: ${IDS[$i]} ($status)"
done

echo
python3 - "$OUT" "$LABEL" <<'PY'
import csv,sys
from collections import defaultdict
rows=[r for r in csv.DictReader(open(sys.argv[1])) if r["confidence"] not in ("-","")]
if not rows: sys.exit("no routed tickets with confidence")
by=defaultdict(list)
for r in rows: by[r["actual"]].append(r)
print(f"=== {sys.argv[2].upper()} ===\n")
print(f"{'Category':<10}{'N':>6}{'Mean':>9}{'<0.7':>8}{'Correct':>9}")
for c in sorted(by):
    rs=by[c]; cs=[float(r['confidence']) for r in rs]
    print(f"{c:<10}{len(rs):>6}{sum(cs)/len(cs):>9.3f}"
          f"{100*sum(1 for x in cs if x<0.7)/len(rs):>7.0f}%"
          f"{100*sum(1 for r in rs if r['actual']==r['expected'])/len(rs):>8.0f}%")
cs=[float(r['confidence']) for r in rows]
print(f"\n{'ALL':<10}{len(rows):>6}{sum(cs)/len(cs):>9.3f}"
      f"{100*sum(1 for x in cs if x<0.7)/len(rows):>7.0f}%"
      f"{100*sum(1 for r in rows if r['actual']==r['expected'])/len(rows):>8.0f}%")
print("\nLowest confidence:")
for r in sorted(rows,key=lambda r: float(r['confidence']))[:8]:
    flag="" if r['actual']==r['expected'] else "  MISCLASSIFIED"
    print(f"  {r['confidence']:>5}  {r['actual']:<9} {r['text'][:55]}{flag}")
PY
echo
echo "Results: $OUT"
