#!/usr/bin/env python3
"""Download external eval corpora into tests/eval/cases/.

The harness ships with only the small, committed cases (the sample-incident
canary and negative cases). Large labeled corpora are downloaded here rather
than committed. The corpora themselves arrive in follow-up issues:

  - RCAEval RE2/RE3  (issue #78)
  - OpenTelemetry-demo + Chaos Mesh generated incidents (issue #79)
  - Loghub-2.0 normalization ground truth (issue #80)

Until a downloader lands, this is a no-op that documents the contract: each
corpus writes case directories under tests/eval/cases/ in the format described
in tests/eval/README.md, then `make eval` picks them up automatically.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "No external corpora are wired up yet — see issues #78/#79/#80.\n"
        "The committed cases under tests/eval/cases/ already run via `make eval`."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
