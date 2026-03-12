from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    db: str


@router.get("/health", response_model=HealthResponse)
def health_check():
    from src.db.session import check_connection
    db_ok = check_connection()
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        db="connected" if db_ok else "disconnected",
    )
