"""Paste-ready GitHub-flavored markdown incident report for `explain` output."""

from __future__ import annotations

import shlex
from datetime import datetime, timedelta
from typing import Optional

from src.core.explain.summarizer import ExplainResult

DEFAULT_MAX_CLUSTERS = 10

_MD_SPECIAL = frozenset("\\`*_{}[]<>|#")


def build_explain_reproduce_cmd(
    *,
    since: Optional[str] = None,
    from_time: Optional[str] = None,
    to_time: Optional[str] = None,
    service: Optional[str] = None,
    env: Optional[str] = None,
    no_llm: bool = False,
    baseline_window: Optional[str] = None,
    ingestion_job: Optional[str] = None,
    all_ingestions: bool = False,
    max_clusters: int = DEFAULT_MAX_CLUSTERS,
) -> str:
    """Build the exact `raglogs explain` invocation that produced this report."""
    tokens: list[str] = ["raglogs", "explain"]
    if since:
        tokens.extend(["--since", since])
    if from_time:
        tokens.extend(["--from", from_time])
    if to_time:
        tokens.extend(["--to", to_time])
    if service:
        tokens.extend(["--service", service])
    if env:
        tokens.extend(["--env", env])
    if no_llm:
        tokens.append("--no-llm")
    if baseline_window:
        tokens.extend(["--baseline-window", baseline_window])
    if ingestion_job:
        tokens.extend(["--ingestion-job", ingestion_job])
    if all_ingestions:
        tokens.append("--all-ingestions")
    if max_clusters != DEFAULT_MAX_CLUSTERS:
        tokens.extend(["--max-clusters", str(max_clusters)])
    tokens.extend(["--format", "markdown"])
    return " ".join(shlex.quote(t) for t in tokens)


def render_incident_report(
    result: ExplainResult,
    *,
    reproduce_cmd: Optional[str] = None,
    environment: Optional[str] = None,
) -> str:
    """
    Render a paste-ready markdown incident report from an ExplainResult.

    Optional sections (primary cluster, secondary clusters, triggers, reproduce)
    are omitted when empty rather than printed as "None".
    """
    lines: list[str] = [
        "# Incident report",
        "",
        _escape_md(_title(result)),
        "",
        *_metadata_lines(result, environment=environment),
        "",
        "## Summary",
        "",
        (result.summary_text or "").rstrip(),
        "",
        "## Evidence",
        "",
    ]

    if result.evidence_items:
        for item in result.evidence_items:
            lines.append(f"- {_escape_md(str(item))}")
        lines.append("")

    primary_lines = _primary_cluster_section(result.primary_cluster)
    if primary_lines:
        lines.extend(primary_lines)

    secondary_lines = _secondary_clusters_section(result.secondary_clusters)
    if secondary_lines:
        lines.extend(secondary_lines)

    trigger_lines = _trigger_candidates_section(result.trigger_candidates)
    if trigger_lines:
        lines.extend(trigger_lines)

    if reproduce_cmd:
        lines.extend(
            [
                "## Reproduce",
                "",
                "Operators can redirect this command to a file for tickets and postmortems:",
                "",
                "```bash",
                reproduce_cmd,
                "```",
                "",
                f"`{reproduce_cmd} > postmortem.md`",
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def _title(result: ExplainResult) -> str:
    if result.primary_cluster:
        message = result.primary_cluster.get("message") or ""
        if message:
            return _single_line(str(message))
    for line in (result.summary_text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered in {"incident summary", "incident report"}:
            continue
        if lowered.startswith("window:"):
            continue
        return _single_line(stripped)
    return "Untitled incident"


def _metadata_lines(result: ExplainResult, *, environment: Optional[str]) -> list[str]:
    services = ", ".join(result.services_affected) if result.services_affected else "none"
    lines = [
        f"- **Window:** {_format_window_meta(result.window_start, result.window_end)}",
        f"- **Services:** {_escape_md(services)}",
    ]
    if environment:
        lines.append(f"- **Environment:** {_escape_md(environment)}")
    lines.extend(
        [
            f"- **Total logs:** {result.total_logs}",
            f"- **Confidence:** {_escape_md(str(result.confidence))}",
            f"- **Mode:** {_escape_md(str(result.mode))}",
        ]
    )
    return lines


def _primary_cluster_section(cluster: Optional[dict]) -> list[str]:
    if not cluster:
        return []
    lines = ["## Primary cluster", ""]
    fingerprint = cluster.get("fingerprint")
    if fingerprint:
        lines.append(f"- **Fingerprint:** {_inline_code(str(fingerprint))}")
    if cluster.get("count") is not None:
        lines.append(f"- **Count:** {cluster['count']}")
    if cluster.get("importance_score") is not None:
        lines.append(f"- **Importance:** {cluster['importance_score']}")
    message = cluster.get("message")
    if message:
        lines.append(f"- **Message:** {_inline_code(str(message))}")
    services = cluster.get("services") or []
    if services:
        joined = ", ".join(_escape_md(str(s)) for s in services)
        lines.append(f"- **Services:** {joined}")
    first_seen = cluster.get("first_seen")
    if first_seen:
        lines.append(f"- **First seen:** {_escape_md(str(first_seen))}")
    last_seen = cluster.get("last_seen")
    if last_seen:
        lines.append(f"- **Last seen:** {_escape_md(str(last_seen))}")
    if cluster.get("baseline_count") is not None:
        lines.append(f"- **Baseline count:** {cluster['baseline_count']}")
    if cluster.get("change_ratio") is not None:
        lines.append(f"- **Change ratio:** {cluster['change_ratio']}")
    lines.append("")
    return lines


def _secondary_clusters_section(clusters: list[dict]) -> list[str]:
    if not clusters:
        return []
    lines = ["## Secondary clusters", ""]
    for cluster in clusters:
        message = _inline_code(str(cluster.get("message") or ""))
        count = cluster.get("count") or 0
        event_word = "event" if count == 1 else "events"
        extras: list[str] = []
        fingerprint = cluster.get("fingerprint")
        if fingerprint:
            extras.append(_inline_code(str(fingerprint)))
        services = cluster.get("services") or []
        if services:
            extras.append(", ".join(_escape_md(str(s)) for s in services))
        suffix = f" ({'; '.join(extras)})" if extras else ""
        lines.append(f"- {message} — {count} {event_word}{suffix}")
    lines.append("")
    return lines


def _trigger_candidates_section(triggers: list[dict]) -> list[str]:
    if not triggers:
        return []
    lines = ["## Trigger candidates", ""]
    for trigger in triggers:
        ts = trigger.get("timestamp") or "unknown time"
        message = _inline_code(str(trigger.get("message") or ""))
        service = trigger.get("service")
        if service:
            lines.append(f"- {_escape_md(str(ts))} · {_escape_md(str(service))} — {message}")
        else:
            lines.append(f"- {_escape_md(str(ts))} — {message}")
    lines.append("")
    return lines


def _format_window_meta(start: datetime, end: datetime) -> str:
    iso = f"{start.isoformat()} → {end.isoformat()}"
    duration = _human_duration(end - start)
    return f"{iso} ({duration})" if duration else iso


def _human_duration(delta: timedelta) -> str:
    total = int(round(delta.total_seconds()))
    if total < 0:
        total = -total
    if total == 0:
        return "0s"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds and not days:
        parts.append(f"{seconds}s")
    return " ".join(parts) or "0s"


def _single_line(text: str, max_len: int = 100) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_len:
        return collapsed
    truncated = collapsed[:max_len]
    last_space = truncated.rfind(" ")
    if last_space > max_len // 2:
        truncated = truncated[:last_space]
    return truncated + "…"


def _escape_md(text: str) -> str:
    """Escape GFM inline markup so titles and list items stay paste-safe."""
    collapsed = " ".join(text.split())
    return "".join(f"\\{ch}" if ch in _MD_SPECIAL else ch for ch in collapsed)


def _inline_code(text: str) -> str:
    """Wrap text in markdown inline code, using enough backticks to be safe."""
    collapsed = " ".join(text.split())
    if not collapsed:
        return "` `"
    longest = 0
    run = 0
    for ch in collapsed:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    ticks = "`" * (longest + 1)
    pad_left = " " if collapsed.startswith("`") else ""
    pad_right = " " if collapsed.endswith("`") else ""
    return f"{ticks}{pad_left}{collapsed}{pad_right}{ticks}"
