.PHONY: help install install-dev db-up db-down init migrate ingest explain clusters test test-unit test-integration lint format api worker clean

PYTHON := python
PIP := pip
SAMPLE := sample_data/sample_incident

help:
	@echo "raglogs — incident explanation tool"
	@echo ""
	@echo "Setup"
	@echo "  make install         Install production dependencies"
	@echo "  make install-dev     Install all dependencies including dev"
	@echo "  make db-up           Start PostgreSQL via Docker Compose"
	@echo "  make db-down         Stop PostgreSQL"
	@echo "  make init            Initialize config and run DB migrations"
	@echo ""
	@echo "Demo"
	@echo "  make demo            Full demo: db-up + init + ingest + explain"
	@echo "  make ingest          Ingest sample incident logs"
	@echo "  make explain         Run explain on the last hour"
	@echo "  make clusters        Show top clusters in the last hour"
	@echo ""
	@echo "Development"
	@echo "  make api             Start FastAPI server"
	@echo "  make worker          Start background worker"
	@echo "  make test            Run all tests"
	@echo "  make test-unit       Run unit tests only"
	@echo "  make test-int        Run integration tests (requires DB)"
	@echo "  make lint            Run ruff linter"
	@echo "  make format          Run ruff formatter"
	@echo "  make clean           Remove build artifacts and caches"

# ── Setup ──────────────────────────────────────────────────────────────────────

install:
	$(PIP) install -r requirements.txt

install-dev:
	$(PIP) install -r requirements.txt
	$(PIP) install -e .

db-up:
	docker compose up postgres -d
	@echo "Waiting for PostgreSQL to be ready..."
	@sleep 3

db-down:
	docker compose down

init:
	raglogs init

migrate:
	alembic upgrade head

# ── Demo ───────────────────────────────────────────────────────────────────────

demo: db-up
	@sleep 3
	raglogs init
	raglogs ingest $(SAMPLE)
	raglogs explain --since 1h

ingest:
	raglogs ingest $(SAMPLE)

explain:
	raglogs explain --since 1h

explain-nollm:
	raglogs explain --since 1h --no-llm

clusters:
	raglogs clusters --since 1h

ask:
	raglogs ask "why did the webhook fail?" --since 1h

# ── Development ────────────────────────────────────────────────────────────────

api:
	uvicorn raglogs.api.app:app --host 0.0.0.0 --port 8000 --reload

worker:
	raglogs worker

test:
	pytest tests/ -v

test-unit:
	pytest tests/unit/ -v

test-int:
	RAGLOGS_INTEGRATION_TESTS=1 pytest tests/integration/ -v

test-cov:
	pytest tests/unit/ --cov=raglogs --cov-report=term-missing

lint:
	ruff check raglogs/ tests/

format:
	ruff format raglogs/ tests/

# ── Cleanup ────────────────────────────────────────────────────────────────────

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	@echo "Cleaned."
