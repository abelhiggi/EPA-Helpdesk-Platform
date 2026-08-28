#!/usr/bin/env bash
# Four gates. Each one fails loudly rather than warning.
set -euo pipefail
STACK="${1:?usage: smoke-test.sh <stack-name>}"

get() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?starts_with(OutputKey,'$1')].OutputValue | [0]" \
    --output text
}

API_URL=$(get ApiUrl)
SITE=$(get SiteUrl)

echo "1/4 site serves the app"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$SITE/")" = "200" ]

echo "2/4 runtime config is present"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$SITE/runtime-config.json")" = "200" ]

echo "3/4 unauthenticated POST is rejected"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "${API_URL%/}/tickets" \
  -H 'Content-Type: application/json' -d '{"description":"smoke"}')
[ "$code" = "401" ] || { echo "expected 401, got $code"; exit 1; }

echo "4/4 CORS preflight returns the exact site origin, not a wildcard"
allowed=$(curl -s -X OPTIONS "${API_URL%/}/tickets" \
  -H "Origin: $SITE" -H 'Access-Control-Request-Method: POST' -D - -o /dev/null \
  | grep -i 'access-control-allow-origin' | tr -d '\r' | awk '{print $2}')
[ "$allowed" = "$SITE" ] || { echo "expected $SITE, got '$allowed'"; exit 1; }

echo "All gates passed."
