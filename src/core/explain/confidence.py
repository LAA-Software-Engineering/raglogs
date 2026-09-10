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


def label_from_calibrated_probability(p: float) -> str:
    """Bucket a calibrated P(top-1 root-cause correct) into a confidence label
    (#83). Bands are probability ranges (``confidence_calibrated_*`` settings)
    because the input is a genuine calibrated probability, not an ordinal score.

    NB the label here means "confidence that the predicted root-cause **service**
    is correct" — not that the whole narrative / trigger / causal chain is right.
    """
    s = get_settings()
    if p >= s.confidence_calibrated_high:
        return "high"
    if p >= s.confidence_calibrated_medium_high:
        return "medium-high"
    if p >= s.confidence_calibrated_medium:
        return "medium"
    return "low"


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

    # Only legacy regex triggers contribute confidence points. In rare_event mode
    # (trigger_found is not None) the rare-event trigger fires on nearly every
    # log-announced incident (#82 T3), so it carries no information about whether
    # the explanation is correct and must not inflate confidence.
    if packet.trigger_found is None and packet.trigger_candidates:
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

    # rare_event mode (trigger_found is not None): T3 (#82) showed a rare+linked
    # trigger fires on nearly every log-announced incident, so it does NOT
    # validate the explanation — gating "high" on it over-promoted (RE3: 56/90
    # "high" at 21% accuracy, below base rate). Until confidence is calibrated
    # against measured accuracy (#83 / Phase D), rare_event mode makes no "high"
    # claim: the label rides on evidence volume only, capped at "medium-high".
    if packet.trigger_found is not None:
        if score >= s.confidence_threshold_medium_high:
            return "medium-high"
        elif score >= s.confidence_threshold_medium:
            return "medium"
        return "low"

    # Legacy regex mode: "high" requires a (regex) trigger candidate — byte-identical
    # to pre-#82 behaviour.
    has_trigger = bool(packet.trigger_candidates)
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
