"""
Tests for the Health RPC (Phase 2A).

Uses an in-memory SQLite DB and a direct RegistryServicer instance --
no gRPC server needed since we call the servicer methods directly.
Requires generated stubs (run `make proto` in registry/ first).
"""

import time
from datetime import timedelta
from unittest.mock import MagicMock

import base58
import pytest
from nacl.signing import SigningKey
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from registry.auth.crypto import decode_peer_id, generate_token, hash_token
from registry.db.models import (
    AnnouncementModel,
    Base,
    PeerModel,
    PermissionEnum,
    ShareModel,
    SharePeerModel,
    create_tables,
    utcnow,
)
from registry.servicer.registry import RegistryServicer
from registry.ttl import TTL_SWEEP_INTERVAL_SECONDS


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return factory


@pytest.fixture()
def servicer(session_factory):
    return RegistryServicer(session_factory)


def _fake_context():
    ctx = MagicMock()
    ctx.peer.return_value = "ipv4:127.0.0.1:12345"
    ctx.is_active.return_value = True
    return ctx


def _make_peer(session_factory, name="test-peer"):
    """Insert a PeerModel row directly and return (peer_id, token)."""
    sk = SigningKey.generate()
    peer_id = base58.b58encode(bytes(sk.verify_key)).decode()
    token = generate_token()
    with session_factory() as session:
        session.add(PeerModel(
            peer_id=peer_id,
            name=name,
            token_hash=hash_token(token),
        ))
        session.commit()
    return peer_id, token


def _make_share(session_factory, owner_id, share_name="s"):
    """Insert a ShareModel + owner SharePeerModel directly."""
    sk = SigningKey.generate()
    share_id = base58.b58encode(bytes(sk.verify_key)).decode()
    with session_factory() as session:
        session.add(ShareModel(share_id=share_id, name=share_name, owner_id=owner_id))
        session.add(SharePeerModel(
            share_id=share_id, peer_id=owner_id, permission=PermissionEnum.READ_WRITE
        ))
        session.commit()
    return share_id


def _make_announcement(session_factory, share_id, peer_id, ttl_seconds=300):
    """Insert an AnnouncementModel with the given TTL from now."""
    from datetime import timezone
    now = utcnow()
    expires = now + timedelta(seconds=ttl_seconds)
    with session_factory() as session:
        session.add(AnnouncementModel(
            share_id=share_id,
            peer_id=peer_id,
            internal_addrs="[]",
            announced_at=now,
            expires_at=expires,
        ))
        session.commit()


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestHealthOk:
    def test_health_ok_after_sweep(self, servicer):
        """status='ok' when db is healthy and sweep has run recently."""
        import registry_pb2 as pb  # type: ignore

        servicer.record_sweep()  # simulate a recent sweep

        resp = servicer.Health(pb.HealthRequest(), _fake_context())

        assert resp.status == "ok"
        assert resp.db_ok is True
        assert resp.ttl_sweep_ok is True
        assert resp.version != ""
        assert resp.uptime_s >= 0

    def test_health_degraded_no_sweep_yet(self, servicer):
        """
        status='degraded' when the sweep has never run and enough time has passed.

        We fake elapsed time by backdating _start_time so the grace period expires.
        """
        import registry_pb2 as pb  # type: ignore

        # Backdate start time so 2x sweep interval has elapsed.
        servicer._start_time = time.monotonic() - (2 * TTL_SWEEP_INTERVAL_SECONDS + 1)

        resp = servicer.Health(pb.HealthRequest(), _fake_context())

        # DB should be fine; sweep has not run.
        assert resp.db_ok is True
        assert resp.ttl_sweep_ok is False
        assert resp.status == "degraded"

    def test_health_ok_within_grace_period(self, servicer):
        """
        ttl_sweep_ok=True when no sweep has run but server just started
        (within the 2x grace period).
        """
        import registry_pb2 as pb  # type: ignore

        # _start_time was just set in __init__, so grace period has not expired.
        resp = servicer.Health(pb.HealthRequest(), _fake_context())

        assert resp.ttl_sweep_ok is True

    def test_health_sweep_stale(self, servicer):
        """ttl_sweep_ok=False when the last sweep was too long ago."""
        import registry_pb2 as pb  # type: ignore

        servicer._last_sweep_at = (
            time.monotonic() - (2 * TTL_SWEEP_INTERVAL_SECONDS + 1)
        )

        resp = servicer.Health(pb.HealthRequest(), _fake_context())

        assert resp.ttl_sweep_ok is False
        assert resp.status == "degraded"


class TestHealthCounts:
    def test_counts_empty_db(self, servicer):
        """Counts are zero for an empty database."""
        import registry_pb2 as pb  # type: ignore

        servicer.record_sweep()
        resp = servicer.Health(pb.HealthRequest(), _fake_context())

        assert resp.peers_registered == 0
        assert resp.shares_registered == 0
        assert resp.peers_online_now == 0

    def test_counts_with_data(self, session_factory):
        """Registered peers, shares, and online peers are counted correctly."""
        import registry_pb2 as pb  # type: ignore

        svc = RegistryServicer(session_factory)
        svc.record_sweep()

        peer_id_a, _ = _make_peer(session_factory, "alice")
        peer_id_b, _ = _make_peer(session_factory, "bob")
        share_id = _make_share(session_factory, peer_id_a)
        # Add bob as member.
        with session_factory() as session:
            session.add(SharePeerModel(
                share_id=share_id, peer_id=peer_id_b,
                permission=PermissionEnum.READ_ONLY,
            ))
            session.commit()

        # Only alice has an active announcement.
        _make_announcement(session_factory, share_id, peer_id_a, ttl_seconds=300)

        resp = svc.Health(pb.HealthRequest(), _fake_context())

        assert resp.peers_registered == 2
        assert resp.shares_registered == 1
        assert resp.peers_online_now == 1

    def test_expired_announcement_not_counted(self, session_factory):
        """Expired announcements do not count toward peers_online_now."""
        import registry_pb2 as pb  # type: ignore

        svc = RegistryServicer(session_factory)
        svc.record_sweep()

        peer_id, _ = _make_peer(session_factory, "charlie")
        share_id = _make_share(session_factory, peer_id)
        # Insert an already-expired announcement.
        _make_announcement(session_factory, share_id, peer_id, ttl_seconds=-10)

        resp = svc.Health(pb.HealthRequest(), _fake_context())

        assert resp.peers_online_now == 0


class TestHealthDbFail:
    def test_health_db_fail(self, servicer):
        """db_ok=False when session_factory throws; status is degraded/error."""
        import registry_pb2 as pb  # type: ignore

        def _bad_factory():
            raise RuntimeError("DB connection failed")

        servicer._sf = _bad_factory
        servicer.record_sweep()

        resp = servicer.Health(pb.HealthRequest(), _fake_context())

        assert resp.db_ok is False
        assert resp.status in ("degraded", "error")

    def test_health_both_fail(self, servicer):
        """status='error' when both db_ok and ttl_sweep_ok are False."""
        import registry_pb2 as pb  # type: ignore

        def _bad_factory():
            raise RuntimeError("DB down")

        servicer._sf = _bad_factory
        # No sweep recorded, and backdate start to expire grace period.
        servicer._start_time = time.monotonic() - (2 * TTL_SWEEP_INTERVAL_SECONDS + 1)

        resp = servicer.Health(pb.HealthRequest(), _fake_context())

        assert resp.db_ok is False
        assert resp.ttl_sweep_ok is False
        assert resp.status == "error"
