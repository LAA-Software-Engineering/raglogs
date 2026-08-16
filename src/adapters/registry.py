from src.adapters.base import SourceAdapter
from src.core.errors import AdapterUnavailableError

ADAPTER_NAMES = ("file", "cloudwatch", "datadog", "loki", "k8s")


def get_adapter(name: str, settings) -> SourceAdapter:
    """Factory: build the named source adapter. Mirrors build_llm_provider's
    settings-driven construction (src/core/llm/provider.py)."""
    if name == "file":
        from src.adapters.file.adapter import FileSourceAdapter

        return FileSourceAdapter()
    if name == "cloudwatch":
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        return CloudWatchSourceAdapter(region=settings.adapter_cloudwatch_region)
    if name == "datadog":
        from src.adapters.datadog.adapter import DatadogSourceAdapter

        return DatadogSourceAdapter(
            api_key=settings.datadog_api_key,
            app_key=settings.datadog_app_key,
            site=settings.datadog_site,
            page_size=settings.datadog_page_size,
            max_rows=settings.datadog_max_rows,
        )
    if name == "loki":
        from src.adapters.loki.adapter import LokiSourceAdapter

        return LokiSourceAdapter(
            base_url=settings.loki_url,
            tenant=settings.loki_tenant,
            bearer_token=settings.loki_bearer_token,
            username=settings.loki_username,
            password=settings.loki_password,
            default_query=settings.loki_query,
        )
    if name in ("k8s", "kubernetes"):
        from src.adapters.k8s.adapter import KubernetesExportAdapter

        return KubernetesExportAdapter()
    raise AdapterUnavailableError(f"Unknown adapter: {name!r}")
