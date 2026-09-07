#!/usr/bin/env python3
"""Write docs/configuration.md from the Settings model.

Run via `make config-docs`. A unit test (tests/unit/test_config_docs.py) fails
if the committed file is out of date, so the reference can't silently drift.
"""

from __future__ import annotations

import sys
from pathlib import Path

OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "configuration.md"


def main() -> int:
    from src.config.config_docs import render_config_reference

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(render_config_reference())
    print(f"Wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
