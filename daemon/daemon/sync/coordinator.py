"""
SyncCoordinator - the brain of the daemon.

Wires together:
  - FileWatcher   -> detects local changes
  - RegistryClient -> announces presence, watches peer state
  - LibtorrentSession -> manages actual transfers
  - StateDB        -> persists share/file/peer state across restarts

One coordinator instance per daemon. All async.

Sub-components handle their own concerns:
  - AnnounceManager  (announce.py)    : per-share registry heartbeat
  - TorrentManager   (torrent_mgr.py) : libtorrent handle lifecycle + rebuild
  - PeerEventHandler (peer_handler.py): registry stream + LAN peer events
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from typing import Callable

import base58
from nacl.signing import SigningKey

from daemon.identity import Identity
from daemon.registry.client import RegistryClient, watch_share_peers_loop
from daemon.state.db import ConflictStrategy, ShareState, StateDB
from daemon.torrent.session import LibtorrentSession, TorrentStatus
from daemon.watcher.fs import FileChangedEvent, FileWatcher
from daemon.sync.announce import AnnounceManager
from daemon.sync.torrent_mgr import TorrentManager
from daemon.sync.peer_handler import PeerEventHandler

log = logging.getLogger(__name__)


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

        # Control event bus for the ControlService to subscribe to.
        self._control_subscribers: list[asyncio.Queue] = []

        # share_ids currently mid-switch (prevents concurrent _switch_to_remote_torrent
        # calls from racing on DB state and libtorrent handles).
        self._switching: set[str] = set()

        self._file_watcher: FileWatcher | None = None
        self._lan = None   # LanDiscovery, set in start() if enabled

        # Set in start(); used by ControlServicer to schedule tasks on the
        # correct event loop via asyncio.run_coroutine_threadsafe.
        self._loop: asyncio.AbstractEventLoop | None = None

        # Sub-components (constructed last because callbacks reference self).
        self._announce_mgr = AnnounceManager(
            identity    = self._identity,
            registry    = self._registry,
            db          = self._db,
            listen_port = self._listen_port,
            stop_event  = self._stop_event,
        )
        self._torrent_mgr = TorrentManager(
            lt                  = self._lt,
            db                  = self._db,
            data_dir            = self._data_dir,
            on_rebuild_complete = self._on_rebuild_complete,
        )
        self._peer_handler = PeerEventHandler(
            identity            = self._identity,
            db                  = self._db,
            lt                  = self._lt,
            relay_config        = self._relay_config,
            on_switch_to_remote = self._switch_to_remote_torrent,
            on_publish_event    = self._publish_control_event,
            on_pause_watcher    = lambda sid: (
                self._file_watcher.remove_share(sid) if self._file_watcher else None
            ),
        )

    # -- Startup ---------------------------------------------------------------

    async def start(self, loop: asyncio.AbstractEventLoop, lan_config=None):
        """Boot the coordinator. Call once after registry.connect()."""
        self._loop = loop

        # Bridge watchdog thread -> asyncio queue.
        bridge = FileWatcher.make_async_bridge(loop, self._fs_event_queue)
        self._file_watcher = FileWatcher(on_change=bridge)
        self._file_watcher.start()

        # Bridge libtorrent status alerts -> asyncio queue.
        def _lt_bridge(status: TorrentStatus):
            loop.call_soon_threadsafe(self._status_queue.put_nowait, status)

        # Bridge libtorrent metadata_received alerts -> pre-positioning coroutine.
        def _metadata_bridge(sid: str):
            loop.call_soon_threadsafe(
                lambda: loop.create_task(
                    self._torrent_mgr.on_metadata_received(sid),
                    name=f"preposition-{sid[:8]}",
                )
            )

        self._lt.register_alert_handlers(
            on_status   = _lt_bridge,
            on_metadata = _metadata_bridge,
        )
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
            actual_port = self._lt.actual_listen_port
            if actual_port != self._listen_port:
                log.info(
                    "libtorrent bound to port %d (configured %d) - advertising actual port",
                    actual_port, self._listen_port,
                )
            self._lan = LanDiscovery(
                identity      = self._identity,
                config        = lan_config,
                get_share_ids = self._get_active_share_ids,
                on_peer_seen  = self._peer_handler.handle_lan_peer,
                listen_port   = actual_port,
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
        for task in list(self._watch_tasks.values()):
            task.cancel()
        self._torrent_mgr.stop_all()
        await self._announce_mgr.stop_all()
        if self._file_watcher:
            self._file_watcher.stop()
        if self._lan:
            await self._lan.stop()
        self._lt.stop()
        log.info("SyncCoordinator stopped")

    # -- Runtime share management (called by ControlService) ------------------

    async def create_share(self, name: str, local_path: str,
                           permission: str = "rw",
                           import_key_hex: str = "",
                           conflict_strategy: str = "last_write_wins",
                           local_only: bool = False) -> dict:
        """
        Create a brand-new share (or re-import an existing one):
          1. Generate a fresh Ed25519 keypair - or load seed from import_key_hex
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
            raise KeyError(f"Share {share_id} not found locally - "
                           "you must be a member to grant access")
        if not share.is_owner:
            raise PermissionError(
                f"You are not the owner of share {share_id}"
            )
        if share.local_only:
            raise ValueError(
                f"Share {share_id} is local-only. "
                "Access control is managed via LAN discovery, not the registry."
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
        if share.local_only:
            raise ValueError(
                f"Share {share_id} is local-only. "
                "Access control is managed via LAN discovery, not the registry."
            )

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
        registry_policy = None
        if not local_only:
            try:
                share_proto = self._registry.get_share(share_id)
                if not name or name == share_id[:8]:
                    name = share_proto.name
                if share_proto.HasField("policy"):
                    registry_policy = share_proto.policy
            except Exception as e:
                log.warning("Could not fetch share from registry: %s", e)

        # Persist to local state.
        self._db.add_share(share_id, name, local_path, permission,
                           conflict_strategy=ConflictStrategy(conflict_strategy),
                           local_only=local_only)

        # Activate.
        await self._activate_share(share_id, local_path)

        # Apply any registry-advertised bandwidth policy.
        if registry_policy is not None:
            self._peer_handler._apply_policy(
                share_id,
                registry_policy.upload_limit_bps,
                registry_policy.download_limit_bps,
            )

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

        self._announce_mgr.stop_share(share_id)
        self._torrent_mgr.clear_state(share_id)

        # Stop watching filesystem.
        if self._file_watcher:
            self._file_watcher.remove_share(share_id)

        # Remove libtorrent torrent.
        self._torrent_mgr.deactivate(share_id, delete_files=delete_files)

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
          1. Registry  - authoritative ACL (who has access, permission, name)
          2. known_peers table - online status + last-seen addresses
          3. libtorrent - active transfer rates for connected peers
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
            # libtorrent doesn't give per-peer rates via get_status -
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
        Return metadata for a single share - no peer roster.
        Merges local DB state + libtorrent status + registry metadata.
        """
        local_share = self._db.get_share(share_id)
        if not local_share:
            raise KeyError(f"Share {share_id} not found")

        lt_status = self._lt.get_status(share_id)

        # Registry metadata (best-effort, registry shares only).
        owner_id   = ""
        created_at = ""
        total_peers = 0
        if not local_share.local_only:
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
        resolution: "keep-local"  - discard remote version, resume seeding ours
                    "keep-remote" - switch to remote torrent, accept their version
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

    # -- Registry health / status (proxied to ControlService) -----------------

    async def registry_health(self) -> dict:
        """Probe the registry Health RPC and return a status dict."""
        if not self._registry.is_configured:
            return {"status": "not_configured"}
        try:
            return self._registry.health_detail()
        except Exception as e:
            return {"status": "unreachable", "error_message": str(e)}

    async def registry_status(self) -> dict:
        """Return the daemon's current registry connection info."""
        if not self._registry.is_configured:
            connection_state = "not_configured"
        elif self._registry._token:
            connection_state = "connected"
        else:
            connection_state = "disconnected"

        return {
            "registry_address":  self._registry.address or "",
            "tls_enabled":       self._registry.tls_enabled,
            "ca_file":           self._registry.ca_file or "",
            "mtls_configured":   self._registry.mtls_configured,
            "connection_state":  connection_state,
            "last_rpc_ok_ago_s": self._registry.last_rpc_ok_ago_s(),
            "token_valid":       bool(self._registry._token),
        }

    # -- Internal activation --------------------------------------------------

    async def _activate_share(self, share_id: str, local_path: str):
        """Set up watcher, libtorrent, announce, and start watch stream."""

        if self._file_watcher:
            self._file_watcher.add_share(share_id, local_path)

        loop = asyncio.get_running_loop()
        has_files = await loop.run_in_executor(
            None,
            lambda: (
                Path(local_path).exists()
                and any(f.is_file() for f in Path(local_path).rglob("*"))
            ),
        )

        torrent_path = self._torrent_mgr.torrent_path(share_id)
        local_share  = self._db.get_share(share_id)
        local_only   = local_share.local_only if local_share else False

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
            # provide the remote info_hash; handle_lan_peer will call
            # _switch_to_remote_torrent to bootstrap the magnet download.
            self._db.set_share_state(share_id, ShareState.SYNCING)
            log.info("Share %s: no files and no peer info_hash - waiting for LAN peer",
                     share_id)
        else:
            try:
                ih = await self._torrent_mgr.activate(
                    share_id,
                    local_path,
                    seed_mode    = has_files,
                    info_hash    = peer_info_hash,
                    torrent_path = torrent_path,
                )
                self._db.set_share_state(share_id,
                                         ShareState.SEEDING if has_files
                                         else ShareState.SYNCING,
                                         info_hash=ih)
                if has_files and (local_share is None or (local_share.seq or 0) == 0):
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
            await self._announce_mgr.start_share(share_id)

        if not local_only and share_id not in self._watch_tasks:
            stop_ev = asyncio.Event()
            self._watch_stop[share_id] = stop_ev
            handler = self._peer_handler.make_registry_handler(share_id)
            self._watch_tasks[share_id] = asyncio.create_task(
                watch_share_peers_loop(
                    self._registry, share_id, handler, stop_ev,
                )
            )

    # -- Switch to remote torrent --------------------------------------------

    async def _switch_to_remote_torrent(self, share_id: str,
                                        remote_info_hash: str) -> None:
        """
        Drop our current torrent handle and re-add with the remote peer's
        info_hash so libtorrent fetches metadata + content from them.

        The _switching guard prevents concurrent calls racing on DB state and
        libtorrent handles. DB update happens BEFORE watcher pause and lt handle
        removal so the LAN announce loop broadcasts the new info_hash rather than
        the stale old one during the executor call.
        """
        if share_id in self._switching:
            log.debug("Switch already in progress for share=%s - skipping duplicate",
                      share_id[:8])
            return
        self._switching.add(share_id)
        try:
            share = self._db.get_share(share_id)
            if not share:
                return
            # Update DB before removing/re-adding the torrent. Ordering matters:
            # if the LAN announce loop fires during the executor call it will
            # broadcast the new info_hash rather than the stale old one.
            self._db.set_share_state(share_id, ShareState.SYNCING,
                                     info_hash=remote_info_hash)
            # Pause file watcher so pre-positioning file moves don't trigger rebuild.
            # Re-enabled in _status_update_loop on SEEDING transition.
            if self._file_watcher:
                self._file_watcher.remove_share(share_id)
            await self._torrent_mgr.replace_with_remote(share_id, remote_info_hash)
            log.info("Switched to remote torrent share=%s ih=%s", share_id, remote_info_hash)
        except Exception:
            log.exception("Failed to switch to remote torrent share=%s", share_id)
        finally:
            self._switching.discard(share_id)

    # -- Rebuild complete callback -------------------------------------------

    async def _on_rebuild_complete(self, share_id: str, info_hash: str,
                                   local_only: bool) -> None:
        """Called by TorrentManager after a successful rebuild.

        Re-injects known peers into the new torrent handle and announces
        to the registry so remote peers see the updated info_hash immediately.
        """
        for kp in self._db.get_online_peers(share_id):
            try:
                for addr in json.loads(kp.addresses):
                    self._lt.add_peer(share_id, addr["host"], addr["port"])
            except Exception:
                pass
        if not local_only:
            await self._announce_mgr.announce_once(share_id)

    # -- Registry connect loop ------------------------------------------------

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

    # -- File event loop ------------------------------------------------------

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

        # Don't rebuild the torrent while we're actively downloading from a peer.
        # libtorrent is writing files - rebuilding from partial content would
        # produce a wrong info_hash and cause the seeder to chase our stub.
        # Only suppress when we have an info_hash (mid-download); if info_hash
        # is absent we are still waiting for the initial torrent build and FS
        # events should proceed so newly-added files seed correctly.
        if share.state == ShareState.SYNCING and share.info_hash:
            log.debug("FS event ignored during SYNCING share=%s", evt.share_id)
            return

        # Update file index.
        local_path = Path(share.local_path) / evt.rel_path
        loop = asyncio.get_running_loop()
        if evt.change_type == "deleted":
            self._db.delete_file(evt.share_id, evt.rel_path)
        elif evt.change_type == "moved" and evt.dest_path:
            # Remove old DB entry, add new one at the destination path.
            self._db.delete_file(evt.share_id, evt.rel_path)
            dest_abs = Path(share.local_path) / evt.dest_path
            try:
                st = await loop.run_in_executor(None, dest_abs.stat)
                self._db.upsert_file(
                    evt.share_id, evt.dest_path,
                    size=st.st_size, mtime_ns=st.st_mtime_ns
                )
            except FileNotFoundError:
                pass  # dest vanished between event and handling - torrent rebuild still proceeds
        else:
            try:
                st = await loop.run_in_executor(None, local_path.stat)
                self._db.upsert_file(
                    evt.share_id, evt.rel_path,
                    size=st.st_size, mtime_ns=st.st_mtime_ns
                )
            except FileNotFoundError:
                return

        # Rebuild torrent and re-announce via TorrentManager (handles debounce).
        self._torrent_mgr.schedule_or_rebuild(
            evt.share_id, share.local_path, share.local_only
        )

    # -- Status update loop --------------------------------------------------

    async def _status_update_loop(self):
        while not self._stop_event.is_set():
            try:
                status: TorrentStatus = await asyncio.wait_for(
                    self._status_queue.get(), timeout=1.0
                )
                # Transition SYNCING -> SEEDING when libtorrent finishes.
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
                        # Populate local_files DB from the downloaded content so
                        # future pre-positioning has accurate tracked_paths.
                        # (The watcher was paused during sync so no events fired.)
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(
                            None,
                            self._index_local_files,
                            status.share_id,
                            share.local_path,
                        )
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

    # -- Local file indexing --------------------------------------------------

    def _index_local_files(self, share_id: str, local_path: str) -> None:
        """Scan local_path and upsert all files into local_files DB.

        Called after a download completes so that tracked_paths is accurate
        for future pre-positioning on this peer. Runs in executor (blocking I/O).
        """
        root = Path(local_path)
        if not root.exists():
            return
        for f in root.rglob("*"):
            if not f.is_file():
                continue
            try:
                st = f.stat()
                rel = str(f.relative_to(root))
                self._db.upsert_file(share_id, rel,
                                     size=st.st_size, mtime_ns=st.st_mtime_ns)
            except Exception:
                pass
        log.debug("Indexed local files share=%s path=%s", share_id[:8], local_path)

    # -- Control event bus ----------------------------------------------------

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

    # -- LAN discovery --------------------------------------------------------

    def _get_active_share_ids(self) -> list[tuple[str, str, int]]:
        """Return (share_id, info_hash, seq) triples for all non-paused local shares."""
        return [
            (s.share_id, s.info_hash or "", s.seq or 0)
            for s in self._db.list_shares()
            if s.state != ShareState.PAUSED
        ]

    def global_rates(self) -> tuple[int, int]:
        """Return (upload_bytes_per_sec, download_bytes_per_sec) summed across all shares."""
        up = 0
        down = 0
        for share in self._db.list_shares():
            lt_status = self._lt.get_status(share.share_id)
            if lt_status:
                up   += lt_status.upload_rate   or 0
                down += lt_status.download_rate or 0
        return up, down

    # -- Helpers --------------------------------------------------------------

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
