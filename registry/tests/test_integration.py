"""
Integration tests for RegistryService.

Spins up a real in-process gRPC server backed by an in-memory SQLite database.
No network dependency beyond localhost; no TLS. Requires generated stubs
(run `make proto` in registry/ first).
"""

import asyncio
import queue
import struct
import threading
import time
from concurrent import futures

import base58
import grpc
import pytest
from nacl.signing import SigningKey

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from registry.auth.crypto import canonical_addrs, decode_peer_id
from registry.db.models import Base, create_tables
from registry.servicer.registry import RegistryServicer


# ── Helpers ───────────────────────────────────────────────────────────────────

class _Peer:
    """Test helper: bundles an Ed25519 keypair, peer_id, and bearer token."""

    def __init__(self, name: str = ""):
        self.sk      = SigningKey.generate()
        self.peer_id = base58.b58encode(bytes(self.sk.verify_key)).decode()
        self.name    = name or self.peer_id[:8]
        self.token: str | None = None

    def sign_register(self) -> bytes:
        msg = decode_peer_id(self.peer_id) + self.name.encode()
        return self.sk.sign(msg).signature

    def sign_create_share(self, share_id: str, name: str) -> bytes:
        msg = decode_peer_id(share_id) + name.encode()
        return self.sk.sign(msg).signature

    def sign_announce(self, share_id: str, addrs: list[dict], ttl: int) -> bytes:
        class _A:
            def __init__(self, h, p, lan):
                self.host = h; self.port = p; self.is_lan = lan
        fake = [_A(a["host"], a["port"], a.get("is_lan", False)) for a in addrs]
        msg = (
            decode_peer_id(share_id)
            + decode_peer_id(self.peer_id)
            + canonical_addrs(fake)
            + struct.pack(">I", ttl)
        )
        return self.sk.sign(msg).signature

    def meta(self) -> list:
        """gRPC call metadata with bearer token."""
        return [("authorization", f"Bearer {self.token}")] if self.token else []


def _fresh_share_id() -> str:
    """Generate a fresh share_id (base58 Ed25519 public key)."""
    sk = SigningKey.generate()
    return base58.b58encode(bytes(sk.verify_key)).decode()


def _register(stub, peer: _Peer) -> "RegisterPeerResponse":
    """Register peer and store the returned token on the peer object."""
    import registry_pb2 as pb
    resp = stub.RegisterPeer(pb.RegisterPeerRequest(
        peer_id=peer.peer_id, name=peer.name, signature=peer.sign_register(),
    ))
    peer.token = resp.token
    return resp


def _create_share(stub, owner: _Peer, name: str) -> tuple[str, "CreateShareResponse"]:
    """Helper: register owner (if not already), create share. Returns (share_id, resp)."""
    import registry_pb2 as pb
    share_id = _fresh_share_id()
    resp = stub.CreateShare(
        pb.CreateShareRequest(
            share_id=share_id, name=name,
            signature=owner.sign_create_share(share_id, name),
        ),
        metadata=owner.meta(),
    )
    return share_id, resp


def _announce(stub, peer: _Peer, share_id: str,
              addrs: list[dict] | None = None, ttl: int = 300):
    """Helper: announce peer presence for a share."""
    import registry_pb2 as pb
    if addrs is None:
        addrs = [{"host": "192.168.1.10", "port": 55000, "is_lan": True}]
    proto_addrs = [
        pb.PeerAddress(host=a["host"], port=a["port"], is_lan=a.get("is_lan", False))
        for a in addrs
    ]
    return stub.Announce(
        pb.AnnounceRequest(
            share_id=share_id, peer_id=peer.peer_id,
            internal_addrs=proto_addrs, ttl_seconds=ttl,
            signature=peer.sign_announce(share_id, addrs, ttl),
        ),
        metadata=peer.meta(),
    )


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def server_port():
    """
    In-process registry server. Module-scoped — one server for all tests.

    Runs a dedicated asyncio event loop in a background thread so the
    servicer can publish live WatchSharePeers events (publish and subscribe
    must share the same loop for asyncio.Lock correctness).
    """
    import registry_pb2_grpc as pb_grpc

    # Dedicated event loop for event bus operations.
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    # StaticPool forces all connections to share the same in-memory SQLite DB.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sf = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    create_tables(engine)

    servicer = RegistryServicer(sf, event_loop=loop)
    srv      = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    pb_grpc.add_RegistryServiceServicer_to_server(servicer, srv)
    port = srv.add_insecure_port("[::]:0")
    srv.start()

    yield port

    srv.stop(grace=1)

    # Cancel all pending tasks before stopping the loop so that drain
    # coroutines (WatchSharePeers) can clean up their subscriptions.
    async def _shutdown():
        tasks = [t for t in asyncio.all_tasks(loop)
                 if t is not asyncio.current_task(loop)]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run_coroutine_threadsafe(_shutdown(), loop).result(timeout=5)
    loop.call_soon_threadsafe(loop.stop)
    loop_thread.join(timeout=5)


@pytest.fixture
def stub(server_port):
    """Fresh channel + stub per test."""
    import registry_pb2_grpc as pb_grpc
    ch = grpc.insecure_channel(f"localhost:{server_port}")
    yield pb_grpc.RegistryServiceStub(ch)
    ch.close()


# ── Health ────────────────────────────────────────────────────────────────────

class TestHealth:
    def test_returns_ok(self, stub):
        import registry_pb2 as pb
        resp = stub.Health(pb.HealthRequest())
        assert resp.status == "ok"
        assert resp.version


# ── RegisterPeer ──────────────────────────────────────────────────────────────

class TestRegisterPeer:
    def test_new_peer_gets_token(self, stub):
        peer = _Peer("alice")
        resp = _register(stub, peer)
        assert resp.peer.peer_id == peer.peer_id
        assert resp.peer.name    == "alice"
        assert len(resp.token)   >= 32

    def test_reregister_rotates_token(self, stub):
        peer = _Peer("bob")
        r1   = _register(stub, peer)
        peer.token = None           # clear so _register re-registers
        r2   = _register(stub, peer)
        assert r1.token != r2.token
        assert r2.peer.peer_id == peer.peer_id

    def test_invalid_signature_rejected(self, stub):
        import registry_pb2 as pb
        peer = _Peer("mallory")
        with pytest.raises(grpc.RpcError) as exc:
            stub.RegisterPeer(pb.RegisterPeerRequest(
                peer_id=peer.peer_id, name=peer.name, signature=b"\x00" * 64,
            ))
        assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED


# ── CreateShare ───────────────────────────────────────────────────────────────

class TestCreateShare:
    def test_creates_share_with_owner(self, stub):
        owner = _Peer("owner-cs")
        _register(stub, owner)
        share_id, resp = _create_share(stub, owner, "my-photos")
        assert resp.share.share_id == share_id
        assert resp.share.name     == "my-photos"
        assert resp.share.owner_id == owner.peer_id

    def test_owner_is_automatically_a_member(self, stub):
        import registry_pb2 as pb
        owner = _Peer("owner-mem")
        _register(stub, owner)
        share_id, _ = _create_share(stub, owner, "auto-member")
        # GetShare should succeed (owner is a member).
        resp = stub.GetShare(
            pb.GetShareRequest(share_id=share_id), metadata=owner.meta()
        )
        member_ids = [p.peer_id for p in resp.share.peers]
        assert owner.peer_id in member_ids

    def test_duplicate_share_id_rejected(self, stub):
        import registry_pb2 as pb
        owner    = _Peer("owner-dup")
        _register(stub, owner)
        share_id = _fresh_share_id()
        req = pb.CreateShareRequest(
            share_id=share_id, name="first",
            signature=owner.sign_create_share(share_id, "first"),
        )
        stub.CreateShare(req, metadata=owner.meta())
        with pytest.raises(grpc.RpcError) as exc:
            stub.CreateShare(req, metadata=owner.meta())
        assert exc.value.code() == grpc.StatusCode.ALREADY_EXISTS

    def test_unauthenticated_create_fails(self, stub):
        import registry_pb2 as pb
        peer     = _Peer("unauth")
        _register(stub, peer)
        share_id = _fresh_share_id()
        with pytest.raises(grpc.RpcError) as exc:
            stub.CreateShare(
                pb.CreateShareRequest(
                    share_id=share_id, name="x",
                    signature=peer.sign_create_share(share_id, "x"),
                ),
                # no metadata — no token
            )
        assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED


# ── GetShare ──────────────────────────────────────────────────────────────────

class TestGetShare:
    def test_member_can_get_share(self, stub):
        import registry_pb2 as pb
        owner  = _Peer("owner-gs")
        member = _Peer("member-gs")
        _register(stub, owner)
        _register(stub, member)
        share_id, _ = _create_share(stub, owner, "visible-share")
        stub.AddPeerToShare(
            pb.AddPeerToShareRequest(
                share_id=share_id, peer_id=member.peer_id,
                permission=pb.Permission.Value("PERMISSION_READ_WRITE"),
            ),
            metadata=owner.meta(),
        )
        resp = stub.GetShare(pb.GetShareRequest(share_id=share_id), metadata=member.meta())
        assert resp.share.share_id == share_id

    def test_non_member_cannot_get_share(self, stub):
        import registry_pb2 as pb
        owner   = _Peer("owner-gs2")
        outside = _Peer("outside-gs")
        _register(stub, owner)
        _register(stub, outside)
        share_id, _ = _create_share(stub, owner, "private-share")
        with pytest.raises(grpc.RpcError) as exc:
            stub.GetShare(
                pb.GetShareRequest(share_id=share_id), metadata=outside.meta()
            )
        assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED


# ── AddPeerToShare / RemovePeerFromShare ──────────────────────────────────────

class TestAccessControl:
    def test_owner_can_add_peer(self, stub):
        import registry_pb2 as pb
        owner  = _Peer("owner-acl")
        member = _Peer("member-acl")
        _register(stub, owner)
        _register(stub, member)
        share_id, _ = _create_share(stub, owner, "acl-share")

        resp = stub.AddPeerToShare(
            pb.AddPeerToShareRequest(
                share_id=share_id, peer_id=member.peer_id,
                permission=pb.Permission.Value("PERMISSION_READ_ONLY"),
            ),
            metadata=owner.meta(),
        )
        peer_ids = [p.peer_id for p in resp.share.peers]
        assert member.peer_id in peer_ids

    def test_non_owner_cannot_add_peer(self, stub):
        import registry_pb2 as pb
        owner    = _Peer("owner-noacl")
        intruder = _Peer("intruder")
        victim   = _Peer("victim")
        _register(stub, owner)
        _register(stub, intruder)
        _register(stub, victim)
        share_id, _ = _create_share(stub, owner, "locked-share")

        with pytest.raises(grpc.RpcError) as exc:
            stub.AddPeerToShare(
                pb.AddPeerToShareRequest(
                    share_id=share_id, peer_id=victim.peer_id,
                    permission=pb.Permission.Value("PERMISSION_READ_WRITE"),
                ),
                metadata=intruder.meta(),
            )
        assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_permission_upgrade(self, stub):
        """AddPeerToShare on an existing member updates their permission."""
        import registry_pb2 as pb
        owner  = _Peer("owner-perm")
        member = _Peer("member-perm")
        _register(stub, owner)
        _register(stub, member)
        share_id, _ = _create_share(stub, owner, "perm-share")

        stub.AddPeerToShare(
            pb.AddPeerToShareRequest(
                share_id=share_id, peer_id=member.peer_id,
                permission=pb.Permission.Value("PERMISSION_READ_ONLY"),
            ),
            metadata=owner.meta(),
        )
        stub.AddPeerToShare(
            pb.AddPeerToShareRequest(
                share_id=share_id, peer_id=member.peer_id,
                permission=pb.Permission.Value("PERMISSION_READ_WRITE"),
            ),
            metadata=owner.meta(),
        )
        resp = stub.GetShare(pb.GetShareRequest(share_id=share_id), metadata=owner.meta())
        member_entry = next(p for p in resp.share.peers if p.peer_id == member.peer_id)
        assert member_entry.permission == pb.Permission.Value("PERMISSION_READ_WRITE")

    def test_owner_can_remove_peer(self, stub):
        import registry_pb2 as pb
        owner  = _Peer("owner-rm")
        member = _Peer("member-rm")
        _register(stub, owner)
        _register(stub, member)
        share_id, _ = _create_share(stub, owner, "rm-share")

        stub.AddPeerToShare(
            pb.AddPeerToShareRequest(
                share_id=share_id, peer_id=member.peer_id,
                permission=pb.Permission.Value("PERMISSION_READ_WRITE"),
            ),
            metadata=owner.meta(),
        )
        stub.RemovePeerFromShare(
            pb.RemovePeerFromShareRequest(share_id=share_id, peer_id=member.peer_id),
            metadata=owner.meta(),
        )
        # Member is now expelled — GetShare should be denied.
        with pytest.raises(grpc.RpcError) as exc:
            stub.GetShare(pb.GetShareRequest(share_id=share_id), metadata=member.meta())
        assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_owner_cannot_remove_themselves(self, stub):
        import registry_pb2 as pb
        owner = _Peer("owner-self-rm")
        _register(stub, owner)
        share_id, _ = _create_share(stub, owner, "self-rm-share")

        with pytest.raises(grpc.RpcError) as exc:
            stub.RemovePeerFromShare(
                pb.RemovePeerFromShareRequest(share_id=share_id, peer_id=owner.peer_id),
                metadata=owner.meta(),
            )
        assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT


# ── Announce / GetSharePeers ──────────────────────────────────────────────────

class TestAnnounce:
    def test_announce_returns_granted_ttl(self, stub):
        owner = _Peer("owner-ann")
        _register(stub, owner)
        share_id, _ = _create_share(stub, owner, "ann-share")
        resp = _announce(stub, owner, share_id, ttl=300)
        assert resp.ttl_seconds > 0

    def test_announced_peer_appears_in_snapshot(self, stub):
        import registry_pb2 as pb
        owner = _Peer("owner-snap")
        _register(stub, owner)
        share_id, _ = _create_share(stub, owner, "snap-share")
        _announce(stub, owner, share_id)

        resp = stub.GetSharePeers(
            pb.GetSharePeersRequest(share_id=share_id, online_only=True),
            metadata=owner.meta(),
        )
        assert any(p.peer_id == owner.peer_id for p in resp.peers)

    def test_unannnounced_peer_not_in_online_snapshot(self, stub):
        import registry_pb2 as pb
        owner  = _Peer("owner-off")
        member = _Peer("member-off")
        _register(stub, owner)
        _register(stub, member)
        share_id, _ = _create_share(stub, owner, "off-share")
        stub.AddPeerToShare(
            pb.AddPeerToShareRequest(
                share_id=share_id, peer_id=member.peer_id,
                permission=pb.Permission.Value("PERMISSION_READ_WRITE"),
            ),
            metadata=owner.meta(),
        )
        # Only owner announces.
        _announce(stub, owner, share_id)

        resp = stub.GetSharePeers(
            pb.GetSharePeersRequest(share_id=share_id, online_only=True),
            metadata=owner.meta(),
        )
        online_ids = [p.peer_id for p in resp.peers]
        assert owner.peer_id  in online_ids
        assert member.peer_id not in online_ids

    def test_tampered_signature_rejected(self, stub):
        import registry_pb2 as pb
        owner = _Peer("owner-tamp")
        _register(stub, owner)
        share_id, _ = _create_share(stub, owner, "tamp-share")

        with pytest.raises(grpc.RpcError) as exc:
            stub.Announce(
                pb.AnnounceRequest(
                    share_id=share_id, peer_id=owner.peer_id,
                    internal_addrs=[pb.PeerAddress(host="1.2.3.4", port=55000)],
                    ttl_seconds=300, signature=b"\x00" * 64,
                ),
                metadata=owner.meta(),
            )
        assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED

    def test_non_member_cannot_announce(self, stub):
        import registry_pb2 as pb
        owner   = _Peer("owner-na")
        outside = _Peer("outside-na")
        _register(stub, owner)
        _register(stub, outside)
        share_id, _ = _create_share(stub, owner, "na-share")

        addrs = [{"host": "10.0.0.1", "port": 55000, "is_lan": True}]
        with pytest.raises(grpc.RpcError) as exc:
            stub.Announce(
                pb.AnnounceRequest(
                    share_id=share_id, peer_id=outside.peer_id,
                    internal_addrs=[pb.PeerAddress(host="10.0.0.1", port=55000, is_lan=True)],
                    ttl_seconds=300,
                    signature=outside.sign_announce(share_id, addrs, 300),
                ),
                metadata=outside.meta(),
            )
        assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_announced_addresses_returned_in_snapshot(self, stub):
        import registry_pb2 as pb
        owner = _Peer("owner-addr")
        _register(stub, owner)
        share_id, _ = _create_share(stub, owner, "addr-share")
        addrs = [{"host": "10.0.1.5", "port": 55010, "is_lan": True}]
        _announce(stub, owner, share_id, addrs=addrs)

        resp = stub.GetSharePeers(
            pb.GetSharePeersRequest(share_id=share_id, online_only=True),
            metadata=owner.meta(),
        )
        peer = next(p for p in resp.peers if p.peer_id == owner.peer_id)
        lan_addrs = [a for a in peer.addresses if a.is_lan]
        assert any(a.host == "10.0.1.5" and a.port == 55010 for a in lan_addrs)


# ── WatchSharePeers (streaming) ───────────────────────────────────────────────

class TestWatchSharePeers:
    def _collect_events(self, stream, duration: float = 0.5) -> list:
        """Read from a gRPC stream for `duration` seconds, then cancel."""
        q = queue.Queue()

        def _drain():
            try:
                for event in stream:
                    q.put(event)
            except grpc.RpcError:
                pass

        t = threading.Thread(target=_drain, daemon=True)
        t.start()
        time.sleep(duration)
        stream.cancel()
        t.join(timeout=2)

        events = []
        while not q.empty():
            events.append(q.get_nowait())
        return events

    def test_online_peer_in_snapshot(self, stub):
        """Connecting after an announce yields an ONLINE event in the snapshot."""
        import registry_pb2 as pb
        owner = _Peer("owner-ws")
        _register(stub, owner)
        share_id, _ = _create_share(stub, owner, "ws-share")
        _announce(stub, owner, share_id)

        stream = stub.WatchSharePeers(
            pb.WatchSharePeersRequest(share_id=share_id, include_offline=False),
            metadata=owner.meta(),
        )
        events = self._collect_events(stream)

        assert len(events) >= 1
        online_ids = {e.peer.peer_id for e in events
                      if e.type == pb.PeerEvent.EVENT_TYPE_ONLINE}
        assert owner.peer_id in online_ids

    def test_offline_peer_in_snapshot_when_requested(self, stub):
        """include_offline=True yields an OFFLINE event for non-announcing members."""
        import registry_pb2 as pb
        owner  = _Peer("owner-wsoff")
        member = _Peer("member-wsoff")
        _register(stub, owner)
        _register(stub, member)
        share_id, _ = _create_share(stub, owner, "wsoff-share")
        stub.AddPeerToShare(
            pb.AddPeerToShareRequest(
                share_id=share_id, peer_id=member.peer_id,
                permission=pb.Permission.Value("PERMISSION_READ_WRITE"),
            ),
            metadata=owner.meta(),
        )
        # Nobody announces — expect OFFLINE events for both.
        stream = stub.WatchSharePeers(
            pb.WatchSharePeersRequest(share_id=share_id, include_offline=True),
            metadata=owner.meta(),
        )
        events = self._collect_events(stream)
        offline_ids = {e.peer.peer_id for e in events
                       if e.type == pb.PeerEvent.EVENT_TYPE_OFFLINE}
        assert owner.peer_id  in offline_ids
        assert member.peer_id in offline_ids

    def test_live_announce_delivers_online_event(self, stub):
        """A peer that announces AFTER the watch stream opens receives an ONLINE event."""
        import registry_pb2 as pb
        owner = _Peer("owner-live")
        _register(stub, owner)
        share_id, _ = _create_share(stub, owner, "live-share")

        # Open the stream BEFORE announcing.
        stream = stub.WatchSharePeers(
            pb.WatchSharePeersRequest(share_id=share_id, include_offline=True),
            metadata=owner.meta(),
        )

        received: queue.Queue = queue.Queue()

        def _drain():
            try:
                for event in stream:
                    received.put(event)
            except grpc.RpcError:
                pass

        t = threading.Thread(target=_drain, daemon=True)
        t.start()

        # Give the drain thread time to subscribe before announcing.
        time.sleep(0.2)
        _announce(stub, owner, share_id)
        time.sleep(0.5)
        stream.cancel()
        t.join(timeout=3)

        events = []
        while not received.empty():
            events.append(received.get_nowait())

        event_types = {e.type for e in events}
        assert pb.PeerEvent.EVENT_TYPE_ONLINE in event_types
