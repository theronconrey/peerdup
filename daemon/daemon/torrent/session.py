"""
libtorrent session manager.

Creates and manages a single lt.session configured for private use:
  - DHT disabled (peer discovery via registry only)
  - LSD disabled
  - Peers injected directly from registry WatchSharePeers events
  - One torrent handle per share

The session runs its alert loop in a background thread and dispatches
alerts to registered callbacks via an asyncio queue bridge.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# Alert polling interval in milliseconds.
_ALERT_POLL_MS = 500


@dataclass
class TorrentStatus:
    share_id:    str
    info_hash:   str
    state:       str    # libtorrent state name
    progress:    float  # 0.0–1.0
    bytes_done:  int
    bytes_total: int
    peers:       int
    seeds:       int
    upload_rate:   int  # bytes/sec
    download_rate: int


class LibtorrentSession:
    """
    Manages the libtorrent session for all shares.
    Thread-safe: all public methods may be called from any thread.
    """

    def __init__(self,
                 listen_interfaces: str = "0.0.0.0:55000",
                 upload_rate_limit: int = 0,
                 download_rate_limit: int = 0,
                 on_status_update: Callable[[TorrentStatus], None] | None = None):
        self._listen        = listen_interfaces
        self._ul_limit      = upload_rate_limit
        self._dl_limit      = download_rate_limit
        self._on_status     = on_status_update
        # Called with share_id when ut_metadata finishes for a magnet-added torrent.
        self._on_metadata: Callable[[str], None] | None = None

        self._session       = None
        self._handles:       dict[str, object] = {}  # share_id -> lt.torrent_handle
        self._lock           = threading.Lock()
        self._alert_thread: threading.Thread | None = None
        self._stop_event     = threading.Event()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        """Start the libtorrent session and alert polling thread."""
        import libtorrent as lt  # type: ignore

        settings = {
            # Network
            "listen_interfaces":        self._listen,
            "upload_rate_limit":        self._ul_limit,
            "download_rate_limit":      self._dl_limit,
            # Privacy — no public swarm participation
            "enable_dht":               False,
            "enable_lsd":               False,
            "enable_upnp":              False,
            "enable_natpmp":            False,
            # Encryption — prefer encrypted, fall back allowed
            "out_enc_policy":           lt.enc_policy.enabled,
            "in_enc_policy":            lt.enc_policy.enabled,
            # Performance
            "alert_mask":               (
                lt.alert.category_t.status_notification |
                lt.alert.category_t.progress_notification |
                lt.alert.category_t.error_notification |
                lt.alert.category_t.peer_notification
            ),
        }

        self._session = lt.session(settings)
        self._stop_event.clear()
        self._alert_thread = threading.Thread(
            target=self._alert_loop, daemon=True, name="lt-alerts"
        )
        self._alert_thread.start()
        log.info("libtorrent session started listen=%s", self._listen)

    def stop(self):
        """Gracefully stop the session."""
        self._stop_event.set()
        if self._alert_thread:
            self._alert_thread.join(timeout=5)
        if self._session:
            self._session.pause()
        log.info("libtorrent session stopped")

    def register_alert_handlers(
        self,
        on_status: Callable[[TorrentStatus], None] | None = None,
        on_metadata: Callable[[str], None] | None = None,
    ) -> None:
        """Register callbacks invoked from the alert polling thread.

        on_status:   called with a TorrentStatus when torrent state changes
                     (torrent_finished_alert, state_changed_alert, etc.).
        on_metadata: called with share_id when ut_metadata finishes fetching
                     torrent metadata from a remote peer.

        Both callbacks are invoked from the background alert thread - callers
        MUST bridge back to their asyncio event loop via
        ``loop.call_soon_threadsafe``.  Safe to call before ``start()``.
        Replaces any previously registered handlers.
        """
        self._on_status   = on_status
        self._on_metadata = on_metadata

    # ── Torrent management ────────────────────────────────────────────────────

    def add_share(self, share_id: str, local_path: str,
                  torrent_path: str | None = None,
                  seed_mode: bool = False,
                  info_hash: str | None = None) -> str:
        """
        Add a torrent for a share. Returns the info-hash hex string.

        If torrent_path is None and seed_mode is False and info_hash is provided,
        adds a magnet-style torrent (metadata fetched from peers via ut_metadata).
        If torrent_path is None and seed_mode is True, creates a new torrent
        from local_path contents.
        """
        import libtorrent as lt  # type: ignore

        Path(local_path).mkdir(parents=True, exist_ok=True)

        # Build torrent params BEFORE acquiring the lock.
        # _make_torrent_info calls lt.add_files + lt.set_piece_hashes which do
        # blocking directory-walk / full-read I/O — keeping them outside the lock
        # means the session remains usable for other shares during the hashing.
        if torrent_path and os.path.exists(torrent_path):
            info = lt.torrent_info(torrent_path)
            params = {
                "ti":        info,
                "save_path": str(Path(local_path).parent),
                "flags":     lt.torrent_flags.default_flags,
            }
            if seed_mode:
                params["flags"] |= lt.torrent_flags.seed_mode
        elif info_hash and not seed_mode:
            # Magnet-style: add with only info_hash; metadata fetched via
            # the ut_metadata (BEP 9) extension once we connect to a peer.
            atp = lt.add_torrent_params()
            ih_bytes = bytes.fromhex(info_hash)
            sha1 = lt.sha1_hash(ih_bytes)
            if hasattr(lt, "info_hash_t"):
                atp.info_hashes = lt.info_hash_t(sha1)
            else:
                atp.info_hash = sha1
            atp.save_path = str(Path(local_path).parent)
            params = atp
        else:
            # Create torrent metadata from the directory contents.
            # This is the slow path: lt.add_files walks the tree and
            # lt.set_piece_hashes reads every byte to compute piece hashes.
            info = self._make_torrent_info(local_path)
            if torrent_path:
                self._save_torrent(info, torrent_path)
            params = {
                "ti":        info,
                "save_path": str(Path(local_path).parent),
                "flags":     lt.torrent_flags.default_flags,
            }
            if seed_mode:
                params["flags"] |= lt.torrent_flags.seed_mode

        # Lock only for the fast session.add_torrent call and handle bookkeeping.
        with self._lock:
            if share_id in self._handles:
                log.debug("Share %s already has a torrent handle", share_id)
                ih = self._handles[share_id].info_hash()
                return str(ih)

            handle = self._session.add_torrent(params)
            handle.set_max_connections(50)

            ih_str = str(handle.info_hash())
            self._handles[share_id] = handle
            log.info("Added torrent share=%s info_hash=%s seed=%s magnet=%s",
                     share_id, ih_str, seed_mode, bool(info_hash and not seed_mode))
            return ih_str

    def remove_share(self, share_id: str, delete_files: bool = False):
        """Remove torrent for a share from the session."""
        import libtorrent as lt  # type: ignore

        with self._lock:
            handle = self._handles.pop(share_id, None)
            if handle:
                flags = lt.options_t.delete_files if delete_files else 0
                self._session.remove_torrent(handle, flags)
                log.info("Removed torrent share=%s delete_files=%s",
                         share_id, delete_files)

    def add_peer(self, share_id: str, host: str, port: int):
        """Inject a peer address directly into a torrent's peer list."""
        with self._lock:
            handle = self._handles.get(share_id)
            if handle and handle.is_valid():
                handle.connect_peer((host, port))
                log.debug("Injected peer share=%s peer=%s:%s", share_id, host, port)

    def set_rate_limit(self, share_id: str,
                       upload_limit: int, download_limit: int):
        """Set per-torrent upload/download rate limits (bytes/sec, 0 = unlimited)."""
        with self._lock:
            handle = self._handles.get(share_id)
            if handle and handle.is_valid():
                handle.set_upload_limit(upload_limit)
                handle.set_download_limit(download_limit)
                log.debug("Rate limit share=%s up=%d down=%d",
                          share_id, upload_limit, download_limit)

    def get_status(self, share_id: str) -> TorrentStatus | None:
        """Return current status for a share's torrent."""
        with self._lock:
            handle = self._handles.get(share_id)
            if not handle or not handle.is_valid():
                return None
            return self._build_status(share_id, handle)

    def get_all_statuses(self) -> list[TorrentStatus]:
        with self._lock:
            return [self._build_status(sid, h)
                    for sid, h in self._handles.items()
                    if h.is_valid()]

    def force_recheck(self, share_id: str):
        """Force libtorrent to re-hash all pieces for a share."""
        with self._lock:
            handle = self._handles.get(share_id)
            if handle and handle.is_valid():
                handle.force_recheck()
                log.debug("Force recheck share=%s", share_id)

    def get_torrent_file_list(self, share_id: str) -> list[tuple[str, int]]:
        """
        Return (relative_path, size_bytes) for each file in a share's torrent.
        Paths are relative to the session save_path (i.e. include the top-level
        share directory name). Returns [] if metadata not yet available.
        """
        with self._lock:
            handle = self._handles.get(share_id)
            if not handle or not handle.is_valid():
                return []
            ti = handle.torrent_file()
            if not ti:
                return []
            files = ti.files()
            return [
                (files.file_path(i), files.file_size(i))
                for i in range(files.num_files())
            ]

    # ── Alert loop ────────────────────────────────────────────────────────────

    def _alert_loop(self):
        import libtorrent as lt  # type: ignore

        while not self._stop_event.is_set():
            self._session.wait_for_alert(_ALERT_POLL_MS)
            alerts = self._session.pop_alerts()
            for alert in alerts:
                self._handle_alert(alert)

    def _handle_alert(self, alert):
        import libtorrent as lt  # type: ignore

        cat = type(alert).__name__

        if cat in ("torrent_finished_alert", "torrent_error_alert",
                   "state_changed_alert", "block_finished_alert",
                   "metadata_received_alert"):
            # Find which share this handle belongs to.
            handle = getattr(alert, "handle", None)
            if not handle:
                return
            with self._lock:
                share_id = next(
                    (sid for sid, h in self._handles.items()
                     if h == handle), None
                )
            if share_id:
                if cat == "metadata_received_alert" and self._on_metadata:
                    try:
                        self._on_metadata(share_id)
                    except Exception:
                        log.exception("on_metadata callback failed share=%s", share_id)
                elif self._on_status:
                    status = self.get_status(share_id)
                    if status:
                        try:
                            self._on_status(status)
                        except Exception:
                            log.exception("on_status_update callback failed")

        if cat == "torrent_error_alert":
            log.error("libtorrent error: %s", alert.message())
        elif cat == "peer_connect_alert":
            log.debug("Peer connected: %s", alert.message())
        elif cat == "peer_disconnected_alert":
            log.debug("Peer disconnected: %s", alert.message())

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _build_status(self, share_id: str, handle) -> TorrentStatus:
        s = handle.status()
        return TorrentStatus(
            share_id      = share_id,
            info_hash     = str(handle.info_hash()),
            state         = str(s.state),
            progress      = s.progress,
            bytes_done    = int(s.total_done),
            bytes_total   = int(s.total_wanted),
            peers         = s.num_peers,
            seeds         = s.num_seeds,
            upload_rate   = int(s.upload_rate),
            download_rate = int(s.download_rate),
        )

    def _make_torrent_info(self, local_path: str):
        """
        Build a libtorrent torrent_info from a directory.
        Uses piece size of 256 KiB — reasonable for mixed file sizes.
        """
        import libtorrent as lt  # type: ignore

        fs = lt.file_storage()
        lt.add_files(fs, local_path)

        ct = lt.create_torrent(fs, piece_size=256 * 1024)
        # DHT/PEX/LSD are disabled at the session level — setting priv=True
        # here would also disable ut_metadata exchange (BEP 9), which we need
        # for metadata bootstrapping when joining a share without a .torrent file.

        lt.set_piece_hashes(ct, str(Path(local_path).parent))
        return lt.torrent_info(ct.generate())

    def _save_torrent(self, info, path: str):
        import libtorrent as lt  # type: ignore
        # info.metadata() / info_section() return the raw bencoded info-dict
        # bytes — not a complete torrent file. Wrap it in {"info": ...} to
        # produce a valid .torrent file that lt.torrent_info() can load.
        raw = info.info_section() if hasattr(info, "info_section") else info.metadata()
        data = lt.bencode({"info": lt.bdecode(raw)})
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        log.debug("Saved torrent metadata to %s", path)
