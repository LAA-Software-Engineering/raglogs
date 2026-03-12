import re
from datetime import datetime
from typing import Optional

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from src.core.clustering.clusterer import ClusterData, run_clustering
from src.db.models import LogEntry
from src.utils.time import resolve_window
from dataclasses import dataclass


LEVEL_KEYWORDS = {
    "error": ["error", "fail", "failure", "failed", "exception", "crash", "broken"],
    "warn": ["warn", "warning", "slow", "latency", "timeout"],
    "info": ["start", "started", "deploy", "restart"],
}

SERVICE_STOP_WORDS = {
    "the", "a", "an", "in", "on", "at", "to", "is", "was", "did", "do",
    "why", "what", "when", "how", "which", "who", "service", "error",
    "fail", "failed", "not", "working",
}


def extract_keywords(question: str) -> list[str]:
    """Extract meaningful keywords from a natural language question."""
    words = re.findall(r"\b[a-zA-Z][a-zA-Z0-9_\-]*\b", question.lower())
    return [w for w in words if w not in SERVICE_STOP_WORDS and len(w) > 2]


def infer_level_bias(question: str) -> Optional[str]:
    """Infer if the question is about errors, warnings, etc."""
    q_lower = question.lower()
    for level, keywords in LEVEL_KEYWORDS.items():
        if any(kw in q_lower for kw in keywords):
            return level
    return None


def search_logs(
    db: Session,
    keywords: list[str],
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
    service: Optional[str] = None,
    level_bias: Optional[str] = None,
    limit: int = 200,
) -> list[LogEntry]:
    """Search logs by keyword matching on normalized messages."""
    q = select(LogEntry)

    conditions = []
    for kw in keywords[:5]:
        conditions.append(LogEntry.normalized_message.ilike(f"%{kw}%"))

    if conditions:
        q = q.where(or_(*conditions))

    if window_start:
        q = q.where(LogEntry.timestamp >= window_start)
    if window_end:
        q = q.where(LogEntry.timestamp <= window_end)
    if service:
        q = q.where(LogEntry.service == service)
    if level_bias:
        q = q.where(LogEntry.level == level_bias)

    q = q.order_by(LogEntry.timestamp.desc()).limit(limit)
    return db.execute(q).scalars().all()


@dataclass
class AskResult:
    question: str
    answer_text: str
    evidence_items: list[str]
    clusters_used: list[dict]
    total_matches: int


def answer_question(
    db: Session,
    question: str,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
    service: Optional[str] = None,
) -> AskResult:
    """Answer a natural language question about logs."""
    from src.config import get_settings
    from src.core.llm.provider import NoopLLMProvider, build_llm_provider

    settings = get_settings()

    keywords = extract_keywords(question)
    level_bias = infer_level_bias(question)

    # Default window: last 24h if not specified
    if window_start is None:
        from src.utils.time import resolve_window
        window_start, window_end = resolve_window(since="24h")

    # Search matching logs
    matching_logs = search_logs(
        db=db,
        keywords=keywords,
        window_start=window_start,
        window_end=window_end,
        service=service,
        level_bias=level_bias,
        limit=500,
    )

    if not matching_logs:
        return AskResult(
            question=question,
            answer_text=f"No relevant logs found for: {question}",
            evidence_items=["No matching logs found in the specified window"],
            clusters_used=[],
            total_matches=0,
        )

    # Cluster the matching logs by fingerprint
    from collections import Counter, defaultdict
    fp_groups: dict[str, list[LogEntry]] = defaultdict(list)
    for entry in matching_logs:
        if entry.fingerprint:
            fp_groups[entry.fingerprint].append(entry)

    # Sort groups by count
    sorted_groups = sorted(fp_groups.items(), key=lambda x: len(x[1]), reverse=True)

    clusters_used = []
    evidence_items = []

    for fp, entries in sorted_groups[:5]:
        count = len(entries)
        rep = entries[0].normalized_message or entries[0].raw_message or ""
        services = list(set(e.service for e in entries if e.service))
        timestamps = sorted([e.timestamp for e in entries if e.timestamp])

        first_seen = timestamps[0].strftime("%H:%M:%S") if timestamps else "unknown"
        last_seen = timestamps[-1].strftime("%H:%M:%S") if timestamps else "unknown"

        clusters_used.append({
            "message": rep[:120],
            "count": count,
            "services": services,
            "first_seen": first_seen,
            "last_seen": last_seen,
        })
        evidence_items.append(f"{count} events: '{rep[:80]}' in {', '.join(services) or 'unknown'}")

    # Build answer text
    top = clusters_used[0] if clusters_used else None
    if top:
        svc = ", ".join(top["services"]) or "unknown service"
        answer = f"Most likely cause related to '{question}':\n{top['message']}\n\nIn service: {svc}\n\nEvidence:\n"
        for item in evidence_items:
            answer += f"- {item}\n"
        answer += f"\nTotal matching log events: {len(matching_logs)}"
    else:
        answer = f"Found {len(matching_logs)} related log entries but could not identify a clear pattern."

    return AskResult(
        question=question,
        answer_text=answer,
        evidence_items=evidence_items,
        clusters_used=clusters_used,
        total_matches=len(matching_logs),
    )
