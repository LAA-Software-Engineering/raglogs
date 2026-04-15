"""Plain-text timeline rendering (no Rich) for API and tooling."""
from __future__ import annotations

from src.core.timeline.builder import TimelineEvent
from src.utils.time import format_window


def format_timeline_plain(
    events: list[TimelineEvent],
    window_start,
    window_end,
) -> str:
    """Mirror CLI timeline layout without ANSI/markup."""
    lines: list[str] = []
    lines.append("")
    lines.append(f"Incident timeline  {format_window(window_start, window_end)}")
    lines.append("")

    if not events:
        lines.append("  No significant events found in this window.")
        lines.append("")
        return "\n".join(lines)

    prev_ts = None
    for event in events:
        if prev_ts is not None:
            gap = (event.timestamp - prev_ts).total_seconds()
            if gap > 60:
                lines.append("")

        ts_str = event.timestamp.strftime("%H:%M:%S")
        label = event.label

        if event.count is None:
            svc = " · ".join(event.services)
            suffix = f" · {svc}" if svc else ""
            lines.append(f"  {ts_str}  {label:<10} {event.description}{suffix}")
        else:
            lines.append(f"  {ts_str}  {label:<10} {event.description}")
            plural = "s" if event.count != 1 else ""
            parts = [f"{event.count} event{plural}"]
            if event.services:
                parts.append(", ".join(event.services))
            if event.duration_minutes:
                parts.append(f"{event.duration_minutes} min span")
            lines.append(f"             {' · '.join(parts)}")

        prev_ts = event.timestamp

    lines.append("")
    return "\n".join(lines)
