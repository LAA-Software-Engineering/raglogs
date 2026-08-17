#!/usr/bin/env python3
"""Export Pydantic v1 query models to clients/jsonschema/*.v1.json.

Works the same way CI tests do: ``PYTHONPATH=. python scripts/export_jsonschema.py``.
Does not start a server or connect to the database.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.api.schemas.v1 import (  # noqa: E402
    AskResponse,
    ClustersResponse,
    CompareResponse,
    ExplainResponse,
    TimelineResponse,
)

MODELS: tuple[tuple[str, type], ...] = (
    ("explain.v1.json", ExplainResponse),
    ("timeline.v1.json", TimelineResponse),
    ("compare.v1.json", CompareResponse),
    ("ask.v1.json", AskResponse),
    ("clusters.v1.json", ClustersResponse),
)


def main() -> None:
    out_dir = ROOT / "clients" / "jsonschema"
    out_dir.mkdir(parents=True, exist_ok=True)
    for filename, model in MODELS:
        path = out_dir / filename
        schema = model.model_json_schema()
        path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {path}")


if __name__ == "__main__":
    main()
