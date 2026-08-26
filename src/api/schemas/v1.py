"""Versioned JSON evidence schema for ``/v1/query/*`` (G7, schema_version 1.0).

Pydantic models here are the HTTP contract. CLI still uses ``ExplainResult``
string confidence and ``summary_text``; this module only shapes JSON bodies.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from fastapi import Request
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from src.core.explain.confidence import score_from_label
from src.core.normalization.patterns import infer_trigger_type
from src.utils.time import rewrite_iso_z

if TYPE_CHECKING:
    from src.core.explain.summarizer import ExplainResult

SCHEMA_VERSION = "1.0"

COMPARE_MARKERS: dict[str, str] = {
    "new": "+",
    "disappeared": "-",
    "increased": "↑",
    "decreased": "↓",
}
TRIGGER_MARKERS: dict[str, str] = {
    "a": "+⚡",
    "b": "-⚡",
}


class TimeWindow(BaseModel):
    """Inclusive analysis window. Serialized as ``from`` / ``to`` (ISO-8601)."""

    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(
        validation_alias=AliasChoices("from", "from_", "start"),
        serialization_alias="from",
    )
    to: str = Field(
        validation_alias=AliasChoices("to", "end"), serialization_alias="to"
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_start_end(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if "from" not in out and "from_" not in out and "start" in out:
            out["from"] = out["start"]
        if "to" not in out and "end" in out:
            out["to"] = out["end"]
        return out


class Confidence(BaseModel):
    label: str
    score: float = Field(ge=0.0, le=1.0)


class TriggerInfo(BaseModel):
    detected: bool
    type: Optional[str] = None
    service: Optional[str] = None
    at: Optional[str] = None
    correlation: Optional[str] = None


class ClusterEvidence(BaseModel):
    fingerprint: Optional[str] = None
    template: Optional[str] = None
    count: Optional[int] = None
    baseline_count: Optional[int] = None
    change_ratio: Optional[float] = None
    services: list[str] = Field(default_factory=list)
    levels: list[str] = Field(default_factory=list)
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    # Additive alias so existing UIs that read ``message`` keep working.
    message: Optional[str] = None


class EvidenceItem(BaseModel):
    kind: str
    detail: str
    ref: Optional[str] = None
    source_ref: Optional[str] = None


class LlmProvenance(BaseModel):
    used: bool
    provider: str
    model: str
    fell_back: bool


class ExplainResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: str = SCHEMA_VERSION
    scope: str = "default"
    window: TimeWindow
    confidence: Confidence
    summary: str
    trigger: TriggerInfo
    primary_cluster: Optional[ClusterEvidence] = None
    secondary_clusters: list[ClusterEvidence] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    llm: LlmProvenance
    rendered_text: str = ""
    markdown: Optional[str] = None
    cached: bool = False
    total_logs: int = 0
    mode: str = "rules"
    services_affected: list[str] = Field(default_factory=list)


class TimelineEventModel(BaseModel):
    timestamp: str
    category: str
    label: str
    description: str
    count: Optional[int] = None
    services: list[str] = Field(default_factory=list)
    duration_minutes: Optional[float] = None


class TimelineResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: str = SCHEMA_VERSION
    scope: str = "default"
    window: TimeWindow
    events: list[TimelineEventModel] = Field(default_factory=list)
    llm: LlmProvenance
    rendered_text: Optional[str] = None
    text: Optional[str] = None
    ingestion_job_id: Optional[str] = None
    all_ingestions: bool = False


class ClusterDiffModel(BaseModel):
    fingerprint: str
    message: str
    services: list[str] = Field(default_factory=list)
    count_a: Optional[int] = None
    count_b: Optional[int] = None
    marker: str


class TriggerDiffModel(BaseModel):
    message: str
    service: Optional[str] = None
    only_in: str
    marker: str


class CompareResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: str = SCHEMA_VERSION
    scope: str = "default"
    window_a: TimeWindow
    window_b: TimeWindow
    has_changes: bool
    new_clusters: list[ClusterDiffModel] = Field(default_factory=list)
    disappeared_clusters: list[ClusterDiffModel] = Field(default_factory=list)
    increased_clusters: list[ClusterDiffModel] = Field(default_factory=list)
    decreased_clusters: list[ClusterDiffModel] = Field(default_factory=list)
    new_triggers: list[TriggerDiffModel] = Field(default_factory=list)
    dropped_triggers: list[TriggerDiffModel] = Field(default_factory=list)
    llm: LlmProvenance
    rendered_text: Optional[str] = None
    text: Optional[str] = None


class AskResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    question: str
    answer: str
    evidence: list[str] = Field(default_factory=list)
    clusters: list[dict[str, Any]] = Field(default_factory=list)
    total_matches: int = 0
    retrieval_mode: str = "keyword"
    llm: LlmProvenance
    rendered_text: Optional[str] = None
    mode: Optional[str] = None


class ClustersResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    scope: str = "default"
    window: TimeWindow
    clusters: list[dict[str, Any]] = Field(default_factory=list)
    llm: LlmProvenance
    ingestion_job_id: Optional[str] = None


class SimilarQueryCluster(BaseModel):
    fingerprint: str
    template: Optional[str] = None


class SimilarMatchModel(BaseModel):
    scope: str
    fingerprint: str
    template: Optional[str] = None
    similarity: float
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    count: int = 0


class SimilarResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: str = SCHEMA_VERSION
    scope: str = "default"
    window: TimeWindow
    query_clusters: list[SimilarQueryCluster] = Field(default_factory=list)
    matches: list[SimilarMatchModel] = Field(default_factory=list)
    retrieval_mode: str = "fingerprint"
    llm: LlmProvenance
    rendered_text: Optional[str] = None


def scope_from_request(http_request: Optional[Request] = None) -> str:
    """Resolved G8 scope when ``bind_request_scope`` ran; else principal or ``default``."""
    if http_request is None:
        return "default"
    resolved = getattr(http_request.state, "resolved_scope", None)
    if isinstance(resolved, str) and resolved.strip():
        return resolved.strip()
    principal = getattr(http_request.state, "auth_principal", None)
    if principal is None:
        return "default"
    scope = getattr(principal, "scope", None)
    return str(scope) if scope else "default"


def build_llm_provenance(
    *,
    used: bool,
    fell_back: bool,
    provider: Optional[str] = None,
) -> LlmProvenance:
    from src.config import get_settings

    settings = get_settings()
    return LlmProvenance(
        used=used,
        provider=provider or settings.llm_provider,
        model=settings.llm_model,
        fell_back=fell_back,
    )


def llm_from_mode(
    *,
    mode: str,
    requested: bool,
    provider: Optional[str] = None,
) -> LlmProvenance:
    used = mode == "llm"
    fell_back = requested and not used
    return build_llm_provenance(used=used, fell_back=fell_back, provider=provider)


def llm_from_overrides(*, mode: str, llm_provider: str, llm_enabled: bool) -> LlmProvenance:
    requested = llm_enabled and llm_provider != "disabled"
    return llm_from_mode(mode=mode, requested=requested, provider=llm_provider)


def llm_rules_only() -> LlmProvenance:
    return build_llm_provenance(used=False, fell_back=False)


def llm_requested(
    *,
    no_llm: bool = False,
    llm_provider: Optional[str] = None,
) -> bool:
    from src.config import get_settings

    settings = get_settings()
    provider = llm_provider if llm_provider is not None else settings.llm_provider
    return (not no_llm) and provider != "disabled"


def window_from_bounds(start: datetime, end: datetime) -> TimeWindow:
    return TimeWindow(from_=start.isoformat(), to=end.isoformat())


def window_from_mapping(
    raw: Any, fallback_start: str = "", fallback_end: str = ""
) -> TimeWindow:
    if isinstance(raw, TimeWindow):
        return raw
    if not isinstance(raw, dict):
        return TimeWindow(from_=fallback_start, to=fallback_end)
    start = raw.get("from") or raw.get("from_") or raw.get("start") or fallback_start
    end = raw.get("to") or raw.get("end") or fallback_end
    return TimeWindow(from_=str(start), to=str(end))


def short_summary(summary_text: str) -> str:
    """First useful sentence/paragraph; full prose stays on ``rendered_text``."""
    text = (summary_text or "").strip()
    if not text:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("primary issue:"):
            rest = stripped.split(":", 1)[1].strip()
            if rest:
                return rest
    skip = {"incident summary", "evidence:"}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower() in skip:
            continue
        if stripped.lower().startswith("window:"):
            continue
        return stripped
    return text.split("\n", 1)[0]


def _levels_list(raw: Any) -> list[str]:
    if isinstance(raw, dict):
        return [str(k) for k in raw.keys()]
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return []


def _services_list(raw: Any) -> list[str]:
    if isinstance(raw, dict):
        return [str(k) for k in raw.keys()]
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return []


def cluster_from_mapping(raw: Optional[dict[str, Any]]) -> Optional[ClusterEvidence]:
    if not raw:
        return None
    template = (
        raw.get("template") or raw.get("message") or raw.get("representative_message")
    )
    template_str = str(template) if template is not None else None
    return ClusterEvidence(
        fingerprint=raw.get("fingerprint"),
        template=template_str,
        message=template_str,
        count=raw.get("count"),
        baseline_count=raw.get("baseline_count"),
        change_ratio=raw.get("change_ratio"),
        services=_services_list(raw.get("services")),
        levels=_levels_list(raw.get("levels")),
        first_seen=raw.get("first_seen"),
        last_seen=raw.get("last_seen"),
    )


def evidence_from_items(items: Any) -> list[EvidenceItem]:
    out: list[EvidenceItem] = []
    for item in items or []:
        if isinstance(item, EvidenceItem):
            out.append(item)
        elif isinstance(item, dict) and item.get("detail") is not None:
            out.append(
                EvidenceItem(
                    kind=str(item.get("kind") or "log"),
                    detail=str(item["detail"]),
                    ref=item.get("ref"),
                    source_ref=item.get("source_ref"),
                )
            )
        else:
            out.append(EvidenceItem(kind="log", detail=str(item)))
    return out


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Optional ISO parse for API payloads; shares only the Z-rewrite helper.

    Unlike ``src.utils.time.parse_iso``, this returns ``None`` for empty/invalid
    input and leaves naive datetimes naive.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(rewrite_iso_z(value))
    except ValueError:
        return None


def trigger_from_candidates(
    candidates: list[Any],
    primary: Optional[ClusterEvidence] = None,
) -> TriggerInfo:
    if not candidates:
        return TriggerInfo(
            detected=False, type=None, service=None, at=None, correlation=None
        )
    first = candidates[0]
    if isinstance(first, dict):
        message = str(first.get("message") or "")
        service = first.get("service")
        at = first.get("timestamp") or first.get("at")
    else:
        message = str(getattr(first, "message", "") or "")
        service = getattr(first, "service", None)
        ts = getattr(first, "timestamp", None)
        at = ts.isoformat() if hasattr(ts, "isoformat") else ts
    at_str = str(at) if at else None
    first_seen = primary.first_seen if primary is not None else None
    correlation: Optional[str] = None
    trigger_dt = _parse_iso(at_str)
    seen_dt = _parse_iso(first_seen)
    if trigger_dt is not None and seen_dt is not None and trigger_dt <= seen_dt:
        correlation = "precedes_primary_spike"
    return TriggerInfo(
        detected=True,
        type=infer_trigger_type(message),
        service=str(service) if service else None,
        at=at_str,
        correlation=correlation,
    )


def trigger_from_mapping(
    raw: Any, candidates: list[Any], primary: Optional[ClusterEvidence]
) -> TriggerInfo:
    if isinstance(raw, TriggerInfo):
        return raw
    if isinstance(raw, dict) and "detected" in raw:
        return TriggerInfo.model_validate(raw)
    return trigger_from_candidates(candidates, primary)


def confidence_from_value(raw: Any) -> Confidence:
    if isinstance(raw, Confidence):
        return raw
    if isinstance(raw, dict):
        label = str(raw.get("label") or "low")
        score = raw.get("score")
        if score is None:
            score = score_from_label(label)
        return Confidence(label=label, score=float(score))
    label = str(raw or "low")
    return Confidence(label=label, score=score_from_label(label))


def explain_from_result(
    result: ExplainResult,
    *,
    no_llm: bool,
    cached: bool,
    scope: str = "default",
    llm_provider: Optional[str] = None,
) -> ExplainResponse:
    primary = cluster_from_mapping(result.primary_cluster)
    secondary = [
        c
        for c in (cluster_from_mapping(item) for item in result.secondary_clusters)
        if c is not None
    ]
    prose = result.summary_text or ""
    return ExplainResponse(
        schema_version=SCHEMA_VERSION,
        scope=scope,
        window=window_from_bounds(result.window_start, result.window_end),
        confidence=confidence_from_value(result.confidence),
        summary=short_summary(prose),
        trigger=trigger_from_candidates(result.trigger_candidates, primary),
        primary_cluster=primary,
        secondary_clusters=secondary,
        evidence=evidence_from_items(result.evidence_items),
        llm=llm_from_mode(
            mode=result.mode,
            requested=llm_requested(no_llm=no_llm, llm_provider=llm_provider),
            provider=llm_provider,
        ),
        rendered_text=prose,
        cached=cached,
        total_logs=result.total_logs,
        mode=result.mode,
        services_affected=list(result.services_affected or []),
    )


def explain_from_cached(
    payload: dict[str, Any],
    *,
    window_start: datetime,
    window_end: datetime,
    no_llm: bool,
    scope: str = "default",
    llm_provider: Optional[str] = None,
) -> ExplainResponse:
    """Upgrade a cached dict (old or v1) so the response always has v1 fields."""
    primary = cluster_from_mapping(payload.get("primary_cluster"))
    secondary_raw = payload.get("secondary_clusters") or []
    secondary = [
        c
        for c in (cluster_from_mapping(item) for item in secondary_raw)
        if c is not None
    ]
    candidates = payload.get("trigger_candidates") or []
    prose = str(payload.get("rendered_text") or payload.get("summary") or "")
    already_v1 = isinstance(payload.get("confidence"), dict) and bool(
        payload.get("schema_version")
    )
    if already_v1:
        summary_text = str(payload.get("summary") or short_summary(prose))
    else:
        summary_text = short_summary(prose)
    mode = str(payload.get("mode") or "rules")
    return ExplainResponse(
        schema_version=SCHEMA_VERSION,
        scope=str(payload.get("scope") or scope),
        window=window_from_mapping(
            payload.get("window"),
            fallback_start=window_start.isoformat(),
            fallback_end=window_end.isoformat(),
        ),
        confidence=confidence_from_value(payload.get("confidence")),
        summary=summary_text or short_summary(prose),
        trigger=trigger_from_mapping(payload.get("trigger"), candidates, primary),
        primary_cluster=primary,
        secondary_clusters=secondary,
        evidence=evidence_from_items(payload.get("evidence")),
        llm=llm_from_mode(
            mode=mode,
            requested=llm_requested(no_llm=no_llm, llm_provider=llm_provider),
            provider=llm_provider,
        ),
        rendered_text=prose,
        cached=True,
        total_logs=int(payload.get("total_logs") or 0),
        mode=mode,
        services_affected=list(payload.get("services_affected") or []),
    )


def cluster_diff_model(diff: Any, kind: str) -> ClusterDiffModel:
    return ClusterDiffModel(
        fingerprint=diff.fingerprint,
        message=diff.message,
        services=list(diff.services or []),
        count_a=diff.count_a,
        count_b=diff.count_b,
        marker=COMPARE_MARKERS[kind],
    )


def trigger_diff_model(diff: Any) -> TriggerDiffModel:
    only_in = getattr(diff, "only_in", "a")
    return TriggerDiffModel(
        message=diff.message,
        service=diff.service or None,
        only_in=only_in,
        marker=TRIGGER_MARKERS.get(only_in, "+⚡"),
    )
