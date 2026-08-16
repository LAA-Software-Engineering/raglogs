.PHONY: help install install-dev \
        db-up db-down docker-up docker-down docker-demo docker-logs \
        init migrate demo ingest explain clusters ask \
        api web worker test test-unit test-int test-cov lint format clean

PYTHON  := python
PIP     := pip
SAMPLE  := sample_data/sample_incident

help:
	@echo "raglogs — incident explanation tool"
	@echo ""
	@echo "Local dev (needs a running Postgres — use 'make db-up')"
	@echo "  make install         Install production dependencies"
	@echo "  make install-dev     Install all dependencies including dev"
	@echo "  make db-up           Start only PostgreSQL via Docker Compose"
	@echo "  make db-down         Stop PostgreSQL"
	@echo "  make init            Run DB migrations"
	@echo "  make demo            Full CLI demo: db-up + init + ingest + explain"
	@echo "  make ingest          Ingest sample incident logs"
	@echo "  make explain         Explain the last hour"
	@echo "  make clusters        Show top clusters"
	@echo "  make api             Start FastAPI server (reload)"
	@echo "  make web             db-up + migrate + start server, then open the web UI"
	@echo "  make worker          Start background worker"
	@echo ""
	@echo "Docker (full stack)"
	@echo "  make docker-up       Build and start api + worker + postgres"
	@echo "  make docker-down     Stop and remove containers"
	@echo "  make docker-logs     Follow api and worker logs"
	@echo "  make docker-demo     Run raglogs demo inside the api container"
	@echo ""
	@echo "Testing"
	@echo "  make test            All tests"
	@echo "  make test-unit       Unit tests only"
	@echo "  make test-int        Integration tests (needs live DB)"
	@echo "  make test-cov        Unit tests with coverage"
	@echo "  make lint            Ruff lint"
	@echo "  make format          Ruff format"
	@echo "  make clean           Remove build artifacts"

# ── Setup ─────────────────────────────────────────────────────────────────────

install-requirements:
	$(PIP) install -r requirements.txt

install-dev:
	$(PIP) install -e ".[dev]"

install:
	$(PIP) install -e .

# ── Local dev ──────────────────────────────────────────────────────────────────

db-up:
	docker compose up postgres -d
	@echo "Waiting for PostgreSQL..."
	@sleep 3

db-down:
	docker compose down

init:
	alembic upgrade head

migrate: init

demo: db-up
	@sleep 2
	alembic upgrade head
	raglogs demo
	raglogs timeline --since 2h
	raglogs compare --since 30m --baseline 24h

ingest:
	raglogs ingest $(SAMPLE)

explain:
	raglogs explain --since 1h

clusters:
	raglogs clusters --since 1h

ask:
	raglogs ask "why did the webhook fail?" --since 1h

# ── Development servers ────────────────────────────────────────────────────────

api:
	uvicorn src.api.app:app --host 0.0.0.0 --port 8000 --reload

web: db-up
	@sleep 2
	alembic upgrade head
	@echo "Web UI: http://localhost:8000/"
	uvicorn src.api.app:app --host 0.0.0.0 --port 8000 --reload

worker:
	raglogs worker

# ── Docker full stack ──────────────────────────────────────────────────────────

docker-up:
	docker compose up --build -d
	@echo "API: http://localhost:8000"
	@echo "Docs: http://localhost:8000/docs"

docker-down:
	docker compose down -v

docker-logs:
	docker compose logs -f api worker

docker-demo:
	docker compose exec api raglogs demo

# ── Tests ──────────────────────────────────────────────────────────────────────

test:
	pytest tests/ -v

test-unit:
	pytest tests/unit/ -v

test-int:
	RAGLOGS_INTEGRATION_TESTS=1 pytest tests/integration/ -v

test-cov:
	pytest tests/unit/ --cov=src --cov-report=term-missing

# ── Quality ───────────────────────────────────────────────────────────────────

lint:
	ruff check src/ tests/

format:
	ruff format src/ tests/

# ── Cleanup ───────────────────────────────────────────────────────────────────

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	@echo "Cleaned."
