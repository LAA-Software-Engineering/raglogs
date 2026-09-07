"""The generated config reference must stay in sync with the Settings model."""

from pathlib import Path

from src.config.config_docs import render_config_reference
from src.config.settings import Settings

DOC = Path(__file__).resolve().parents[2] / "docs" / "configuration.md"


def test_committed_doc_is_up_to_date():
    """Fails if someone changed Settings without running `make config-docs`."""
    assert DOC.exists(), "docs/configuration.md is missing; run `make config-docs`"
    assert DOC.read_text() == render_config_reference(), (
        "docs/configuration.md is stale — run `make config-docs` and commit the result"
    )


def test_every_setting_is_documented():
    rendered = render_config_reference()
    for name in Settings.model_fields:
        assert f"`{name.upper()}`" in rendered, f"{name} missing from the config reference"


def test_renders_literal_and_bool_and_empty_string():
    rendered = render_config_reference()
    assert "disabled \\| openai \\| local" in rendered  # Literal
    assert "| `AUTH_ENABLED` | bool | `false` |" in rendered  # bool default
    assert '`""`' in rendered  # empty-string default (e.g. OPENAI_API_KEY)
