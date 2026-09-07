"""Performance benchmark for raglogs (issue #85).

Ingests N synthetic log lines and explains the window, recording wall time and
query counts per phase so regressions are visible. The reusable, unit-tested
pieces live in :mod:`src.perf.bench`; ``scripts/benchmark.py`` drives them
against a live database.
"""
