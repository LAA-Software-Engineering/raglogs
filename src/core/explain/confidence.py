from src.core.explain.evidence import EvidencePacket

# Design §5.7 example maps medium-high → 0.72. Labels are produced by the
# same integer scoring as compute_confidence; this table turns them into a
# stable 0–1 score for the versioned JSON schema without changing the CLI.
CONFIDENCE_LABEL_SCORES: dict[str, float] = {
    "low": 0.25,
    "medium": 0.50,
    "medium-high": 0.72,
    "high": 0.90,
}


def score_from_label(label: str) -> float:
    """Map a confidence label to a 0–1 float for the v1 JSON schema."""
    return CONFIDENCE_LABEL_SCORES.get(label, 0.0)


def compute_confidence_points(packet: EvidencePacket) -> int:
    """Integer evidence score used by ``compute_confidence``. Max is 8."""
    if packet.primary_cluster is None:
        return 0

    pc = packet.primary_cluster
    points = 0

    if pc.count >= 50:
        points += 2
    elif pc.count >= 10:
        points += 1

    if pc.baseline_count > 0:
        if pc.change_ratio > 10:
            points += 2
        elif pc.change_ratio > 3:
            points += 1

    if packet.trigger_candidates:
        points += 2

    if packet.secondary_clusters:
        points += 1

    if len(packet.services_affected) > 1:
        points += 1

    return points


def compute_confidence(packet: EvidencePacket) -> str:
    """
    Compute a confidence level based on the evidence quality.
    Returns: 'low', 'medium', 'medium-high', or 'high'

    Scoring rationale:
    - 'high' requires a trigger candidate AND strong cluster signal
    - 'medium-high' is the ceiling when no trigger is identified
    - baseline_count == 0 is not scored as a signal when job-scoped
      (it's always 0 in that mode, so it carries no information)
    """
    if packet.primary_cluster is None:
        return "low"

    score = compute_confidence_points(packet)
    has_trigger = bool(packet.trigger_candidates)

    if score >= 5 and has_trigger:
        return "high"
    elif score >= 4:
        return "medium-high"
    elif score >= 2:
        return "medium"
    else:
        return "low"


def compute_confidence_score(packet: EvidencePacket) -> float:
    """0–1 score from the same signals as ``compute_confidence``."""
    if packet.primary_cluster is None:
        return 0.0
    return score_from_label(compute_confidence(packet))
