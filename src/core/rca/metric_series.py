"""Metric series identity (#209 M2b) — one definition shared by every reducer of metric samples.

A metric *name* is not a series. One instrument can emit several series — per datapoint attribute
(``cpu.mode``, ``system.memory.state``, a route) and per reporting instance (replicas) — and averaging
across them produces a number that describes no real series. Ingestion records a sample's series
identity in ``attributes``; every consumer that reduces metric samples must group by it.

Three levels of knowledge, deliberately kept distinct:

* ``attributes is None`` — the source never recorded identity (legacy corpora, RCAEval). All such
  samples of a ``(service, metric)`` form one legacy group, exactly as before identity existed.
* a dict **with** a named instance — the series is fully identified.
* a dict **without** a named instance (``{}`` included) — the datapoint attributes are known, but the
  reporting process is not: two replicas look like one series. That is *not* a verified series.
"""
from __future__ import annotations

import json
from typing import Optional

# Resource attributes that each name one reporting process (OpenTelemetry semantic conventions).
INSTANCE_ATTRS: tuple[str, ...] = ("service.instance.id", "k8s.pod.uid", "container.id")
# Attributes that name one process only together (a pid alone repeats across hosts).
INSTANCE_ATTR_PAIRS: tuple[tuple[str, str], ...] = (("host.name", "process.pid"),)
_INSTANCE_KEYS = frozenset(INSTANCE_ATTRS) | {k for pair in INSTANCE_ATTR_PAIRS for k in pair}


def series_key(attributes) -> Optional[str]:
    """Canonical full series identity (datapoint attributes + instance), or ``None`` when the source
    never recorded identity. Reducers group by ``(service, metric, series_key)``."""
    if attributes is None:
        return None
    return json.dumps(attributes, sort_keys=True, default=str)


def instance_identity(attributes) -> Optional[str]:
    """The reporting instance named by the recorded attributes, or ``None`` if none is named."""
    if not isinstance(attributes, dict):
        return None
    for key in INSTANCE_ATTRS:
        if attributes.get(key):
            return f"{key}={attributes[key]}"
    for a, b in INSTANCE_ATTR_PAIRS:
        if attributes.get(a) and attributes.get(b):
            return f"{a}={attributes[a]},{b}={attributes[b]}"
    return None


def datapoint_signature(attributes) -> Optional[str]:
    """The datapoint-attribute part of the identity (instance attributes removed), or ``None`` when
    identity was never recorded. Two samples with different signatures are different *modes* of the
    instrument (e.g. ``cpu.mode=idle`` vs ``user``), not different replicas."""
    if attributes is None:
        return None
    return json.dumps({k: v for k, v in attributes.items() if k not in _INSTANCE_KEYS},
                      sort_keys=True, default=str)


def instance_attributes(resource_attrs: dict[str, str]) -> dict[str, str]:
    """The instance-naming attributes present on a resource, for the converter to record."""
    out = {k: resource_attrs[k] for k in INSTANCE_ATTRS if resource_attrs.get(k)}
    for a, b in INSTANCE_ATTR_PAIRS:
        if resource_attrs.get(a) and resource_attrs.get(b):
            out[a], out[b] = resource_attrs[a], resource_attrs[b]
    return out
