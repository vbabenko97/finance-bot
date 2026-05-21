.PHONY: test lint fmt check help

test:  ## Run bot tests
	pytest telegram_bot/tests -q

lint:  ## Run ruff linter
	ruff check telegram_bot/

fmt:  ## Auto-format with ruff
	ruff format telegram_bot/
	ruff check --fix telegram_bot/

check: lint test  ## Lint + tests (CI-equivalent)

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-8s %s\n", $$1, $$2}'
