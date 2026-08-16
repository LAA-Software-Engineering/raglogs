"""
Natural language question answering over ingested logs.

Pipeline:
  1. Extract keywords + infer level bias from the question
  2. Search logs by keyword match on normalized_message
  3. If keyword search yields nothing, fall back to the top clusters in the
     window — the question may use terms not literally present in log text
     (e.g. "why did login fail?" when the logs say "auth token invalid")
  4. Assemble an evidence dict from matching clusters
  5. Call the LLM provider with that evidence (if configured)
  6. Fall back to a deterministic text answer when LLM is disabled
"""
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from src.db.models import LogEntry


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


def extract_keywords(question: str) -> list[str]:
    words = re.findall(r"\b[a-zA-Z][a-zA-Z0-9_\-]*\b", question.lower())
    return [w for w in words if w not in SERVICE_STOP_WORDS and len(w) > 2]


def infer_level_bias(question: str) -> Optional[str]:
    q = question.lower()
    for level, kws in LEVEL_KEYWORDS.items():
        if any(kw in q for kw in kws):
            return level
    return None


def search_logs(
    db: Session,
    keywords: list[str],
    window_start: Optional[datetime],
    window_end: Optional[datetime],
    service: Optional[str],
    level_bias: Optional[str],
    limit: int = 500,
    ingestion_job_id: Optional[uuid.UUID] = None,
) -> list[LogEntry]:
    q = select(LogEntry)

    if keywords:
        conditions = [LogEntry.normalized_message.ilike(f"%{kw}%") for kw in keywords[:6]]
        q = q.where(or_(*conditions))

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

    q = q.order_by(LogEntry.timestamp.desc()).limit(limit)
    return list(db.execute(q).scalars().all())


def cluster_logs(entries: list[LogEntry]) -> list[dict]:
    """Group log entries by fingerprint, return top clusters sorted by count."""
    groups: dict[str, list[LogEntry]] = defaultdict(list)
    for entry in entries:
        key = entry.fingerprint or entry.normalized_message or entry.raw_message or ""
        groups[key].append(entry)

    clusters = []
    for fp, group in sorted(groups.items(), key=lambda x: len(x[1]), reverse=True)[:8]:
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
        f"",
        f"Observed {top['count']} times in {svc}.",
        f"",
        f"Supporting evidence:",
    ]
    for c in clusters:
        svc_label = ", ".join(c["services"]) if c["services"] else "unknown"
        lines.append(f"- {c['count']}x '{c['message'][:80]}' in {svc_label}")
    lines.append(f"")
    lines.append(f"Total matching log events: {total}")
    return "\n".join(lines)


def answer_question(
    db: Session,
    question: str,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
    service: Optional[str] = None,
    ingestion_job_id: Optional[uuid.UUID] = None,
) -> AskResult:
    from src.config import get_settings
    from src.core.llm.provider import NoopLLMProvider, build_llm_provider
    from src.utils.time import resolve_window

    settings = get_settings()

    if window_start is None:
        window_start, window_end = resolve_window(since="24h")

    keywords = extract_keywords(question)
    level_bias = infer_level_bias(question)

    # 1. Keyword search
    matching = search_logs(
        db, keywords, window_start, window_end, service, level_bias,
        ingestion_job_id=ingestion_job_id,
    )

    # 2. Fallback: if nothing matched, use all error/warn logs in the window.
    #    The user's terminology may not match log text literally.
    used_fallback = False
    if not matching:
        matching = fetch_fallback_clusters(
            db, window_start, window_end, service, level_bias,
            ingestion_job_id=ingestion_job_id,
        )
        used_fallback = True

    if not matching:
        return AskResult(
            question=question,
            answer_text=f"No log activity found in the requested window. "
                        f"Try a wider time range with --since.",
            evidence_items=["No logs found in window"],
            clusters_used=[],
            total_matches=0,
        )

    clusters = cluster_logs(matching)
    evidence_items = [
        f"{c['count']} events: '{c['message'][:80]}' in "
        f"{', '.join(c['services']) or 'unknown'}"
        for c in clusters
    ]

    evidence_packet = {
        "question": question,
        "window": {
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
        },
        "note": (
            "Keyword search found no direct matches; showing most significant "
            "error/warn patterns in the window." if used_fallback else None
        ),
        "clusters": clusters,
        "total_matching_logs": len(matching),
    }

    # 3. LLM answer
    mode = "rules"
    answer_text = ""

    llm = build_llm_provider(settings)
    if not isinstance(llm, NoopLLMProvider):
        try:
            import json
            import httpx
            user_msg = (
                f"Question: {question}\n\n"
                f"Log evidence:\n{json.dumps(evidence_packet, default=str, indent=2)}"
            )
            # Re-use the provider's HTTP client but with the ask-specific system prompt
            # by calling the provider directly with a custom prompt structure
            answer_text = _call_llm_ask(llm, question, evidence_packet)
            if answer_text:
                mode = "llm"
        except Exception:
            pass  # degrade to rules

    if not answer_text:
        answer_text = _rules_answer(question, clusters, len(matching))

    return AskResult(
        question=question,
        answer_text=answer_text,
        evidence_items=evidence_items,
        clusters_used=clusters,
        total_matches=len(matching),
        mode=mode,
    )


def _call_llm_ask(llm, question: str, evidence_packet: dict) -> str:
    """
    Call the LLM provider with the ask-specific system prompt.
    Constructs the HTTP call directly rather than reusing generate_summary,
    which uses the incident-summary system prompt.
    """
    import json
    import httpx
    from src.core.llm.provider import OpenAILLMProvider, OllamaLLMProvider

    payload_str = json.dumps(evidence_packet, default=str, indent=2)
    user_message = f"Question: {question}\n\nLog evidence:\n{payload_str}"

    if isinstance(llm, OpenAILLMProvider):
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                f"{llm.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {llm.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": llm.model,
                    "max_tokens": 400,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": ASK_SYSTEM_PROMPT},
                        {"role": "user", "content": user_message},
                    ],
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()

    elif isinstance(llm, OllamaLLMProvider):
        prompt = f"{ASK_SYSTEM_PROMPT}\n\n{user_message}\n\nAnswer:"
        with httpx.Client(timeout=120) as client:
            resp = client.post(
                f"{llm.base_url}/api/generate",
                json={
                    "model": llm.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0},
                },
            )
            resp.raise_for_status()
            return resp.json().get("response", "").strip()

    return ""
