"""
Tests for MetricsCollector (Phase 3A).

Tests cover both the no-op stub behaviour when prometheus_client is absent
and (when available) the actual metric recording behaviour.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_collector_without_prometheus():
    """
    Return a MetricsCollector instance with prometheus_client forcibly absent.
    Patches the module-level flag so no prometheus objects are created.
    """
    import registry.metrics as m_mod

    original_available = m_mod._PROMETHEUS_AVAILABLE
    m_mod._PROMETHEUS_AVAILABLE = False
    try:
        from registry.metrics import MetricsCollector
        collector = MetricsCollector()
    finally:
        m_mod._PROMETHEUS_AVAILABLE = original_available
    return collector


# ── No-op tests (prometheus_client absent) ────────────────────────────────────

class TestNoOpWhenNotInstalled:
    def test_record_rpc_no_op(self):
        """record_rpc does not raise when prometheus is unavailable."""
        collector = _make_collector_without_prometheus()
        # Should not raise.
        collector.record_rpc("Health", "ok", 0.001)

    def test_record_announce_no_op(self):
        collector = _make_collector_without_prometheus()
        collector.record_announce("someShareId1234")

    def test_set_peers_online_no_op(self):
        collector = _make_collector_without_prometheus()
        collector.set_peers_online("shareId", 5)

    def test_record_sweep_no_op(self):
        collector = _make_collector_without_prometheus()
        collector.record_sweep(0.012)

    def test_set_active_watches_no_op(self):
        collector = _make_collector_without_prometheus()
        collector.set_active_watches(3)

    def test_enabled_flag_false(self):
        collector = _make_collector_without_prometheus()
        assert collector._enabled is False

    def test_start_metrics_server_no_op(self):
        """start_metrics_server should not raise when prometheus is absent."""
        import registry.metrics as m_mod
        original = m_mod._PROMETHEUS_AVAILABLE
        m_mod._PROMETHEUS_AVAILABLE = False
        try:
            # Should log a warning but not raise.
            m_mod.start_metrics_server(9090)
        finally:
            m_mod._PROMETHEUS_AVAILABLE = original


# ── Tests when prometheus_client IS available ─────────────────────────────────

def _prometheus_available():
    import importlib.util
    return importlib.util.find_spec("prometheus_client") is not None


def _make_collector_with_fresh_registry():
    """
    Create a MetricsCollector backed by a fresh prometheus CollectorRegistry
    so test instances don't conflict with each other.
    """
    import prometheus_client  # type: ignore
    import registry.metrics as m_mod

    fresh_registry = prometheus_client.CollectorRegistry()

    # Monkey-patch the module-level metric constructors to use our fresh registry.
    original_counter    = m_mod.Counter
    original_gauge      = m_mod.Gauge
    original_histogram  = m_mod.Histogram

    def _counter(*args, **kwargs):
        kwargs.setdefault("registry", fresh_registry)
        return original_counter(*args, **kwargs)

    def _gauge(*args, **kwargs):
        kwargs.setdefault("registry", fresh_registry)
        return original_gauge(*args, **kwargs)

    def _histogram(*args, **kwargs):
        kwargs.setdefault("registry", fresh_registry)
        return original_histogram(*args, **kwargs)

    m_mod.Counter    = _counter
    m_mod.Gauge      = _gauge
    m_mod.Histogram  = _histogram
    try:
        collector = m_mod.MetricsCollector()
    finally:
        m_mod.Counter   = original_counter
        m_mod.Gauge     = original_gauge
        m_mod.Histogram = original_histogram

    return collector


@pytest.mark.skipif(
    not _prometheus_available(),
    reason="prometheus_client not installed",
)
class TestWithPrometheus:
    def test_enabled_flag_true(self):
        from registry.metrics import _PROMETHEUS_AVAILABLE
        assert _PROMETHEUS_AVAILABLE is True
        collector = _make_collector_with_fresh_registry()
        assert collector._enabled is True

    def test_record_rpc_increments_counter(self):
        collector = _make_collector_with_fresh_registry()
        # Should not raise; we just verify the calls succeed.
        collector.record_rpc("Announce", "ok", 0.005)
        collector.record_rpc("Health", "error", 0.001)

    def test_record_announce(self):
        collector = _make_collector_with_fresh_registry()
        # share label uses first 8 chars of share_id.
        share_id = "abcdef1234567890"
        collector.record_announce(share_id)

    def test_set_peers_online(self):
        collector = _make_collector_with_fresh_registry()
        collector.set_peers_online("shareId1", 3)
        collector.set_peers_online("shareId1", 0)

    def test_record_sweep(self):
        collector = _make_collector_with_fresh_registry()
        collector.record_sweep(0.025)

    def test_set_active_watches(self):
        collector = _make_collector_with_fresh_registry()
        collector.set_active_watches(10)
        collector.set_active_watches(0)
