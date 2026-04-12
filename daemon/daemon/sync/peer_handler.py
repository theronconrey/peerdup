"""
peer_handler.py - Peer presence logic for the peerdup daemon.

Handles registry stream events, LAN peer arrivals, conflict detection,
conflict file renaming, and relay bridge setup. Extracted from coordinator.py
to keep that module focused on orchestration.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from daemon.identity import Identity
from daemon.state.db import ConflictStrategy, ShareState, StateDB
from daemon.torrent.session import LibtorrentSession

log = logging.getLogger(__name__)


def _effective_limit(registry: int, local: int) -> int:
    """
    Return the effective rate limit given registry and local caps.

    0 means unlimited. When both are set (non-zero), use the lower value.
    When only one is set, use it. When neither is set, return 0 (unlimited).
    """
    if registry == 0 and local == 0:
        return 0
    if registry == 0:
        return local
    if local == 0:
        return registry
    return min(registry, local)


class PeerEventHandler:
    """
    Handles all peer-presence events for the daemon.

    Responsibilities:
    - Dispatch registry stream events (online/offline/updated/removed) per share.
    - Handle LAN peer announcements with sequence-number conflict resolution.
    - Detect and dispatch info_hash mismatches using the share's conflict strategy.
    - Rename local files as conflict copies when RENAME_CONFLICT strategy fires.
    - Attempt relay bridge setup alongside direct connections.
    """

    def __init__(
        self,
        identity: Identity,
        db: StateDB,
        lt: LibtorrentSession,
        relay_config,
        on_switch_to_remote: Callable[[str, str], Awaitable[None]],
        on_publish_event: Callable[[dict], Awaitable[None]],
        on_pause_watcher: Callable[[str], None],
    ) -> None:
        """
        Initialise the handler.

        Parameters
        ----------
        identity:
            The local peer's identity (used for self-filtering and relay auth).
        db:
            Local state database.
        lt:
            Libtorrent session wrapper.
        relay_config:
            Relay configuration object (may be None); expected attributes:
            .enabled (bool), .address (str), .pair_timeout (int|float).
        on_switch_to_remote:
            Async callback ``(share_id, remote_info_hash) -> None`` that
            replaces the current torrent with the one identified by
            ``remote_info_hash``.  Maps to coordinator's
            ``_switch_to_remote_torrent``.
        on_publish_event:
            Async callback ``(event_dict) -> None`` that forwards an event
            to connected CLI watchers.  Maps to coordinator's
            ``_publish_control_event``.
        on_pause_watcher:
            Synchronous callback ``(share_id) -> None`` called when the ASK
            conflict strategy pauses a share.  The coordinator implements this
            as ``if self._file_watcher: self._file_watcher.remove_share(share_id)``.
        """
        self._identity = identity
        self._db = db
        self._lt = lt
        self._relay_config = relay_config
        self._on_switch_to_remote = on_switch_to_remote
        self._on_publish_event = on_publish_event
        self._on_pause_watcher = on_pause_watcher

    # ------------------------------------------------------------------
    # Registry stream
    # ------------------------------------------------------------------

    def make_registry_handler(self, share_id: str) -> Callable:
        """
        Return a per-share async event handler for the registry watch stream.

        The returned coroutine is passed as ``on_event`` to
        ``watch_share_peers_loop``.  It filters out self-events, upserts peer
        presence, detects info_hash mismatches, and optionally opens a relay
        bridge.
        """
        from daemon import registry_pb2 as pb  # type: ignore

        # Resolve POLICY_UPDATED safely whether or not the stubs were
        # regenerated from the updated proto (new constant = 5).
        _POLICY_UPDATED = getattr(pb.PeerEvent, 'EVENT_TYPE_POLICY_UPDATED', 5)

        async def _handler(event) -> None:
            # POLICY_UPDATED events carry no peer field - handle them first.
            if event.type == _POLICY_UPDATED:
                up   = event.policy.upload_limit_bps
                down = event.policy.download_limit_bps
                log.info(
                    "Policy updated share=%s up=%d down=%d",
                    share_id, up, down,
                )
                self._apply_policy(share_id, up, down)
                await self._on_publish_event({
                    "type":         "policy_updated",
                    "share_id":     share_id,
                    "upload_bps":   up,
                    "download_bps": down,
                })
                return  # no peer event to publish after this

            peer_id = event.peer.peer_id

            if peer_id == self._identity.peer_id:
                return

            addrs = [
                {"host": a.host, "port": a.port, "is_lan": a.is_lan}
                for a in event.peer.addresses
            ]

            if event.type in (
                pb.PeerEvent.EVENT_TYPE_ONLINE,
                pb.PeerEvent.EVENT_TYPE_UPDATED,
            ):
                log.info(
                    "Peer online share=%s peer=%s addrs=%s",
                    share_id, peer_id[:8], addrs,
                )
                self._db.upsert_peer(share_id, peer_id, addrs, online=True)

                remote_ih = event.peer.info_hash
                local_share = self._db.get_share(share_id)
                local_ih = local_share.info_hash if local_share else ""

                if remote_ih and local_ih and remote_ih != local_ih:
                    await self.handle_remote_update(
                        share_id, remote_ih, peer_id, local_share,
                    )
                else:
                    for addr in addrs:
                        self._lt.add_peer(share_id, addr["host"], addr["port"])

                if (
                    self._relay_config
                    and self._relay_config.enabled
                    and self._relay_config.address
                ):
                    asyncio.create_task(
                        self._try_relay(share_id, peer_id),
                        name=f"relay-{share_id[:8]}-{peer_id[:8]}",
                    )

            elif event.type in (
                pb.PeerEvent.EVENT_TYPE_OFFLINE,
                pb.PeerEvent.EVENT_TYPE_REMOVED,
            ):
                log.info("Peer offline share=%s peer=%s", share_id, peer_id[:8])
                self._db.upsert_peer(share_id, peer_id, addrs, online=False)

            await self._on_publish_event({
                "type":     "peer_event",
                "share_id": share_id,
                "peer_id":  peer_id,
                "online":   event.type == pb.PeerEvent.EVENT_TYPE_ONLINE,
            })

        return _handler

    # ------------------------------------------------------------------
    # LAN peer handling
    # ------------------------------------------------------------------

    async def handle_lan_peer(
        self,
        peer_id: str,
        shares: list[tuple[str, str, int]],
        host: str,
        port: int,
        name: str = "",
    ) -> None:
        """
        Handle a verified LAN peer announcement.

        Called by ``LanDiscovery`` for each decoded and signature-verified
        remote announcement packet.

        ACL check: for registry shares only peers already present in the
        ``known_peers`` cache are admitted; for local-only shares the registry
        never populates that cache so any peer whose signed packet carries the
        correct ``share_id`` is admitted directly.

        Sequence numbers resolve conflicts: higher seq = more recent local
        changes.  If ``remote_seq > local_seq`` we accept their version; if
        ``remote_seq < local_seq`` we are ahead and seed to them; equal seq
        with a differing hash is a true conflict and the share's conflict
        strategy is applied.

        Parameters
        ----------
        peer_id:
            Base58-encoded Ed25519 public key of the announcing peer.
        shares:
            List of ``(share_id, remote_info_hash, remote_seq)`` tuples from
            the announcement.
        host:
            Source IP address of the announcement.
        port:
            libtorrent listen port of the announcing peer.
        name:
            Optional human-readable name of the announcing peer.
        """
        try:
            for share_id, remote_ih, remote_seq in shares:
                local_share = self._db.get_share(share_id)
                if not local_share:
                    continue

                if not local_share.local_only:
                    known = {
                        kp.peer_id
                        for kp in self._db.list_all_known_peers(share_id)
                    }
                    if peer_id not in known:
                        log.debug(
                            "LAN peer %s not in ACL cache for share %s - skip",
                            peer_id[:8], share_id[:8],
                        )
                        continue

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
                    log.info(
                        "LAN bootstrap share=%s peer=%s - fetching from peer",
                        share_id[:8], peer_id[:8],
                    )
                    self._db.set_share_seq(share_id, remote_seq)
                    await self._on_switch_to_remote(share_id, remote_ih)
                    self._lt.add_peer(share_id, host, port)

                elif remote_ih and local_ih and remote_ih != local_ih:
                    if remote_seq > local_seq:
                        log.info(
                            "LAN mismatch share=%s peer=%s remote_seq=%d>local=%d - accepting",
                            share_id[:8], peer_id[:8], remote_seq, local_seq,
                        )
                        self._db.set_share_seq(share_id, remote_seq)
                        await self.handle_remote_update(
                            share_id, remote_ih, peer_id, local_share,
                        )
                        self._lt.add_peer(share_id, host, port)
                    elif remote_seq == local_seq:
                        log.info(
                            "LAN conflict share=%s peer=%s seq=%d - applying strategy",
                            share_id[:8], peer_id[:8], remote_seq,
                        )
                        await self.handle_remote_update(
                            share_id, remote_ih, peer_id, local_share,
                        )
                        self._lt.add_peer(share_id, host, port)
                    else:
                        log.info(
                            "LAN mismatch share=%s peer=%s remote_seq=%d<local=%d - seeding to them",
                            share_id[:8], peer_id[:8], remote_seq, local_seq,
                        )
                        self._lt.add_peer(share_id, host, port)

                else:
                    log.info(
                        "LAN peer injected share=%s peer=%s addr=%s:%d",
                        share_id[:8], peer_id[:8], host, port,
                    )
                    self._lt.add_peer(share_id, host, port)

        except Exception:
            log.exception(
                "_on_lan_peer failed peer=%s shares=%s",
                peer_id[:8], [s[0][:8] for s in shares],
            )

    # ------------------------------------------------------------------
    # Conflict detection and dispatch
    # ------------------------------------------------------------------

    async def handle_remote_update(
        self,
        share_id: str,
        remote_info_hash: str,
        remote_peer_id: str,
        local_share,
    ) -> None:
        """
        Handle a remote peer announcing a different info_hash than we hold locally.

        Applies the share's conflict strategy before switching to their torrent:

        - ``LAST_WRITE_WINS``: silently switch to the remote torrent.
        - ``RENAME_CONFLICT``: rename all local files with a ``.conflict.*``
          suffix, then switch to the remote torrent.
        - ``ASK``: record the conflict in the DB, pause the share, remove it
          from the file watcher (via the injected ``on_pause_watcher``
          callback), and publish a ``conflict`` control event.

        Parameters
        ----------
        share_id:
            The share identifier.
        remote_info_hash:
            The info_hash announced by the remote peer.
        remote_peer_id:
            The peer_id of the remote peer (used for logging and naming
            conflict copies).
        local_share:
            The ``LocalShare`` ORM object for this share.
        """
        strategy = local_share.conflict_strategy or ConflictStrategy.LAST_WRITE_WINS
        log.info(
            "Remote update detected share=%s peer=%s strategy=%s "
            "local_ih=%s remote_ih=%s",
            share_id, remote_peer_id[:8], strategy.value,
            local_share.info_hash, remote_info_hash,
        )

        if strategy == ConflictStrategy.LAST_WRITE_WINS:
            await self._on_switch_to_remote(share_id, remote_info_hash)

        elif strategy == ConflictStrategy.RENAME_CONFLICT:
            db_files = self._db.get_files(share_id)
            self.rename_conflict_files(
                share_id, local_share.local_path, remote_peer_id, db_files,
            )
            await self._on_switch_to_remote(share_id, remote_info_hash)

        elif strategy == ConflictStrategy.ASK:
            conflict_id = self._db.add_conflict(
                share_id         = share_id,
                remote_peer_id   = remote_peer_id,
                remote_info_hash = remote_info_hash,
                local_info_hash  = local_share.info_hash,
            )
            self._db.set_share_state(share_id, ShareState.PAUSED)
            self._on_pause_watcher(share_id)
            log.info(
                "Conflict recorded id=%d share=%s - paused awaiting resolution",
                conflict_id, share_id,
            )
            await self._on_publish_event({
                "type":        "conflict",
                "share_id":    share_id,
                "conflict_id": conflict_id,
                "message": (
                    f"Content conflict: peer {remote_peer_id[:8]} has different "
                    f"version. Use 'peerdup share resolve {conflict_id} "
                    f"<keep-local|keep-remote>' to resolve."
                ),
            })

    # ------------------------------------------------------------------
    # Conflict file renaming
    # ------------------------------------------------------------------

    @staticmethod
    def rename_conflict_files(
        share_id: str,
        local_path: str,
        remote_peer_id: str,
        db_files: list,
    ) -> None:
        """
        Rename all local share files with a timestamped conflict suffix.

        Each file ``foo.txt`` is renamed to
        ``foo.conflict.<YYYYMMDD.HHMMSS>.<peer8>.txt`` in-place.  Files that
        no longer exist on disk are silently skipped.

        Parameters
        ----------
        share_id:
            The share identifier (used only for logging).
        local_path:
            Absolute path to the share root directory.
        remote_peer_id:
            The remote peer's peer_id; its first 8 characters are embedded
            in the conflict filename so the user knows whose version arrived.
        db_files:
            Pre-fetched list of ``LocalFile`` ORM objects; each must have a
            ``.rel_path`` attribute.  Pass ``db.get_files(share_id)`` from
            the caller.
        """
        ts = datetime.now().strftime("%Y%m%d.%H%M%S")
        peer_tag = remote_peer_id[:8]
        local_root = Path(local_path)

        for db_file in db_files:
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

    # ------------------------------------------------------------------
    # Policy application
    # ------------------------------------------------------------------

    def _apply_policy(self, share_id: str, upload_bps: int, download_bps: int) -> None:
        """
        Apply registry-advertised rate limits to the libtorrent handle.

        Merges with local config caps: if both are set, use the more restrictive.
        If either is 0 (unlimited), the other takes effect. Local caps always
        override registry policy when more restrictive.

        Args:
            share_id: The share to apply limits to.
            upload_bps: Registry advisory upload limit in bytes/sec (0 = unlimited).
            download_bps: Registry advisory download limit in bytes/sec (0 = unlimited).
        """
        local_share = self._db.get_share(share_id)
        if not local_share or local_share.local_only:
            return  # Local-only shares ignore registry policy.

        # Merge with local limits from DB (set via peerdup share set-limit).
        local_up   = getattr(local_share, 'upload_limit', 0) or 0
        local_down = getattr(local_share, 'download_limit', 0) or 0

        effective_up   = _effective_limit(upload_bps,   local_up)
        effective_down = _effective_limit(download_bps,  local_down)

        self._lt.set_share_rate_limit(share_id, effective_up, effective_down)

    # ------------------------------------------------------------------
    # Relay bridge (private)
    # ------------------------------------------------------------------

    async def _try_relay(self, share_id: str, peer_id: str) -> None:
        """
        Attempt to open a relay bridge for the given share/peer pair.

        Opens a local loopback port via the relay rendezvous server and injects
        it into the libtorrent session as an additional peer address.  Errors
        are logged at DEBUG level and never propagate - this is fire-and-forget.
        """
        from daemon.relay.client import open_relay_bridge  # noqa: PLC0415
        try:
            local_port = await open_relay_bridge(
                relay_address = self._relay_config.address,
                share_id      = share_id,
                identity      = self._identity,
                want_peer_id  = peer_id,
                pair_timeout  = float(self._relay_config.pair_timeout),
            )
            self._lt.add_peer(share_id, "127.0.0.1", local_port)
            log.info(
                "Relay bridge up share=%s peer=%s local_port=%d",
                share_id[:8], peer_id[:8], local_port,
            )
        except asyncio.TimeoutError:
            log.debug(
                "Relay bridge timed out waiting for partner share=%s peer=%s",
                share_id[:8], peer_id[:8],
            )
        except Exception as exc:
            log.debug(
                "Relay bridge failed share=%s peer=%s: %s",
                share_id[:8], peer_id[:8], exc,
            )
