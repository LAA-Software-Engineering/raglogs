FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]"

COPY . .

EXPOSE 8000

# Default: run the API. Override CMD for the worker.
CMD ["sh", "-c", "alembic upgrade head && uvicorn src.api.app:app --host 0.0.0.0 --port 8000"]
