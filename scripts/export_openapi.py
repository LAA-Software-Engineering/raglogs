#!/usr/bin/env python3
"""Export the FastAPI OpenAPI schema to clients/openapi.json.

Works the same way CI tests do: ``PYTHONPATH=. python scripts/export_openapi.py``.
Does not start a server or connect to the database.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.api.app import app  # noqa: E402


def main() -> None:
    out_dir = ROOT / "clients"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "openapi.json"
    schema = app.openapi()
    out.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
