"""
Tests for `peerdup registry` subcommand group.

These tests mock the gRPC stub directly - no daemon process required.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_stub(overrides: dict | None = None):
    """Return a MagicMock stub with sensible defaults."""
    stub = MagicMock()
    defaults = {
        "RegistryHealth": MagicMock(return_value=SimpleNamespace(
            status           = "ok",
            version          = "0.1.0",
            uptime_s         = 3 * 86400 + 4 * 3600,  # 3d 4h
            peers_registered = 12,
            shares_registered = 5,
            peers_online_now = 7,
            db_ok            = True,
            ttl_sweep_ok     = True,
            error_message    = "",
        )),
        "RegistryStatus": MagicMock(return_value=SimpleNamespace(
            registry_address  = "registry.example.com:443",
            tls_enabled       = True,
            ca_file           = "/etc/peerdup/ca.crt",
            mtls_configured   = False,
            connection_state  = "connected",
            last_rpc_ok_ago_s = 4,
            token_valid       = True,
        )),
    }
    if overrides:
        defaults.update(overrides)
    for attr, val in defaults.items():
        setattr(stub, attr, val)
    return stub


def _run_cmd(cmd_fn, args_ns, stub):
    """Invoke a CLI command function with a mocked _stub() and mocked control_pb2."""
    # control_pb2 may not be generated in CI (requires protoc).
    # Provide a minimal mock so the CLI can call RegistryHealthRequest() etc.
    mock_pb2 = MagicMock()
    mock_pb2.RegistryHealthRequest.return_value = MagicMock()
    mock_pb2.RegistryStatusRequest.return_value = MagicMock()

    import sys
    with patch.dict(sys.modules, {"daemon.control_pb2": mock_pb2}), \
         patch("cli.peerdup._stub", return_value=stub):
        cmd_fn(args_ns)


# ── Tests for registry health ─────────────────────────────────────────────────

def test_registry_health_not_configured(capsys):
    from cli import peerdup as cli

    stub = _make_stub({
        "RegistryHealth": MagicMock(return_value=SimpleNamespace(
            status           = "not_configured",
            version          = "",
            uptime_s         = 0,
            peers_registered = 0,
            shares_registered = 0,
            peers_online_now = 0,
            db_ok            = False,
            ttl_sweep_ok     = False,
            error_message    = "",
        )),
    })

    args = SimpleNamespace(socket="/run/peerdup/control.sock")
    _run_cmd(cli.cmd_registry_health, args, stub)

    out = capsys.readouterr().out
    assert "no registry configured" in out.lower()
    assert "peerdup-setup" in out


def test_registry_health_unreachable(capsys):
    from cli import peerdup as cli

    stub = _make_stub({
        "RegistryHealth": MagicMock(return_value=SimpleNamespace(
            status           = "unreachable",
            version          = "",
            uptime_s         = 0,
            peers_registered = 0,
            shares_registered = 0,
            peers_online_now = 0,
            db_ok            = False,
            ttl_sweep_ok     = False,
            error_message    = "connection refused",
        )),
    })

    args = SimpleNamespace(socket="/run/peerdup/control.sock")
    _run_cmd(cli.cmd_registry_health, args, stub)

    out = capsys.readouterr().out
    assert "unreachable" in out
    assert "connection refused" in out


def test_registry_health_ok(capsys):
    from cli import peerdup as cli

    stub = _make_stub()
    args = SimpleNamespace(socket="/run/peerdup/control.sock")
    _run_cmd(cli.cmd_registry_health, args, stub)

    out = capsys.readouterr().out
    assert "ok" in out
    assert "0.1.0" in out
    assert "12" in out   # peers_registered
    assert "7" in out    # peers_online_now
    assert "5" in out    # shares_registered
    # Uptime should show days
    assert "3d" in out


# ── Tests for registry status ─────────────────────────────────────────────────

def test_registry_status_human(capsys):
    from cli import peerdup as cli

    stub = _make_stub()
    args = SimpleNamespace(socket="/run/peerdup/control.sock", json=False)
    _run_cmd(cli.cmd_registry_status, args, stub)

    out = capsys.readouterr().out
    assert "registry.example.com:443" in out
    assert "enabled" in out          # TLS enabled
    assert "/etc/peerdup/ca.crt" in out
    assert "not configured" in out   # mTLS not configured
    assert "connected" in out
    assert "4s ago" in out
    assert "valid" in out


def test_registry_status_json(capsys):
    from cli import peerdup as cli
    import json

    stub = _make_stub()
    args = SimpleNamespace(socket="/run/peerdup/control.sock", json=True)
    _run_cmd(cli.cmd_registry_status, args, stub)

    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert data["connection_state"]  == "connected"
    assert data["registry_address"]  == "registry.example.com:443"
    assert data["tls_enabled"]       is True


def test_registry_status_not_configured(capsys):
    from cli import peerdup as cli

    stub = _make_stub({
        "RegistryStatus": MagicMock(return_value=SimpleNamespace(
            registry_address  = "",
            tls_enabled       = False,
            ca_file           = "",
            mtls_configured   = False,
            connection_state  = "not_configured",
            last_rpc_ok_ago_s = -1,
            token_valid       = False,
        )),
    })

    args = SimpleNamespace(socket="/run/peerdup/control.sock", json=False)
    _run_cmd(cli.cmd_registry_status, args, stub)

    out = capsys.readouterr().out
    assert "no registry configured" in out.lower()
