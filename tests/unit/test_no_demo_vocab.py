"""Guard against demo-fitted vocabulary leaking back into the analysis engine.

#81: the analysis engine must not hard-code strings from the sample fixture
('checkout', 'evt_', 'Stripe', and the demo's 'webhook' narrative labels).
Narrative must derive from cluster relationships, not fixture string matching.

#109: the guard must NOT flag the *legitimate* webhook features — the ingest
completion-webhook delivery subsystem and the generic "webhook config changed"
trigger classifier are real, source-agnostic features, not demo fitting. So the
word "webhook" is allowed only in those two files; the fixture-specific tokens
are banned everywhere in src/core.
"""
from pathlib import Path

CORE = Path(__file__).resolve().parents[2] / "src" / "core"

# Tokens taken verbatim from sample_data/sample_incident — never legitimate in
# a source-agnostic analysis engine.
BANNED_TOKENS = ("checkout", "evt_", "stripe")

# "webhook" is a real feature word, not demo fitting, in exactly these files:
WEBHOOK_ALLOWLIST = {
    CORE / "ingestion" / "webhooks.py",   # ingest completion-webhook delivery
    CORE / "normalization" / "patterns.py",  # generic "webhook config changed" trigger
}


def _core_py_files():
    return sorted(CORE.rglob("*.py"))


def test_no_fixture_tokens_in_core():
    offenders = []
    for path in _core_py_files():
        for i, line in enumerate(path.read_text().splitlines(), 1):
            low = line.lower()
            for tok in BANNED_TOKENS:
                if tok in low:
                    offenders.append(f"{path.relative_to(CORE.parents[1])}:{i}: {tok!r} -> {line.strip()}")
    assert not offenders, (
        "Demo-fitted vocabulary found in src/core (#81). Derive narrative from "
        "cluster relationships, not fixture strings:\n" + "\n".join(offenders)
    )


def test_webhook_word_only_in_legitimate_files():
    offenders = []
    for path in _core_py_files():
        if path in WEBHOOK_ALLOWLIST:
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if "webhook" in line.lower():
                offenders.append(f"{path.relative_to(CORE.parents[1])}:{i}: {line.strip()}")
    assert not offenders, (
        "The word 'webhook' appears outside the legitimate delivery/trigger "
        "files (#81/#109) — the analysis narrative must stay source-agnostic:\n"
        + "\n".join(offenders)
    )
