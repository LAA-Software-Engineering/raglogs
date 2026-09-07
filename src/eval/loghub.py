"""Benchmark raglogs' normalization/fingerprinting against Loghub-2.0 templates.

Loghub-2.0 labels are parse templates, not incident narratives, so this scores
the *normalization* layer, not explanations: run ``fingerprint_message`` over a
system's lines, group by fingerprint, and compare the induced partition against
the ground-truth ``EventId`` labels.

Two numbers per system:

- **Grouping accuracy (GA)** — the standard Loghub message-level metric: the
  fraction of messages whose induced group is *exactly* their ground-truth
  group. Over-merging and fragmentation both lower it.
- **Template-count ratio** — ``induced / ground-truth`` distinct templates.
  ``>1`` means fragmentation, ``<1`` means over-merging.

The scoring here is pure and unit-tested. Loghub is research/academic-use only
(attribution + citation required); it is downloaded on demand and never
committed — see ``tests/eval/README.md``.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from collections import defaultdict

from src.core.normalization.fingerprint import fingerprint_message


def load_structured_csv(text: str) -> list[tuple[str, str]]:
    """Parse a Loghub ``*_structured.csv`` into ``(content, ground_truth_label)``.

    Uses ``Content`` for the message and ``EventId`` for the label, falling back
    to ``EventTemplate`` when ``EventId`` is absent. Rows missing content are
    skipped.
    """
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return []
    fields = {f.strip(): f for f in reader.fieldnames}
    content_key = fields.get("Content")
    label_key = fields.get("EventId") or fields.get("EventTemplate")
    if content_key is None or label_key is None:
        raise ValueError("structured CSV needs a Content column and EventId/EventTemplate")

    rows: list[tuple[str, str]] = []
    for row in reader:
        content = (row.get(content_key) or "").strip()
        label = (row.get(label_key) or "").strip()
        if not content:
            continue
        rows.append((content, label))
    return rows


def induced_labels(contents: list[str]) -> list[str]:
    """Predicted cluster label per message: raglogs' fingerprint."""
    return [fingerprint_message(c)[1] for c in contents]


def grouping_accuracy(ground_truth: list[str], predicted: list[str]) -> float:
    """Standard Loghub grouping accuracy (message-level, exact group match).

    A message counts as correct iff the set of messages sharing its predicted
    label is identical to the set sharing its ground-truth label.
    """
    n = len(ground_truth)
    if n == 0:
        return 0.0
    if len(predicted) != n:
        raise ValueError("ground_truth and predicted must be the same length")

    gt_groups: dict[str, set[int]] = defaultdict(set)
    pred_groups: dict[str, set[int]] = defaultdict(set)
    for i, (g, p) in enumerate(zip(ground_truth, predicted)):
        gt_groups[g].add(i)
        pred_groups[p].add(i)

    correct = sum(
        1 for i in range(n) if pred_groups[predicted[i]] == gt_groups[ground_truth[i]]
    )
    return correct / n


def template_count_ratio(ground_truth: list[str], predicted: list[str]) -> float:
    """``distinct induced / distinct ground-truth`` (>1 fragments, <1 over-merges)."""
    gt = len(set(ground_truth))
    if gt == 0:
        return 0.0
    return len(set(predicted)) / gt


@dataclass
class LoghubResult:
    system: str
    n_lines: int
    ground_truth_templates: int
    induced_templates: int
    grouping_accuracy: float
    template_count_ratio: float

    def to_dict(self) -> dict:
        return {
            "system": self.system,
            "n_lines": self.n_lines,
            "ground_truth_templates": self.ground_truth_templates,
            "induced_templates": self.induced_templates,
            "grouping_accuracy": round(self.grouping_accuracy, 4),
            "template_count_ratio": round(self.template_count_ratio, 4),
        }


def score_system(system: str, rows: list[tuple[str, str]]) -> LoghubResult:
    """Score one system's ``(content, label)`` rows."""
    contents = [c for c, _ in rows]
    ground_truth = [g for _, g in rows]
    predicted = induced_labels(contents)
    return LoghubResult(
        system=system,
        n_lines=len(rows),
        ground_truth_templates=len(set(ground_truth)),
        induced_templates=len(set(predicted)),
        grouping_accuracy=grouping_accuracy(ground_truth, predicted),
        template_count_ratio=template_count_ratio(ground_truth, predicted),
    )
