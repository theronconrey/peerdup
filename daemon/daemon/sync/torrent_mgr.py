"""
TorrentManager - libtorrent handle lifecycle for peerdup shares.

Extracts torrent add/remove/rebuild/pre-positioning logic from
coordinator.py so the coordinator can stay focused on orchestration.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import time
from pathlib import Path
from typing import Awaitable, Callable

from daemon.state.db import ShareState, StateDB
from daemon.torrent.session import LibtorrentSession

log = logging.getLogger(__name__)

_REBUILD_DEBOUNCE_SECS = 2.0


class TorrentManager:
    """
    Owns the libtorrent handle lifecycle for all shares managed by the daemon.

    Responsibilities:
    - Adding and removing torrent handles (activate / deactivate)
    - Rebuilding torrents from the local filesystem after changes (rebuild /
      schedule_or_rebuild with debounce)
    - Swapping in a remote torrent when a peer has a newer sequence number
      (replace_with_remote)
    - Pre-positioning local files to match incoming torrent layout before
      libtorrent begins downloading (on_metadata_received)

    The manager does NOT write share state to the DB except inside rebuild(),
    which updates state and info_hash after a successful rebuild. All other
    DB interactions go through the coordinator.
    """

    def __init__(
        self,
        lt: LibtorrentSession,
        db: StateDB,
        data_dir: str,
        on_rebuild_complete: Callable[[str, str, bool], Awaitable[None]],
    ) -> None:
        """
        Initialise the TorrentManager.

        Args:
            lt: The shared libtorrent session.
            db: The local SQLite state database.
            data_dir: Root directory used for .torrent cache files.
            on_rebuild_complete: Async callback invoked after a successful
                rebuild with (share_id, new_info_hash, local_only).  The
                coordinator uses this to re-inject known peers and call
                announce_once().
        """
        self._lt = lt
        self._db = db
        self._data_dir = data_dir
        self._on_rebuild_complete = on_rebuild_complete

        # share_id -> monotonic timestamp of last completed rebuild
        self._last_rebuild: dict[str, float] = {}
        # share_id -> pending deferred rebuild asyncio.Task
        self._pending_rebuild: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]

    # ── Torrent path helper ────────────────────────────────────────────────────

    def torrent_path(self, share_id: str) -> str:
        """Return the filesystem path for the cached .torrent file of a share."""
        return os.path.join(self._data_dir, "torrents", f"{share_id}.torrent")

    # ── Low-level handle management ────────────────────────────────────────────

    async def activate(
        self,
        share_id: str,
        local_path: str,
        *,
        seed_mode: bool,
        info_hash: str | None = None,
        torrent_path: str | None = None,
    ) -> str:
        """
        Add a torrent handle to the libtorrent session for a share.

        The blocking lt.add_share() call is run in the default executor so it
        does not block the event loop during directory walks or piece hashing.

        Does NOT write any state to the DB.

        Args:
            share_id: Identifier for the share.
            local_path: Absolute path to the share's local directory.
            seed_mode: If True, create/seed from local files. If False,
                download from peers.
            info_hash: Optional info-hash to use when adding as a magnet
                (download mode without an existing .torrent file).
            torrent_path: Optional path to an existing .torrent file to load.

        Returns:
            The info-hash hex string for the added torrent.
        """
        loop = asyncio.get_running_loop()
        ih: str = await loop.run_in_executor(
            None,
            functools.partial(
                self._lt.add_share,
                share_id,
                local_path,
                torrent_path=torrent_path,
                seed_mode=seed_mode,
                info_hash=info_hash,
            ),
        )
        return ih

    def deactivate(self, share_id: str, *, delete_files: bool = False) -> None:
        """
        Remove the libtorrent handle for a share.

        Does NOT write any state to the DB.

        Args:
            share_id: Identifier for the share to deactivate.
            delete_files: If True, also delete the downloaded files from disk.
        """
        self._lt.remove_share(share_id, delete_files=delete_files)

    async def replace_with_remote(self, share_id: str, remote_info_hash: str) -> str:
        """
        Swap in a remote torrent for a share (metadata-fetch / download mode).

        Deletes any cached .torrent file, removes the existing handle, then
        re-adds the share using the remote info-hash so libtorrent fetches
        metadata via ut_metadata extension.

        Does NOT touch the DB or the file watcher - the coordinator's
        _switch_to_remote_torrent handles those before and after this call.

        Args:
            share_id: Identifier for the share.
            remote_info_hash: The info-hash reported by the remote peer.

        Returns:
            The info-hash string returned by lt.add_share() (should match
            remote_info_hash).
        """
        tp = self.torrent_path(share_id)
        try:
            if os.path.exists(tp):
                os.remove(tp)
        except Exception:
            log.debug("Could not remove cached torrent file %s", tp)

        self._lt.remove_share(share_id, delete_files=False)

        # We need the local_path to call add_share; read it from the DB.
        local_share = self._db.get_share(share_id)
        local_path = local_share.local_path if local_share else ""

        loop = asyncio.get_running_loop()
        ih: str = await loop.run_in_executor(
            None,
            functools.partial(
                self._lt.add_share,
                share_id,
                local_path,
                torrent_path=tp,
                seed_mode=False,
                info_hash=remote_info_hash,
            ),
        )
        return ih

    # ── Rebuild with debounce ──────────────────────────────────────────────────

    def schedule_or_rebuild(
        self, share_id: str, local_path: str, local_only: bool
    ) -> None:
        """
        Entry point called by the coordinator after a filesystem event.

        If the share has been rebuilt recently (within the debounce window)
        a deferred asyncio Task is scheduled to rebuild after the window
        expires. Any previously pending task for the same share is cancelled
        first.  If the debounce window has already passed, asyncio.create_task
        is used to kick off a rebuild immediately.

        This method is NOT async - it schedules a task and returns immediately.

        Args:
            share_id: Identifier for the share.
            local_path: Absolute path to the share's local directory.
            local_only: True if the share does not use the registry.
        """
        now = time.monotonic()
        last = self._last_rebuild.get(share_id, 0.0)

        if now - last < _REBUILD_DEBOUNCE_SECS:
            existing = self._pending_rebuild.pop(share_id, None)
            if existing and not existing.done():
                existing.cancel()

            delay = _REBUILD_DEBOUNCE_SECS - (now - last)
            sid = share_id

            async def _deferred_rebuild() -> None:
                await asyncio.sleep(delay)
                self._pending_rebuild.pop(sid, None)
                s = self._db.get_share(sid)
                if s and s.state != ShareState.PAUSED and s.state != ShareState.SYNCING:
                    await self.rebuild(sid, s.local_path, s.local_only)

            task: asyncio.Task = asyncio.create_task(  # type: ignore[type-arg]
                _deferred_rebuild(), name=f"rebuild-{share_id[:8]}"
            )
            self._pending_rebuild[share_id] = task
            log.debug(
                "Rebuild debounced share=%s retry in %.1fs", share_id[:8], delay
            )
            return

        # Outside debounce window - fire immediately.
        self._last_rebuild[share_id] = now
        asyncio.create_task(
            self.rebuild(share_id, local_path, local_only),
            name=f"rebuild-{share_id[:8]}",
        )

    async def rebuild(self, share_id: str, local_path: str, local_only: bool) -> None:
        """
        Fully rebuild the torrent for a share from its local directory.

        Steps:
        1. Delete the cached .torrent file.
        2. Remove the existing libtorrent handle.
        3. Re-add the torrent in seed mode (blocking call in executor).
        4. Increment the share sequence number.
        5. Update DB share state to SEEDING with the new info_hash.
        6. Update _last_rebuild timestamp.
        7. Call on_rebuild_complete(share_id, new_info_hash, local_only).

        Does NOT re-inject peers or call announce directly - those are the
        responsibility of the on_rebuild_complete callback.

        Args:
            share_id: Identifier for the share.
            local_path: Absolute path to the share's local directory.
            local_only: True if the share does not use the registry.
        """
        tp = self.torrent_path(share_id)
        try:
            try:
                if os.path.exists(tp):
                    os.remove(tp)
            except Exception:
                log.debug("Could not remove stale torrent file %s", tp)

            self._lt.remove_share(share_id, delete_files=False)

            loop = asyncio.get_running_loop()
            ih: str = await loop.run_in_executor(
                None,
                functools.partial(
                    self._lt.add_share,
                    share_id,
                    local_path,
                    torrent_path=tp,
                    seed_mode=True,
                ),
            )

            new_seq = self._db.increment_share_seq(share_id)
            self._db.set_share_state(share_id, ShareState.SEEDING, info_hash=ih)
            self._last_rebuild[share_id] = time.monotonic()

            log.debug(
                "Torrent rebuilt share=%s info_hash=%s seq=%d",
                share_id[:8],
                ih,
                new_seq,
            )

            await self._on_rebuild_complete(share_id, ih, local_only)

        except Exception:
            log.exception(
                "Failed to update torrent after fs event share=%s", share_id
            )

    # ── Pre-positioning ────────────────────────────────────────────────────────

    async def on_metadata_received(self, share_id: str) -> None:
        """
        Pre-positioning trigger called when libtorrent finishes fetching
        torrent metadata from a remote peer (ut_metadata).

        Reads the expected file layout from the libtorrent handle, compares it
        to the files tracked in the DB, moves existing local files into the
        correct positions to avoid re-downloading, then forces a recheck.

        Args:
            share_id: Identifier for the share whose metadata was received.
        """
        local_share = self._db.get_share(share_id)
        if not local_share:
            return

        local_path = Path(local_share.local_path)
        save_path = local_path.parent

        torrent_files = self._lt.get_torrent_file_list(share_id)
        if not torrent_files:
            return

        tracked_paths = {
            local_path / db_file.rel_path
            for db_file in self._db.get_files(share_id)
        }

        loop = asyncio.get_running_loop()
        moved_any: bool = await loop.run_in_executor(
            None,
            functools.partial(
                self.preposition_files,
                share_id,
                local_path,
                save_path,
                torrent_files,
                tracked_paths,
            ),
        )

        if moved_any:
            self._lt.force_recheck(share_id)
            log.info(
                "Pre-positioning complete share=%s forcing recheck", share_id[:8]
            )

    @staticmethod
    def preposition_files(
        share_id: str,
        local_path: Path,
        save_path: Path,
        torrent_files: list[tuple[str, int]],
        tracked_paths: set[Path],
    ) -> bool:
        """
        Pure filesystem work: move local files into the positions expected by
        the incoming torrent layout to avoid unnecessary re-downloads.

        Also removes stale tracked files that are no longer in the torrent
        layout, and cleans up empty directories left behind after moves.

        This method is executor-safe (no asyncio, no shared mutable state).

        Args:
            share_id: Used only for log messages.
            local_path: Absolute path to the share's root directory.
            save_path: Parent of local_path (libtorrent save_path).
            torrent_files: List of (relative_path, size) tuples from lt.
            tracked_paths: Set of absolute Path objects from the DB file index.

        Returns:
            True if any files were moved or removed (caller should recheck).
        """
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
                try:
                    if expected.stat().st_size == size:
                        continue  # Already in place with correct size.
                except OSError:
                    pass
                # Exists but wrong size (libtorrent pre-allocated) - proceed.

            src = local_index.get((expected.name, size))
            if src and src.exists():
                try:
                    expected.parent.mkdir(parents=True, exist_ok=True)
                    vacated_dirs.add(src.parent)
                    src.rename(expected)
                    log.info(
                        "Pre-positioned share=%s %s -> %s",
                        share_id[:8],
                        src.relative_to(save_path),
                        rel_path,
                    )
                    moved_any = True
                except Exception as exc:
                    log.debug("Could not pre-position %s: %s", src, exc)

        # Walk up from vacated dirs to remove newly-empty ancestor dirs.
        dirs_to_try: set[Path] = set()
        for d in vacated_dirs:
            p = d
            while p != local_path and p.is_relative_to(local_path):
                dirs_to_try.add(p)
                p = p.parent
        for d in sorted(dirs_to_try, key=lambda p: len(p.parts), reverse=True):
            try:
                d.rmdir()
                log.info(
                    "Removed vacated dir share=%s %s",
                    share_id[:8],
                    d.relative_to(local_path),
                )
            except OSError:
                pass

        # Remove stale tracked files that are no longer in the new layout.
        new_layout = {save_path / rel_path for rel_path, _ in torrent_files}
        for f in tracked_paths:
            if f not in new_layout and f.exists():
                try:
                    f.unlink()
                    log.info(
                        "Removed stale tracked file share=%s %s",
                        share_id[:8],
                        f.relative_to(local_path),
                    )
                    moved_any = True
                except Exception as exc:
                    log.debug("Could not remove stale file %s: %s", f, exc)

        # Remove any empty directories left under local_path.
        if local_path.exists():
            for d in sorted(
                [p for p in local_path.rglob("*") if p.is_dir()],
                key=lambda p: len(p.parts),
                reverse=True,
            ):
                try:
                    d.rmdir()
                except OSError:
                    pass

        return moved_any

    # ── Task management helpers ────────────────────────────────────────────────

    def cancel_pending_rebuild(self, share_id: str) -> None:
        """
        Cancel any pending deferred rebuild task for a share.

        Safe to call even if no task is pending.

        Args:
            share_id: Identifier for the share.
        """
        task = self._pending_rebuild.pop(share_id, None)
        if task and not task.done():
            task.cancel()

    def clear_state(self, share_id: str) -> None:
        """
        Remove a share from the internal rebuild-tracking dicts.

        Should be called when a share is removed so bookkeeping does not
        accumulate stale entries.

        Args:
            share_id: Identifier for the share.
        """
        self._last_rebuild.pop(share_id, None)
        self.cancel_pending_rebuild(share_id)

    def stop_all(self) -> None:
        """
        Cancel all pending deferred rebuild tasks.

        Called during daemon shutdown to clean up background tasks.
        """
        for share_id in list(self._pending_rebuild):
            self.cancel_pending_rebuild(share_id)
