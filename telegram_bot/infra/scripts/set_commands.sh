#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(dirname "$SCRIPT_DIR")"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

# Curated /-menu for Telegram clients. Commands hidden from this menu
# (set_balance, set_budget, delete_budget, rates, cancel) remain reachable
# via /help and direct typing.
read -r -d '' PAYLOAD <<'EOF' || true
{
  "commands": [
    {"command": "start", "description": "quick examples"},
    {"command": "help", "description": "show all commands and syntax"},
    {"command": "add", "description": "add expense step by step"},
    {"command": "income", "description": "record income (amount description)"},
    {"command": "balance", "description": "show account balances"},
    {"command": "portfolio", "description": "show net worth by account group"},
    {"command": "history", "description": "show recent transactions"},
    {"command": "search", "description": "search transactions (text and filters)"},
    {"command": "summary", "description": "monthly summary and pace"},
    {"command": "budget", "description": "show monthly budget status"},
    {"command": "edit", "description": "edit recent item (/edit n)"},
    {"command": "delete", "description": "delete last transaction"},
    {"command": "recurring", "description": "manage recurring transactions"},
    {"command": "transfer", "description": "move funds (amount from to)"},
    {"command": "export", "description": "download CSV (all or YYYY-MM)"}
  ]
}
EOF

if [[ "$DRY_RUN" == "1" ]]; then
  echo "Payload (dry run):"
  echo "$PAYLOAD" | python3 -m json.tool
  exit 0
fi

cd "$INFRA_DIR"
BOT_PARAM=$(terraform output -raw telegram_bot_token_parameter_name)

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  TELEGRAM_BOT_TOKEN=$(aws ssm get-parameter \
    --name "$BOT_PARAM" \
    --with-decryption \
    --query 'Parameter.Value' --output text --no-cli-pager)
fi

: "${TELEGRAM_BOT_TOKEN:?Set TELEGRAM_BOT_TOKEN env var or store it in SSM first}"

echo "Posting setMyCommands to Telegram..."
RESPONSE=$(curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setMyCommands" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD")
echo "$RESPONSE" | python3 -m json.tool

OK=$(echo "$RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('ok', False))")
if [[ "$OK" != "True" ]]; then
  echo "ERROR: Telegram returned ok != true" >&2
  exit 1
fi
echo "Commands updated."
