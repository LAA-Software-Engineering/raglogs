from typing import Optional

from src.core.explain.evidence import EvidencePacket


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
    score = 0

    if packet.primary_cluster is None:
        return "low"

    pc = packet.primary_cluster
    has_trigger = bool(packet.trigger_candidates)

    # Primary cluster size
    if pc.count >= 50:
        score += 2
    elif pc.count >= 10:
        score += 1

    # Baseline change ratio — only meaningful when baseline is non-empty
    if pc.baseline_count > 0:
        if pc.change_ratio > 10:
            score += 2
        elif pc.change_ratio > 3:
            score += 1

    # Trigger candidate is the strongest corroboration signal
    if has_trigger:
        score += 2

    # Secondary clusters corroborate the primary
    if packet.secondary_clusters:
        score += 1

    # Multiple services affected → broader blast radius → more confident it's real
    if len(packet.services_affected) > 1:
        score += 1

    # 'high' requires trigger correlation — without it, cap at medium-high
    if score >= 5 and has_trigger:
        return "high"
    elif score >= 4:
        return "medium-high"
    elif score >= 2:
        return "medium"
    else:
        return "low"
