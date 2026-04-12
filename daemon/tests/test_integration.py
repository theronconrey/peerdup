"""
Integration tests for SyncCoordinator.

Spins up a real in-process registry gRPC server. Uses real RegistryClient and
StateDB instances. LibtorrentSession and FileWatcher are stubbed — actual file
transfer is in TestFileTransfer, which requires libtorrent and is currently
xfail (see note there).

Background tasks (announce loop, WatchSharePeers stream) are neutralized by
patching _activate_share on each coordinator instance. This isolates the
coordinator's state management and registry interaction from threading/blocking
concerns introduced by those tasks.

Run with: cd daemon && make test
Requires: make proto (both registry/ and daemon/) run first.
"""

import asyncio
from concurrent import futures
from pathlib import Path
from unittest.mock import MagicMock

import base58
import grpc
import pytest
from nacl.signing import SigningKey

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from registry.db.models import Base as RegistryBase, create_tables
from registry.servicer.registry import RegistryServicer

from daemon.identity import Identity
from daemon.registry.client import RegistryClient
from daemon.state.db import ShareState, StateDB, make_state_db
from daemon.sync.coordinator import SyncCoordinator
from daemon.torrent.session import TorrentStatus


# ── Test infrastructure ───────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def registry_port():
    """In-process registry server. Module-scoped — shared across all tests."""
    from registry import registry_pb2_grpc as pb_grpc

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sf = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    create_tables(engine)

    servicer = RegistryServicer(sf)
    srv      = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    pb_grpc.add_RegistryServiceServicer_to_server(servicer, srv)
    port = srv.add_insecure_port("[::]:0")
    srv.start()

    yield port

    srv.stop(grace=1)


def _make_identity() -> Identity:
    return Identity(SigningKey.generate())


def _connect_client(port: int, identity: Identity) -> RegistryClient:
    """Create a RegistryClient and register the identity with the registry."""
    client = RegistryClient(address=f"localhost:{port}", tls=False)
    client.connect()
    sig = identity.sign_register(identity.peer_id[:8])
    client.register_peer(identity.peer_id, identity.peer_id[:8], sig)
    return client


def _stub_lt():
    """
    Minimal LibtorrentSession stub. Provides enough interface for the
    coordinator to call without touching actual libtorrent.
    """
    lt = MagicMock()
    lt.add_share.return_value = "aa" * 20          # fake 40-hex info_hash
    lt.get_status.return_value = TorrentStatus(
        share_id="", info_hash="aa" * 20,
        state="seeding", progress=1.0,
        bytes_done=0, bytes_total=0,
        peers=0, seeds=0, upload_rate=0, download_rate=0,
    )
    return lt


def _make_coordinator(
    identity: Identity,
    client: RegistryClient,
    db: StateDB,
    data_dir: Path,
    lt=None,
) -> SyncCoordinator:
    """
    Build a coordinator with stubbed libtorrent and file watcher.

    _activate_share is patched to skip background task creation
    (announce loop and WatchSharePeers stream) which would otherwise
    block the event loop via synchronous gRPC calls. The patch still
    exercises the libtorrent stub and updates local state, keeping
    the coordinator in a consistent state for subsequent calls.
    """
    coord = SyncCoordinator(
        identity    = identity,
        state_db    = db,
        registry    = client,
        lt_session  = lt or _stub_lt(),
        peer_name   = identity.peer_id[:8],
        listen_port = 55000,
        data_dir    = str(data_dir),
    )
    coord._file_watcher = MagicMock()

    # Replace _activate_share with a minimal version that updates state
    # without spawning background tasks.
    async def _activate_no_tasks(share_id: str, local_path: str):
        info_hash = coord._lt.add_share(
            share_id, local_path,
            torrent_path=str(data_dir / "torrents" / f"{share_id}.torrent"),
            seed_mode=True,
        )
        coord._db.set_share_state(share_id, ShareState.SEEDING, info_hash=info_hash)

    coord._activate_share = _activate_no_tasks
    return coord


# ── Peer registration ─────────────────────────────────────────────────────────

class TestPeerRegistration:
    def test_register_returns_token(self, registry_port):
        identity = _make_identity()
        client   = _connect_client(registry_port, identity)
        assert client._token is not None
        assert len(client._token) >= 32
        client.close()

    def test_two_peers_get_distinct_ids(self, registry_port):
        a = _make_identity()
        b = _make_identity()
        ca = _connect_client(registry_port, a)
        cb = _connect_client(registry_port, b)
        assert a.peer_id != b.peer_id
        ca.close()
        cb.close()

    def test_health_check_passes(self, registry_port):
        identity = _make_identity()
        client   = _connect_client(registry_port, identity)
        assert client.health() is True
        client.close()


# ── Share creation ────────────────────────────────────────────────────────────

class TestShareCreation:
    def test_create_share_returns_share_id(self, registry_port, tmp_path):
        identity = _make_identity()
        client   = _connect_client(registry_port, identity)
        db       = StateDB(make_state_db(str(tmp_path / "db")))
        coord    = _make_coordinator(identity, client, db, tmp_path)

        async def run():
            return await coord.create_share("photos", str(tmp_path / "photos"))

        result = asyncio.run(run())
        assert "share_id" in result
        assert len(result["share_id"]) > 20
        client.close()

    def test_share_persisted_in_local_db(self, registry_port, tmp_path):
        identity = _make_identity()
        client   = _connect_client(registry_port, identity)
        db       = StateDB(make_state_db(str(tmp_path / "db")))
        coord    = _make_coordinator(identity, client, db, tmp_path)

        async def run():
            r = await coord.create_share("local-persist", str(tmp_path / "lp"))
            return r["share_id"]

        share_id = asyncio.run(run())
        local    = db.get_share(share_id)

        assert local is not None
        assert local.share_id == share_id
        assert local.is_owner is True
        client.close()

    def test_share_registered_in_registry(self, registry_port, tmp_path):
        identity = _make_identity()
        client   = _connect_client(registry_port, identity)
        db       = StateDB(make_state_db(str(tmp_path / "db")))
        coord    = _make_coordinator(identity, client, db, tmp_path)

        async def run():
            r = await coord.create_share("reg-check", str(tmp_path / "rc"))
            return r["share_id"]

        share_id    = asyncio.run(run())
        share_proto = client.get_share(share_id)

        assert share_proto.share_id == share_id
        assert share_proto.owner_id == identity.peer_id
        client.close()

    def test_keypair_saved_for_owned_share(self, registry_port, tmp_path):
        identity = _make_identity()
        client   = _connect_client(registry_port, identity)
        db       = StateDB(make_state_db(str(tmp_path / "db")))
        coord    = _make_coordinator(identity, client, db, tmp_path)

        async def run():
            r = await coord.create_share("keypair-check", str(tmp_path / "kc"))
            return r["share_id"]

        share_id = asyncio.run(run())
        seed     = db.get_share_keypair(share_id)
        assert seed is not None
        assert len(seed) == 32
        client.close()

    def test_list_shares_includes_created_share(self, registry_port, tmp_path):
        identity = _make_identity()
        client   = _connect_client(registry_port, identity)
        db       = StateDB(make_state_db(str(tmp_path / "db")))
        coord    = _make_coordinator(identity, client, db, tmp_path)

        async def run():
            r = await coord.create_share("list-me", str(tmp_path / "lm"))
            return r["share_id"]

        share_id = asyncio.run(run())
        shares   = coord.list_shares()
        assert any(s["share_id"] == share_id for s in shares)
        client.close()


# ── Grant / revoke access ─────────────────────────────────────────────────────

class TestAccessControl:
    def test_grant_adds_peer_to_registry(self, registry_port, tmp_path):
        id_a = _make_identity()
        id_b = _make_identity()
        ca   = _connect_client(registry_port, id_a)
        cb   = _connect_client(registry_port, id_b)
        db_a = StateDB(make_state_db(str(tmp_path / "db-a")))
        coord_a = _make_coordinator(id_a, ca, db_a, tmp_path / "a")

        async def run():
            r = await coord_a.create_share("grant-share", str(tmp_path / "a" / "s"))
            share_id = r["share_id"]
            await coord_a.grant_access(share_id, id_b.peer_id)
            return share_id

        share_id = asyncio.run(run())
        share_proto = ca.get_share(share_id)
        peer_ids    = [p.peer_id for p in share_proto.peers]
        assert id_b.peer_id in peer_ids
        ca.close(); cb.close()

    def test_revoke_removes_peer_from_registry(self, registry_port, tmp_path):
        id_a = _make_identity()
        id_b = _make_identity()
        ca   = _connect_client(registry_port, id_a)
        cb   = _connect_client(registry_port, id_b)
        db_a = StateDB(make_state_db(str(tmp_path / "db-a")))
        coord_a = _make_coordinator(id_a, ca, db_a, tmp_path / "a")

        async def run():
            r = await coord_a.create_share("revoke-share", str(tmp_path / "a" / "s"))
            share_id = r["share_id"]
            await coord_a.grant_access(share_id, id_b.peer_id)
            await coord_a.revoke_access(share_id, id_b.peer_id)
            return share_id

        share_id    = asyncio.run(run())
        share_proto = ca.get_share(share_id)
        peer_ids    = [p.peer_id for p in share_proto.peers]
        assert id_b.peer_id not in peer_ids
        ca.close(); cb.close()

    def test_non_owner_grant_raises(self, registry_port, tmp_path):
        id_a = _make_identity()
        id_b = _make_identity()
        id_c = _make_identity()
        ca   = _connect_client(registry_port, id_a)
        cb   = _connect_client(registry_port, id_b)
        cc   = _connect_client(registry_port, id_c)
        db_a = StateDB(make_state_db(str(tmp_path / "db-a")))
        db_b = StateDB(make_state_db(str(tmp_path / "db-b")))
        coord_a = _make_coordinator(id_a, ca, db_a, tmp_path / "a")
        coord_b = _make_coordinator(id_b, cb, db_b, tmp_path / "b")

        async def run():
            r = await coord_a.create_share("owner-share", str(tmp_path / "a" / "s"))
            share_id = r["share_id"]
            await coord_a.grant_access(share_id, id_b.peer_id)
            # Peer B joins.
            await coord_b.add_share(share_id, str(tmp_path / "b" / "s"))
            # Peer B tries to grant access to peer C — should fail.
            await coord_b.grant_access(share_id, id_c.peer_id)

        with pytest.raises(PermissionError):
            asyncio.run(run())
        ca.close(); cb.close(); cc.close()


# ── Add share (joining) ───────────────────────────────────────────────────────

class TestAddShare:
    def test_add_share_persists_locally(self, registry_port, tmp_path):
        id_a = _make_identity()
        id_b = _make_identity()
        ca   = _connect_client(registry_port, id_a)
        cb   = _connect_client(registry_port, id_b)
        db_a = StateDB(make_state_db(str(tmp_path / "db-a")))
        db_b = StateDB(make_state_db(str(tmp_path / "db-b")))
        coord_a = _make_coordinator(id_a, ca, db_a, tmp_path / "a")
        coord_b = _make_coordinator(id_b, cb, db_b, tmp_path / "b")

        async def run():
            r = await coord_a.create_share("shared", str(tmp_path / "a" / "s"))
            share_id = r["share_id"]
            await coord_a.grant_access(share_id, id_b.peer_id)
            await coord_b.add_share(share_id, str(tmp_path / "b" / "s"))
            return share_id

        share_id = asyncio.run(run())
        local_b  = db_b.get_share(share_id)
        assert local_b is not None
        assert local_b.share_id == share_id
        assert local_b.is_owner is False
        ca.close(); cb.close()

    def test_add_share_fetches_name_from_registry(self, registry_port, tmp_path):
        id_a = _make_identity()
        id_b = _make_identity()
        ca   = _connect_client(registry_port, id_a)
        cb   = _connect_client(registry_port, id_b)
        db_a = StateDB(make_state_db(str(tmp_path / "db-a")))
        db_b = StateDB(make_state_db(str(tmp_path / "db-b")))
        coord_a = _make_coordinator(id_a, ca, db_a, tmp_path / "a")
        coord_b = _make_coordinator(id_b, cb, db_b, tmp_path / "b")

        async def run():
            r = await coord_a.create_share("named-share", str(tmp_path / "a" / "s"))
            share_id = r["share_id"]
            await coord_a.grant_access(share_id, id_b.peer_id)
            await coord_b.add_share(share_id, str(tmp_path / "b" / "s"))
            return share_id

        share_id = asyncio.run(run())
        local_b  = db_b.get_share(share_id)
        assert local_b.name == "named-share"
        ca.close(); cb.close()


# ── List share peers ──────────────────────────────────────────────────────────

class TestListSharePeers:
    def test_both_peers_appear_in_roster(self, registry_port, tmp_path):
        id_a = _make_identity()
        id_b = _make_identity()
        ca   = _connect_client(registry_port, id_a)
        cb   = _connect_client(registry_port, id_b)
        db_a = StateDB(make_state_db(str(tmp_path / "db-a")))
        coord_a = _make_coordinator(id_a, ca, db_a, tmp_path / "a")

        async def run():
            r = await coord_a.create_share("roster", str(tmp_path / "a" / "s"))
            share_id = r["share_id"]
            await coord_a.grant_access(share_id, id_b.peer_id)
            return share_id

        share_id = asyncio.run(run())
        roster   = coord_a.list_share_peers(share_id)
        peer_ids = {p["peer_id"] for p in roster["peers"]}
        assert id_a.peer_id in peer_ids
        assert id_b.peer_id in peer_ids
        assert roster["total_granted"] == 2
        ca.close(); cb.close()

    def test_self_entry_is_marked(self, registry_port, tmp_path):
        id_a = _make_identity()
        ca   = _connect_client(registry_port, id_a)
        db_a = StateDB(make_state_db(str(tmp_path / "db")))
        coord_a = _make_coordinator(id_a, ca, db_a, tmp_path)

        async def run():
            r = await coord_a.create_share("self-mark", str(tmp_path / "s"))
            return r["share_id"]

        share_id = asyncio.run(run())
        roster   = coord_a.list_share_peers(share_id)
        self_entries = [p for p in roster["peers"] if p["is_self"]]
        assert len(self_entries) == 1
        assert self_entries[0]["peer_id"] == id_a.peer_id
        ca.close()

    def test_owner_entry_is_marked(self, registry_port, tmp_path):
        id_a = _make_identity()
        ca   = _connect_client(registry_port, id_a)
        db_a = StateDB(make_state_db(str(tmp_path / "db")))
        coord_a = _make_coordinator(id_a, ca, db_a, tmp_path)

        async def run():
            r = await coord_a.create_share("owner-mark", str(tmp_path / "s"))
            return r["share_id"]

        share_id = asyncio.run(run())
        roster   = coord_a.list_share_peers(share_id)
        owner_entries = [p for p in roster["peers"] if p["is_owner"]]
        assert len(owner_entries) == 1
        assert owner_entries[0]["peer_id"] == id_a.peer_id
        ca.close()

    def test_permission_reported_correctly(self, registry_port, tmp_path):
        id_a = _make_identity()
        id_b = _make_identity()
        ca   = _connect_client(registry_port, id_a)
        cb   = _connect_client(registry_port, id_b)
        db_a = StateDB(make_state_db(str(tmp_path / "db-a")))
        coord_a = _make_coordinator(id_a, ca, db_a, tmp_path / "a")

        async def run():
            r = await coord_a.create_share("perm-share", str(tmp_path / "a" / "s"))
            share_id = r["share_id"]
            await coord_a.grant_access(share_id, id_b.peer_id, permission="ro")
            return share_id

        share_id = asyncio.run(run())
        roster   = coord_a.list_share_peers(share_id)
        b_entry  = next(p for p in roster["peers"] if p["peer_id"] == id_b.peer_id)
        assert b_entry["permission"] == "ro"
        ca.close(); cb.close()


# ── Pause / resume ────────────────────────────────────────────────────────────

class TestPauseResume:
    def test_pause_sets_paused_state(self, registry_port, tmp_path):
        id_a = _make_identity()
        ca   = _connect_client(registry_port, id_a)
        db_a = StateDB(make_state_db(str(tmp_path / "db")))
        coord_a = _make_coordinator(id_a, ca, db_a, tmp_path)

        async def run():
            r = await coord_a.create_share("pause-me", str(tmp_path / "s"))
            share_id = r["share_id"]
            await coord_a.pause_share(share_id)
            return share_id

        share_id = asyncio.run(run())
        assert db_a.get_share(share_id).state == ShareState.PAUSED
        ca.close()

    def test_resume_clears_paused_state(self, registry_port, tmp_path):
        id_a = _make_identity()
        ca   = _connect_client(registry_port, id_a)
        db_a = StateDB(make_state_db(str(tmp_path / "db")))
        coord_a = _make_coordinator(id_a, ca, db_a, tmp_path)

        async def run():
            r = await coord_a.create_share("resume-me", str(tmp_path / "s"))
            share_id = r["share_id"]
            await coord_a.pause_share(share_id)
            await coord_a.resume_share(share_id)
            return share_id

        share_id = asyncio.run(run())
        assert db_a.get_share(share_id).state != ShareState.PAUSED
        ca.close()


# ── Remove share ──────────────────────────────────────────────────────────────

class TestRemoveShare:
    def test_remove_clears_local_state(self, registry_port, tmp_path):
        id_a = _make_identity()
        ca   = _connect_client(registry_port, id_a)
        db_a = StateDB(make_state_db(str(tmp_path / "db")))
        coord_a = _make_coordinator(id_a, ca, db_a, tmp_path)

        async def run():
            r = await coord_a.create_share("rm-me", str(tmp_path / "s"))
            share_id = r["share_id"]
            await coord_a.remove_share(share_id, delete_files=False)
            return share_id

        share_id = asyncio.run(run())
        assert db_a.get_share(share_id) is None
        assert not any(s["share_id"] == share_id for s in coord_a.list_shares())
        ca.close()

    def test_remove_unknown_share_raises(self, registry_port, tmp_path):
        id_a = _make_identity()
        ca   = _connect_client(registry_port, id_a)
        db_a = StateDB(make_state_db(str(tmp_path / "db")))
        coord_a = _make_coordinator(id_a, ca, db_a, tmp_path)

        with pytest.raises(KeyError):
            asyncio.run(coord_a.remove_share("nonexistent-share-id"))
        ca.close()


# ── File transfer (requires libtorrent) ───────────────────────────────────────

class TestFileTransfer:
    def test_file_arrives_on_peer_b(self, registry_port, tmp_path):
        """
        Peer A creates a share, writes a file, and seeds it.
        Peer B joins the share and should receive the file via libtorrent.

        Currently xfail: see class docstring.
        """
        lt = pytest.importorskip("libtorrent")
        from daemon.torrent.session import LibtorrentSession

        id_a = _make_identity()
        id_b = _make_identity()
        ca   = _connect_client(registry_port, id_a)
        cb   = _connect_client(registry_port, id_b)

        path_a = tmp_path / "peer_a" / "share"
        path_b = tmp_path / "peer_b" / "share"
        path_a.mkdir(parents=True)
        path_b.mkdir(parents=True)

        (path_a / "hello.txt").write_text("hello from peerdup\n")

        lt_a = LibtorrentSession(listen_interfaces="127.0.0.1:0")
        lt_b = LibtorrentSession(listen_interfaces="127.0.0.1:0")
        lt_a.start()
        lt_b.start()

        try:
            db_a = StateDB(make_state_db(str(tmp_path / "db-a")))
            db_b = StateDB(make_state_db(str(tmp_path / "db-b")))

            # Use real LibtorrentSession — don't patch _activate_share.
            coord_a = SyncCoordinator(
                identity=id_a, state_db=db_a, registry=ca,
                lt_session=lt_a, peer_name="peer-a",
                listen_port=lt_a._session.listen_port(),
                data_dir=str(tmp_path / "data_a"),
            )
            coord_b = SyncCoordinator(
                identity=id_b, state_db=db_b, registry=cb,
                lt_session=lt_b, peer_name="peer-b",
                listen_port=lt_b._session.listen_port(),
                data_dir=str(tmp_path / "data_b"),
            )
            coord_a._file_watcher = MagicMock()
            coord_b._file_watcher = MagicMock()

            async def run():
                r = await coord_a.create_share("ft-share", str(path_a))
                share_id = r["share_id"]
                await coord_a.grant_access(share_id, id_b.peer_id)
                await coord_b.add_share(share_id, str(path_b))

                # Manually inject Peer A's address into Peer B's libtorrent
                # (bypassing the registry WatchSharePeers stream).
                port_a = lt_a._session.listen_port()
                lt_b.add_peer(share_id, "127.0.0.1", port_a)

                for _ in range(300):
                    if (path_b / "hello.txt").exists():
                        return share_id, True
                    await asyncio.sleep(0.1)
                return share_id, False

            _, arrived = asyncio.run(run())
            assert arrived, "hello.txt did not arrive on Peer B within 10 seconds"
            assert (path_b / "hello.txt").read_text() == "hello from peerdup\n"

        finally:
            lt_a.stop()
            lt_b.stop()
            ca.close()
            cb.close()
