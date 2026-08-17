"""Per-request query config overrides (G14).

Precedence: request field > per-key default > server default.
Omitted fields fall through. ``AUTH_ENABLED=false`` skips the per-key layer.

Bounds (inclusive):
- ``max_clusters``: 1–100
- ``max_evidence_items``: 1–50
- ``baseline_window``: duration string parsed by ``parse_duration``
- ``llm.provider``: ``openai`` | ``ollama`` | ``disabled``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional

from fastapi import Request
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from src.api.auth.middleware import AuthPrincipal
from src.config.settings import Settings

ERROR_INVALID_OVERRIDE = "INVALID_OVERRIDE"

MAX_CLUSTERS_MIN = 1
MAX_CLUSTERS_MAX = 100
MAX_EVIDENCE_ITEMS_MIN = 1
MAX_EVIDENCE_ITEMS_MAX = 50

LLM_PROVIDERS: frozenset[str] = frozenset({"openai", "ollama", "disabled"})
LlmProviderName = Literal["disabled", "openai", "ollama"]


class OverrideValidationError(Exception):
    """Typed 400 when a query override is out of bounds or otherwise invalid."""

    def __init__(
        self,
        message: str,
        *,
        field: str,
        min_value: int | None = None,
        max_value: int | None = None,
    ) -> None:
        self.status_code = 400
        self.error_code = ERROR_INVALID_OVERRIDE
        self.message = message
        self.field = field
        self.min_value = min_value
        self.max_value = max_value
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "error_code": self.error_code,
            "message": self.message,
            "field": self.field,
        }
        if self.min_value is not None:
            body["min"] = self.min_value
        if self.max_value is not None:
            body["max"] = self.max_value
        return body


def override_error_response(exc: OverrideValidationError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.as_dict())


class LlmOverride(BaseModel):
    """Optional per-call LLM selection. Unknown providers are rejected at resolve time."""

    provider: Optional[str] = Field(
        default=None,
        description="openai | ollama | disabled. Omitted: per-key default, then LLM_PROVIDER.",
    )
    enabled: Optional[bool] = Field(
        default=None,
        description="false acts like no_llm. Omitted: inferred from provider / server default.",
    )


class QueryOverrideFields(BaseModel):
    """Shared optional tunables on ``/v1/query/*`` request bodies."""

    baseline_window: Optional[str] = Field(
        default=None,
        description="Baseline duration (e.g. 24h). Omitted: per-key default, then DEFAULT_BASELINE_WINDOW.",
    )
    max_clusters: Optional[int] = Field(
        default=None,
        description="Max clusters to analyze (1–100). Omitted: per-key default, then MAX_CLUSTERS_FOR_EXPLAIN.",
    )
    max_evidence_items: Optional[int] = Field(
        default=None,
        description="Max evidence items (1–50). Omitted: per-key default, then MAX_EVIDENCE_ITEMS.",
    )
    llm: Optional[LlmOverride] = Field(
        default=None,
        description="Per-call LLM provider/enabled. Does not persist server config.",
    )


@dataclass(frozen=True)
class OverrideInput:
    """Request-layer values. ``None`` means omitted (fall through)."""

    baseline_window: Optional[str] = None
    max_clusters: Optional[int] = None
    max_evidence_items: Optional[int] = None
    llm_provider: Optional[str] = None
    llm_enabled: Optional[bool] = None
    no_llm: Optional[bool] = None


@dataclass(frozen=True)
class KeyOverrideDefaults:
    baseline_window: Optional[str] = None
    max_clusters: Optional[int] = None
    max_evidence_items: Optional[int] = None
    llm_provider: Optional[str] = None
    llm_enabled: Optional[bool] = None


@dataclass(frozen=True)
class ResolvedOverrides:
    baseline_window: str
    max_clusters: int
    max_evidence_items: int
    llm_provider: LlmProviderName
    llm_enabled: bool

    def cache_parts(self) -> dict[str, Any]:
        """Canonical resolved tunables for the explain cache key."""
        return {
            "baseline_window": self.baseline_window,
            "max_clusters": self.max_clusters,
            "max_evidence_items": self.max_evidence_items,
            "llm_provider": self.llm_provider,
            "llm_enabled": self.llm_enabled,
        }

    @property
    def no_llm(self) -> bool:
        return not self.llm_enabled


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def override_input_from_request(request: Any) -> OverrideInput:
    """Pull G14 fields off a query request body. Unknown attributes are omitted."""
    llm = getattr(request, "llm", None)
    return OverrideInput(
        baseline_window=getattr(request, "baseline_window", None),
        max_clusters=getattr(request, "max_clusters", None),
        max_evidence_items=getattr(request, "max_evidence_items", None),
        llm_provider=getattr(llm, "provider", None) if llm is not None else None,
        llm_enabled=getattr(llm, "enabled", None) if llm is not None else None,
        no_llm=getattr(request, "no_llm", None),
    )


def _normalize_provider(value: str, *, field: str) -> LlmProviderName:
    normalized = value.strip().lower()
    if normalized not in LLM_PROVIDERS:
        allowed = ", ".join(sorted(LLM_PROVIDERS))
        raise OverrideValidationError(
            f"Unknown llm.provider {value!r}. Allowed: {allowed}",
            field=field,
        )
    return normalized  # type: ignore[return-value]


def _parse_int_bound(
    value: Any,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OverrideValidationError(
            f"{field} must be an integer between {minimum} and {maximum}",
            field=field,
            min_value=minimum,
            max_value=maximum,
        )
    if value < minimum or value > maximum:
        raise OverrideValidationError(
            f"{field} must be between {minimum} and {maximum}",
            field=field,
            min_value=minimum,
            max_value=maximum,
        )
    return value


def _parse_baseline_window(value: str, *, field: str) -> str:
    from src.utils.time import parse_duration

    stripped = value.strip()
    if not stripped:
        raise OverrideValidationError(
            "baseline_window must be a duration like 24h or 7d",
            field=field,
        )
    try:
        parse_duration(stripped)
    except ValueError as exc:
        raise OverrideValidationError(str(exc), field=field) from exc
    return stripped


def _enabled_from_layer(
    *,
    llm_enabled: Optional[bool],
    llm_provider: Optional[str],
    no_llm: Optional[bool] = None,
) -> Optional[bool]:
    if llm_enabled is not None:
        return bool(llm_enabled)
    if llm_provider is not None:
        return llm_provider != "disabled"
    if no_llm is not None:
        return not bool(no_llm)
    return None


def parse_key_config(raw: Any) -> KeyOverrideDefaults:
    """Parse ``api_keys.config_json``. Invalid stored values are ignored (None)."""
    if not isinstance(raw, Mapping):
        return KeyOverrideDefaults()
    llm = raw.get("llm") if isinstance(raw.get("llm"), Mapping) else {}
    provider = llm.get("provider") if isinstance(llm, Mapping) else None
    enabled = llm.get("enabled") if isinstance(llm, Mapping) else None
    return KeyOverrideDefaults(
        baseline_window=raw.get("baseline_window") if isinstance(raw.get("baseline_window"), str) else None,
        max_clusters=raw.get("max_clusters") if isinstance(raw.get("max_clusters"), int) and not isinstance(raw.get("max_clusters"), bool) else None,
        max_evidence_items=(
            raw.get("max_evidence_items")
            if isinstance(raw.get("max_evidence_items"), int)
            and not isinstance(raw.get("max_evidence_items"), bool)
            else None
        ),
        llm_provider=provider if isinstance(provider, str) else None,
        llm_enabled=enabled if isinstance(enabled, bool) else None,
    )


def build_key_config_json(
    *,
    baseline_window: Optional[str] = None,
    max_clusters: Optional[int] = None,
    max_evidence_items: Optional[int] = None,
    llm_provider: Optional[str] = None,
    llm_enabled: Optional[bool] = None,
) -> dict[str, Any] | None:
    """Validate CLI/key-default fields and return a JSON object, or None if empty."""
    payload: dict[str, Any] = {}
    if baseline_window is not None:
        payload["baseline_window"] = _parse_baseline_window(
            baseline_window, field="baseline_window"
        )
    if max_clusters is not None:
        payload["max_clusters"] = _parse_int_bound(
            max_clusters,
            field="max_clusters",
            minimum=MAX_CLUSTERS_MIN,
            maximum=MAX_CLUSTERS_MAX,
        )
    if max_evidence_items is not None:
        payload["max_evidence_items"] = _parse_int_bound(
            max_evidence_items,
            field="max_evidence_items",
            minimum=MAX_EVIDENCE_ITEMS_MIN,
            maximum=MAX_EVIDENCE_ITEMS_MAX,
        )
    llm: dict[str, Any] = {}
    if llm_provider is not None:
        llm["provider"] = _normalize_provider(llm_provider, field="llm.provider")
    if llm_enabled is not None:
        llm["enabled"] = bool(llm_enabled)
    if llm:
        payload["llm"] = llm
    return payload or None


def merge_key_config(
    existing: Any,
    *,
    baseline_window: Optional[str] = None,
    max_clusters: Optional[int] = None,
    max_evidence_items: Optional[int] = None,
    llm_provider: Optional[str] = None,
    llm_enabled: Optional[bool] = None,
    clear: bool = False,
) -> dict[str, Any] | None:
    """Replace or merge per-key defaults. ``clear`` drops the stored object."""
    if clear:
        return None
    current = dict(existing) if isinstance(existing, Mapping) else {}
    current_llm = dict(current.get("llm") or {}) if isinstance(current.get("llm"), Mapping) else {}
    incoming = build_key_config_json(
        baseline_window=baseline_window,
        max_clusters=max_clusters,
        max_evidence_items=max_evidence_items,
        llm_provider=llm_provider,
        llm_enabled=llm_enabled,
    )
    if not incoming:
        return current or None
    if "baseline_window" in incoming:
        current["baseline_window"] = incoming["baseline_window"]
    if "max_clusters" in incoming:
        current["max_clusters"] = incoming["max_clusters"]
    if "max_evidence_items" in incoming:
        current["max_evidence_items"] = incoming["max_evidence_items"]
    incoming_llm = incoming.get("llm")
    if isinstance(incoming_llm, Mapping):
        current_llm.update(incoming_llm)
        current["llm"] = current_llm
    return current or None


def resolve_query_overrides(
    request_fields: OverrideInput,
    principal: AuthPrincipal | None,
    settings: Settings,
    *,
    auth_enabled: bool,
) -> ResolvedOverrides:
    """Resolve tunables with request > per-key default > server default."""
    key = (
        parse_key_config(principal.config_json)
        if auth_enabled and principal is not None
        else KeyOverrideDefaults()
    )

    raw_baseline = _coalesce(request_fields.baseline_window, key.baseline_window, settings.default_baseline_window)
    baseline_window = _parse_baseline_window(str(raw_baseline), field="baseline_window")

    raw_clusters = _coalesce(
        request_fields.max_clusters, key.max_clusters, settings.max_clusters_for_explain
    )
    max_clusters = _parse_int_bound(
        raw_clusters,
        field="max_clusters",
        minimum=MAX_CLUSTERS_MIN,
        maximum=MAX_CLUSTERS_MAX,
    )

    raw_evidence = _coalesce(
        request_fields.max_evidence_items,
        key.max_evidence_items,
        settings.max_evidence_items,
    )
    max_evidence_items = _parse_int_bound(
        raw_evidence,
        field="max_evidence_items",
        minimum=MAX_EVIDENCE_ITEMS_MIN,
        maximum=MAX_EVIDENCE_ITEMS_MAX,
    )

    raw_provider = _coalesce(request_fields.llm_provider, key.llm_provider, settings.llm_provider)
    llm_provider = _normalize_provider(str(raw_provider), field="llm.provider")

    request_enabled = _enabled_from_layer(
        llm_enabled=request_fields.llm_enabled,
        llm_provider=request_fields.llm_provider,
        no_llm=request_fields.no_llm,
    )
    key_enabled = _enabled_from_layer(
        llm_enabled=key.llm_enabled,
        llm_provider=key.llm_provider,
    )
    server_enabled = settings.llm_provider != "disabled"
    enabled_flag = bool(_coalesce(request_enabled, key_enabled, server_enabled))
    llm_enabled = enabled_flag and llm_provider != "disabled"

    return ResolvedOverrides(
        baseline_window=baseline_window,
        max_clusters=max_clusters,
        max_evidence_items=max_evidence_items,
        llm_provider=llm_provider,
        llm_enabled=llm_enabled,
    )


def resolve_overrides_from_http(http_request: Request, body: Any) -> ResolvedOverrides:
    """Resolve overrides for a query route. Raises ``OverrideValidationError``."""
    from src.config import get_settings

    settings = get_settings()
    principal = getattr(http_request.state, "auth_principal", None)
    if not isinstance(principal, AuthPrincipal):
        principal = None
    return resolve_query_overrides(
        override_input_from_request(body),
        principal,
        settings,
        auth_enabled=bool(settings.auth_enabled),
    )


def settings_with_overrides(settings: Settings, overrides: ResolvedOverrides) -> Settings:
    """Copy of server settings with per-call tunables. Does not persist globally."""
    return settings.model_copy(
        update={
            "default_baseline_window": overrides.baseline_window,
            "max_clusters_for_explain": overrides.max_clusters,
            "max_evidence_items": overrides.max_evidence_items,
            "llm_provider": overrides.llm_provider,
        }
    )
