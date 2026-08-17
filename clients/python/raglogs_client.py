# Re-export of the committed typed Python client (targets /v1).
#
# Prefer: from src.clients.v1 import RaglogsClient
# Optional generated dump: make client-python → clients/python/generated/

from src.clients.v1 import RaglogsAPIError, RaglogsClient

__all__ = ["RaglogsAPIError", "RaglogsClient"]
