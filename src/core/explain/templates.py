from typing import Optional

from src.core.explain.evidence import EvidencePacket
from src.utils.time import format_window


def _trunc(text: str, max_len: int) -> str:
    """Truncate at a word boundary."""
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    last_space = truncated.rfind(" ")
    if last_space > max_len // 2:
        truncated = truncated[:last_space]
    return truncated + "…"


def render_text_summary(packet: EvidencePacket, confidence: str) -> str:
    """Render a deterministic text incident summary from an evidence packet."""
    lines: list[str] = []

    lines.append("Incident summary")
    lines.append("")

    # Window
    lines.append(f"Window: {format_window(packet.window_start, packet.window_end)}")

    # Services
    if packet.services_affected:
        lines.append(f"Services affected: {', '.join(packet.services_affected)}")
    else:
        lines.append("Services affected: unknown")

    # Primary issue
    pc = packet.primary_cluster
    if pc:
        lines.append(f"Primary issue: {_trunc(pc.representative_message, 120)}")
    else:
        lines.append("Primary issue: No significant error cluster identified")

    # Secondary effects (multiline)
    if packet.secondary_clusters:
        lines.append("Secondary effects:")
        for c in packet.secondary_clusters[:3]:
            n = c.count
            lines.append(f"  - {_trunc(c.representative_message, 90)} ({n} {'event' if n == 1 else 'events'})")
    else:
        lines.append("Secondary effects: None identified")

    # Likely trigger — first candidate is primary, rest are supporting
    if packet.trigger_candidates:
        primary_t = packet.trigger_candidates[0]
        ts = primary_t.timestamp.strftime("%H:%M:%S UTC") if primary_t.timestamp else "unknown time"
        svc = f" in {primary_t.service}" if primary_t.service else ""
        lines.append(f"Likely trigger: {_trunc(primary_t.message, 100)}{svc} at {ts}")
        if len(packet.trigger_candidates) > 1:
            lines.append("Supporting trigger evidence:")
            for t in packet.trigger_candidates[1:]:
                ts2 = t.timestamp.strftime("%H:%M:%S UTC") if t.timestamp else "unknown time"
                svc2 = f" in {t.service}" if t.service else ""
                lines.append(f"  - {_trunc(t.message, 90)}{svc2} at {ts2}")
    elif pc and pc.change_ratio > 5 and pc.baseline_count == 0:
        lines.append("Likely trigger: none identified")
    else:
        lines.append("Likely trigger: none identified")

    # Evidence
    lines.append("")
    lines.append("Evidence:")
    if packet.evidence_items:
        for item in packet.evidence_items:
            lines.append(f"- {item}")
    else:
        lines.append("- Insufficient evidence collected")

    lines.append("")
    lines.append(f"Confidence: {confidence}")

    return "\n".join(lines)


def render_insufficient_evidence(window_start, window_end, total_logs: int) -> str:
    """Render a message when there's not enough data to analyze."""
    lines = [
        "Incident summary",
        "",
        f"Window: {format_window(window_start, window_end)}",
        "",
        "Insufficient evidence to identify a likely issue in the requested window.",
        "",
        "Evidence:",
        f"- {total_logs} total logs matched filters",
        "- No error-level clusters detected",
        "- No cluster showed meaningful deviation from baseline",
        "",
        "Confidence: low",
    ]
    return "\n".join(lines)
