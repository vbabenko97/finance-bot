.PHONY: test lint fmt check help sync-finance

test:  ## Run bot tests
	pytest telegram_bot/tests -q

lint:  ## Run ruff linter
	ruff check telegram_bot/ tools/

fmt:  ## Auto-format with ruff
	ruff format telegram_bot/ tools/
	ruff check --fix telegram_bot/ tools/

check: lint test  ## Lint + tests (CI-equivalent)

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-8s %s\n", $$1, $$2}'

OUT_DIR ?= $(CURDIR)/exports
sync-finance:  ## Export a read-only DynamoDB snapshot to OUT_DIR
	python -m telegram_bot.scripts.export_snapshot --out-dir "$(OUT_DIR)"
