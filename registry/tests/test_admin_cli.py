"""
Tests for registry.cli.admin

These tests run without a live registry - gRPC stubs are mocked.
"""

from __future__ import annotations

import argparse
import types
from unittest.mock import MagicMock, patch

import pytest

from registry.cli.admin import (
    _fmt_uptime,
    _make_channel,
    _tls_label,
    cmd_health,
    cmd_audit_tail,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _args(**kwargs) -> argparse.Namespace:
    """Build a minimal args namespace with sensible defaults."""
    defaults = dict(
        registry   = "localhost:50051",
        tls        = False,
        ca_file    = None,
        cert_file  = None,
        key_file   = None,
        token      = "",
        audit_log  = None,
        lines      = 20,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ── _fmt_uptime ───────────────────────────────────────────────────────────────

def test_fmt_uptime_zero():
    assert _fmt_uptime(0) == "unknown"


def test_fmt_uptime_minutes():
    assert _fmt_uptime(90) == "1m"


def test_fmt_uptime_hours():
    assert _fmt_uptime(3 * 3600 + 45 * 60) == "3h 45m"


def test_fmt_uptime_days():
    result = _fmt_uptime(3 * 86400 + 4 * 3600)
    assert "3d" in result
    assert "4h" in result


# ── _tls_label ────────────────────────────────────────────────────────────────

def test_tls_label_no_tls():
    args = _args(registry="localhost:50051")
    assert _tls_label(args) == "no TLS"


def test_tls_label_forced():
    args = _args(registry="registry.example.com:50051", tls=True)
    assert _tls_label(args) == "TLS"


def test_tls_label_mtls():
    args = _args(registry="registry.example.com:443", cert_file="/etc/peerdup/client.crt")
    assert _tls_label(args) == "mTLS"


def test_tls_label_auto_detect_443():
    args = _args(registry="registry.example.com:443")
    assert _tls_label(args) == "TLS"


def test_tls_label_auto_detect_50443():
    args = _args(registry="registry.example.com:50443")
    assert _tls_label(args) == "TLS"


# ── _make_channel - TLS detection ─────────────────────────────────────────────

def test_channel_no_tls():
    """Port 50051 should produce an insecure channel."""
    import grpc

    args = _args(registry="localhost:50051")
    channel, use_tls = _make_channel(args)
    assert use_tls is False
    # Verify it's an insecure channel by checking its type name.
    assert "insecure" in type(channel).__name__.lower() or hasattr(channel, "_channel")
    channel.close()


def test_channel_tls_detection():
    """Port 443 should produce a secure channel without explicit --tls."""
    import grpc

    args = _args(registry="registry.example.com:443")
    channel, use_tls = _make_channel(args)
    assert use_tls is True
    channel.close()


def test_channel_tls_forced():
    """--tls flag should produce a secure channel regardless of port."""
    args = _args(registry="localhost:50051", tls=True)
    channel, use_tls = _make_channel(args)
    assert use_tls is True
    channel.close()


# ── cmd_health ────────────────────────────────────────────────────────────────

def _mock_health_response(**kwargs):
    resp = MagicMock()
    resp.status           = kwargs.get("status", "ok")
    resp.version          = kwargs.get("version", "0.1.0")
    resp.uptime_s         = kwargs.get("uptime_s", 3600)
    resp.peers_registered = kwargs.get("peers_registered", 12)
    resp.peers_online_now = kwargs.get("peers_online_now", 7)
    resp.shares_registered = kwargs.get("shares_registered", 5)
    resp.db_ok            = kwargs.get("db_ok", True)
    resp.ttl_sweep_ok     = kwargs.get("ttl_sweep_ok", True)
    return resp


def _patch_health(stub, fake_pb2=None):
    """
    Context manager that patches both _make_stub and the registry_pb2 import
    used inside cmd_health. The proto import happens before the stub call so
    both must be patched together when stubs are not generated.
    """
    import sys
    import types

    # Build a minimal fake pb2 module with a HealthRequest class.
    pb2 = fake_pb2 or types.SimpleNamespace(HealthRequest=MagicMock)

    return (
        patch("registry.cli.admin._make_stub", return_value=stub),
        patch.dict("sys.modules", {"registry.registry_pb2": pb2}),
    )


def test_health_command_calls_health_rpc(capsys):
    """Health command should call Health() and display formatted output."""
    args = _args(registry="localhost:50051")
    fake_resp = _mock_health_response()

    stub = MagicMock()
    stub.Health.return_value = fake_resp

    p1, p2 = _patch_health(stub)
    with p1, p2:
        rc = cmd_health(args)

    stub.Health.assert_called_once()
    assert rc == 0
    out = capsys.readouterr().out
    assert "localhost:50051" in out
    assert "12 registered" in out
    assert "7 online now" in out
    assert "5 registered" in out


def test_health_command_status_ok_returns_0(capsys):
    args = _args(registry="localhost:50051")
    fake_resp = _mock_health_response(status="ok")

    stub = MagicMock()
    stub.Health.return_value = fake_resp

    p1, p2 = _patch_health(stub)
    with p1, p2:
        rc = cmd_health(args)

    assert rc == 0


def test_health_command_status_degraded_returns_2(capsys):
    args = _args(registry="localhost:50051")
    fake_resp = _mock_health_response(status="degraded")

    stub = MagicMock()
    stub.Health.return_value = fake_resp

    p1, p2 = _patch_health(stub)
    with p1, p2:
        rc = cmd_health(args)

    assert rc == 2


def test_health_unreachable(capsys):
    """When the registry is unreachable, print an error and return non-zero."""
    import grpc

    args = _args(registry="localhost:19999")

    rpc_error = grpc.RpcError()
    rpc_error.code    = lambda: grpc.StatusCode.UNAVAILABLE
    rpc_error.details = lambda: "Connection refused"

    stub = MagicMock()
    stub.Health.side_effect = rpc_error

    p1, p2 = _patch_health(stub)
    with p1, p2:
        rc = cmd_health(args)

    assert rc == 1
    err = capsys.readouterr().err
    assert "error" in err.lower()
    assert "localhost:19999" in err


# ── cmd_audit_tail ────────────────────────────────────────────────────────────

def test_audit_tail_no_log_path(capsys):
    args = _args(audit_log=None, lines=20)
    rc = cmd_audit_tail(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "--audit-log" in out


def test_audit_tail_reads_last_n_lines(tmp_path, capsys):
    log_file = tmp_path / "audit.log"
    log_file.write_text("\n".join(f"line {i}" for i in range(1, 51)) + "\n")

    args = _args(audit_log=str(log_file), lines=10)
    rc = cmd_audit_tail(args)

    assert rc == 0
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l]
    assert len(lines) == 10
    assert lines[-1] == "line 50"
    assert lines[0] == "line 41"


def test_audit_tail_missing_file(capsys):
    args = _args(audit_log="/nonexistent/audit.log", lines=20)
    rc = cmd_audit_tail(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "error" in err.lower()
