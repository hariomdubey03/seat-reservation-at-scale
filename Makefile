PYTHON ?= python3
BASE_URL ?= http://127.0.0.1:8000
ADMIN_TOKEN ?= local-admin-token-change-before-deployment
REQUESTS ?= 20000
CONCURRENCY ?= 500

.PHONY: up down logs install-dev migrate run test burst health check-operations audit lint

up:
	docker compose up --build -d --wait

down:
	docker compose down

logs:
	docker compose logs --follow api

install-dev:
	$(PYTHON) -m pip install -r requirements-dev.txt

migrate:
	$(PYTHON) -m app.migrate

run: migrate
	$(PYTHON) -m uvicorn app.main:app --host 0.0.0.0 --port $${PORT:-8000}

test:
	TEST_BASE_URL="$(BASE_URL)" ADMIN_TOKEN="$(ADMIN_TOKEN)" $(PYTHON) -m pytest -q

burst:
	$(PYTHON) scripts/burst.py "$(BASE_URL)" --admin-token "$(ADMIN_TOKEN)" --requests "$(REQUESTS)" --concurrency "$(CONCURRENCY)"

health:
	curl --fail --silent --show-error "$(BASE_URL)/health/live"
	curl --fail --silent --show-error "$(BASE_URL)/health/ready"

check-operations:
	BASE_URL="$(BASE_URL)" ADMIN_TOKEN="$(ADMIN_TOKEN)" $(PYTHON) scripts/check_operations.py

audit:
	docker compose exec -T db psql -U reservation -d reservation -v ON_ERROR_STOP=1 < scripts/reconcile.sql

lint:
	$(PYTHON) -m ruff check app scripts tests
	$(PYTHON) -m ruff format --check app scripts tests
