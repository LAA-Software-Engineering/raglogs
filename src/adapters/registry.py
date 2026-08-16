from src.adapters.base import SourceAdapter
from src.core.errors import AdapterUnavailableError

ADAPTER_NAMES = ("file", "cloudwatch")


def get_adapter(name: str, settings) -> SourceAdapter:
    """Factory: build the named source adapter. Mirrors build_llm_provider's
    settings-driven construction (src/core/llm/provider.py)."""
    if name == "file":
        from src.adapters.file.adapter import FileSourceAdapter

        return FileSourceAdapter()
    if name == "cloudwatch":
        from src.adapters.cloudwatch.adapter import CloudWatchSourceAdapter

        return CloudWatchSourceAdapter(region=settings.adapter_cloudwatch_region)
    raise AdapterUnavailableError(f"Unknown adapter: {name!r}")
