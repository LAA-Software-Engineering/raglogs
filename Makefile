.PHONY: help install install-dev \
        db-up db-down docker-up docker-down docker-demo docker-logs \
        init migrate demo ingest explain clusters ask \
        api web web-serve worker test test-unit test-int test-cov lint format \
        openapi jsonschema client-go client-python clean

PYTHON  := python
PIP     := pip
SAMPLE  := sample_data/sample_incident
OPENAPI := clients/openapi.json

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
	@echo "  make web             Seed a fresh sample incident (demo) + serve at http://localhost:8000/"
	@echo "  make web-serve       db-up + migrate + serve — no reseed, for repeat runs"
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
	@echo "  make openapi         Export OpenAPI schema to clients/openapi.json"
	@echo "  make jsonschema      Export /v1/query JSON Schemas to clients/jsonschema/"
	@echo "  make client-go       Generate Go client (oapi-codegen; no-op if missing)"
	@echo "  make client-python   Optional OpenAPI Python generator, or use src/clients/v1.py"
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

# Bind host passed to uvicorn *and* read by the startup auth guard (API_BIND_HOST).
# 0.0.0.0 matches historical `make api` behaviour; with AUTH_ENABLED=false the
# process logs a warning. Loopback: `make api API_BIND_HOST=127.0.0.1`.
API_BIND_HOST ?= 0.0.0.0

api:
	API_BIND_HOST=$(API_BIND_HOST) uvicorn src.api.app:app --host $(API_BIND_HOST) --port 8000 --reload

web: demo
	@echo "Web UI: http://localhost:8000/ — open it in a browser"
	API_BIND_HOST=$(API_BIND_HOST) uvicorn src.api.app:app --host $(API_BIND_HOST) --port 8000 --reload

web-serve: db-up
	@sleep 2
	alembic upgrade head
	@echo "Web UI: http://localhost:8000/ — open it in a browser"
	API_BIND_HOST=$(API_BIND_HOST) uvicorn src.api.app:app --host $(API_BIND_HOST) --port 8000 --reload

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
	INTEGRATION_TESTS=1 pytest tests/integration/ -v

test-cov:
	pytest tests/unit/ --cov=src --cov-report=term-missing

# ── Quality ───────────────────────────────────────────────────────────────────

lint:
	ruff check src/ tests/

format:
	ruff format src/ tests/

# ── OpenAPI / clients ─────────────────────────────────────────────────────────

openapi:
	PYTHONPATH=. $(PYTHON) scripts/export_openapi.py

jsonschema:
	PYTHONPATH=. $(PYTHON) scripts/export_jsonschema.py

# Generates clients/go/client.go when oapi-codegen is installed.
# Missing binary: print install hint and exit 0 so CI without Go still passes.
client-go: openapi
	@if command -v oapi-codegen >/dev/null 2>&1; then \
		mkdir -p clients/go; \
		oapi-codegen -generate client,types -package raglogs -o clients/go/client.go $(OPENAPI); \
		echo "Wrote clients/go/client.go"; \
	else \
		echo "oapi-codegen not found. Install with:"; \
		echo "  go install github.com/oapi-codegen/oapi-codegen/v2/cmd/oapi-codegen@latest"; \
		echo "Then re-run: make client-go"; \
	fi

# Committed client is src/clients/v1.py. Optional generator dump is gitignored.
client-python: openapi
	@echo "Committed typed client: src/clients/v1.py (targets /v1)."
	@if command -v openapi-python-client >/dev/null 2>&1; then \
		mkdir -p clients/python/generated; \
		openapi-python-client generate --path $(OPENAPI) --output-path clients/python/generated --overwrite; \
		echo "Wrote clients/python/generated/"; \
	else \
		echo "Optional generator openapi-python-client not installed."; \
		echo "  pip install openapi-python-client"; \
		echo "Using the thin committed client instead (src/clients/v1.py)."; \
	fi

# ── Cleanup ───────────────────────────────────────────────────────────────────

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	@echo "Cleaned."
