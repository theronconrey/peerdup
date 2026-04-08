"""
In-process event bus for WatchSharePeers streaming.

When peer state changes (online, offline, updated, removed), the servicer
publishes a PeerEvent. All active WatchSharePeers streams for that share_id
receive the event via an asyncio.Queue.

This is intentionally simple — single process, in-memory. If you scale to
multiple registry instances, replace with a Redis pub/sub or similar.
"""

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import AsyncIterator

# Import the generated proto class at runtime to avoid circular issues.
# Callers pass fully-constructed PeerEvent protos.


class EventBus:
    def __init__(self):
        # share_id -> list of asyncio.Queue
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def publish(self, share_id: str, event) -> None:
        """Publish a PeerEvent to all subscribers of share_id."""
        async with self._lock:
            queues = list(self._subscribers.get(share_id, []))
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer — drop the event rather than block.
                pass

    @asynccontextmanager
    async def subscribe(self, share_id: str,
                        maxsize: int = 64) -> AsyncIterator[asyncio.Queue]:
        """
        Context manager that registers a queue for share_id events
        and deregisters it on exit.

        Usage:
            async with bus.subscribe(share_id) as q:
                while True:
                    event = await q.get()
                    yield event
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        async with self._lock:
            self._subscribers[share_id].append(q)
        try:
            yield q
        finally:
            async with self._lock:
                try:
                    self._subscribers[share_id].remove(q)
                except ValueError:
                    pass


# Module-level singleton — imported by the servicer.
event_bus = EventBus()
