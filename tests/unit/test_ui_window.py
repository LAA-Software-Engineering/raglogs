"""UI time-window contract tests (issue #26).

There is no JS test runner. These tests load the static HTML/JS and lock
the relative/absolute mode switch plus the datetime-local → ISO helper.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
HTML_PATH = ROOT / "src/api/templates/index.html"
JS_PATH = ROOT / "src/api/static/js/app.js"
CSS_PATH = ROOT / "src/api/static/css/app.css"


def datetime_local_to_iso(value: str | None) -> str:
    """Mirror of ``datetimeLocalToIso`` in app.js — keep these in lockstep.

    datetime-local values are ``YYYY-MM-DDTHH:MM`` (seconds optional, no tz).
    The API treats naive timestamps as UTC, so we emit explicit UTC ISO-8601.
    """
    trimmed = "" if value is None else str(value).strip()
    if not trimmed:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", trimmed):
        return trimmed + ":00Z"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", trimmed):
        return trimmed + "Z"
    return trimmed


class _FormCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self._form: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ad = {k: v or "" for k, v in attrs}
        if tag == "form" and "window-form" in ad.get("class", "").split():
            self._form = {
                "attrs": ad,
                "mode_btns": [],
                "inputs": [],
            }
            self.forms.append(self._form)
            return
        if self._form is None:
            return
        if tag == "button" and "mode-btn" in ad.get("class", "").split():
            self._form["mode_btns"].append(ad)
        elif tag == "input":
            self._form["inputs"].append(ad)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._form = None


def _parse_forms() -> list[dict[str, Any]]:
    parser = _FormCollector()
    parser.feed(HTML_PATH.read_text(encoding="utf-8"))
    return parser.forms


def test_datetime_local_to_iso_contract() -> None:
    assert datetime_local_to_iso("") == ""
    assert datetime_local_to_iso("   ") == ""
    assert datetime_local_to_iso(None) == ""
    assert datetime_local_to_iso("2026-03-12T22:00") == "2026-03-12T22:00:00Z"
    assert datetime_local_to_iso("2026-03-12T22:00:00") == "2026-03-12T22:00:00Z"
    assert datetime_local_to_iso("2026-03-12T22:00:00Z") == "2026-03-12T22:00:00Z"
    assert datetime_local_to_iso(" 2026-08-17T09:15 ") == "2026-08-17T09:15:00Z"


def test_js_defines_datetime_local_to_iso() -> None:
    js = JS_PATH.read_text(encoding="utf-8")
    assert "function datetimeLocalToIso(value)" in js
    assert 'return trimmed + ":00Z"' in js
    assert 'return trimmed + "Z"' in js
    assert "DATETIME_WINDOW_KEYS" in js
    assert "function applyWindowMode(" in js
    assert "function initWindowModes(" in js
    assert "initWindowModes()" in js
    assert "datetimeLocalToIso(trimmed)" in js


def test_each_window_form_defaults_to_relative_mode() -> None:
    forms = _parse_forms()
    assert len(forms) == 4
    renders = {form["attrs"]["data-render"] for form in forms}
    assert renders == {"explain", "timeline", "compare", "ask"}

    for form in forms:
        attrs = form["attrs"]
        assert attrs["data-window-mode"] == "relative"
        mode_btns = form["mode_btns"]
        assert [b["data-mode"] for b in mode_btns] == ["relative", "absolute"]
        relative_btn = mode_btns[0]
        absolute_btn = mode_btns[1]
        assert "active" in relative_btn["class"].split()
        assert "active" not in absolute_btn["class"].split()
        assert relative_btn["aria-pressed"] == "true"
        assert absolute_btn["aria-pressed"] == "false"


def test_explain_timeline_ask_wire_from_to_datetime_local() -> None:
    by_render = {form["attrs"]["data-render"]: form for form in _parse_forms()}
    for key in ("explain", "timeline", "ask"):
        names = {inp["name"] for inp in by_render[key]["inputs"] if inp.get("name")}
        assert "since" in names
        assert "from_time" in names
        assert "to_time" in names
        assert "window_a_from" not in names
        dt_names = {
            inp["name"]
            for inp in by_render[key]["inputs"]
            if inp.get("type") == "datetime-local"
        }
        assert dt_names == {"from_time", "to_time"}
        for inp in by_render[key]["inputs"]:
            if inp.get("type") == "datetime-local":
                assert "disabled" in inp


def test_compare_wires_window_a_b_datetime_local() -> None:
    by_render = {form["attrs"]["data-render"]: form for form in _parse_forms()}
    compare = by_render["compare"]
    names = {inp["name"] for inp in compare["inputs"] if inp.get("name")}
    assert "since" in names
    assert "baseline" in names
    assert names >= {"window_a_from", "window_a_to", "window_b_from", "window_b_to"}
    assert "from_time" not in names
    dt_names = {
        inp["name"] for inp in compare["inputs"] if inp.get("type") == "datetime-local"
    }
    assert dt_names == {"window_a_from", "window_a_to", "window_b_from", "window_b_to"}


def test_absolute_section_is_hidden_by_default() -> None:
    html = HTML_PATH.read_text(encoding="utf-8")
    assert html.count('class="window-absolute" hidden') == 4
    assert html.count('class="window-relative"') == 4
    css = CSS_PATH.read_text(encoding="utf-8")
    assert ".window-absolute[hidden]" in css
    assert "datetime-local" in css
