from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
import re

from fastapi import APIRouter, FastAPI
from fastapi.responses import ORJSONResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles

from src.api.auth.middleware import AuthMiddleware
from src.api.deprecation import DeprecationHeaderMiddleware
from src.api.routes import ask, clusters, compare_windows, config, explain, health, ingestions, timeline, ui

_OPENAPI_DESCRIPTION = """Incident explanation API — ask your logs what happened.

Canonical query, ingest, and config routes live under `/v1/`. Unversioned
`/ingestions`, `/query`, and `/config` paths are deprecated aliases for one
release and include a `Deprecation: true` header.

Compatibility: additive changes stay in `v1`; breaking changes require `v2`.
JSON response bodies are unchanged in this release.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from src.api.auth.bind_guard import warn_if_insecure_bind
    from src.config import get_settings
    from src.db.session import check_connection

    settings = get_settings()
    warn_if_insecure_bind(settings.api_bind_host, settings)

    if not check_connection():
        import structlog
        log = structlog.get_logger()
        log.warning("database_not_available", hint="Run 'raglogs init' to set up the database")
    yield


def _unique_id(mount_prefix: str) -> Callable[[APIRoute], str]:
    """Build OpenAPI operationIds that include the mount prefix (v1 vs alias)."""
    slug = mount_prefix.strip("/").replace("/", "_") or "root"

    def generate(route: APIRoute) -> str:
        raw = f"{slug}_{route.name}_{route.path_format}"
        return re.sub(r"\W", "_", raw).strip("_")

    return generate


app = FastAPI(
    title="raglogs",
    description=_OPENAPI_DESCRIPTION,
    version="0.1.0",
    default_response_class=ORJSONResponse,
    lifespan=lifespan,
)

# Last added middleware is outermost: deprecation headers apply even to auth errors.
app.add_middleware(AuthMiddleware)
app.add_middleware(DeprecationHeaderMiddleware)

app.include_router(health.router, tags=["health"])
app.include_router(ui.router, tags=["ui"])


def _include_v1_and_alias(router: APIRouter, *, suffix: str, tags: list[str]) -> None:
    """Mount a router at ``/v1{suffix}`` (canonical) and ``{suffix}`` (deprecated)."""
    app.include_router(
        router,
        prefix=f"/v1{suffix}",
        tags=tags,
        generate_unique_id_function=_unique_id(f"v1{suffix}"),
    )
    app.include_router(
        router,
        prefix=suffix,
        tags=tags,
        deprecated=True,
        generate_unique_id_function=_unique_id(f"legacy{suffix}"),
    )


_include_v1_and_alias(ingestions.router, suffix="/ingestions", tags=["ingestions"])
_include_v1_and_alias(explain.router, suffix="/query", tags=["query"])
_include_v1_and_alias(ask.router, suffix="/query", tags=["query"])
_include_v1_and_alias(clusters.router, suffix="/query", tags=["query"])
_include_v1_and_alias(timeline.router, suffix="/query", tags=["query"])
_include_v1_and_alias(compare_windows.router, suffix="/query", tags=["query"])
_include_v1_and_alias(config.router, suffix="/config", tags=["config"])

app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
    name="static",
)
