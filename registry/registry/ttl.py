"""
Background task: TTL expiry sweep.

Runs every TTL_SWEEP_INTERVAL_SECONDS, finds announcements that have expired,
marks those peers offline, and publishes OFFLINE events to the event bus.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

from registry.db.models import AnnouncementModel, SharePeerModel, PermissionEnum
from registry.events import event_bus

log = logging.getLogger(__name__)

TTL_SWEEP_INTERVAL_SECONDS = 30
# Registry clamps client TTL hints to this range.
TTL_MIN_SECONDS = 60
TTL_MAX_SECONDS = 600


def clamp_ttl(requested: int) -> int:
    return max(TTL_MIN_SECONDS, min(TTL_MAX_SECONDS, requested))


async def ttl_sweep_loop(session_factory, on_sweep_complete=None):
    """
    Continuously sweeps for expired announcements and fires OFFLINE events.
    Intended to run as an asyncio background task for the lifetime of the server.

    Args:
        session_factory:    SQLAlchemy session factory.
        on_sweep_complete:  Optional callable invoked after each successful sweep.
                            Used by RegistryServicer.record_sweep() to track health.
    """
    # Import proto here to avoid import-time dependency before stubs are generated.
    from registry_pb2 import PeerEvent, SharePeer, PeerAddress  # type: ignore
    from google.protobuf.timestamp_pb2 import Timestamp          # type: ignore

    while True:
        await asyncio.sleep(TTL_SWEEP_INTERVAL_SECONDS)
        t0 = asyncio.get_event_loop().time()
        try:
            await _sweep(session_factory, PeerEvent, SharePeer, PeerAddress, Timestamp)
            duration_s = asyncio.get_event_loop().time() - t0
            if on_sweep_complete is not None:
                on_sweep_complete(duration_s)
        except Exception:
            log.exception("TTL sweep failed")


async def _sweep(session_factory, PeerEvent, SharePeer, PeerAddress, Timestamp):
    now = datetime.now(timezone.utc)

    with session_factory() as session:
        expired = (
            session.query(AnnouncementModel)
            .filter(AnnouncementModel.expires_at <= now)
            .all()
        )

        if not expired:
            return

        for ann in expired:
            share_id = ann.share_id
            peer_id  = ann.peer_id

            # Look up membership for name + permission.
            membership = (
                session.query(SharePeerModel)
                .filter_by(share_id=share_id, peer_id=peer_id)
                .first()
            )
            if not membership:
                session.delete(ann)
                continue

            name = membership.peer.name if membership.peer else peer_id

            log.info("TTL expired: peer=%s share=%s", peer_id, share_id)
            session.delete(ann)

            # Build and publish OFFLINE event.
            ts = Timestamp()
            ts.FromDatetime(now)

            sp = SharePeer(
                peer_id    = peer_id,
                name       = name,
                permission = _perm_to_proto(membership.permission),
                online     = False,
            )
            event = PeerEvent(
                type        = PeerEvent.EVENT_TYPE_OFFLINE,
                peer        = sp,
                occurred_at = ts,
            )
            await event_bus.publish(share_id, event)

        session.commit()


def _perm_to_proto(perm: PermissionEnum) -> int:
    from registry_pb2 import Permission  # type: ignore
    mapping = {
        PermissionEnum.READ_WRITE: Permission.Value("PERMISSION_READ_WRITE"),
        PermissionEnum.READ_ONLY:  Permission.Value("PERMISSION_READ_ONLY"),
        PermissionEnum.ENCRYPTED:  Permission.Value("PERMISSION_ENCRYPTED"),
    }
    return mapping.get(perm, Permission.Value("PERMISSION_UNSPECIFIED"))
