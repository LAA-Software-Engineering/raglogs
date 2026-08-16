from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/raglogs"

    embeddings_provider: Literal["disabled", "openai", "local"] = "disabled"
    embeddings_model: str = "text-embedding-3-small"
    embeddings_dimensions: int = 1536

    llm_provider: Literal["disabled", "openai", "ollama"] = "disabled"
    llm_model: str = "gpt-4.1-mini"

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    ollama_base_url: str = "http://localhost:11434"

    default_baseline_window: str = "24h"
    max_evidence_items: int = 8
    max_clusters_for_explain: int = 10

    # Worker
    worker_poll_interval: int = 2          # seconds between idle polls

    # Adapters
    adapter_cloudwatch_region: str = "us-east-1"
    datadog_api_key: str = ""
    datadog_app_key: str = ""
    datadog_site: str = "datadoghq.com"
    datadog_page_size: int = 1000
    datadog_max_rows: int = 10000

    # Cluster scoring weights
    severity_weight_fatal: float = 5.0
    severity_weight_error: float = 4.0
    severity_weight_warn: float = 3.0
    severity_weight_info: float = 1.0
    severity_weight_debug: float = 0.5


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    global _settings
    _settings = Settings()
    return _settings
