"""
Per-share announce heartbeat manager.

Extracted from coordinator.py so the announce lifecycle (start, stop,
one-shot) can be tested and reasoned about independently.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Dict

from daemon.identity import Identity
from daemon.registry.client import RegistryClient
from daemon.state.db import StateDB

log = logging.getLogger(__name__)

ANNOUNCE_INTERVAL_SECONDS = 120
ANNOUNCE_TTL_SECONDS      = 300


class AnnounceManager:
    """Manages per-share announce heartbeat tasks against the registry.

    Each active share gets its own asyncio task that re-announces every
    ANNOUNCE_INTERVAL_SECONDS. The manager is also callable for one-shot
    announces (e.g. after a torrent rebuild).
    """

    def __init__(
        self,
        identity: Identity,
        registry: RegistryClient,
        db: StateDB,
        listen_port: int,
        stop_event: asyncio.Event,
    ) -> None:
        self._identity    = identity
        self._registry    = registry
        self._db          = db
        self._listen_port = listen_port
        self._stop_event  = stop_event
        self._tasks: Dict[str, asyncio.Task] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    async def start_share(self, share_id: str) -> None:
        """Announce immediately, then start the heartbeat task.

        Idempotent: if a task is already running for this share_id, does
        nothing.
        """
        if share_id in self._tasks:
            return
        await self.announce_once(share_id)
        task = asyncio.create_task(
            self._announce_loop(share_id),
            name=f"announce-{share_id[:8]}",
        )
        self._tasks[share_id] = task

    def stop_share(self, share_id: str) -> None:
        """Cancel the heartbeat task for share_id. No-op if not running."""
        task = self._tasks.pop(share_id, None)
        if task is not None:
            task.cancel()

    async def announce_once(self, share_id: str) -> None:
        """Perform a single announce for share_id.

        Called externally after a torrent rebuild so peers see the updated
        info_hash immediately without waiting for the next heartbeat interval.
        """
        try:
            await self._announce(share_id)
        except Exception:
            log.exception("Announce failed share=%s", share_id)

    def local_addrs(self) -> list[dict]:
        """Return this host's LAN addresses on the listen port.

        Returns a list of dicts with keys ``host``, ``port``, and ``is_lan``.
        Falls back to ``0.0.0.0`` if no non-loopback address is found.
        """
        return self._local_addrs()

    async def stop_all(self) -> None:
        """Cancel all running heartbeat tasks.

        Called from the coordinator's stop() path to ensure clean shutdown.
        """
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _announce_loop(self, share_id: str) -> None:
        """Heartbeat: re-announce every ANNOUNCE_INTERVAL_SECONDS."""
        while not self._stop_event.is_set():
            await asyncio.sleep(ANNOUNCE_INTERVAL_SECONDS)
            if self._stop_event.is_set():
                break
            try:
                await self._announce(share_id)
            except Exception:
                log.exception("Announce failed share=%s", share_id)

    async def _announce(self, share_id: str) -> None:
        addrs = self._local_addrs()
        sig   = self._identity.sign_announce(share_id, addrs, ANNOUNCE_TTL_SECONDS)
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
        log.debug(
            "Announced share=%s ext=%s:%s ttl=%ds info_hash=%s",
            share_id, ext_host, ext_port, ttl, ih,
        )

    def _local_addrs(self) -> list[dict]:
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
        if not addrs:
            addrs.append({"host": "0.0.0.0", "port": self._listen_port,
                          "is_lan": True})
        return addrs
