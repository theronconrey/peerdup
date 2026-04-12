"""
SyncCoordinator — the brain of the daemon.

Wires together:
  - FileWatcher   → detects local changes
  - RegistryClient → announces presence, watches peer state
  - LibtorrentSession → manages actual transfers
  - StateDB        → persists share/file/peer state across restarts

One coordinator instance per daemon. All async.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import logging
import os
import socket
from pathlib import Path
from typing import Callable

import base58
from nacl.signing import SigningKey

from daemon.identity import Identity
from daemon.registry.client import RegistryClient, watch_share_peers_loop
from daemon.state.db import ConflictStrategy, ShareState, StateDB
from daemon.torrent.session import LibtorrentSession, TorrentStatus
from daemon.watcher.fs import FileChangedEvent, FileWatcher

log = logging.getLogger(__name__)

# How often to re-announce even without a file change.
ANNOUNCE_INTERVAL_SECONDS = 120
# TTL sent to registry; re-announce before this expires.
ANNOUNCE_TTL_SECONDS      = 300


class SyncCoordinator:

    def __init__(self,
                 identity:     Identity,
                 state_db:     StateDB,
                 registry:     RegistryClient,
                 lt_session:   LibtorrentSession,
                 peer_name:    str = "",
                 listen_port:  int = 55000,
                 data_dir:     str = "/var/lib/peerdup",
                 relay_config = None):
        self._identity     = identity
        self._db           = state_db
        self._registry     = registry
        self._lt           = lt_session
        self._peer_name    = peer_name
        self._listen_port  = listen_port
        self._data_dir     = data_dir
        self._relay_config = relay_config

        # asyncio plumbing
        self._fs_event_queue: asyncio.Queue[FileChangedEvent] = asyncio.Queue()
        self._status_queue:   asyncio.Queue[TorrentStatus]    = asyncio.Queue()
        self._stop_event      = asyncio.Event()

        # share_id -> asyncio.Event (stop watcher task)
        self._watch_stop:  dict[str, asyncio.Event] = {}
        # share_id -> asyncio.Task
        self._watch_tasks: dict[str, asyncio.Task]  = {}
        # share_id -> asyncio.Task (announce heartbeat)
        self._announce_tasks: dict[str, asyncio.Task] = {}

        # Control event bus for the ControlService to subscribe to.
        self._control_subscribers: list[asyncio.Queue] = []

        self._file_watcher: FileWatcher | None = None
        self._lan = None   # LanDiscovery, set in start() if enabled

        # Set in start(); used by ControlServicer to schedule tasks on the
        # correct event loop via asyncio.run_coroutine_threadsafe.
        self._loop: asyncio.AbstractEventLoop | None = None

    # ── Startup ───────────────────────────────────────────────────────────────

    async def start(self, loop: asyncio.AbstractEventLoop, lan_config=None):
        """Boot the coordinator. Call once after registry.connect()."""
        self._loop = loop

        # Bridge watchdog thread → asyncio queue.
        bridge = FileWatcher.make_async_bridge(loop, self._fs_event_queue)
        self._file_watcher = FileWatcher(on_change=bridge)
        self._file_watcher.start()

        # Bridge libtorrent status alerts → asyncio queue.
        def _lt_bridge(status: TorrentStatus):
            loop.call_soon_threadsafe(self._status_queue.put_nowait, status)

        self._lt._on_status = _lt_bridge

        # Bridge libtorrent metadata_received alerts → pre-positioning coroutine.
        def _metadata_bridge(share_id: str):
            loop.call_soon_threadsafe(
                lambda: loop.create_task(
                    self._on_metadata_received(share_id),
                    name=f"preposition-{share_id[:8]}",
                )
            )

        self._lt._on_metadata = _metadata_bridge
        self._lt.start()

        # Start background tasks immediately so the control socket is usable.
        asyncio.create_task(self._fs_event_loop())
        asyncio.create_task(self._status_update_loop())

        # Connect to registry and resume shares in the background so startup
        # doesn't block if the registry is temporarily unreachable.
        asyncio.create_task(self._registry_connect_loop())

        # LAN multicast discovery (optional).
        if lan_config and lan_config.enabled:
            from daemon.lan.discovery import LanDiscovery
            self._lan = LanDiscovery(
                identity      = self._identity,
                config        = lan_config,
                get_share_ids = self._get_active_share_ids,
                on_peer_seen  = self._on_lan_peer,
                listen_port   = self._listen_port,
                peer_name     = self._peer_name,
            )
            await self._lan.start()

        log.info("SyncCoordinator started peer_id=%s", self._identity.peer_id)

    @property
    def _identity_name(self) -> str:
        """Human-readable name sent to the registry on registration."""
        return self._peer_name or self._identity.peer_id[:8]

    async def stop(self):
        self._stop_event.set()
        for stop_ev in self._watch_stop.values():
            stop_ev.set()
        for task in list(self._watch_tasks.values()) + list(self._announce_tasks.values()):
            task.cancel()
        if self._file_watcher:
            self._file_watcher.stop()
        if self._lan:
            await self._lan.stop()
        self._lt.stop()
        log.info("SyncCoordinator stopped")

    # ── Runtime share management (called by ControlService) ──────────────────

    async def create_share(self, name: str, local_path: str,
                           permission: str = "rw",
                           import_key_hex: str = "",
                           conflict_strategy: str = "last_write_wins",
                           local_only: bool = False) -> dict:
        """
        Create a brand-new share (or re-import an existing one):
          1. Generate a fresh Ed25519 keypair — or load seed from import_key_hex
          2. Call registry.CreateShare (unless local_only=True)
          3. Persist share keypair locally
          4. Activate the share (watch, torrent, announce)
        Returns a status dict with share_id included.
        """
        # 1. Generate or import share keypair.
        if import_key_hex:
            share_seed = bytes.fromhex(import_key_hex)
            if len(share_seed) != 32:
                raise ValueError("Imported key must be exactly 32 bytes")
            share_sk = SigningKey(share_seed)
        else:
            share_sk   = SigningKey.generate()
            share_seed = bytes(share_sk)
        share_id = base58.b58encode(bytes(share_sk.verify_key)).decode()

        # 2. Register with registry (skip for local-only shares).
        if not local_only:
            sig = self._identity.sign_create_share(share_id, name)
            try:
                self._registry.create_share(share_id, name, sig)
            except Exception as e:
                raise RuntimeError(f"Registry CreateShare failed: {e}") from e

        # 3. Persist share keypair locally (owner only).
        self._db.save_share_keypair(share_id, share_seed)
        self._db.add_share(share_id, name, local_path, permission,
                           is_owner=True,
                           conflict_strategy=ConflictStrategy(conflict_strategy),
                           local_only=local_only)

        # 4. Activate.
        await self._activate_share(share_id, local_path)

        status = self._lt.get_status(share_id)
        result = self._build_share_status(share_id, name, local_path,
                                          "syncing", status)
        result["share_id"] = share_id
        result["local_only"] = local_only
        log.info("Created share share_id=%s name=%s local_only=%s",
                 share_id, name, local_only)
        return result

    async def grant_access(self, share_id: str, peer_id: str,
                           permission: str = "rw") -> dict:
        """
        Grant a peer access to a share.
        Caller must be the share owner.
        """
        share = self._db.get_share(share_id)
        if not share:
            raise KeyError(f"Share {share_id} not found locally — "
                           "you must be a member to grant access")
        if not share.is_owner:
            raise PermissionError(
                f"You are not the owner of share {share_id}"
            )

        # Map permission string to registry proto value.
        perm_map = {
            "rw":        "PERMISSION_READ_WRITE",
            "ro":        "PERMISSION_READ_ONLY",
            "encrypted": "PERMISSION_ENCRYPTED",
        }
        if permission not in perm_map:
            raise ValueError(f"Invalid permission '{permission}'. "
                             "Use: rw, ro, encrypted")

        try:
            self._registry.add_peer_to_share(share_id, peer_id,
                                             perm_map[permission])
        except Exception as e:
            raise RuntimeError(f"Registry AddPeerToShare failed: {e}") from e

        log.info("Granted access share=%s peer=%s permission=%s",
                 share_id, peer_id[:8], permission)
        return {
            "share_id":   share_id,
            "peer_id":    peer_id,
            "permission": permission,
        }

    async def revoke_access(self, share_id: str, peer_id: str):
        """Revoke a peer's access to a share. Caller must be owner."""
        share = self._db.get_share(share_id)
        if not share:
            raise KeyError(f"Share {share_id} not found locally")
        if not share.is_owner:
            raise PermissionError(f"You are not the owner of share {share_id}")

        try:
            self._registry.remove_peer_from_share(share_id, peer_id)
        except Exception as e:
            raise RuntimeError(f"Registry RemovePeerFromShare failed: {e}") from e

        log.info("Revoked access share=%s peer=%s", share_id, peer_id[:8])

    async def add_share(self, share_id: str, local_path: str,
                        permission: str = "rw",
                        conflict_strategy: str = "last_write_wins",
                        local_only: bool = False,
                        name: str = "") -> dict:
        """
        Add a share at runtime. Fetches metadata from registry (unless
        local_only=True), persists locally, starts watching and announces.
        Returns a status dict suitable for the ControlService response.
        """
        # Fetch share name from registry (skip for local-only shares).
        # User-supplied name takes precedence over registry name.
        if not name:
            name = share_id[:8]
        if not local_only:
            try:
                share_proto = self._registry.get_share(share_id)
                if not name or name == share_id[:8]:
                    name = share_proto.name
            except Exception as e:
                log.warning("Could not fetch share from registry: %s", e)

        # Persist to local state.
        self._db.add_share(share_id, name, local_path, permission,
                           conflict_strategy=ConflictStrategy(conflict_strategy),
                           local_only=local_only)

        # Activate.
        await self._activate_share(share_id, local_path)

        status = self._lt.get_status(share_id)
        return self._build_share_status(share_id, name, local_path,
                                        "syncing", status)

    async def remove_share(self, share_id: str, delete_files: bool = False):
        """Remove a share at runtime."""
        share = self._db.get_share(share_id)
        if not share:
            raise KeyError(f"Share {share_id} not found")

        # Stop watcher stream.
        stop_ev = self._watch_stop.pop(share_id, None)
        if stop_ev:
            stop_ev.set()

        task = self._watch_tasks.pop(share_id, None)
        if task:
            task.cancel()

        atask = self._announce_tasks.pop(share_id, None)
        if atask:
            atask.cancel()

        # Stop watching filesystem.
        if self._file_watcher:
            self._file_watcher.remove_share(share_id)

        # Remove libtorrent torrent.
        self._lt.remove_share(share_id, delete_files=delete_files)

        # Remove from state db.
        self._db.remove_share(share_id)

        log.info("Removed share %s delete_files=%s", share_id, delete_files)

    async def pause_share(self, share_id: str):
        self._db.set_share_state(share_id, ShareState.PAUSED)
        if self._file_watcher:
            self._file_watcher.remove_share(share_id)
        stop_ev = self._watch_stop.get(share_id)
        if stop_ev:
            stop_ev.set()

    async def resume_share(self, share_id: str):
        share = self._db.get_share(share_id)
        if not share:
            raise KeyError(f"Share {share_id} not found")
        self._db.set_share_state(share_id, ShareState.SYNCING)
        await self._activate_share(share_id, share.local_path)

    def list_share_peers(self, share_id: str) -> dict:
        """
        Return full peer roster for a share, merging three sources:
          1. Registry  — authoritative ACL (who has access, permission, name)
          2. known_peers table — online status + last-seen addresses
          3. libtorrent — active transfer rates for connected peers
        """
        # Source 1: registry ACL (registry shares only).
        local_share_row = self._db.get_share(share_id)
        if local_share_row and local_share_row.local_only:
            share_name     = local_share_row.name or share_id[:8]
            registry_peers = {}
        else:
            try:
                share_proto    = self._registry.get_share(share_id)
                share_name     = share_proto.name
                registry_peers = {sp.peer_id: sp for sp in share_proto.peers}
            except Exception as e:
                log.warning("Could not fetch share from registry: %s", e)
                share_name     = share_id[:8]
                registry_peers = {}

        # Source 2: local known_peers (online status + addresses).
        known = {
            kp.peer_id: kp
            for kp in self._db.get_online_peers(share_id)
        }
        # Also get ALL known peers (online or not) for last_seen.
        all_known = {kp.peer_id: kp for kp in self._db.list_all_known_peers(share_id)}

        # Source 3: libtorrent active connections.
        lt_status = self._lt.get_status(share_id)
        lt_peers: dict[str, dict] = {}
        if lt_status:
            # libtorrent doesn't give per-peer rates via get_status —
            # those come from handle.get_peer_info(). We include
            # aggregate rates here; per-peer rates are a future enhancement.
            pass

        # Build the merged peer list.
        # Union of registry ACL and any locally-known peers.
        all_peer_ids = set(registry_peers.keys()) | set(all_known.keys())

        peers = []
        local_share = self._db.get_share(share_id)

        for pid in all_peer_ids:
            reg_peer  = registry_peers.get(pid)
            loc_peer  = all_known.get(pid)
            is_self   = (pid == self._identity.peer_id)
            is_online = (pid in known)

            # Permission: registry is authoritative; local-only shares are rw.
            permission = "unknown"
            if reg_peer:
                perm_val = reg_peer.permission
                perm_map = {1: "rw", 2: "ro", 3: "encrypted"}
                permission = perm_map.get(perm_val, "unknown")
            elif local_share and local_share.local_only:
                permission = "rw"

            # Name: registry > LAN-announced name > peer_id prefix.
            name = (reg_peer.name if reg_peer
                    else (loc_peer.name if loc_peer and loc_peer.name
                          else pid[:8]))

            # Addresses: from known_peers if online.
            addresses = []
            if loc_peer and is_online:
                import json as _json
                try:
                    raw = _json.loads(loc_peer.addresses)
                    addresses = [f"{a['host']}:{a['port']}" for a in raw]
                except Exception:
                    pass

            # last_seen timestamp.
            last_seen = ""
            if loc_peer and loc_peer.last_seen:
                last_seen = loc_peer.last_seen.isoformat()

            # is_owner: local flag if this daemon created the share.
            is_owner = bool(
                local_share and local_share.is_owner
                and is_self
            )

            peers.append({
                "peer_id":       pid,
                "name":          name,
                "permission":    permission,
                "online":        is_online,
                "is_self":       is_self,
                "is_owner":      is_owner,
                "last_seen":     last_seen,
                "addresses":     addresses,
                "download_rate": 0,
                "upload_rate":   0,
            })

        # Sort: self first, then online peers, then offline.
        peers.sort(key=lambda p: (
            not p["is_self"],
            not p["online"],
            p["name"].lower(),
        ))

        total_online = sum(1 for p in peers if p["online"])

        return {
            "share_id":      share_id,
            "share_name":    share_name,
            "peers":         peers,
            "total_granted": len(peers),
            "total_online":  total_online,
        }

    def get_share_info(self, share_id: str) -> dict:
        """
        Return metadata for a single share — no peer roster.
        Merges local DB state + libtorrent status + registry metadata.
        """
        local_share = self._db.get_share(share_id)
        if not local_share:
            raise KeyError(f"Share {share_id} not found")

        lt_status = self._lt.get_status(share_id)

        # Registry metadata (best-effort).
        owner_id   = ""
        created_at = ""
        total_peers = 0
        try:
            share_proto = self._registry.get_share(share_id)
            owner_id    = share_proto.owner_id
            total_peers = len(share_proto.peers)
            ts = share_proto.created_at
            if ts.seconds:
                from datetime import datetime, timezone
                created_at = datetime.fromtimestamp(
                    ts.seconds + ts.nanos / 1e9, tz=timezone.utc
                ).isoformat()
        except Exception:
            pass

        peers_online = len(self._db.get_online_peers(share_id))

        cs = local_share.conflict_strategy or ConflictStrategy.LAST_WRITE_WINS
        pending = len(self._db.list_conflicts(share_id))

        return {
            "share_id":          share_id,
            "name":              local_share.name,
            "local_path":        local_share.local_path,
            "state":             local_share.state.value,
            "permission":        local_share.permission,
            "is_owner":          bool(local_share.is_owner),
            "owner_id":          owner_id,
            "created_at":        created_at,
            "bytes_total":       lt_status.bytes_total if lt_status else 0,
            "bytes_done":        lt_status.bytes_done  if lt_status else 0,
            "info_hash":         lt_status.info_hash    if lt_status else "",
            "peers_online":      peers_online,
            "total_peers":       total_peers,
            "last_error":        local_share.last_error or "",
            "upload_limit":      local_share.upload_limit   or 0,
            "download_limit":    local_share.download_limit or 0,
            "conflict_strategy": cs.value,
            "pending_conflicts": pending,
        }

    def set_conflict_strategy(self, share_id: str, strategy: str) -> dict:
        """Change the conflict resolution strategy for a share."""
        share = self._db.get_share(share_id)
        if not share:
            raise KeyError(f"Share {share_id} not found")
        try:
            cs = ConflictStrategy(strategy)
        except ValueError:
            raise ValueError(
                f"Invalid conflict strategy '{strategy}'. "
                "Use: last_write_wins, rename_conflict, ask"
            )
        self._db.set_conflict_strategy(share_id, cs)
        log.info("Conflict strategy set share=%s strategy=%s", share_id, strategy)
        return {"share_id": share_id, "conflict_strategy": strategy}

    def list_conflicts(self, share_id: str) -> list[dict]:
        """Return pending conflicts for a share (only relevant for 'ask' strategy)."""
        share = self._db.get_share(share_id)
        if not share:
            raise KeyError(f"Share {share_id} not found")
        conflicts = self._db.list_conflicts(share_id)
        return [
            {
                "conflict_id":      c.id,
                "share_id":         c.share_id,
                "remote_peer_id":   c.remote_peer_id,
                "remote_info_hash": c.remote_info_hash,
                "local_info_hash":  c.local_info_hash or "",
                "detected_at":      c.detected_at.isoformat() if c.detected_at else "",
            }
            for c in conflicts
        ]

    async def resolve_conflict(self, conflict_id: int, resolution: str) -> dict:
        """
        Resolve a pending conflict.
        resolution: "keep-local"  — discard remote version, resume seeding ours
                    "keep-remote" — switch to remote torrent, accept their version
        """
        if resolution not in ("keep-local", "keep-remote"):
            raise ValueError(
                f"Invalid resolution '{resolution}'. "
                "Use: keep-local or keep-remote"
            )

        # Find the conflict across all shares.
        from daemon.state.db import LocalConflict
        conflict = None
        for share in self._db.list_shares():
            for c in self._db.list_conflicts(share.share_id):
                if c.id == conflict_id:
                    conflict = c
                    break
            if conflict:
                break

        if not conflict:
            raise KeyError(f"Conflict {conflict_id} not found")

        share_id = conflict.share_id
        self._db.delete_conflict(conflict_id)

        if resolution == "keep-local":
            # Resume seeding our local version.
            share = self._db.get_share(share_id)
            if share:
                await self._activate_share(share_id, share.local_path)
            log.info("Conflict %d resolved: keep-local share=%s", conflict_id, share_id)

        else:  # keep-remote
            # Accept the remote version.
            await self._switch_to_remote_torrent(share_id,
                                                  conflict.remote_info_hash)
            log.info("Conflict %d resolved: keep-remote share=%s ih=%s",
                     conflict_id, share_id, conflict.remote_info_hash)

        return {"conflict_id": conflict_id, "resolution": resolution}

    def set_share_rate_limit(self, share_id: str,
                             upload_limit: int, download_limit: int) -> dict:
        """Set per-share rate limits and persist them."""
        share = self._db.get_share(share_id)
        if not share:
            raise KeyError(f"Share {share_id} not found")
        self._db.set_share_rate_limits(share_id, upload_limit, download_limit)
        self._lt.set_rate_limit(share_id, upload_limit, download_limit)
        log.info("Rate limit set share=%s up=%d down=%d",
                 share_id, upload_limit, download_limit)
        return {
            "share_id":       share_id,
            "upload_limit":   upload_limit,
            "download_limit": download_limit,
        }

    def list_shares(self) -> list[dict]:
        shares = self._db.list_shares()
        result = []
        for share in shares:
            lt_status = self._lt.get_status(share.share_id)
            result.append(self._build_share_status(
                share.share_id, share.name, share.local_path,
                share.state.value, lt_status,
                last_error=share.last_error,
            ))
        return result

    # ── Relay ─────────────────────────────────────────────────────────────────

    async def _try_relay_bridge(self, share_id: str, peer_id: str):
        """
        Attempt to open a relay bridge to peer_id for share_id.
        On success, adds 127.0.0.1:<local_port> to libtorrent as an extra
        peer endpoint. libtorrent races direct vs relay and uses whichever
        connects first.
        """
        from daemon.relay.client import open_relay_bridge
        try:
            local_port = await open_relay_bridge(
                relay_address = self._relay_config.address,
                share_id      = share_id,
                identity      = self._identity,
                want_peer_id  = peer_id,
                pair_timeout  = float(self._relay_config.pair_timeout),
            )
            self._lt.add_peer(share_id, "127.0.0.1", local_port)
            log.info("Relay bridge up share=%s peer=%s local_port=%d",
                     share_id[:8], peer_id[:8], local_port)
        except asyncio.TimeoutError:
            log.debug("Relay bridge timed out waiting for partner share=%s peer=%s",
                      share_id[:8], peer_id[:8])
        except Exception as exc:
            log.debug("Relay bridge failed share=%s peer=%s: %s",
                      share_id[:8], peer_id[:8], exc)

    # ── Announce loop ─────────────────────────────────────────────────────────

    async def _announce_loop(self, share_id: str):
        """Heartbeat: re-announce every ANNOUNCE_INTERVAL_SECONDS."""
        while not self._stop_event.is_set():
            try:
                await self._announce(share_id)
            except Exception:
                log.exception("Announce failed share=%s", share_id)
            await asyncio.sleep(ANNOUNCE_INTERVAL_SECONDS)

    async def _announce(self, share_id: str):
        addrs = self._local_addrs()
        sig   = self._identity.sign_announce(share_id, addrs, ANNOUNCE_TTL_SECONDS)
        # Include our current info_hash so joining peers can do magnet-style adds.
        local_share = self._db.get_share(share_id)
        ih = local_share.info_hash if local_share else ""
        ext_host, ext_port, ttl = self._registry.announce(
            share_id       = share_id,
            peer_id        = self._identity.peer_id,
            internal_addrs = addrs,
            ttl_seconds    = ANNOUNCE_TTL_SECONDS,
            signature      = sig,
            info_hash      = ih or "",
        )
        log.debug("Announced share=%s ext=%s:%s ttl=%ds info_hash=%s",
                  share_id, ext_host, ext_port, ttl, ih)

    def _local_addrs(self) -> list[dict]:
        """Return this host's LAN addresses on the listen port."""
        addrs = []
        try:
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, self._listen_port,
                                           socket.AF_INET):
                ip = info[4][0]
                if not ip.startswith("127."):
                    addrs.append({"host": ip, "port": self._listen_port,
                                  "is_lan": True})
        except Exception:
            pass
        # Fallback
        if not addrs:
            addrs.append({"host": "0.0.0.0", "port": self._listen_port,
                          "is_lan": True})
        return addrs

    def _make_peer_event_handler(self, share_id: str):
        """Return a per-share event handler closure."""
        from daemon import registry_pb2 as pb  # type: ignore

        async def _handler(event):
            peer_id = event.peer.peer_id

            if peer_id == self._identity.peer_id:
                return

            addrs = [
                {"host": a.host, "port": a.port, "is_lan": a.is_lan}
                for a in event.peer.addresses
            ]

            if event.type in (pb.PeerEvent.EVENT_TYPE_ONLINE,
                               pb.PeerEvent.EVENT_TYPE_UPDATED):
                log.info("Peer online share=%s peer=%s addrs=%s",
                         share_id, peer_id[:8], addrs)
                self._db.upsert_peer(share_id, peer_id, addrs, online=True)

                # Detect info_hash mismatch: remote peer has different content.
                remote_ih = event.peer.info_hash
                local_share = self._db.get_share(share_id)
                local_ih = local_share.info_hash if local_share else ""
                if (remote_ih and local_ih and remote_ih != local_ih):
                    await self._handle_remote_update(
                        share_id, remote_ih, peer_id, local_share,
                    )
                else:
                    # Same torrent or we have no content yet - inject peer.
                    for addr in addrs:
                        self._lt.add_peer(share_id, addr["host"], addr["port"])

                # Attempt relay bridge alongside direct connection (fire and forget).
                if (self._relay_config and self._relay_config.enabled
                        and self._relay_config.address):
                    asyncio.create_task(
                        self._try_relay_bridge(share_id, peer_id),
                        name=f"relay-{share_id[:8]}-{peer_id[:8]}",
                    )

            elif event.type in (pb.PeerEvent.EVENT_TYPE_OFFLINE,
                                 pb.PeerEvent.EVENT_TYPE_REMOVED):
                log.info("Peer offline share=%s peer=%s", share_id, peer_id[:8])
                self._db.upsert_peer(share_id, peer_id, addrs, online=False)

            await self._publish_control_event({
                "type":    "peer_event",
                "share_id": share_id,
                "peer_id":  peer_id,
                "online":   event.type == pb.PeerEvent.EVENT_TYPE_ONLINE,
            })

        return _handler

    async def _handle_remote_update(self, share_id: str, remote_info_hash: str,
                                    remote_peer_id: str, local_share) -> None:
        """
        A remote peer announced a different info_hash than we have locally.
        Apply the share's conflict strategy before switching to their torrent.
        """
        strategy = local_share.conflict_strategy or ConflictStrategy.LAST_WRITE_WINS
        log.info("Remote update detected share=%s peer=%s strategy=%s "
                 "local_ih=%s remote_ih=%s",
                 share_id, remote_peer_id[:8], strategy.value,
                 local_share.info_hash, remote_info_hash)

        if strategy == ConflictStrategy.LAST_WRITE_WINS:
            await self._switch_to_remote_torrent(share_id, remote_info_hash)

        elif strategy == ConflictStrategy.RENAME_CONFLICT:
            self._rename_local_files_as_conflicts(share_id, local_share.local_path,
                                                  remote_peer_id)
            await self._switch_to_remote_torrent(share_id, remote_info_hash)

        elif strategy == ConflictStrategy.ASK:
            # Record the conflict and pause the share until the user resolves it.
            conflict_id = self._db.add_conflict(
                share_id         = share_id,
                remote_peer_id   = remote_peer_id,
                remote_info_hash = remote_info_hash,
                local_info_hash  = local_share.info_hash,
            )
            self._db.set_share_state(share_id, ShareState.PAUSED)
            if self._file_watcher:
                self._file_watcher.remove_share(share_id)
            log.info("Conflict recorded id=%d share=%s - paused awaiting resolution",
                     conflict_id, share_id)
            await self._publish_control_event({
                "type":        "conflict",
                "share_id":    share_id,
                "conflict_id": conflict_id,
                "message":     (
                    f"Content conflict: peer {remote_peer_id[:8]} has different "
                    f"version. Use 'peerdup share resolve {conflict_id} "
                    f"<keep-local|keep-remote>' to resolve."
                ),
            })

    def _rename_local_files_as_conflicts(self, share_id: str, local_path: str,
                                         remote_peer_id: str) -> None:
        """
        Rename all locally-tracked files to conflict copies before accepting
        the remote version. Creates: filename.conflict.TIMESTAMP.EXT
        """
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d.%H%M%S")
        peer_tag = remote_peer_id[:8]
        local_root = Path(local_path)

        for db_file in self._db.get_files(share_id):
            full_path = local_root / db_file.rel_path
            if not full_path.exists():
                continue
            stem   = full_path.stem
            suffix = full_path.suffix
            conflict_name = f"{stem}.conflict.{ts}.{peer_tag}{suffix}"
            conflict_path = full_path.parent / conflict_name
            try:
                full_path.rename(conflict_path)
                log.info("Conflict copy: %s -> %s", full_path.name, conflict_name)
            except Exception:
                log.exception("Failed to rename conflict file %s", full_path)

    async def _switch_to_remote_torrent(self, share_id: str,
                                        remote_info_hash: str) -> None:
        """
        Drop our current torrent handle and re-add with the remote peer's
        info_hash so libtorrent fetches metadata + content from them.
        """
        share = self._db.get_share(share_id)
        if not share:
            return
        torrent_path = os.path.join(
            self._data_dir, "torrents", f"{share_id}.torrent"
        )
        # Remove the existing .torrent file so _activate_share does a fresh
        # magnet-style bootstrap from the remote info_hash.
        try:
            if os.path.exists(torrent_path):
                os.remove(torrent_path)
        except Exception:
            log.warning("Could not remove stale torrent file %s", torrent_path)

        # Update DB before removing/re-adding the torrent so that if the LAN
        # announce loop fires during the executor call it broadcasts the new
        # info_hash rather than the stale old one (which would cause the seeder
        # to see a mismatch and switch back, undoing the change).
        self._db.set_share_state(share_id, ShareState.SYNCING,
                                 info_hash=remote_info_hash)
        # Pause the file watcher so that inotify events from pre-positioning
        # (file moves that rearrange existing content into the new layout) do
        # not fire _handle_fs_event once we transition back to SEEDING.
        # The watcher is re-enabled in _status_update_loop on SEEDING transition.
        if self._file_watcher:
            self._file_watcher.remove_share(share_id)
        self._lt.remove_share(share_id, delete_files=False)
        try:
            loop = asyncio.get_running_loop()
            ih = await loop.run_in_executor(
                None,
                functools.partial(
                    self._lt.add_share,
                    share_id, share.local_path,
                    torrent_path=torrent_path,
                    seed_mode=False,
                    info_hash=remote_info_hash,
                ),
            )
            self._db.set_share_state(share_id, ShareState.SYNCING, info_hash=ih)
            log.info("Switched to remote torrent share=%s ih=%s", share_id, ih)
        except Exception:
            log.exception("Failed to switch to remote torrent share=%s", share_id)

    # ── Internal activation ──────────────────────────────────────────────────

    async def _activate_share(self, share_id: str, local_path: str):
        """Set up watcher, libtorrent, announce, and start watch stream."""

        if self._file_watcher:
            self._file_watcher.add_share(share_id, local_path)

        has_files = any(
            True for f in Path(local_path).rglob("*")
            if Path(f).is_file()
        ) if Path(local_path).exists() else False

        torrent_path = os.path.join(
            self._data_dir, "torrents", f"{share_id}.torrent"
        )

        local_share = self._db.get_share(share_id)
        local_only  = local_share.local_only if local_share else False

        # If we have no files and no saved torrent, try to bootstrap from an
        # online peer's announced info_hash (magnet-style / BEP 9 ut_metadata).
        peer_info_hash: str | None = None
        if not has_files and not os.path.exists(torrent_path) and not local_only:
            try:
                peers = self._registry.get_share_peers(share_id, online_only=True)
                for sp in peers:
                    if sp.info_hash and sp.peer_id != self._identity.peer_id:
                        peer_info_hash = sp.info_hash
                        break
            except Exception:
                pass

        if not has_files and peer_info_hash is None and not os.path.exists(torrent_path):
            # Empty directory with no known info_hash - we can't build a torrent
            # from nothing. Stay in SYNCING and wait for a LAN announcement to
            # provide the remote info_hash; _on_lan_peer will call
            # _switch_to_remote_torrent to bootstrap the magnet download.
            self._db.set_share_state(share_id, ShareState.SYNCING)
            log.info("Share %s: no files and no peer info_hash - waiting for LAN peer",
                     share_id)
        else:
            try:
                loop = asyncio.get_running_loop()
                ih = await loop.run_in_executor(
                    None,
                    functools.partial(
                        self._lt.add_share,
                        share_id, local_path,
                        torrent_path=torrent_path,
                        seed_mode=has_files,
                        info_hash=peer_info_hash,
                    ),
                )
                self._db.set_share_state(share_id,
                                         ShareState.SEEDING if has_files
                                         else ShareState.SYNCING,
                                         info_hash=ih)
                if has_files and (local_share is None or (local_share.seq or 0) == 0):
                    # First activation with local content - set initial seq so
                    # other peers know we have content and can compare.
                    self._db.increment_share_seq(share_id)
            except Exception as e:
                log.exception("Failed to add torrent for share %s", share_id)
                self._db.set_share_state(share_id, ShareState.ERROR, error=str(e))
                return

        # Apply any stored per-share rate limits.
        if local_share and (local_share.upload_limit or local_share.download_limit):
            self._lt.set_rate_limit(share_id,
                                    local_share.upload_limit or 0,
                                    local_share.download_limit or 0)

        for kp in self._db.get_online_peers(share_id):
            try:
                for addr in json.loads(kp.addresses):
                    self._lt.add_peer(share_id, addr["host"], addr["port"])
            except Exception:
                pass

        if not local_only:
            # Announce immediately so peers discover our info_hash without
            # waiting for the background loop's first iteration.
            try:
                await self._announce(share_id)
            except Exception:
                log.warning("Initial announce failed for share=%s (will retry in loop)",
                            share_id)

            if share_id not in self._announce_tasks:
                self._announce_tasks[share_id] = asyncio.create_task(
                    self._announce_loop(share_id)
                )

            if share_id not in self._watch_tasks:
                stop_ev = asyncio.Event()
                self._watch_stop[share_id] = stop_ev
                handler = self._make_peer_event_handler(share_id)
                self._watch_tasks[share_id] = asyncio.create_task(
                    watch_share_peers_loop(
                        self._registry, share_id, handler, stop_ev,
                    )
                )

    # ── Registry connect loop ─────────────────────────────────────────────────

    async def _registry_connect_loop(self):
        """Register with registry (retrying with backoff), then resume shares."""
        if self._registry.is_configured:
            backoff = 1.0
            while True:
                try:
                    sig = self._identity.sign_register(self._identity_name)
                    self._registry.register_peer(
                        self._identity.peer_id,
                        self._identity_name,
                        sig,
                    )
                    log.info("Registered with registry")
                    break
                except Exception as exc:
                    log.warning(
                        "Registry unavailable (%s) - retrying in %.0fs", exc, backoff
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
        else:
            log.info("No registry configured - running in LAN-only mode")

        for share in self._db.list_shares():
            if share.state != ShareState.PAUSED:
                await self._activate_share(share.share_id, share.local_path)

    # ── File event loop ───────────────────────────────────────────────────────

    async def _fs_event_loop(self):
        """Process file change events from the FileWatcher."""
        while not self._stop_event.is_set():
            try:
                evt: FileChangedEvent = await asyncio.wait_for(
                    self._fs_event_queue.get(), timeout=1.0
                )
                await self._handle_fs_event(evt)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Error processing fs event")

    async def _handle_fs_event(self, evt: FileChangedEvent):
        log.debug("FS event share=%s type=%s path=%s",
                  evt.share_id, evt.change_type, evt.rel_path)

        share = self._db.get_share(evt.share_id)
        if not share or share.state == ShareState.PAUSED:
            return

        # Don't rebuild the torrent while we're downloading from a peer.
        # libtorrent is writing files — rebuilding from partial content would
        # produce a wrong info_hash and cause the seeder to chase our stub.
        if share.state == ShareState.SYNCING:
            log.debug("FS event ignored during SYNCING share=%s", evt.share_id)
            return

        # Update file index.
        local_path = Path(share.local_path) / evt.rel_path
        if evt.change_type == "deleted":
            self._db.delete_file(evt.share_id, evt.rel_path)
        elif evt.change_type == "moved" and evt.dest_path:
            # Remove old DB entry, add new one at the destination path.
            self._db.delete_file(evt.share_id, evt.rel_path)
            dest_abs = Path(share.local_path) / evt.dest_path
            try:
                st = dest_abs.stat()
                self._db.upsert_file(
                    evt.share_id, evt.dest_path,
                    size=st.st_size, mtime_ns=st.st_mtime_ns
                )
            except FileNotFoundError:
                pass  # dest vanished between event and handling - torrent rebuild still proceeds
        else:
            try:
                st = local_path.stat()
                self._db.upsert_file(
                    evt.share_id, evt.rel_path,
                    size=st.st_size, mtime_ns=st.st_mtime_ns
                )
            except FileNotFoundError:
                return

        # Rebuild torrent and re-announce.
        torrent_path = os.path.join(
            self._data_dir, "torrents", f"{evt.share_id}.torrent"
        )
        try:
            # Delete the cached .torrent file so add_share always rebuilds from
            # the current directory state. If we let it load the stale file,
            # libtorrent will expect files at their OLD paths and create empty
            # placeholders for any that are now missing (moved or deleted),
            # making them reappear.
            try:
                if os.path.exists(torrent_path):
                    os.remove(torrent_path)
            except Exception:
                log.debug("Could not remove stale torrent file %s", torrent_path)

            # Remove old handle and re-add with fresh metadata.
            self._lt.remove_share(evt.share_id, delete_files=False)
            loop = asyncio.get_running_loop()
            ih = await loop.run_in_executor(
                None,
                functools.partial(
                    self._lt.add_share,
                    evt.share_id, share.local_path,
                    torrent_path=torrent_path,
                    seed_mode=True,
                ),
            )
            new_seq = self._db.increment_share_seq(evt.share_id)
            self._db.set_share_state(evt.share_id, ShareState.SEEDING,
                                     info_hash=ih)
            log.debug("Torrent rebuilt share=%s info_hash=%s seq=%d",
                      evt.share_id[:8], ih, new_seq)

            # Re-inject known peers.
            for kp in self._db.get_online_peers(evt.share_id):
                for addr in json.loads(kp.addresses):
                    self._lt.add_peer(evt.share_id, addr["host"], addr["port"])

            # Announce immediately (registry shares only).
            if not share.local_only:
                await self._announce(evt.share_id)

        except Exception:
            log.exception("Failed to update torrent after fs event share=%s",
                          evt.share_id)

    # ── Status update loop ────────────────────────────────────────────────────

    async def _status_update_loop(self):
        while not self._stop_event.is_set():
            try:
                status: TorrentStatus = await asyncio.wait_for(
                    self._status_queue.get(), timeout=1.0
                )
                # Transition SYNCING → SEEDING when libtorrent finishes.
                # This re-enables the file watcher for future local changes.
                if status.state in ("finished", "seeding"):
                    share = self._db.get_share(status.share_id)
                    if share and share.state == ShareState.SYNCING:
                        self._db.set_share_state(
                            status.share_id, ShareState.SEEDING,
                            info_hash=status.info_hash,
                        )
                        log.info("Share download complete share=%s ih=%s",
                                 status.share_id[:8], status.info_hash[:8])
                        # Re-enable the file watcher now that we're SEEDING.
                        # It was paused in _switch_to_remote_torrent to prevent
                        # pre-positioning file moves from triggering a rebuild.
                        if self._file_watcher:
                            self._file_watcher.add_share(
                                status.share_id, share.local_path
                            )

                await self._publish_control_event({
                    "type":    "sync_progress",
                    "share_id": status.share_id,
                    "progress": status.progress,
                    "bytes_done":  status.bytes_done,
                    "bytes_total": status.bytes_total,
                    "peers":       status.peers,
                })
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    # ── Control event bus ─────────────────────────────────────────────────────

    def subscribe_control(self, queue: asyncio.Queue):
        self._control_subscribers.append(queue)

    def unsubscribe_control(self, queue: asyncio.Queue):
        try:
            self._control_subscribers.remove(queue)
        except ValueError:
            pass

    async def _publish_control_event(self, event: dict):
        for q in list(self._control_subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    # ── Torrent pre-positioning ───────────────────────────────────────────────

    async def _on_metadata_received(self, share_id: str):
        """
        Called when libtorrent receives torrent metadata via ut_metadata (BEP 9).

        Before libtorrent starts downloading, scan the local share directory for
        files that match the torrent's expected content by name and size. Move any
        matches into their expected positions so libtorrent's piece-check finds
        them already in place and skips the download for those pieces.

        This makes folder renames and file moves free — no re-transfer needed.
        """
        local_share = self._db.get_share(share_id)
        if not local_share:
            return

        local_path = Path(local_share.local_path)
        save_path  = local_path.parent   # matches libtorrent's save_path

        torrent_files = self._lt.get_torrent_file_list(share_id)
        if not torrent_files:
            return

        # Index all existing local files by (basename, size).
        local_index: dict[tuple[str, int], Path] = {}
        if local_path.exists():
            for f in local_path.rglob("*"):
                if f.is_file():
                    try:
                        local_index[(f.name, f.stat().st_size)] = f
                    except OSError:
                        pass

        moved_any = False
        vacated_dirs: set[Path] = set()
        for rel_path, size in torrent_files:
            expected = save_path / rel_path
            if expected.exists():
                continue   # already in place

            src = local_index.get((expected.name, size))
            if src and src.exists():
                try:
                    expected.parent.mkdir(parents=True, exist_ok=True)
                    vacated_dirs.add(src.parent)
                    src.rename(expected)
                    log.info("Pre-positioned share=%s %s -> %s",
                             share_id[:8], src.relative_to(save_path), rel_path)
                    moved_any = True
                except Exception as exc:
                    log.debug("Could not pre-position %s: %s", src, exc)

        # Remove directories that were emptied by pre-positioning (e.g. a
        # renamed folder whose files were all moved to the new path).
        # Walk up from each vacated dir to the share root so that parent
        # directories emptied by child-dir removal are also cleaned up.
        dirs_to_try: set[Path] = set()
        for d in vacated_dirs:
            p = d
            while p != local_path and p.is_relative_to(local_path):
                dirs_to_try.add(p)
                p = p.parent
        for d in sorted(dirs_to_try, key=lambda p: len(p.parts), reverse=True):
            try:
                d.rmdir()   # only removes if empty
                log.info("Removed vacated dir share=%s %s",
                         share_id[:8], d.relative_to(local_path))
            except OSError:
                pass  # not empty or already gone - fine

        if moved_any:
            self._lt.force_recheck(share_id)
            log.info("Pre-positioning complete share=%s forcing recheck", share_id[:8])

    # ── LAN discovery ─────────────────────────────────────────────────────────

    def _get_active_share_ids(self) -> list[tuple[str, str, int]]:
        """Return (share_id, info_hash, seq) triples for all non-paused local shares."""
        return [
            (s.share_id, s.info_hash or "", s.seq or 0)
            for s in self._db.list_shares()
            if s.state != ShareState.PAUSED
        ]

    async def _on_lan_peer(self, peer_id: str, shares: list[tuple[str, str, int]],
                           host: str, port: int, name: str = ""):
        """
        Called by LanDiscovery for each verified remote announcement.

        ACL check: for registry shares, only inject peers confirmed in the
        known_peers cache.  For local-only shares the registry never populates
        that cache, so any peer whose signed packet carries the correct
        share_id is admitted directly.

        Sequence numbers resolve conflicts: higher seq = more recent local
        changes. If remote_seq > local_seq, accept their version. If
        remote_seq < local_seq, we have newer changes — seed to them instead.
        Equal seq with different hash = true conflict, apply conflict strategy.
        """
        try:
            for share_id, remote_ih, remote_seq in shares:
                local_share = self._db.get_share(share_id)
                if not local_share:
                    continue
                if not local_share.local_only:
                    known = {kp.peer_id
                             for kp in self._db.list_all_known_peers(share_id)}
                    if peer_id not in known:
                        log.debug("LAN peer %s not in ACL cache for share %s - skip",
                                  peer_id[:8], share_id[:8])
                        continue

                # Persist peer presence before deciding what to do with torrent.
                self._db.upsert_peer(
                    share_id,
                    peer_id,
                    [{"host": host, "port": port, "is_lan": True}],
                    online=True,
                    name=name,
                )

                local_ih  = local_share.info_hash or ""
                local_seq = local_share.seq or 0

                if remote_ih and not local_ih:
                    # No local torrent yet - bootstrap from remote info_hash.
                    log.info("LAN bootstrap share=%s peer=%s - fetching from peer",
                             share_id[:8], peer_id[:8])
                    self._db.set_share_seq(share_id, remote_seq)
                    await self._switch_to_remote_torrent(share_id, remote_ih)
                    self._lt.add_peer(share_id, host, port)
                elif remote_ih and local_ih and remote_ih != local_ih:
                    if remote_seq > local_seq:
                        # Remote has newer changes - accept their version.
                        log.info("LAN mismatch share=%s peer=%s remote_seq=%d>local=%d - accepting",
                                 share_id[:8], peer_id[:8], remote_seq, local_seq)
                        self._db.set_share_seq(share_id, remote_seq)
                        await self._handle_remote_update(
                            share_id, remote_ih, peer_id, local_share,
                        )
                        # Inject peer into the new magnet handle so libtorrent
                        # can fetch metadata and content from them immediately.
                        self._lt.add_peer(share_id, host, port)
                    elif remote_seq == local_seq:
                        # True conflict: both changed since last sync.
                        log.info("LAN conflict share=%s peer=%s seq=%d - applying strategy",
                                 share_id[:8], peer_id[:8], remote_seq)
                        await self._handle_remote_update(
                            share_id, remote_ih, peer_id, local_share,
                        )
                        self._lt.add_peer(share_id, host, port)
                    else:
                        # We have newer changes - seed to them.
                        log.info("LAN mismatch share=%s peer=%s remote_seq=%d<local=%d - seeding to them",
                                 share_id[:8], peer_id[:8], remote_seq, local_seq)
                        self._lt.add_peer(share_id, host, port)
                else:
                    # Same torrent or one side has no content yet - inject peer.
                    log.info("LAN peer injected share=%s peer=%s addr=%s:%d",
                             share_id[:8], peer_id[:8], host, port)
                    self._lt.add_peer(share_id, host, port)

        except Exception:
            log.exception("_on_lan_peer failed peer=%s shares=%s",
                          peer_id[:8], [s[0][:8] for s in shares])

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_share_status(self, share_id: str, name: str, local_path: str,
                            state: str, lt_status: TorrentStatus | None,
                            last_error: str | None = None) -> dict:
        peers_online = len(self._db.get_online_peers(share_id))
        share = self._db.get_share(share_id)
        cs = share.conflict_strategy if share else ConflictStrategy.LAST_WRITE_WINS
        local_only = share.local_only if share else False
        return {
            "share_id":          share_id,
            "name":              name,
            "local_path":        local_path,
            "state":             state,
            "bytes_total":       lt_status.bytes_total    if lt_status else 0,
            "bytes_done":        lt_status.bytes_done     if lt_status else 0,
            "peers_online":      peers_online,
            "lt_peers":          lt_status.peers          if lt_status else 0,
            "upload_rate":       lt_status.upload_rate    if lt_status else 0,
            "download_rate":     lt_status.download_rate  if lt_status else 0,
            "last_error":        last_error or "",
            "info_hash":         lt_status.info_hash      if lt_status else "",
            "conflict_strategy": cs.value if cs else "last_write_wins",
            "mode":              "local" if local_only else "registry",
        }

    @property
    def peer_id(self) -> str:
        return self._identity.peer_id
