from typing import Optional

from src.core.explain.evidence import EvidencePacket


def compute_confidence(packet: EvidencePacket) -> str:
    """
    Compute a confidence level based on the evidence quality.
    Returns: 'low', 'medium', 'medium-high', or 'high'
    """
    score = 0

    if packet.primary_cluster is None:
        return "low"

    pc = packet.primary_cluster

    # Strong signal: high count
    if pc.count >= 50:
        score += 2
    elif pc.count >= 10:
        score += 1

    # Strong signal: no baseline
    if pc.baseline_count == 0:
        score += 2
    elif pc.change_ratio > 10:
        score += 2
    elif pc.change_ratio > 3:
        score += 1

    # Has trigger candidate
    if packet.trigger_candidates:
        score += 2

    # Secondary clusters corroborate
    if packet.secondary_clusters:
        score += 1

    # Multiple services affected
    if len(packet.services_affected) > 1:
        score += 1

    # Total logs reasonable
    if packet.total_logs > 50:
        score += 1

    if score >= 7:
        return "high"
    elif score >= 5:
        return "medium-high"
    elif score >= 3:
        return "medium"
    else:
        return "low"
