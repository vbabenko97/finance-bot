#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(dirname "$SCRIPT_DIR")"

: "${TELEGRAM_BOT_TOKEN:?Set TELEGRAM_BOT_TOKEN env var}"
: "${WEBHOOK_SECRET_TOKEN:?Set WEBHOOK_SECRET_TOKEN env var}"

# Pre-flight: validate the bot token against Telegram before overwriting SSM.
# An invalid token here would otherwise silently brick the production bot.
echo "Validating TELEGRAM_BOT_TOKEN against Telegram..."
GET_ME=$(curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" 2>&1) || {
  echo "ERROR: getMe failed. TELEGRAM_BOT_TOKEN is invalid or unreachable." >&2
  echo "$GET_ME" >&2
  exit 1
}
BOT_USERNAME=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['result']['username'])" "$GET_ME" 2>/dev/null) || {
  echo "ERROR: getMe response missing username field." >&2
  echo "$GET_ME" >&2
  exit 1
}
echo "Bot validated: @$BOT_USERNAME"

# Warn if WEBHOOK_SECRET_TOKEN differs from current SSM value. Rotating the
# secret silently is what caused the May 11 outage; force an explicit ack.
CURRENT_SECRET=$(aws ssm get-parameter \
  --name /finance-bot/webhook_secret_token \
  --with-decryption \
  --query 'Parameter.Value' --output text --no-cli-pager 2>/dev/null || echo "")
if [ -n "$CURRENT_SECRET" ] && [ "$CURRENT_SECRET" != "$WEBHOOK_SECRET_TOKEN" ]; then
  echo "WARNING: WEBHOOK_SECRET_TOKEN differs from the value currently in SSM." >&2
  echo "  Continuing will rotate the secret and require re-running set_webhook.sh." >&2
  if [ "${ALLOW_SECRET_ROTATION:-}" != "1" ]; then
    echo "  Re-run with ALLOW_SECRET_ROTATION=1 to confirm." >&2
    exit 1
  fi
  echo "  ALLOW_SECRET_ROTATION=1 set; proceeding with rotation."
fi

cd "$INFRA_DIR"

# Build Lambda zip before Terraform (so the file exists at plan time)
REPO_ROOT="$(dirname "$INFRA_DIR")"
echo "Packaging Lambda..."
cd "$(dirname "$REPO_ROOT")"
rm -f "$INFRA_DIR/lambda.zip"
zip -rq "$INFRA_DIR/lambda.zip" telegram_bot/ \
  -x 'telegram_bot/tests/*' \
  -x 'telegram_bot/infra/*' \
  -x 'telegram_bot/scripts/*' \
  -x 'telegram_bot/requirements-dev.txt' \
  -x '*/__pycache__/*' \
  -x '*.pyc'
echo "Created lambda.zip ($(du -h "$INFRA_DIR/lambda.zip" | cut -f1))"

cd "$INFRA_DIR"

terraform init -input=false
terraform apply

FUNCTION_NAME=$(terraform output -raw lambda_function_name)
BOT_PARAM=$(terraform output -raw telegram_bot_token_parameter_name)
WEBHOOK_PARAM=$(terraform output -raw webhook_secret_parameter_name)

aws ssm put-parameter \
  --name "$BOT_PARAM" \
  --type SecureString \
  --value "$TELEGRAM_BOT_TOKEN" \
  --overwrite \
  --no-cli-pager > /dev/null

aws ssm put-parameter \
  --name "$WEBHOOK_PARAM" \
  --type SecureString \
  --value "$WEBHOOK_SECRET_TOKEN" \
  --overwrite \
  --no-cli-pager > /dev/null

aws lambda update-function-configuration \
  --function-name "$FUNCTION_NAME" \
  --no-cli-pager > /dev/null

echo ""
echo "Deployed successfully!"
echo "Webhook URL: $(terraform output -raw webhook_url)"
echo "Bot token SSM parameter: $BOT_PARAM"
echo "Webhook secret SSM parameter: $WEBHOOK_PARAM"
echo ""
echo "Next: run set_webhook.sh to register the webhook with Telegram"
