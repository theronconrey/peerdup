"""
Optional Prometheus metrics endpoint.

Only active if prometheus_client is installed. Serves on a separate HTTP port
(default 9090) so the gRPC port is not affected.

Install with: pip install prometheus-client
"""

import logging

log = logging.getLogger(__name__)

try:
    import prometheus_client  # type: ignore
    from prometheus_client import Counter, Gauge, Histogram, start_http_server
    _PROMETHEUS_AVAILABLE = True
except ImportError:
    prometheus_client = None  # type: ignore
    _PROMETHEUS_AVAILABLE = False


class MetricsCollector:
    """
    Wraps prometheus_client metrics for peerdup registry.

    If prometheus_client is not installed, all methods are no-ops so the
    rest of the codebase does not need to guard every call.
    """

    def __init__(self):
        if not _PROMETHEUS_AVAILABLE:
            self._enabled = False
            return

        self._enabled = True

        self._rpc_requests = Counter(
            "peerdup_rpc_requests_total",
            "Total gRPC requests by RPC name and status",
            ["rpc", "status"],
        )
        self._rpc_latency = Histogram(
            "peerdup_rpc_latency_seconds",
            "gRPC request latency in seconds",
            ["rpc"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
        )
        self._announce_total = Counter(
            "peerdup_announce_total",
            "Total announces by share (first 8 chars of share_id)",
            ["share"],
        )
        self._peers_online = Gauge(
            "peerdup_peers_online",
            "Number of currently online peers per share",
            ["share"],
        )
        self._ttl_sweep_duration = Histogram(
            "peerdup_ttl_sweep_duration_seconds",
            "Duration of each TTL sweep in seconds",
            buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0),
        )
        self._active_watches = Gauge(
            "peerdup_active_watch_streams",
            "Number of active WatchSharePeers streaming connections",
        )

    def record_rpc(self, rpc_name: str, status: str, latency_s: float) -> None:
        """Record a completed RPC call."""
        if not self._enabled:
            return
        self._rpc_requests.labels(rpc=rpc_name, status=status).inc()
        self._rpc_latency.labels(rpc=rpc_name).observe(latency_s)

    def record_announce(self, share_id: str) -> None:
        """Increment the announce counter for a share."""
        if not self._enabled:
            return
        self._announce_total.labels(share=share_id[:8]).inc()

    def set_peers_online(self, share_id: str, count: int) -> None:
        """Set the peers-online gauge for a share."""
        if not self._enabled:
            return
        self._peers_online.labels(share=share_id[:8]).set(count)

    def record_sweep(self, duration_s: float) -> None:
        """Record a TTL sweep duration."""
        if not self._enabled:
            return
        self._ttl_sweep_duration.observe(duration_s)

    def set_active_watches(self, count: int) -> None:
        """Set the active WatchSharePeers gauge."""
        if not self._enabled:
            return
        self._active_watches.set(count)


def start_metrics_server(port: int) -> None:
    """
    Start the Prometheus HTTP metrics server on the given port.

    No-op if prometheus_client is not installed.
    """
    if not _PROMETHEUS_AVAILABLE:
        log.warning(
            "Metrics enabled in config but prometheus-client is not installed. "
            "Run: pip install prometheus-client"
        )
        return
    start_http_server(port)
    log.info("Prometheus metrics endpoint started on :%d/metrics", port)
