"""
Tests for bandwidth quota policy handling.

Covers:
  - _effective_limit helper function
  - PeerEventHandler._apply_policy
  - Registry stream POLICY_UPDATED event dispatch
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from daemon.sync.peer_handler import PeerEventHandler, _effective_limit


# ---------------------------------------------------------------------------
# _effective_limit unit tests
# ---------------------------------------------------------------------------

def test_effective_limit_both_unlimited():
    assert _effective_limit(0, 0) == 0


def test_effective_limit_registry_only():
    assert _effective_limit(1000, 0) == 1000


def test_effective_limit_local_only():
    assert _effective_limit(0, 500) == 500


def test_effective_limit_both_set_takes_min():
    assert _effective_limit(1000, 500) == 500


def test_effective_limit_registry_more_restrictive():
    assert _effective_limit(100, 200) == 100


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_handler(db=None, lt=None) -> PeerEventHandler:
    """Construct a PeerEventHandler with mocked dependencies."""
    identity = MagicMock()
    identity.peer_id = "selfpeer123"

    db     = db or MagicMock()
    lt     = lt or MagicMock()

    return PeerEventHandler(
        identity            = identity,
        db                  = db,
        lt                  = lt,
        relay_config        = None,
        on_switch_to_remote = AsyncMock(),
        on_publish_event    = AsyncMock(),
        on_pause_watcher    = MagicMock(),
    )


# ---------------------------------------------------------------------------
# _apply_policy tests
# ---------------------------------------------------------------------------

def test_apply_policy_skips_local_only():
    """Local-only shares must never have their libtorrent limits changed."""
    lt = MagicMock()
    db = MagicMock()

    local_share = MagicMock()
    local_share.local_only = True
    db.get_share.return_value = local_share

    handler = _make_handler(db=db, lt=lt)
    handler._apply_policy("share-abc", upload_bps=5000, download_bps=10000)

    lt.set_share_rate_limit.assert_not_called()


def test_apply_policy_calls_lt():
    """Registry policy is applied to libtorrent with local-limit merging."""
    lt = MagicMock()
    db = MagicMock()

    local_share = MagicMock()
    local_share.local_only    = False
    local_share.upload_limit  = 2000   # local cap - more restrictive than registry
    local_share.download_limit = 0     # local: unlimited
    db.get_share.return_value = local_share

    handler = _make_handler(db=db, lt=lt)
    # Registry says 5000 up / 8000 down; local says 2000 up / unlimited down.
    # Effective: min(5000, 2000)=2000 up, min(8000, 0->unlimited)=8000 down.
    handler._apply_policy("share-abc", upload_bps=5000, download_bps=8000)

    lt.set_share_rate_limit.assert_called_once_with("share-abc", 2000, 8000)


def test_apply_policy_calls_lt_no_local_limits():
    """When no local caps are set, the registry policy is applied directly."""
    lt = MagicMock()
    db = MagicMock()

    local_share = MagicMock()
    local_share.local_only    = False
    local_share.upload_limit  = 0
    local_share.download_limit = 0
    db.get_share.return_value = local_share

    handler = _make_handler(db=db, lt=lt)
    handler._apply_policy("share-xyz", upload_bps=3000, download_bps=6000)

    lt.set_share_rate_limit.assert_called_once_with("share-xyz", 3000, 6000)


def test_apply_policy_missing_share_is_noop():
    """If the share is not in the DB, _apply_policy must not crash."""
    lt = MagicMock()
    db = MagicMock()
    db.get_share.return_value = None

    handler = _make_handler(db=db, lt=lt)
    handler._apply_policy("nonexistent", upload_bps=1000, download_bps=1000)

    lt.set_share_rate_limit.assert_not_called()


# ---------------------------------------------------------------------------
# Registry handler POLICY_UPDATED dispatch test
# ---------------------------------------------------------------------------

def test_policy_event_handler_calls_apply():
    """
    POLICY_UPDATED events from the registry stream must call _apply_policy
    and publish a policy_updated control event, without touching peer state.
    """
    # Build a minimal fake PeerEvent with type == 5 (POLICY_UPDATED).
    policy = MagicMock()
    policy.upload_limit_bps  = 4096
    policy.download_limit_bps = 8192

    event = MagicMock()
    event.type   = 5           # EVENT_TYPE_POLICY_UPDATED
    event.policy = policy

    # Stub pb so the handler can resolve the constant.
    fake_pb = MagicMock()
    fake_pb.PeerEvent.EVENT_TYPE_POLICY_UPDATED = 5
    fake_pb.PeerEvent.EVENT_TYPE_ONLINE         = 1
    fake_pb.PeerEvent.EVENT_TYPE_UPDATED        = 2
    fake_pb.PeerEvent.EVENT_TYPE_OFFLINE        = 3
    fake_pb.PeerEvent.EVENT_TYPE_REMOVED        = 4

    publish_event = AsyncMock()

    handler = PeerEventHandler(
        identity            = MagicMock(peer_id="selfpeer123"),
        db                  = MagicMock(),
        lt                  = MagicMock(),
        relay_config        = None,
        on_switch_to_remote = AsyncMock(),
        on_publish_event    = publish_event,
        on_pause_watcher    = MagicMock(),
    )

    # Patch _apply_policy so we can verify it is called without side effects.
    handler._apply_policy = MagicMock()

    # Patch the lazy registry_pb2 import inside make_registry_handler.
    with patch("daemon.sync.peer_handler.PeerEventHandler.make_registry_handler") as _:
        pass  # we call it directly below with our fake_pb

    # Rebuild the handler closure with our fake pb module.
    import daemon.sync.peer_handler as ph_mod
    original_import = ph_mod.__builtins__  # keep for context

    share_id = "test-share-id-001"

    # Rather than patching the import machinery, invoke the handler
    # indirectly: directly construct the closure by patching registry_pb2.
    import sys
    fake_module = MagicMock()
    fake_module.PeerEvent = fake_pb.PeerEvent
    sys.modules["daemon.registry_pb2"] = fake_module

    try:
        inner_handler = handler.make_registry_handler(share_id)
        asyncio.run(inner_handler(event))
    finally:
        sys.modules.pop("daemon.registry_pb2", None)

    # _apply_policy must have been called with the policy values.
    handler._apply_policy.assert_called_once_with(share_id, 4096, 8192)

    # A policy_updated control event must have been published.
    publish_event.assert_called_once()
    published = publish_event.call_args[0][0]
    assert published["type"]         == "policy_updated"
    assert published["share_id"]     == share_id
    assert published["upload_bps"]   == 4096
    assert published["download_bps"] == 8192

    # No peer-event fields should be in the published dict.
    assert "peer_id" not in published
    assert "online"  not in published
