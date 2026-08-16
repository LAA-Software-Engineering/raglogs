from src.adapters.base import SourceAdapter
from src.core.errors import AdapterUnavailableError

ADAPTER_NAMES = ("file", "cloudwatch", "loki")


def get_adapter(name: str, settings) -> SourceAdapter:
    """Factory: build the named source adapter. Mirrors build_llm_provider's
    settings-driven construction (src/core/llm/provider.py)."""
    if name == "file":
        from src.adapters.file.adapter import FileSourceAdapter

        return FileSourceAdapter()
    if name == "cloudwatch":
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        return CloudWatchSourceAdapter(region=settings.adapter_cloudwatch_region)
    if name == "loki":
        from src.adapters.loki.adapter import LokiSourceAdapter

        return LokiSourceAdapter(
            base_url=settings.adapter_loki_url,
            tenant=settings.adapter_loki_tenant,
            bearer_token=settings.adapter_loki_bearer_token,
            username=settings.adapter_loki_username,
            password=settings.adapter_loki_password,
            default_query=settings.adapter_loki_query,
        )
    raise AdapterUnavailableError(f"Unknown adapter: {name!r}")
