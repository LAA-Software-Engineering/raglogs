from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import ORJSONResponse

from src.api.routes import ask, clusters, compare_windows, config, explain, health, ingestions, timeline


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: check DB
    from src.db.session import check_connection
    if not check_connection():
        import structlog
        log = structlog.get_logger()
        log.warning("database_not_available", hint="Run 'raglogs init' to set up the database")
    yield


app = FastAPI(
    title="raglogs",
    description="Incident explanation API — ask your logs what happened.",
    version="0.1.0",
    default_response_class=ORJSONResponse,
    lifespan=lifespan,
)

app.include_router(health.router, tags=["health"])
app.include_router(ingestions.router, prefix="/ingestions", tags=["ingestions"])
app.include_router(explain.router, prefix="/query", tags=["query"])
app.include_router(ask.router, prefix="/query", tags=["query"])
app.include_router(clusters.router, prefix="/query", tags=["query"])
app.include_router(timeline.router, prefix="/query", tags=["query"])
app.include_router(compare_windows.router, prefix="/query", tags=["query"])
app.include_router(config.router, prefix="/config", tags=["config"])
