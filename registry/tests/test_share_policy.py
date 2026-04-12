"""
Tests for the SetSharePolicy RPC and policy propagation (Phase 5A).

Uses an in-memory SQLite DB and a direct RegistryServicer instance --
no gRPC server needed since we call the servicer methods directly.
Requires generated stubs (run `make proto` in registry/ first).
"""

import logging
from unittest.mock import MagicMock

import base58
import grpc
import pytest
from nacl.signing import SigningKey
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from registry.auth.crypto import generate_token, hash_token
from registry.db.models import (
    Base,
    PeerModel,
    PermissionEnum,
    ShareModel,
    SharePeerModel,
)
from registry.servicer.registry import RegistryServicer

log = logging.getLogger(__name__)


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


def _fake_context(token=None):
    ctx = MagicMock()
    ctx.peer.return_value = "ipv4:127.0.0.1:12345"
    ctx.is_active.return_value = True
    if token:
        ctx.invocation_metadata.return_value = [
            ("authorization", f"Bearer {token}"),
        ]
    else:
        ctx.invocation_metadata.return_value = []
    # Make abort() raise to stop execution (matches gRPC behaviour in tests).
    def _abort(code, msg):
        raise grpc.RpcError(f"{code}: {msg}")
    ctx.abort.side_effect = _abort
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


def _make_share(session_factory, owner_id, share_name="test-share"):
    """Insert a ShareModel + owner SharePeerModel directly."""
    sk = SigningKey.generate()
    share_id = base58.b58encode(bytes(sk.verify_key)).decode()
    with session_factory() as session:
        session.add(ShareModel(
            share_id=share_id,
            name=share_name,
            owner_id=owner_id,
        ))
        session.add(SharePeerModel(
            share_id=share_id,
            peer_id=owner_id,
            permission=PermissionEnum.READ_WRITE,
        ))
        session.commit()
    return share_id


def _add_member(session_factory, share_id, peer_id):
    """Add a non-owner member to a share."""
    with session_factory() as session:
        session.add(SharePeerModel(
            share_id=share_id,
            peer_id=peer_id,
            permission=PermissionEnum.READ_ONLY,
        ))
        session.commit()


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestSetSharePolicyAuth:
    def test_set_share_policy_requires_auth(self, servicer, session_factory):
        """SetSharePolicy without a bearer token returns UNAUTHENTICATED."""
        import registry_pb2 as pb  # type: ignore

        owner_id, _ = _make_peer(session_factory, "owner")
        share_id = _make_share(session_factory, owner_id)

        ctx = _fake_context(token=None)
        req = pb.SetSharePolicyRequest(
            share_id=share_id,
            upload_limit_bps=1_000_000,
            download_limit_bps=2_000_000,
        )
        with pytest.raises(grpc.RpcError) as exc_info:
            servicer.SetSharePolicy(req, ctx)

        ctx.abort.assert_called_once()
        code, _ = ctx.abort.call_args[0]
        assert code == grpc.StatusCode.UNAUTHENTICATED

    def test_set_share_policy_requires_owner(self, servicer, session_factory):
        """Non-owner member gets PERMISSION_DENIED."""
        import registry_pb2 as pb  # type: ignore

        owner_id, _ = _make_peer(session_factory, "owner")
        other_id, other_token = _make_peer(session_factory, "other")
        share_id = _make_share(session_factory, owner_id)
        _add_member(session_factory, share_id, other_id)

        ctx = _fake_context(token=other_token)
        req = pb.SetSharePolicyRequest(
            share_id=share_id,
            upload_limit_bps=500_000,
            download_limit_bps=500_000,
        )
        with pytest.raises(grpc.RpcError):
            servicer.SetSharePolicy(req, ctx)

        ctx.abort.assert_called_once()
        code, _ = ctx.abort.call_args[0]
        assert code == grpc.StatusCode.PERMISSION_DENIED


class TestSetSharePolicyOk:
    def test_set_share_policy_ok(self, servicer, session_factory):
        """Owner sets policy; response contains correct limits."""
        import registry_pb2 as pb  # type: ignore

        owner_id, owner_token = _make_peer(session_factory, "owner")
        share_id = _make_share(session_factory, owner_id)

        ctx = _fake_context(token=owner_token)
        req = pb.SetSharePolicyRequest(
            share_id=share_id,
            upload_limit_bps=1_000_000,
            download_limit_bps=5_000_000,
        )
        resp = servicer.SetSharePolicy(req, ctx)

        assert resp.share_id == share_id
        assert resp.policy.upload_limit_bps == 1_000_000
        assert resp.policy.download_limit_bps == 5_000_000

    def test_set_share_policy_zero_is_unlimited(self, servicer, session_factory):
        """Setting 0/0 clears limits (unlimited)."""
        import registry_pb2 as pb  # type: ignore

        owner_id, owner_token = _make_peer(session_factory, "owner")
        share_id = _make_share(session_factory, owner_id)

        # First set non-zero limits.
        ctx = _fake_context(token=owner_token)
        servicer.SetSharePolicy(pb.SetSharePolicyRequest(
            share_id=share_id,
            upload_limit_bps=999_999,
            download_limit_bps=999_999,
        ), ctx)

        # Now clear them.
        ctx2 = _fake_context(token=owner_token)
        resp = servicer.SetSharePolicy(pb.SetSharePolicyRequest(
            share_id=share_id,
            upload_limit_bps=0,
            download_limit_bps=0,
        ), ctx2)

        assert resp.policy.upload_limit_bps == 0
        assert resp.policy.download_limit_bps == 0

    def test_set_share_policy_negative_clamped(self, servicer, session_factory):
        """Negative values are clamped to 0."""
        import registry_pb2 as pb  # type: ignore

        owner_id, owner_token = _make_peer(session_factory, "owner")
        share_id = _make_share(session_factory, owner_id)

        ctx = _fake_context(token=owner_token)
        req = pb.SetSharePolicyRequest(
            share_id=share_id,
            upload_limit_bps=-500,
            download_limit_bps=-1,
        )
        resp = servicer.SetSharePolicy(req, ctx)

        assert resp.policy.upload_limit_bps == 0
        assert resp.policy.download_limit_bps == 0

    def test_set_share_policy_persisted(self, servicer, session_factory):
        """Policy values survive across requests (are written to the DB)."""
        import registry_pb2 as pb  # type: ignore

        owner_id, owner_token = _make_peer(session_factory, "owner")
        share_id = _make_share(session_factory, owner_id)

        servicer.SetSharePolicy(pb.SetSharePolicyRequest(
            share_id=share_id,
            upload_limit_bps=123_456,
            download_limit_bps=789_012,
        ), _fake_context(token=owner_token))

        # Read back via DB directly.
        with session_factory() as session:
            row = session.query(ShareModel).filter_by(share_id=share_id).first()
            assert row.upload_limit_bps == 123_456
            assert row.download_limit_bps == 789_012


class TestGetShareIncludesPolicy:
    def test_get_share_includes_policy(self, servicer, session_factory):
        """GetShare response includes policy field with correct values after SetSharePolicy."""
        import registry_pb2 as pb  # type: ignore

        owner_id, owner_token = _make_peer(session_factory, "owner")
        share_id = _make_share(session_factory, owner_id)

        # Set a policy.
        servicer.SetSharePolicy(pb.SetSharePolicyRequest(
            share_id=share_id,
            upload_limit_bps=2_000_000,
            download_limit_bps=10_000_000,
        ), _fake_context(token=owner_token))

        # Fetch via GetShare.
        resp = servicer.GetShare(
            pb.GetShareRequest(share_id=share_id),
            _fake_context(token=owner_token),
        )

        assert resp.share.policy.upload_limit_bps == 2_000_000
        assert resp.share.policy.download_limit_bps == 10_000_000

    def test_get_share_default_policy_is_zero(self, servicer, session_factory):
        """Newly created share has zero (unlimited) policy."""
        import registry_pb2 as pb  # type: ignore

        owner_id, owner_token = _make_peer(session_factory, "owner")
        share_id = _make_share(session_factory, owner_id)

        resp = servicer.GetShare(
            pb.GetShareRequest(share_id=share_id),
            _fake_context(token=owner_token),
        )

        assert resp.share.policy.upload_limit_bps == 0
        assert resp.share.policy.download_limit_bps == 0


class TestGetSharePeersIncludesPolicy:
    def test_get_share_peers_includes_policy(self, servicer, session_factory):
        """GetSharePeers response includes policy field with current values."""
        import registry_pb2 as pb  # type: ignore

        owner_id, owner_token = _make_peer(session_factory, "owner")
        share_id = _make_share(session_factory, owner_id)

        # Set a policy.
        servicer.SetSharePolicy(pb.SetSharePolicyRequest(
            share_id=share_id,
            upload_limit_bps=3_000_000,
            download_limit_bps=6_000_000,
        ), _fake_context(token=owner_token))

        # Fetch via GetSharePeers.
        resp = servicer.GetSharePeers(
            pb.GetSharePeersRequest(share_id=share_id),
            _fake_context(token=owner_token),
        )

        assert resp.policy.upload_limit_bps == 3_000_000
        assert resp.policy.download_limit_bps == 6_000_000

    def test_get_share_peers_default_policy_is_zero(self, servicer, session_factory):
        """GetSharePeers on a fresh share returns zero (unlimited) policy."""
        import registry_pb2 as pb  # type: ignore

        owner_id, owner_token = _make_peer(session_factory, "owner")
        share_id = _make_share(session_factory, owner_id)

        resp = servicer.GetSharePeers(
            pb.GetSharePeersRequest(share_id=share_id),
            _fake_context(token=owner_token),
        )

        assert resp.policy.upload_limit_bps == 0
        assert resp.policy.download_limit_bps == 0
