# finance-bot

A Telegram bot that turns personal-finance entry into a 10-second daily habit. Single-user, serverless, ~33 KB Lambda zip.

> This is the sanitized open-source skeleton extracted from a real production bot. The original repo is private because it carries years of personal transactions. The code here is the same; only the data, taxonomy, and account labels have been replaced with generic equivalents.

## What it does

- **Quick-add free-text**: `15 Billa`, `25 USD Netflix`, `100 UAH coffee @bank_uah_1` — single-message expense entry with auto-categorisation and an inline Undo.
- **Step-by-step `/add`** when you want a guided flow.
- **Tagged transactions**: `#italy2026 #trip` collapses cross-cutting analytics like "all transactions tagged italy2026" via `/search -t`.
- **`/edit n`** for in-place transaction editing (one field per session) with atomic balance reconciliation across accounts.
- **`/transfer <amt> <from> <to>`** records paired ledger entries linked through a `paired_tx_sk` reference; same-currency and cross-currency both handled via the FX cache.
- **`/summary [YYYY-MM]`** — monthly digest: spend, income, net, top categories, top merchants, vs-previous-month delta, projected end-of-month pace.
- **`/budget` + `/set_budget`** per-category monthly limits, EUR-normalised so a mixed-currency ledger still reports cleanly.
- **`/recurring`** templates with monthly / weekly / daily schedules booked automatically on a daily EventBridge cron.
- **Proactive pace alerts**: same cron checks budgeted categories and pings if projected spend will overshoot the limit by more than 10%.
- **`/export [YYYY-MM]`** ships a UTF-8 CSV via Telegram's `sendDocument` (stdlib multipart, no extra deps).
- **`/portfolio`** + **`/balance`** with FX normalisation across UAH, USD, EUR, USDT, BTC.

## Architecture

```
Telegram  ──POST──>  API Gateway v2  ──invoke──>  Lambda (Python 3.11)
                                                    │
                                          ┌─────────┴─────────┐
                                          ▼                   ▼
                                    DynamoDB                Telegram API
                                  (single table)           (sendMessage,
                                  PK=USER#<id>              sendDocument,
                                  SK=TX#/BAL#/                etc.)
                                  BUDGET#/RECUR#/
                                  CONV/UPD#/ALERT#/
                                  FX_RATES)

EventBridge cron  ──invoke──>  same Lambda  ──>  scheduler.run_scheduled_tasks
(daily 06:00 UTC)                                  ├─ book due recurring templates
                                                   └─ send pace alerts
```

Single-table DynamoDB. Every entity lives under `PK=USER#<id>` with a typed `SK` prefix; idempotency markers (`UPD#`) prevent transport retries from double-applying balance changes, while DynamoDB `ConditionExpression`s on item state prevent user-driven double-taps. All multi-row mutations (transaction insert, paired transfer, edit with account change, recurring booking) ride a single `TransactWriteItems` so the balances and the transaction record commit atomically or not at all.

## Layout

```
telegram_bot/
├── handler.py           — Lambda entry, routes API Gateway / SNS / EventBridge events
├── bot/
│   ├── commands.py      — every / command handler
│   ├── conversation.py  — multi-step /add and /edit state machines (TTL-bound in DynamoDB)
│   ├── quick_add.py     — free-text parser + tag extraction
│   ├── scheduler.py     — daily cron: recurring bookings + pace alerts
│   ├── formatters.py    — pure Telegram-message renderers
│   └── telegram_api.py  — urllib-only Telegram REST client (incl. multipart sendDocument)
├── storage/
│   ├── models.py        — Transaction / AccountBalance / RecurringTemplate / ConversationState
│   └── dynamodb.py      — all DynamoDB access; transactional add / update / delete / transfer
├── config/
│   ├── accounts.py      — account inventory (generic in this public copy)
│   ├── categories.py    — flat category taxonomy
│   └── merchants.py     — merchant alias canonicalisation for /summary grouping
├── tests/               — 271 tests, pytest + moto, golden-path and double-execution coverage
└── infra/
    ├── *.tf             — Lambda, API Gateway v2, DynamoDB, IAM, EventBridge, SNS, CloudWatch alarms
    └── scripts/         — deploy.sh / set_webhook.sh / set_commands.sh
```

No pip dependencies in the deployed zip beyond what Lambda's runtime already provides — `boto3` is on the runtime, the Telegram client uses `urllib.request`, multipart upload is built from `email`-less primitives.

## Tests

```bash
make test     # pytest, ~3s, 271 tests
make lint     # ruff
make check    # both
```

Tests use `moto` to fake DynamoDB, `unittest.mock.patch` to stub the Telegram client. Notable coverage:

- Idempotency at two layers: transport (callback-id-derived `UPD#` marker) and state (DynamoDB conditional writes).
- Cross-currency `/transfer` math validated against expected minor-unit conversions.
- `/recurring` schedule advancement across short months, year boundaries, missed-cron catch-up (no backfill on resume).
- Pace alert dedup within 24h, cross-currency budget normalisation, edge cases (zero day, zero limit, missing FX).

## Deploy your own

You'll need: AWS account, Terraform ≥ 1.5, a Telegram bot token from BotFather, your Telegram user id.

```bash
cd telegram_bot/infra
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars: set allowed_user_id and aws_region

export TELEGRAM_BOT_TOKEN=<your bot token>
export WEBHOOK_SECRET_TOKEN=<a random 32+ char string>
./scripts/deploy.sh         # packages Lambda, applies Terraform, writes SSM SecureStrings
./scripts/set_webhook.sh    # registers the webhook with Telegram
./scripts/set_commands.sh   # populates the / autocomplete menu
```

`deploy.sh` runs a pre-flight against Telegram's `getMe` before overwriting any secret in SSM, and refuses to silently rotate the webhook secret unless `ALLOW_SECRET_ROTATION=1` is set. (That hardening exists because the author once shipped wrong env vars and brick the bot for an hour. The pre-flight is now load-bearing.)

## Design choices worth pointing out

- **Single-table DynamoDB** rather than per-entity tables. Cheap to operate, fits the access patterns (every read is a `Query` by `PK + begins_with(SK, prefix)`), avoids managing GSIs at this scale. See `storage/dynamodb.py`.
- **Minor-unit arithmetic everywhere** with a per-currency `MINOR_UNIT_FACTOR` (2dp for fiat, 8dp for BTC). No floats touch a balance.
- **Telegram callbacks as a tiny RPC**. Inline-button `callback_data` is a short prefix-typed string (`del:TX#…`, `edit:field:amount`, `add:cat:groceries`), so adding a new flow is a one-line addition to a dispatch table.
- **Conversation state lives in the same DynamoDB table** under SK=`CONV` with a 5-minute TTL — no separate session store, no Redis.
- **Movement-mode transactions** (transfers, withdrawals, FX exchanges) are excluded from `/summary` / `/budget` / pace alerts by filter, so the spending picture stays meaningful even with frequent inter-account moves.
- **Paired transfers via `paired_tx_sk`** — each leg holds a back-reference, so deleting one cascades to the other inside a single transactional write.
- **EventBridge daily cron at 06:00 UTC** branches in the Lambda entry **before** webhook validation, since scheduled events have no `headers` and would otherwise 500.

## License

MIT — see [LICENSE](LICENSE).
