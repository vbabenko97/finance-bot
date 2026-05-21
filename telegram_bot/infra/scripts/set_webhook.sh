#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(dirname "$SCRIPT_DIR")"

cd "$INFRA_DIR"
WEBHOOK_URL=$(terraform output -raw webhook_url)
BOT_PARAM=$(terraform output -raw telegram_bot_token_parameter_name)
WEBHOOK_PARAM=$(terraform output -raw webhook_secret_parameter_name)

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  TELEGRAM_BOT_TOKEN=$(aws ssm get-parameter --name "$BOT_PARAM" --with-decryption --query 'Parameter.Value' --output text --no-cli-pager)
fi

if [[ -z "${WEBHOOK_SECRET_TOKEN:-}" ]]; then
  WEBHOOK_SECRET_TOKEN=$(aws ssm get-parameter --name "$WEBHOOK_PARAM" --with-decryption --query 'Parameter.Value' --output text --no-cli-pager)
fi

: "${TELEGRAM_BOT_TOKEN:?Set TELEGRAM_BOT_TOKEN env var or store it in SSM first}"
: "${WEBHOOK_SECRET_TOKEN:?Set WEBHOOK_SECRET_TOKEN env var or store it in SSM first}"

echo "Setting webhook to: $WEBHOOK_URL"

curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -H "Content-Type: application/json" \
  -d "{\"url\": \"${WEBHOOK_URL}\", \"secret_token\": \"${WEBHOOK_SECRET_TOKEN}\"}" | python3 -m json.tool

echo ""
