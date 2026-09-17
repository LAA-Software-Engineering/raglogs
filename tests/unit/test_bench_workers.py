"""Unit tests for the multi-worker scaling harness pure logic (no DB, no server)."""

from scripts.bench_workers import _find_free_port


def test_find_free_port_returns_usable_port():
    port = _find_free_port()
    assert isinstance(port, int)
    assert 1024 < port < 65536


def test_find_free_port_varies():
    # two consecutive requests should (almost always) differ — the OS hands out
    # a fresh ephemeral port each time the probe socket is closed.
    ports = {_find_free_port() for _ in range(5)}
    assert len(ports) >= 2
