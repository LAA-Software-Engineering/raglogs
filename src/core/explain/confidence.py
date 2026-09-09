from src.config import get_settings
from src.core.explain.evidence import EvidencePacket


def _label_scores() -> dict[str, float]:
    """Label -> ordinal 0-1 rank, from settings (see ``score_from_label``)."""
    s = get_settings()
    return {
        "low": s.confidence_score_low,
        "medium": s.confidence_score_medium,
        "medium-high": s.confidence_score_medium_high,
        "high": s.confidence_score_high,
    }


# Back-compat: the default mapping as a module constant. Prefer ``_label_scores``
# (settings-driven) at call sites so overrides take effect.
CONFIDENCE_LABEL_SCORES: dict[str, float] = {
    "low": 0.25,
    "medium": 0.50,
    "medium-high": 0.72,
    "high": 0.90,
}


def score_from_label(label: str) -> float:
    """Map a confidence label to its 0-1 ordinal rank for the v1 JSON schema.

    This is an *ordinal* rank, not a calibrated probability — a "high" is
    ranked above a "medium", but the number is not P(explanation correct). The
    per-label values are configurable (``confidence_score_*``); they have not
    been fitted to measured accuracy (#83).
    """
    return _label_scores().get(label, 0.0)


def compute_confidence_points(packet: EvidencePacket) -> int:
    """Integer evidence score used by ``compute_confidence``.

    Every threshold and weight is configurable via ``confidence_*`` settings;
    the defaults reproduce the original hand-picked scale (max 8).
    """
    if packet.primary_cluster is None:
        return 0

    s = get_settings()
    pc = packet.primary_cluster
    points = 0

    if pc.count >= s.confidence_count_high:
        points += s.confidence_points_count_high
    elif pc.count >= s.confidence_count_low:
        points += s.confidence_points_count_low

    # After #115 job-scoped runs have a real in-job baseline, so this term now
    # carries information in every mode (it was previously always 0 when
    # ingestion_job_id was set, which capped achievable confidence for the CLI).
    if pc.baseline_count > 0:
        if pc.change_ratio > s.confidence_change_ratio_high:
            points += s.confidence_points_change_high
        elif pc.change_ratio > s.confidence_change_ratio_low:
            points += s.confidence_points_change_low

    # #82: a *found* trigger (rare change near onset) earns the points; whether it
    # gates "high" depends on it being validated (see compute_confidence). None =
    # legacy path -> fall back to bool(trigger_candidates) for exact back-compat.
    found = packet.trigger_found if packet.trigger_found is not None else bool(packet.trigger_candidates)
    if found:
        points += s.confidence_points_trigger

    if packet.secondary_clusters:
        points += s.confidence_points_secondary

    if len(packet.services_affected) > 1:
        points += s.confidence_points_multiservice

    return points


def compute_confidence(packet: EvidencePacket) -> str:
    """
    Compute a confidence level based on the evidence quality.
    Returns: 'low', 'medium', 'medium-high', or 'high'

    Scoring rationale (thresholds are ``confidence_threshold_*`` settings):
    - 'high' requires a trigger candidate AND a score at/above the high threshold
    - 'medium-high' is the ceiling when no trigger is identified
    - the baseline term now contributes in job-scoped mode too (see #115)

    The default thresholds are the original hand-picked values and are NOT
    calibrated against measured accuracy; recalibration is tracked in #83.
    """
    if packet.primary_cluster is None:
        return "low"

    s = get_settings()
    score = compute_confidence_points(packet)
    # #82: "high" requires a *validated* trigger (rare + linked), not a bare regex
    # match. None (legacy) falls back to bool(trigger_candidates) so regex-mode
    # confidence is byte-identical.
    has_trigger = (
        packet.trigger_explains
        if packet.trigger_explains is not None
        else bool(packet.trigger_candidates)
    )

    if score >= s.confidence_threshold_high and has_trigger:
        return "high"
    elif score >= s.confidence_threshold_medium_high:
        return "medium-high"
    elif score >= s.confidence_threshold_medium:
        return "medium"
    else:
        return "low"


def compute_confidence_score(packet: EvidencePacket) -> float:
    """0-1 ordinal rank from the same signals as ``compute_confidence``."""
    if packet.primary_cluster is None:
        return 0.0
    return score_from_label(compute_confidence(packet))
