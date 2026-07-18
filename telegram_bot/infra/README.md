# Bot Infrastructure

Terraform for the Telegram bot: Lambda + API Gateway + DynamoDB (single table) +
CloudWatch monitoring + a scheduled (cron) trigger. One concern per file
(`lambda.tf`, `api_gateway.tf`, `dynamodb.tf`, `iam.tf`, `monitoring.tf`,
`scheduled.tf`, `variables.tf`, `outputs.tf`, `main.tf`).

## Lambda contract

- Handler: `telegram_bot.handler.lambda_handler`
- Runtime: `python3.11`
- Package: `lambda.zip`, built by `scripts/deploy.sh`, which zips `telegram_bot/`
  excluding `tests/`, `infra/`, `scripts/`, `requirements-dev.txt`, and caches.
  Renaming `telegram_bot/`, `handler.py`, or `lambda_handler`, or moving modules
  out of the packaged set, breaks the deployed function.

## Deploy order

Required env: `TELEGRAM_BOT_TOKEN`, `WEBHOOK_SECRET_TOKEN`.

1. `scripts/deploy.sh` — validates the bot token, builds `lambda.zip`, runs
   `terraform apply`, and writes the token + webhook secret to SSM Parameter
   Store. Refuses to rotate the webhook secret unless `ALLOW_SECRET_ROTATION=1`.
2. `scripts/set_webhook.sh` — registers the webhook URL with Telegram (run after
   a token rotation).
3. `scripts/set_commands.sh` — registers the bot's command list with Telegram.

See [../../docs/bot_setup.md](../../docs/bot_setup.md) for the full runbook and
deploy checklist.

## Local-only / gitignored

`.terraform/`, `terraform.tfstate*`, `terraform.tfvars`, and `lambda.zip` are
gitignored. Copy `terraform.tfvars.example` to `terraform.tfvars` and fill in
values before the first apply.
