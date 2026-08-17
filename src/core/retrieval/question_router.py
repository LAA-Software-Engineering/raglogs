"""
Natural language question answering over ingested logs.

Pipeline:
  1. Extract keywords + infer level bias from the question
  2. If an embeddings provider is available, embed the question and retrieve
     nearest log lines from pgvector (cosine similarity)
  3. If semantic search is disabled, errors, or returns nothing, search logs
     by keyword match on normalized_message
  4. If keyword search yields nothing, fall back to the top error/warn clusters
     in the window — the question may use terms not literally present in log text
     (e.g. "why did login fail?" when the logs say "auth token invalid")
  5. Assemble an evidence dict from matching clusters
  6. Call the LLM provider with that evidence (if configured)
  7. Fall back to a deterministic text answer when LLM is disabled
"""
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import structlog
from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from src.config.settings import Settings
from src.core.embeddings.provider import get_embeddings_provider
from src.core.embeddings.store import STORED_EMBEDDING_DIMS
from src.db.models import DEFAULT_LOG_SCOPE, LogEmbedding, LogEntry
from src.db.scope_filter import filter_log_entries_by_scope

log = structlog.get_logger()


LEVEL_KEYWORDS = {
    "error":  ["error", "fail", "failure", "failed", "exception", "crash", "broken",
                "unavailable", "refused", "denied", "invalid", "unauthorized"],
    "warn":   ["warn", "warning", "slow", "latency", "timeout", "degraded"],
    "info":   ["start", "started", "deploy", "restart", "launch"],
}

SERVICE_STOP_WORDS = {
    "the", "a", "an", "in", "on", "at", "to", "is", "was", "did", "do",
    "why", "what", "when", "how", "which", "who", "service", "error",
    "fail", "failed", "not", "working", "happening", "going", "causing",
    "there", "are", "any", "all", "and", "or", "but", "for", "with",
    "my", "our", "your",
}

ASK_SYSTEM_PROMPT = """\
You are a site reliability engineer analyzing production logs to answer a question.
Use ONLY the supplied evidence. Do not invent causes or events not in the evidence.

Answer the question directly in 2-4 sentences. Then list the key supporting evidence
as bullet points. If the evidence does not answer the question, say so clearly.

Keep the entire response under 200 words. No markdown formatting, no headers."""


@dataclass
class AskResult:
    question: str
    answer_text: str
    evidence_items: list[str]
    clusters_used: list[dict]
    total_matches: int
    mode: str = "rules"  # "rules" | "llm"
    retrieval_mode: str = "keyword"  # "semantic" | "keyword" | "fallback"


def extract_keywords(question: str) -> list[str]:
    words = re.findall(r"\b[a-zA-Z][a-zA-Z0-9_\-]*\b", question.lower())
    return [w for w in words if w not in SERVICE_STOP_WORDS and len(w) > 2]


def infer_level_bias(question: str) -> Optional[str]:
    q = question.lower()
    for level, kws in LEVEL_KEYWORDS.items():
        if any(kw in q for kw in kws):
            return level
    return None


def _filter_log_entries(
    q: Select,
    window_start: Optional[datetime],
    window_end: Optional[datetime],
    service: Optional[str],
    level_bias: Optional[str],
    ingestion_job_id: Optional[uuid.UUID],
    scope: str = DEFAULT_LOG_SCOPE,
) -> Select:
    """Apply the shared window / service / level / job / scope filters used by ask search."""
    q = filter_log_entries_by_scope(q, scope)
    if window_start:
        q = q.where(LogEntry.timestamp >= window_start)
    if window_end:
        q = q.where(LogEntry.timestamp <= window_end)
    if service:
        q = q.where(LogEntry.service == service)
    if level_bias:
        q = q.where(LogEntry.level == level_bias)
    if ingestion_job_id:
        q = q.where(LogEntry.ingestion_job_id == ingestion_job_id)
    return q


def search_logs(
    db: Session,
    keywords: list[str],
    window_start: Optional[datetime],
    window_end: Optional[datetime],
    service: Optional[str],
    level_bias: Optional[str],
    limit: int = 500,
    ingestion_job_id: Optional[uuid.UUID] = None,
    scope: str = DEFAULT_LOG_SCOPE,
) -> list[LogEntry]:
    q = select(LogEntry)

    if keywords:
        conditions = [LogEntry.normalized_message.ilike(f"%{kw}%") for kw in keywords[:6]]
        q = q.where(or_(*conditions))

    q = _filter_log_entries(
        q, window_start, window_end, service, level_bias, ingestion_job_id, scope=scope
    )
    q = q.order_by(LogEntry.timestamp.desc()).limit(limit)
    return list(db.execute(q).scalars().all())


def search_logs_semantic(
    db: Session,
    query_vector: list[float],
    window_start: Optional[datetime],
    window_end: Optional[datetime],
    service: Optional[str],
    level_bias: Optional[str],
    limit: int = 100,
    ingestion_job_id: Optional[uuid.UUID] = None,
    min_similarity: float = 0.75,
    scope: str = DEFAULT_LOG_SCOPE,
) -> list[LogEntry]:
    """Nearest-neighbor log lines via pgvector cosine similarity.

    ``query_vector`` is bound as a parameter (never string-interpolated).
    Hits below ``min_similarity`` (``1 - cosine_distance``) are excluded.
    """
    if not query_vector or len(query_vector) != STORED_EMBEDDING_DIMS:
        return []

    distance = LogEmbedding.embedding.cosine_distance(query_vector)
    similarity = 1 - distance
    q = (
        select(LogEntry)
        .join(LogEmbedding, LogEmbedding.log_entry_id == LogEntry.id)
        .where(similarity >= min_similarity)
    )
    q = _filter_log_entries(
        q, window_start, window_end, service, level_bias, ingestion_job_id, scope=scope
    )
    q = q.order_by(distance.asc()).limit(limit)
    return list(db.execute(q).scalars().all())


def cluster_logs(entries: list[LogEntry], max_clusters: int = 8) -> list[dict]:
    """Group log entries by fingerprint, return top clusters sorted by count."""
    groups: dict[str, list[LogEntry]] = defaultdict(list)
    for entry in entries:
        key = entry.fingerprint or entry.normalized_message or entry.raw_message or ""
        groups[key].append(entry)

    clusters = []
    for fp, group in sorted(groups.items(), key=lambda x: len(x[1]), reverse=True)[:max_clusters]:
        timestamps = sorted(e.timestamp for e in group if e.timestamp)
        rep = group[0].normalized_message or group[0].raw_message or ""
        services = sorted(set(e.service for e in group if e.service))
        clusters.append({
            "message": rep[:150],
            "count": len(group),
            "services": services,
            "first_seen": timestamps[0].isoformat() if timestamps else None,
            "last_seen": timestamps[-1].isoformat() if timestamps else None,
            "level": group[0].level,
        })
    return clusters


def fetch_fallback_clusters(
    db: Session,
    window_start: datetime,
    window_end: datetime,
    service: Optional[str],
    level_bias: Optional[str],
    ingestion_job_id: Optional[uuid.UUID] = None,
    scope: str = DEFAULT_LOG_SCOPE,
) -> list[LogEntry]:
    """
    Fallback: fetch the most significant error/warn logs from the window
    when keyword search returns nothing. The user's question may use domain
    terms that don't literally appear in log text.
    """
    q = (
        select(LogEntry)
        .where(LogEntry.timestamp >= window_start, LogEntry.timestamp <= window_end)
        .where(LogEntry.level.in_(["error", "fatal", "warn", "critical"]))
    )
    q = filter_log_entries_by_scope(q, scope)
    if service:
        q = q.where(LogEntry.service == service)
    if ingestion_job_id:
        q = q.where(LogEntry.ingestion_job_id == ingestion_job_id)

    q = q.order_by(LogEntry.timestamp.desc()).limit(500)
    return list(db.execute(q).scalars().all())


def _rules_answer(question: str, clusters: list[dict], total: int) -> str:
    """Deterministic answer when LLM is disabled."""
    if not clusters:
        return f"No relevant log patterns found for: {question}"

    top = clusters[0]
    svc = ", ".join(top["services"]) if top["services"] else "unknown service"
    lines = [
        f"The most likely related issue: {top['message']}",
        "",
        f"Observed {top['count']} times in {svc}.",
        "",
        "Supporting evidence:",
    ]
    for c in clusters:
        svc_label = ", ".join(c["services"]) if c["services"] else "unknown"
        lines.append(f"- {c['count']}x '{c['message'][:80]}' in {svc_label}")
    lines.append("")
    lines.append(f"Total matching log events: {total}")
    return "\n".join(lines)


def answer_question(
    db: Session,
    question: str,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
    service: Optional[str] = None,
    ingestion_job_id: Optional[uuid.UUID] = None,
    scope: str = DEFAULT_LOG_SCOPE,
    no_llm: bool = False,
    max_clusters: Optional[int] = None,
    max_evidence_items: Optional[int] = None,
    llm_provider: Optional[str] = None,
) -> AskResult:
    from src.config import get_settings
    from src.core.llm.provider import (
        NoopLLMProvider,
        build_llm_provider,
        unwrap_llm_provider,
    )
    from src.utils.time import resolve_window

    settings = get_settings()
    if llm_provider is not None or max_evidence_items is not None:
        update: dict[str, object] = {}
        if llm_provider is not None:
            update["llm_provider"] = llm_provider
        if max_evidence_items is not None:
            update["max_evidence_items"] = max_evidence_items
        settings = settings.model_copy(update=update)

    if window_start is None:
        window_start, window_end = resolve_window(since="24h")

    keywords = extract_keywords(question)
    level_bias = infer_level_bias(question)

    matching, retrieval_mode = _retrieve_matching_logs(
        db,
        question=question,
        keywords=keywords,
        window_start=window_start,
        window_end=window_end,
        service=service,
        level_bias=level_bias,
        ingestion_job_id=ingestion_job_id,
        settings=settings,
        scope=scope,
    )

    if not matching:
        return AskResult(
            question=question,
            answer_text=(
                "No log activity found in the requested window. "
                "Try a wider time range with --since."
            ),
            evidence_items=["No logs found in window"],
            clusters_used=[],
            total_matches=0,
            retrieval_mode=retrieval_mode,
        )

    cluster_cap = max_clusters if max_clusters is not None else 8
    clusters = cluster_logs(matching, max_clusters=cluster_cap)
    evidence_items = [
        f"{c['count']} events: '{c['message'][:80]}' in "
        f"{', '.join(c['services']) or 'unknown'}"
        for c in clusters
    ]
    if max_evidence_items is not None:
        evidence_items = evidence_items[:max_evidence_items]

    evidence_packet = {
        "question": question,
        "window": {
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
        },
        "note": (
            "Keyword search found no direct matches; showing most significant "
            "error/warn patterns in the window." if retrieval_mode == "fallback" else None
        ),
        "retrieval_mode": retrieval_mode,
        "clusters": clusters,
        "total_matching_logs": len(matching),
    }

    # 3. LLM answer
    mode = "rules"
    answer_text = ""

    llm_requested = (not no_llm) and settings.llm_provider != "disabled"
    if llm_requested:
        llm = build_llm_provider(settings)
        if isinstance(unwrap_llm_provider(llm), NoopLLMProvider):
            llm_requested = False
        else:
            try:
                answer_text = _call_llm_ask(llm, question, evidence_packet, settings=settings)
                if answer_text:
                    mode = "llm"
            except Exception:
                log.warning("llm_ask_failed", exc_info=True)

    if llm_requested and mode != "llm":
        from src.observability.metrics import record_llm_fallback

        record_llm_fallback()

    if not answer_text:
        answer_text = _rules_answer(question, clusters, len(matching))

    return AskResult(
        question=question,
        answer_text=answer_text,
        evidence_items=evidence_items,
        clusters_used=clusters,
        total_matches=len(matching),
        mode=mode,
        retrieval_mode=retrieval_mode,
    )


def _retrieve_matching_logs(
    db: Session,
    *,
    question: str,
    keywords: list[str],
    window_start: Optional[datetime],
    window_end: Optional[datetime],
    service: Optional[str],
    level_bias: Optional[str],
    ingestion_job_id: Optional[uuid.UUID],
    settings: Settings,
    scope: str = DEFAULT_LOG_SCOPE,
) -> tuple[list[LogEntry], str]:
    """Semantic-first retrieval, then keyword, then error/warn fallback.

    Returns ``(entries, retrieval_mode)`` where mode is semantic, keyword,
    or fallback. Provider errors and empty semantic hits fall through; they
    never raise to the caller.
    """
    provider = get_embeddings_provider(settings)
    if provider.is_available():
        try:
            vectors = provider.embed_texts([question])
            query_vector = vectors[0] if vectors else []
            if query_vector:
                semantic_hits = search_logs_semantic(
                    db,
                    query_vector,
                    window_start,
                    window_end,
                    service,
                    level_bias,
                    limit=settings.ask_semantic_top_k,
                    ingestion_job_id=ingestion_job_id,
                    min_similarity=settings.ask_semantic_min_similarity,
                    scope=scope,
                )
                if semantic_hits:
                    return semantic_hits, "semantic"
        except Exception:
            log.warning("ask_semantic_retrieval_failed", exc_info=True)

    matching = search_logs(
        db, keywords, window_start, window_end, service, level_bias,
        ingestion_job_id=ingestion_job_id,
        scope=scope,
    )
    if matching:
        return matching, "keyword"

    matching = fetch_fallback_clusters(
        db, window_start, window_end, service, level_bias,
        ingestion_job_id=ingestion_job_id,
        scope=scope,
    )
    return matching, "fallback"


def _call_llm_ask(
    llm: object,
    question: str,
    evidence_packet: dict,
    settings: Optional[Settings] = None,
) -> str:
    """
    Call the LLM provider with the ask-specific system prompt.
    Constructs the HTTP call directly rather than reusing generate_summary,
    which uses the incident-summary system prompt. Shares the G9 LLM
    concurrency semaphore with generate_summary.

    Order matches generate_summary: cap (slot) → breaker/retries (invoke_llm)
    → single HTTP attempt on the unwrapped OpenAI/Ollama/Claude provider.
    """
    import json

    from src.config import get_settings
    from src.core.llm.provider import (
        ClaudeLLMProvider,
        NoopLLMProvider,
        OpenAILLMProvider,
        OllamaLLMProvider,
        llm_concurrency_slot,
        unwrap_llm_provider,
    )
    from src.core.llm.resilience import estimate_tokens, invoke_llm, prepare_llm_packet
    from src.observability.metrics import record_llm_estimated_tokens

    inner = unwrap_llm_provider(llm)  # type: ignore[arg-type]
    resolved = settings if settings is not None else get_settings()
    prepared = prepare_llm_packet(evidence_packet, resolved)
    record_llm_estimated_tokens(estimate_tokens(prepared))
    payload_str = json.dumps(prepared, default=str, indent=2)
    user_message = f"Question: {question}\n\nLog evidence:\n{payload_str}"

    def _attempt() -> str:
        if isinstance(inner, OpenAILLMProvider):
            return inner.complete(ASK_SYSTEM_PROMPT, user_message)
        if isinstance(inner, ClaudeLLMProvider):
            return inner.complete(ASK_SYSTEM_PROMPT, user_message)
        if isinstance(inner, OllamaLLMProvider):
            return inner.complete(ASK_SYSTEM_PROMPT, f"{user_message}\n\nAnswer:")
        return ""

    with llm_concurrency_slot(skip=isinstance(inner, NoopLLMProvider)):
        if isinstance(inner, (OpenAILLMProvider, OllamaLLMProvider, ClaudeLLMProvider)):
            return invoke_llm(_attempt)

    return ""
