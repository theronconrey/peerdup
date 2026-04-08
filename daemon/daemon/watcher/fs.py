"""
Filesystem watcher.

Uses watchdog (inotify on Linux) to watch share directories for changes.
Debounces rapid sequences of events (e.g. editor save sequences) into
a single FileChangedEvent after a quiet period.

Emits:
    FileChangedEvent(share_id, rel_path, change_type)
    change_type: "created" | "modified" | "deleted" | "moved"
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from watchdog.events import (
    FileCreatedEvent, FileDeletedEvent,
    FileModifiedEvent, FileMovedEvent,
    FileSystemEvent, FileSystemEventHandler,
)
from watchdog.observers import Observer

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 0.5   # wait this long after last event before emitting


@dataclass
class FileChangedEvent:
    share_id:    str
    rel_path:    str   # relative to share root
    change_type: str   # "created" | "modified" | "deleted" | "moved"
    dest_path:   str | None = None  # for "moved" events


class _ShareHandler(FileSystemEventHandler):
    """
    watchdog handler for a single share directory.
    Debounces events and forwards them to the coordinator queue.
    """

    def __init__(self, share_id: str, root: str,
                 emit: Callable[[FileChangedEvent], None]):
        super().__init__()
        self._share_id = share_id
        self._root     = root.rstrip("/") + "/"
        self._emit     = emit

        # Debounce: path -> (change_type, dest_path, deadline)
        self._pending:  dict[str, tuple[str, str | None, float]] = {}
        self._lock      = threading.Lock()
        self._timer: threading.Timer | None = None

    def _rel(self, path: str) -> str:
        return os.path.relpath(path, self._root.rstrip("/"))

    def on_created(self, event: FileSystemEvent):
        if event.is_directory:
            return
        self._debounce(event.src_path, "created", None)

    def on_modified(self, event: FileSystemEvent):
        if event.is_directory:
            return
        self._debounce(event.src_path, "modified", None)

    def on_deleted(self, event: FileSystemEvent):
        if event.is_directory:
            return
        self._debounce(event.src_path, "deleted", None)

    def on_moved(self, event: FileMovedEvent):
        if event.is_directory:
            return
        self._debounce(event.src_path, "moved", event.dest_path)

    def _debounce(self, path: str, change_type: str, dest: str | None):
        deadline = time.monotonic() + DEBOUNCE_SECONDS
        with self._lock:
            # Later events win for the same path, except deleted always wins.
            existing = self._pending.get(path)
            if existing and existing[0] == "deleted":
                return
            self._pending[path] = (change_type, dest, deadline)
            self._reschedule()

    def _reschedule(self):
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(DEBOUNCE_SECONDS, self._flush)
        self._timer.daemon = True
        self._timer.start()

    def _flush(self):
        now = time.monotonic()
        with self._lock:
            ready = {
                path: val for path, val in self._pending.items()
                if val[2] <= now
            }
            for path in ready:
                del self._pending[path]
            still_pending = bool(self._pending)

        for path, (change_type, dest, _) in ready.items():
            try:
                rel = self._rel(path)
                dest_rel = self._rel(dest) if dest else None
                evt = FileChangedEvent(
                    share_id    = self._share_id,
                    rel_path    = rel,
                    change_type = change_type,
                    dest_path   = dest_rel,
                )
                log.debug("FileEvent share=%s type=%s path=%s",
                          self._share_id, change_type, rel)
                self._emit(evt)
            except Exception:
                log.exception("Error emitting FileChangedEvent path=%s", path)

        if still_pending:
            with self._lock:
                self._reschedule()


class FileWatcher:
    """
    Manages watchdog Observer instances for multiple share directories.
    Thread-safe: add_share / remove_share can be called at runtime.
    """

    def __init__(self, on_change: Callable[[FileChangedEvent], None]):
        """
        on_change: called (from watchdog thread) for each debounced file event.
        For asyncio integration, use FileWatcher.make_async_bridge().
        """
        self._on_change  = on_change
        self._observer   = Observer()
        self._watches:    dict[str, object] = {}   # share_id -> watchdog watch
        self._handlers:   dict[str, _ShareHandler] = {}
        self._lock        = threading.Lock()

    def start(self):
        self._observer.start()
        log.info("FileWatcher started")

    def stop(self):
        self._observer.stop()
        self._observer.join()
        log.info("FileWatcher stopped")

    def add_share(self, share_id: str, local_path: str):
        """Start watching local_path for share_id. Idempotent."""
        with self._lock:
            if share_id in self._watches:
                log.debug("FileWatcher: share %s already watched", share_id)
                return

            Path(local_path).mkdir(parents=True, exist_ok=True)
            handler = _ShareHandler(share_id, local_path, self._on_change)
            watch   = self._observer.schedule(handler, local_path, recursive=True)
            self._watches[share_id]  = watch
            self._handlers[share_id] = handler
            log.info("FileWatcher watching share=%s path=%s", share_id, local_path)

    def remove_share(self, share_id: str):
        """Stop watching a share directory."""
        with self._lock:
            watch = self._watches.pop(share_id, None)
            self._handlers.pop(share_id, None)
            if watch:
                self._observer.unschedule(watch)
                log.info("FileWatcher stopped watching share=%s", share_id)

    @staticmethod
    def make_async_bridge(loop: asyncio.AbstractEventLoop,
                          queue: asyncio.Queue
                          ) -> Callable[[FileChangedEvent], None]:
        """
        Returns an on_change callback that is safe to call from watchdog's
        thread — it schedules the event onto the given asyncio event loop.
        """
        def _bridge(event: FileChangedEvent):
            loop.call_soon_threadsafe(queue.put_nowait, event)
        return _bridge
