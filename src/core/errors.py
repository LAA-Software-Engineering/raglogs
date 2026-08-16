class RaglogsError(Exception):
    """Base class for typed raglogs errors."""
    error_code: str = "RAGLOGS_ERROR"


class AdapterUnavailableError(RaglogsError):
    """A source adapter could not be reached or is missing required credentials."""
    error_code = "ADAPTER_UNAVAILABLE"
