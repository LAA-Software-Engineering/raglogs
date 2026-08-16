from src.adapters.base import SourceAdapter
from src.core.errors import AdapterUnavailableError

ADAPTER_NAMES = ("file", "cloudwatch", "datadog")


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
            api_key=settings.adapter_datadog_api_key,
            app_key=settings.adapter_datadog_app_key,
            site=settings.adapter_datadog_site,
            page_size=settings.adapter_datadog_page_size,
            max_rows=settings.adapter_datadog_max_rows,
        )
    raise AdapterUnavailableError(f"Unknown adapter: {name!r}")
