"""UI field-coverage tests (issue #27).

There is no JS test runner. These tests load the static JS/CSS and lock the
Explain / Compare / Timeline render helpers against the JSON fields the API
already returns. Python helpers below mirror the JS — keep them in lockstep.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
JS_PATH = ROOT / "src/api/static/js/app.js"
CSS_PATH = ROOT / "src/api/static/css/app.css"


def _js() -> str:
    return JS_PATH.read_text(encoding="utf-8")


def _css() -> str:
    return CSS_PATH.read_text(encoding="utf-8")


def _escape(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=False)


def _fmt_time(iso: Any) -> str:
    """Mirror of ``fmtTime`` for snapshot tests — pass through ISO strings."""
    if not iso:
        return ""
    return str(iso)


# ── Explain: trigger_candidates / G7 trigger ─────────────────────────────────


def trigger_candidates_from(data: dict[str, Any]) -> list[Any]:
    """Mirror of ``triggerCandidatesFrom`` in app.js."""
    raw = data.get("trigger_candidates") if data else None
    if isinstance(raw, list) and raw:
        return raw
    trigger = data.get("trigger") if data else None
    if isinstance(trigger, dict) and trigger.get("detected"):
        return [trigger]
    return []


def render_trigger_candidate(trigger: dict[str, Any]) -> str:
    """Mirror of ``renderTriggerCandidate`` in app.js."""
    message = trigger.get("message") or trigger.get("detail") or trigger.get("type") or ""
    ts = _fmt_time(trigger.get("timestamp") or trigger.get("at"))
    extras: list[str] = []
    if trigger.get("service"):
        extras.append(_escape(trigger["service"]))
    if ts:
        extras.append(_escape(ts))
    if trigger.get("type") and trigger.get("message"):
        extras.append(_escape(trigger["type"]))
    if trigger.get("correlation"):
        extras.append(_escape(trigger["correlation"]))
    extras_html = f'<div class="cluster-meta">{" · ".join(extras)}</div>' if extras else ""
    return (
        f'<div class="cluster-card">'
        f'<div class="cluster-message">{_escape(message)}</div>'
        f"{extras_html}"
        f"</div>"
    )


def render_trigger_section(data: dict[str, Any]) -> str:
    """Mirror of ``renderTriggerSection`` in app.js."""
    candidates = trigger_candidates_from(data)
    if not candidates:
        return ""
    cards = "".join(render_trigger_candidate(c) for c in candidates)
    return f'<div class="section-title">Likely trigger</div>{cards}'


# ── Compare: window bounds + dropped triggers ────────────────────────────────


def window_start(w: Any) -> str:
    if not isinstance(w, dict):
        return ""
    return str(w.get("from") or w.get("from_") or w.get("start") or "")


def window_end(w: Any) -> str:
    if not isinstance(w, dict):
        return ""
    return str(w.get("to") or w.get("end") or "")


def format_window_bounds(w: Any) -> str:
    """Mirror of ``formatWindowBounds`` in app.js."""
    start = window_start(w)
    end = window_end(w)
    if not start and not end:
        return ""
    if start and end:
        return f"{_fmt_time(start)} → {_fmt_time(end)}"
    return _fmt_time(start or end)


def render_compare_windows(data: dict[str, Any]) -> str:
    """Mirror of ``renderCompareWindows`` in app.js."""
    a = format_window_bounds(data.get("window_a"))
    b = format_window_bounds(data.get("window_b"))
    if not a and not b:
        return ""
    parts = ['<div class="meta-row window-bounds">']
    if a:
        parts.append(f"<span>Window A (now): {_escape(a)}</span>")
    if b:
        parts.append(f"<span>Window B (baseline): {_escape(b)}</span>")
    parts.append("</div>")
    return "".join(parts)


def render_trigger_diff_list(title: str, items: Any, css_class: str) -> str:
    """Mirror of ``renderTriggerDiffList`` in app.js."""
    if not items:
        return ""
    rows: list[str] = []
    for t in items:
        service = (
            f' <span class="cluster-meta">({_escape(t.get("service"))})</span>'
            if t.get("service")
            else ""
        )
        rows.append(f'<li class="{css_class}">{_escape(t.get("message"))}{service}</li>')
    return (
        f'<div class="section-title">{_escape(title)}</div>'
        f'<ul class="plain-list">{"".join(rows)}</ul>'
    )


# ── Timeline: count / services / duration_minutes ────────────────────────────


def timeline_event_meta(event: dict[str, Any]) -> str:
    """Mirror of ``timelineEventMeta`` in app.js."""
    parts: list[str] = []
    if event.get("label") and event.get("label") != event.get("category"):
        parts.append(_escape(event["label"]))
    count = event.get("count")
    if count is not None and count != "":
        n = int(count)
        parts.append(f"{n} event{'s' if n != 1 else ''}")
    services = [_escape(s) for s in (event.get("services") or []) if s]
    if services:
        parts.append(", ".join(services))
    duration = event.get("duration_minutes")
    if duration is not None and duration != "":
        parts.append(f"{_escape(duration)} min span")
    return " · ".join(parts)


def render_timeline_event(event: dict[str, Any]) -> str:
    """Mirror of ``renderTimelineEvent`` in app.js."""
    meta = timeline_event_meta(event)
    title_bits = [b for b in (event.get("label"), event.get("category"), event.get("description")) if b]
    title_attr = f' title="{_escape(" · ".join(title_bits))}"' if title_bits else ""
    meta_html = f'<div class="timeline-meta">{meta}</div>' if meta else ""
    return (
        f'<div class="timeline-event"{title_attr}>'
        f'<span class="timeline-ts">{_escape(_fmt_time(event.get("timestamp")))}</span>'
        f'<span class="timeline-category">{_escape(event.get("category"))}</span>'
        f'<div class="timeline-body">'
        f'<span class="timeline-desc">{_escape(event.get("description"))}</span>'
        f"{meta_html}"
        f"</div></div>"
    )


# ── JS / CSS contract ─────────────────────────────────────────────────────────


def test_js_defines_explain_trigger_helpers() -> None:
    js = _js()
    assert "function triggerCandidatesFrom(data)" in js
    assert "function renderTriggerCandidate(trigger)" in js
    assert "function renderTriggerSection(data)" in js
    assert "data.trigger_candidates" in js
    assert "data && data.trigger" in js
    assert "Likely trigger" in js
    assert "html += renderTriggerSection(data)" in js
    assert "trigger.detected" in js
    assert "trigger.timestamp || trigger.at" in js
    assert "trigger.correlation" in js


def test_js_defines_compare_window_and_dropped_helpers() -> None:
    js = _js()
    assert "function renderCompareWindows(data)" in js
    assert "function formatWindowBounds(w)" in js
    assert "function renderTriggerDiffList(" in js
    assert "data.window_a" in js
    assert "data.window_b" in js
    assert "data.dropped_triggers" in js
    assert "Dropped triggers" in js
    assert "Window A (now):" in js
    assert "Window B (baseline):" in js
    assert "html = renderCompareWindows(data)" in js
    assert 'renderTriggerDiffList("Dropped triggers", data.dropped_triggers' in js
    assert "diff-dropped" in js
    assert "w.from || w.from_ || w.start" in js
    assert "w.to || w.end" in js


def test_js_defines_timeline_event_helpers() -> None:
    js = _js()
    assert "function timelineEventMeta(event)" in js
    assert "function renderTimelineEvent(event)" in js
    assert "event.count" in js
    assert "event.services" in js
    assert "event.duration_minutes" in js
    assert "event.label" in js
    assert "timeline-meta" in js
    assert "min span" in js
    assert "events.map(renderTimelineEvent)" in js


def test_css_styles_new_render_classes() -> None:
    css = _css()
    assert ".timeline-body" in css
    assert ".timeline-meta" in css
    assert ".window-bounds" in css
    assert ".diff-dropped" in css
    assert ".cluster-meta" in css


# ── Snapshot-ish HTML from Python mirrors ────────────────────────────────────


def test_explain_renders_trigger_candidates() -> None:
    html_out = render_trigger_section(
        {
            "trigger_candidates": [
                {
                    "message": "Deploy completed for billing-worker version v2.4.1",
                    "timestamp": "2026-03-12T13:05:00+00:00",
                    "service": "deployment-controller",
                },
                {
                    "message": "config reload",
                    "timestamp": "2026-03-12T13:06:00+00:00",
                    "service": "api",
                },
            ],
            "trigger": {"detected": True, "type": "deploy"},
        }
    )
    assert "Likely trigger" in html_out
    assert "Deploy completed for billing-worker version v2.4.1" in html_out
    assert "deployment-controller" in html_out
    assert "config reload" in html_out
    assert "cluster-card" in html_out
    assert html_out.count("cluster-card") == 2


def test_explain_falls_back_to_g7_trigger_object() -> None:
    html_out = render_trigger_section(
        {
            "trigger_candidates": [],
            "trigger": {
                "detected": True,
                "type": "deploy",
                "service": "deployment-controller",
                "at": "2026-03-12T13:05:00+00:00",
                "correlation": "precedes_primary_spike",
            },
        }
    )
    assert "Likely trigger" in html_out
    assert "deploy" in html_out
    assert "deployment-controller" in html_out
    assert "2026-03-12T13:05:00+00:00" in html_out
    assert "precedes_primary_spike" in html_out


def test_explain_skips_undetected_trigger() -> None:
    html_out = render_trigger_section(
        {
            "trigger_candidates": [],
            "trigger": {
                "detected": False,
                "type": None,
                "service": None,
                "at": None,
                "correlation": None,
            },
        }
    )
    assert html_out == ""


def test_explain_escapes_trigger_html() -> None:
    html_out = render_trigger_section(
        {
            "trigger_candidates": [
                {
                    "message": "<script>alert(1)</script>",
                    "service": "a&b",
                }
            ]
        }
    )
    assert "<script>" not in html_out
    assert "&lt;script&gt;" in html_out
    assert "a&amp;b" in html_out


def test_compare_renders_window_bounds_from_and_to() -> None:
    html_out = render_compare_windows(
        {
            "window_a": {
                "from": "2026-03-16T15:17:42+00:00",
                "to": "2026-03-16T15:47:42+00:00",
            },
            "window_b": {
                "from": "2026-03-15T15:17:42+00:00",
                "to": "2026-03-15T15:47:42+00:00",
            },
        }
    )
    assert "window-bounds" in html_out
    assert "Window A (now):" in html_out
    assert "Window B (baseline):" in html_out
    assert "2026-03-16T15:17:42+00:00" in html_out
    assert "2026-03-15T15:47:42+00:00" in html_out
    assert "→" in html_out


def test_compare_accepts_start_end_aliases() -> None:
    html_out = render_compare_windows(
        {
            "window_a": {
                "start": "2026-03-16T15:17:42+00:00",
                "end": "2026-03-16T15:47:42+00:00",
            },
            "window_b": {},
        }
    )
    assert "Window A (now):" in html_out
    assert "Window B (baseline):" not in html_out


def test_compare_renders_dropped_triggers() -> None:
    html_out = render_trigger_diff_list(
        "Dropped triggers",
        [{"message": "old deploy v2.3.0", "service": "checkout", "only_in": "b"}],
        "diff-dropped",
    )
    assert "Dropped triggers" in html_out
    assert "old deploy v2.3.0" in html_out
    assert "checkout" in html_out
    assert "diff-dropped" in html_out


def test_compare_dropped_triggers_empty_is_blank() -> None:
    assert render_trigger_diff_list("Dropped triggers", [], "diff-dropped") == ""
    assert render_trigger_diff_list("Dropped triggers", None, "diff-dropped") == ""


def test_timeline_meta_includes_count_services_duration() -> None:
    meta = timeline_event_meta(
        {
            "category": "error",
            "label": "error ↑",
            "description": "Stripe signature verification failed",
            "count": 5,
            "services": ["billing-worker", "api"],
            "duration_minutes": 3,
        }
    )
    assert "error ↑" in meta
    assert "5 events" in meta
    assert "billing-worker, api" in meta
    assert "3 min span" in meta


def test_timeline_meta_singular_count_and_skips_duplicate_label() -> None:
    meta = timeline_event_meta(
        {
            "category": "deploy",
            "label": "deploy",
            "count": 1,
            "services": ["deployment-controller"],
            "duration_minutes": None,
        }
    )
        assert "1 event" in meta
        assert "events" not in meta
        assert "deployment-controller" in meta
        assert "deploy" not in [part.strip() for part in meta.split("·")]


def test_timeline_event_html_shows_description_and_meta() -> None:
    html_out = render_timeline_event(
        {
            "timestamp": "2026-03-12T22:00:00+00:00",
            "category": "error",
            "label": "error ↑",
            "description": "Something failed",
            "count": 5,
            "services": ["api"],
            "duration_minutes": 3,
        }
    )
    assert "timeline-event" in html_out
    assert "timeline-category" in html_out
    assert ">error<" in html_out
    assert "Something failed" in html_out
    assert "timeline-meta" in html_out
    assert "5 events" in html_out
    assert "api" in html_out
    assert "3 min span" in html_out
    assert "error ↑" in html_out
    assert "timeline-body" in html_out


def test_timeline_point_event_puts_services_inline() -> None:
    html_out = render_timeline_event(
        {
            "timestamp": "2026-03-12T22:00:00+00:00",
            "category": "deploy",
            "label": "deploy",
            "description": "Deploy completed",
            "count": None,
            "services": ["deployment-controller"],
            "duration_minutes": None,
        }
    )
    assert "Deploy completed" in html_out
    assert "deployment-controller" in html_out
    assert "timeline-meta" in html_out
    assert "min span" not in html_out
    assert "event" not in html_out[html_out.index("timeline-meta") :]
