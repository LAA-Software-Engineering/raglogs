from fastapi import APIRouter

router = APIRouter()


@router.get("")
def get_config():
    from src.config import get_settings
    settings = get_settings()
    return {
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "embeddings_provider": settings.embeddings_provider,
        "embeddings_model": settings.embeddings_model,
        "default_baseline_window": settings.default_baseline_window,
        "max_clusters_for_explain": settings.max_clusters_for_explain,
        "max_evidence_items": settings.max_evidence_items,
    }
