#!/usr/bin/env bash
# Generate runtime-config.json from stack outputs, upload the UI, invalidate.
# No generated AWS identifier is ever hardcoded in the frontend source.
set -euo pipefail
STACK="${1:?usage: publish-frontend.sh <stack-name>}"

get() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?starts_with(OutputKey,'$1')].OutputValue | [0]" \
    --output text
}

API_URL=$(get ApiUrl)
BUCKET=$(get SiteBucketName)
DIST=$(get DistributionId)
CLIENT_ID=$(get CognitoClientId)
COGNITO_DOMAIN=$(get CognitoDomain)
SITE=$(get SiteUrl)

cat > frontend/runtime-config.json <<JSON
{
  "apiUrl": "${API_URL%/}",
  "cognitoDomain": "$COGNITO_DOMAIN",
  "cognitoClientId": "$CLIENT_ID",
  "redirectUri": "$SITE/"
}
JSON

aws s3 sync frontend/ "s3://$BUCKET/" --delete \
  --cache-control "no-cache, no-store, must-revalidate"
aws cloudfront create-invalidation --distribution-id "$DIST" --paths "/*" >/dev/null

echo "site=$SITE" >> "${GITHUB_OUTPUT:-/dev/null}"
echo "Published to $SITE"
