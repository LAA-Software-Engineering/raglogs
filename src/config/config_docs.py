"""Generate the configuration reference from the Settings model.

The env-var docs used to be hand-maintained in the README and drift from
``src/config/settings.py``. This derives them from the model itself so they
cannot: ``render_config_reference`` is pure and unit-tested, ``make config-docs``
writes ``docs/configuration.md``, and a unit test fails if the committed file is
stale.
"""

from __future__ import annotations

import types
import typing

from pydantic_settings import BaseSettings

from src.config.settings import Settings

_REQUIRED = object()  # field has no default


def _render_type(annotation: object) -> str:
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    if origin is typing.Literal:
        return " \\| ".join(str(a) for a in args)

    if origin in (typing.Union, types.UnionType):
        non_none = [a for a in args if a is not type(None)]
        rendered = " \\| ".join(_render_type(a) for a in non_none)
        if type(None) in args:
            return f"{rendered} (optional)"
        return rendered

    return getattr(annotation, "__name__", str(annotation))


def _field_default(field) -> object:
    from pydantic_core import PydanticUndefined

    if field.default is not PydanticUndefined:
        return field.default
    if field.default_factory is not None:  # pragma: no cover - no factory defaults today
        try:
            return field.default_factory()
        except Exception:
            return _REQUIRED
    return _REQUIRED


def _render_default(value: object) -> str:
    if value is _REQUIRED:
        return "_(required)_"
    if value is None:
        return "_(unset)_"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if value == "":
        return '`""`'
    return f"`{value}`"


def render_config_reference(settings_cls: type[BaseSettings] = Settings) -> str:
    """Render a Markdown config reference from a settings model.

    One row per field: the env var (the upper-cased field name, since the model
    reads unprefixed env vars), its type, and its default.
    """
    lines = [
        "# Configuration",
        "",
        "Every setting is read from an environment variable (see `.env.example`)."
        " This table is generated from `src/config/settings.py` by"
        " `make config-docs` — edit the model, not this file.",
        "",
        "| Env var | Type | Default |",
        "| --- | --- | --- |",
    ]
    for name, field in settings_cls.model_fields.items():
        env = name.upper()
        type_str = _render_type(field.annotation)
        default_str = _render_default(_field_default(field))
        lines.append(f"| `{env}` | {type_str} | {default_str} |")
    return "\n".join(lines) + "\n"
