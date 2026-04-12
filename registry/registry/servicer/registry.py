"""
RegistryService gRPC servicer.

Implements all RPCs defined in proto/registry.proto.
All database operations are synchronous SQLAlchemy; the async wrapper
handles offloading to a thread pool so the event loop stays free.
"""

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import grpc

from registry.auth.crypto import (
    extract_bearer_token,
    generate_token,
    hash_token,
    verify_announce,
    verify_create_share,
    verify_register_peer,
    verify_token,
)
from registry.db.models import (
    AnnouncementModel,
    PermissionEnum,
    PeerModel,
    ShareModel,
    SharePeerModel,
)
from registry.audit import make_audit_logger
from registry.events import event_bus
from registry.ttl import TTL_MAX_SECONDS, TTL_MIN_SECONDS, clamp_ttl

log = logging.getLogger(__name__)

VERSION = "0.1.0"


class RateLimiter:
    """
    Thread-safe token bucket rate limiter keyed by (peer_id, share_id).

    Each key gets its own bucket. Buckets are lazily created and never
    deleted — the number of active (peer_id, share_id) pairs is bounded
    by the registry's share membership, so unbounded growth is not a concern.

    Args:
        rate_per_minute: Sustained announce rate allowed per key.
                         0 disables limiting entirely.
        burst:           Maximum burst size (tokens above steady-state).
                         Defaults to rate_per_minute (one minute of credit).
    """

    def __init__(self, rate_per_minute: int, burst: int | None = None) -> None:
        self._rpm   = rate_per_minute
        self._burst = burst if burst is not None else rate_per_minute
        self._lock  = threading.Lock()
        self._buckets: dict[tuple[str, str], tuple[float, float]] = {}
        # bucket value: (tokens, last_refill_time)

    def is_allowed(self, peer_id: str, share_id: str) -> tuple[bool, float]:
        """
        Check if the (peer_id, share_id) pair is within rate limits.

        Returns:
            (allowed, retry_after_seconds)
            retry_after_seconds is 0.0 when allowed is True.
        """
        if self._rpm == 0:
            return True, 0.0

        key = (peer_id, share_id)
        now = time.monotonic()
        rate_per_sec = self._rpm / 60.0

        with self._lock:
            tokens, last = self._buckets.get(key, (float(self._burst), now))
            # Refill tokens based on elapsed time.
            elapsed = now - last
            tokens  = min(self._burst, tokens + elapsed * rate_per_sec)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                return True, 0.0
            else:
                # Don't update last — no refill credit consumed.
                self._buckets[key] = (tokens, last)
                retry_after = (1.0 - tokens) / rate_per_sec
                return False, retry_after


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_proto_ts(dt: datetime | None, Timestamp):
    if dt is None:
        return None
    ts = Timestamp()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ts.FromDatetime(dt)
    return ts


def _perm_model_to_proto(perm: PermissionEnum, Permission) -> int:
    return {
        PermissionEnum.READ_WRITE: Permission.Value("PERMISSION_READ_WRITE"),
        PermissionEnum.READ_ONLY:  Permission.Value("PERMISSION_READ_ONLY"),
        PermissionEnum.ENCRYPTED:  Permission.Value("PERMISSION_ENCRYPTED"),
    }.get(perm, Permission.Value("PERMISSION_UNSPECIFIED"))


def _perm_proto_to_model(perm_int: int, Permission) -> PermissionEnum:
    return {
        Permission.Value("PERMISSION_READ_WRITE"): PermissionEnum.READ_WRITE,
        Permission.Value("PERMISSION_READ_ONLY"):  PermissionEnum.READ_ONLY,
        Permission.Value("PERMISSION_ENCRYPTED"):  PermissionEnum.ENCRYPTED,
    }.get(perm_int, PermissionEnum.READ_WRITE)


def _build_share_peer(membership, ann, pb, Timestamp) -> object:
    """Build a SharePeer proto from a SharePeerModel + optional AnnouncementModel."""
    addrs = []
    online = False

    if ann and ann.expires_at.replace(tzinfo=timezone.utc) > _now():
        online = True
        try:
            raw = json.loads(ann.internal_addrs)
            for a in raw:
                addrs.append(pb.PeerAddress(
                    host=a["host"], port=a["port"], is_lan=a.get("is_lan", False)
                ))
        except Exception:
            pass
        if ann.external_host:
            addrs.append(pb.PeerAddress(
                host=ann.external_host, port=ann.external_port or 0, is_lan=False
            ))

    return pb.SharePeer(
        peer_id    = membership.peer_id,
        name       = membership.peer.name if membership.peer else membership.peer_id,
        permission = _perm_model_to_proto(membership.permission, pb.Permission),
        online     = online,
        addresses  = addrs,
        last_seen  = _to_proto_ts(
            ann.announced_at if ann else None, Timestamp
        ) if ann else None,
        info_hash  = (ann.info_hash or "") if ann else "",
    )


class RegistryServicer:
    def __init__(self, session_factory,
                 event_loop: asyncio.AbstractEventLoop | None = None,
                 rate_limiter: RateLimiter | None = None,
                 audit=None):
        self._sf           = session_factory
        self._loop         = event_loop   # running loop shared by publish + drain
        self._rate_limiter = rate_limiter or RateLimiter(rate_per_minute=0)  # 0 = disabled
        self._audit        = audit or make_audit_logger({"enabled": False})

        # Lazy-import generated stubs so this module can be read
        # before code generation has run.
        from registry import registry_pb2 as pb           # type: ignore
        from registry import registry_pb2_grpc as pb_grpc  # type: ignore
        from google.protobuf.timestamp_pb2 import Timestamp
        self._pb        = pb
        self._pb_grpc   = pb_grpc
        self._Timestamp = Timestamp

    # ── Auth helper ───────────────────────────────────────────────────────────

    def _authenticated_peer(self, context) -> str | None:
        """Return peer_id if the bearer token in metadata is valid, else None."""
        token = extract_bearer_token(context)
        if not token:
            return None
        with self._sf() as session:
            # Hash and look up — avoids scanning all peers.
            h = hash_token(token)
            peer = session.query(PeerModel).filter_by(token_hash=h).first()
            return peer.peer_id if peer else None

    def _require_auth(self, context):
        """Abort with UNAUTHENTICATED if bearer token missing/invalid."""
        peer_id = self._authenticated_peer(context)
        if not peer_id:
            self._audit.log("auth_failure", "", "denied",
                            remote_ip=_remote_ip(context))
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Valid bearer token required")
        return peer_id

    def _require_share_member(self, context, share_id: str, caller_id: str,
                              action: str = ""):
        """Abort with PERMISSION_DENIED if caller is not a member of the share."""
        with self._sf() as session:
            m = session.query(SharePeerModel).filter_by(
                share_id=share_id, peer_id=caller_id
            ).first()
            if not m:
                self._audit.log(action, caller_id, "denied",
                                share_id=share_id, remote_ip=_remote_ip(context))
                context.abort(grpc.StatusCode.PERMISSION_DENIED,
                              "Not a member of this share")
            return m

    def _require_share_owner(self, context, share_id: str, caller_id: str,
                             action: str = ""):
        """Abort with PERMISSION_DENIED if caller is not the share owner."""
        with self._sf() as session:
            share = session.query(ShareModel).filter_by(share_id=share_id).first()
            if not share:
                self._audit.log(action, caller_id, "denied",
                                share_id=share_id, remote_ip=_remote_ip(context))
                context.abort(grpc.StatusCode.NOT_FOUND, "Share not found")
            if share.owner_id != caller_id:
                self._audit.log(action, caller_id, "denied",
                                share_id=share_id, remote_ip=_remote_ip(context))
                context.abort(grpc.StatusCode.PERMISSION_DENIED,
                              "Only the share owner can perform this action")
            return share

    # ── RegisterPeer ──────────────────────────────────────────────────────────

    def RegisterPeer(self, request, context):
        pb, Timestamp = self._pb, self._Timestamp

        if not verify_register_peer(request.peer_id, request.name, request.signature):
            self._audit.log("register_peer", request.peer_id, "denied",
                            remote_ip=_remote_ip(context))
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Invalid signature")

        token = generate_token()

        with self._sf() as session:
            existing = session.query(PeerModel).filter_by(
                peer_id=request.peer_id
            ).first()
            if existing:
                # Re-registration: rotate token, update name.
                existing.name       = request.name
                existing.token_hash = hash_token(token)
                session.commit()
                peer_model = existing
            else:
                peer_model = PeerModel(
                    peer_id    = request.peer_id,
                    name       = request.name,
                    token_hash = hash_token(token),
                )
                session.add(peer_model)
                session.commit()

            peer_proto = pb.Peer(
                peer_id = peer_model.peer_id,
                name    = peer_model.name,
                online  = False,
            )

        log.info("RegisterPeer peer_id=%s name=%s", request.peer_id, request.name)
        self._audit.log("register_peer", request.peer_id, "ok",
                        remote_ip=_remote_ip(context))
        return pb.RegisterPeerResponse(peer=peer_proto, token=token)

    # ── GetPeer ───────────────────────────────────────────────────────────────

    def GetPeer(self, request, context):
        pb, Timestamp = self._pb, self._Timestamp
        caller_id = self._require_auth(context)

        with self._sf() as session:
            peer = session.query(PeerModel).filter_by(
                peer_id=request.peer_id
            ).first()
            if not peer:
                context.abort(grpc.StatusCode.NOT_FOUND, "Peer not found")

            # Find any recent announcement to get address / online state.
            ann = (
                session.query(AnnouncementModel)
                .filter(AnnouncementModel.peer_id == request.peer_id,
                        AnnouncementModel.expires_at > _now())
                .first()
            )
            addrs = []
            if ann:
                try:
                    raw = json.loads(ann.internal_addrs)
                    for a in raw:
                        addrs.append(pb.PeerAddress(
                            host=a["host"], port=a["port"],
                            is_lan=a.get("is_lan", False)
                        ))
                except Exception:
                    pass

            self._audit.log("get_peer", caller_id, "ok",
                            remote_ip=_remote_ip(context))
            return pb.GetPeerResponse(peer=pb.Peer(
                peer_id   = peer.peer_id,
                name      = peer.name,
                addresses = addrs,
                online    = ann is not None,
                last_seen = _to_proto_ts(ann.announced_at if ann else None, Timestamp),
            ))

    # ── CreateShare ───────────────────────────────────────────────────────────

    def CreateShare(self, request, context):
        pb, Timestamp = self._pb, self._Timestamp
        caller_id = self._require_auth(context)

        if not verify_create_share(caller_id, request.share_id,
                                   request.name, request.signature):
            self._audit.log("create_share", caller_id, "denied",
                            share_id=request.share_id, remote_ip=_remote_ip(context))
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Invalid signature")

        with self._sf() as session:
            if session.query(ShareModel).filter_by(
                share_id=request.share_id
            ).first():
                self._audit.log("create_share", caller_id, "denied",
                                share_id=request.share_id, remote_ip=_remote_ip(context))
                context.abort(grpc.StatusCode.ALREADY_EXISTS, "Share already exists")

            share = ShareModel(
                share_id = request.share_id,
                name     = request.name,
                owner_id = caller_id,
            )
            session.add(share)
            # Owner is automatically a read-write member.
            membership = SharePeerModel(
                share_id   = request.share_id,
                peer_id    = caller_id,
                permission = PermissionEnum.READ_WRITE,
            )
            session.add(membership)
            session.commit()
            session.refresh(share)

            share_proto = pb.Share(
                share_id   = share.share_id,
                name       = share.name,
                owner_id   = share.owner_id,
                created_at = _to_proto_ts(share.created_at, Timestamp),
            )

        log.info("CreateShare share_id=%s owner=%s", request.share_id, caller_id)
        self._audit.log("create_share", caller_id, "ok",
                        share_id=request.share_id, remote_ip=_remote_ip(context))
        return pb.CreateShareResponse(share=share_proto)

    # ── GetShare ──────────────────────────────────────────────────────────────

    def GetShare(self, request, context):
        pb, Timestamp = self._pb, self._Timestamp
        caller_id = self._require_auth(context)

        with self._sf() as session:
            share = session.query(ShareModel).filter_by(
                share_id=request.share_id
            ).first()
            if not share:
                context.abort(grpc.StatusCode.NOT_FOUND, "Share not found")

            # Must be a member to see the share.
            if not session.query(SharePeerModel).filter_by(
                share_id=request.share_id, peer_id=caller_id
            ).first():
                self._audit.log("get_share", caller_id, "denied",
                                share_id=request.share_id, remote_ip=_remote_ip(context))
                context.abort(grpc.StatusCode.PERMISSION_DENIED,
                              "Not a member of this share")

            members = []
            for m in share.members:
                ann = (
                    session.query(AnnouncementModel)
                    .filter_by(share_id=request.share_id, peer_id=m.peer_id)
                    .first()
                )
                members.append(_build_share_peer(m, ann, pb, Timestamp))

            self._audit.log("get_share", caller_id, "ok",
                            share_id=request.share_id, remote_ip=_remote_ip(context))
            return pb.GetShareResponse(share=pb.Share(
                share_id   = share.share_id,
                name       = share.name,
                owner_id   = share.owner_id,
                peers      = members,
                created_at = _to_proto_ts(share.created_at, Timestamp),
            ))

    # ── AddPeerToShare ────────────────────────────────────────────────────────

    def AddPeerToShare(self, request, context):
        pb, Timestamp = self._pb, self._Timestamp
        caller_id = self._require_auth(context)
        self._require_share_owner(context, request.share_id, caller_id,
                                  action="add_peer_to_share")

        with self._sf() as session:
            if not session.query(PeerModel).filter_by(
                peer_id=request.peer_id
            ).first():
                context.abort(grpc.StatusCode.NOT_FOUND, "Target peer not found")

            existing = session.query(SharePeerModel).filter_by(
                share_id=request.share_id, peer_id=request.peer_id
            ).first()
            if existing:
                existing.permission = _perm_proto_to_model(
                    request.permission, pb.Permission
                )
            else:
                session.add(SharePeerModel(
                    share_id   = request.share_id,
                    peer_id    = request.peer_id,
                    permission = _perm_proto_to_model(request.permission, pb.Permission),
                ))
            session.commit()

            share = session.query(ShareModel).filter_by(
                share_id=request.share_id
            ).first()
            members = []
            for m in share.members:
                ann = (
                    session.query(AnnouncementModel)
                    .filter_by(share_id=request.share_id, peer_id=m.peer_id)
                    .first()
                )
                members.append(_build_share_peer(m, ann, pb, Timestamp))

        # Notify watchers that membership changed.
        self._publish_updated(request.share_id, request.peer_id)

        log.info("AddPeerToShare share=%s peer=%s", request.share_id, request.peer_id)
        self._audit.log("add_peer_to_share", caller_id, "ok",
                        share_id=request.share_id, remote_ip=_remote_ip(context))
        return pb.AddPeerToShareResponse(share=pb.Share(
            share_id = request.share_id,
            name     = share.name,
            owner_id = share.owner_id,
            peers    = members,
        ))

    # ── RemovePeerFromShare ───────────────────────────────────────────────────

    def RemovePeerFromShare(self, request, context):
        pb = self._pb
        caller_id = self._require_auth(context)
        self._require_share_owner(context, request.share_id, caller_id,
                                  action="remove_peer_from_share")

        if request.peer_id == caller_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT,
                          "Owner cannot remove themselves")

        with self._sf() as session:
            m = session.query(SharePeerModel).filter_by(
                share_id=request.share_id, peer_id=request.peer_id
            ).first()
            if m:
                session.delete(m)
            # Also delete any active announcement.
            ann = session.query(AnnouncementModel).filter_by(
                share_id=request.share_id, peer_id=request.peer_id
            ).first()
            if ann:
                session.delete(ann)
            session.commit()

        # Notify watchers.
        from google.protobuf.timestamp_pb2 import Timestamp
        ts = Timestamp()
        ts.FromDatetime(_now())
        sp = pb.SharePeer(peer_id=request.peer_id, online=False)
        event = pb.PeerEvent(
            type        = pb.PeerEvent.EVENT_TYPE_REMOVED,
            peer        = sp,
            occurred_at = ts,
        )
        self._publish_event(request.share_id, event)

        log.info("RemovePeerFromShare share=%s peer=%s",
                 request.share_id, request.peer_id)
        self._audit.log("remove_peer_from_share", caller_id, "ok",
                        share_id=request.share_id, remote_ip=_remote_ip(context))
        return pb.RemovePeerFromShareResponse()

    # ── Announce ──────────────────────────────────────────────────────────────

    def Announce(self, request, context):
        pb, Timestamp = self._pb, self._Timestamp
        caller_id = self._require_auth(context)

        if caller_id != request.peer_id:
            self._audit.log("announce", caller_id, "denied",
                            share_id=request.share_id, remote_ip=_remote_ip(context))
            context.abort(grpc.StatusCode.PERMISSION_DENIED,
                          "peer_id in request must match authenticated peer")

        if not verify_announce(request.peer_id, request.share_id,
                               request.internal_addrs, request.ttl_seconds,
                               request.signature):
            self._audit.log("announce", caller_id, "denied",
                            share_id=request.share_id, remote_ip=_remote_ip(context))
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Invalid announce signature")

        # Must be a member of the share.
        with self._sf() as session:
            if not session.query(SharePeerModel).filter_by(
                share_id=request.share_id, peer_id=request.peer_id
            ).first():
                self._audit.log("announce", caller_id, "denied",
                                share_id=request.share_id, remote_ip=_remote_ip(context))
                context.abort(grpc.StatusCode.PERMISSION_DENIED,
                              "Not a member of this share")

        # Rate limit check — after auth/membership, before DB write.
        allowed, retry_after = self._rate_limiter.is_allowed(
            request.peer_id, request.share_id
        )
        if not allowed:
            self._audit.log("announce", caller_id, "denied",
                            share_id=request.share_id, remote_ip=_remote_ip(context))
            context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                f"Announce rate limit exceeded. Retry after {retry_after:.1f}s",
            )

        # Extract caller's external IP from gRPC peer info.
        peer_info = context.peer()  # e.g. "ipv4:1.2.3.4:54321"
        ext_host, ext_port = _parse_peer_addr(peer_info)

        ttl = clamp_ttl(request.ttl_seconds or TTL_MIN_SECONDS)
        expires = _now() + timedelta(seconds=ttl)

        addrs_json = json.dumps([
            {"host": a.host, "port": a.port, "is_lan": a.is_lan}
            for a in request.internal_addrs
        ])

        was_online = False
        with self._sf() as session:
            existing = session.query(AnnouncementModel).filter_by(
                share_id=request.share_id, peer_id=request.peer_id
            ).first()

            if existing:
                was_online = existing.expires_at.replace(
                    tzinfo=timezone.utc
                ) > _now()
                existing.internal_addrs = addrs_json
                existing.external_host  = ext_host
                existing.external_port  = ext_port
                existing.info_hash      = request.info_hash or existing.info_hash
                existing.announced_at   = _now()
                existing.expires_at     = expires
            else:
                session.add(AnnouncementModel(
                    share_id       = request.share_id,
                    peer_id        = request.peer_id,
                    internal_addrs = addrs_json,
                    external_host  = ext_host,
                    external_port  = ext_port,
                    info_hash      = request.info_hash or None,
                    announced_at   = _now(),
                    expires_at     = expires,
                ))
            session.commit()

        # Publish event to watchers.
        import asyncio
        ts = Timestamp()
        ts.FromDatetime(_now())

        all_addrs = list(request.internal_addrs)
        if ext_host:
            all_addrs.append(pb.PeerAddress(
                host=ext_host, port=ext_port or 0, is_lan=False
            ))

        event_type = (pb.PeerEvent.EVENT_TYPE_UPDATED if was_online
                      else pb.PeerEvent.EVENT_TYPE_ONLINE)
        sp = pb.SharePeer(
            peer_id   = request.peer_id,
            online    = True,
            addresses = all_addrs,
            info_hash = request.info_hash or "",
        )
        event = pb.PeerEvent(type=event_type, peer=sp, occurred_at=ts)
        self._publish_event(request.share_id, event)

        log.info("Announce peer=%s share=%s ttl=%ds ext=%s:%s",
                 request.peer_id, request.share_id, ttl, ext_host, ext_port)
        self._audit.log("announce", caller_id, "ok",
                        share_id=request.share_id, remote_ip=_remote_ip(context))

        return pb.AnnounceResponse(
            observed_external=pb.PeerAddress(
                host=ext_host or "", port=ext_port or 0, is_lan=False
            ),
            ttl_seconds=ttl,
        )

    # ── GetSharePeers (snapshot) ───────────────────────────────────────────────

    def GetSharePeers(self, request, context):
        pb, Timestamp = self._pb, self._Timestamp
        caller_id = self._require_auth(context)
        self._require_share_member(context, request.share_id, caller_id,
                                   action="get_share_peers")

        with self._sf() as session:
            share = session.query(ShareModel).filter_by(
                share_id=request.share_id
            ).first()
            if not share:
                context.abort(grpc.StatusCode.NOT_FOUND, "Share not found")

            peers = []
            for m in share.members:
                ann = session.query(AnnouncementModel).filter_by(
                    share_id=request.share_id, peer_id=m.peer_id
                ).first()
                sp = _build_share_peer(m, ann, pb, Timestamp)
                if request.online_only and not sp.online:
                    continue
                peers.append(sp)

        self._audit.log("get_share_peers", caller_id, "ok",
                        share_id=request.share_id, remote_ip=_remote_ip(context))
        return pb.GetSharePeersResponse(peers=peers)

    # ── WatchSharePeers (server-streaming) ────────────────────────────────────

    def WatchSharePeers(self, request, context):
        """
        Server-streaming RPC. Yields PeerEvents as peer state changes.

        Flow:
          1. Auth + membership check.
          2. Yield current snapshot as synthetic ONLINE events (so client
             has full state before deltas arrive).
          3. Subscribe to event bus and yield events until client disconnects.
        """
        pb, Timestamp = self._pb, self._Timestamp
        caller_id = self._require_auth(context)
        self._require_share_member(context, request.share_id, caller_id,
                                   action="watch_share_peers")
        self._audit.log("watch_share_peers", caller_id, "ok",
                        share_id=request.share_id, remote_ip=_remote_ip(context))

        import asyncio
        import queue
        import threading

        ts_now = Timestamp()
        ts_now.FromDatetime(_now())

        # 1. Emit snapshot.
        with self._sf() as session:
            share = session.query(ShareModel).filter_by(
                share_id=request.share_id
            ).first()
            if not share:
                context.abort(grpc.StatusCode.NOT_FOUND, "Share not found")

            for m in share.members:
                ann = session.query(AnnouncementModel).filter_by(
                    share_id=request.share_id, peer_id=m.peer_id
                ).first()
                sp = _build_share_peer(m, ann, pb, Timestamp)
                if sp.online:
                    yield pb.PeerEvent(
                        type        = pb.PeerEvent.EVENT_TYPE_ONLINE,
                        peer        = sp,
                        occurred_at = ts_now,
                    )
                elif request.include_offline:
                    yield pb.PeerEvent(
                        type        = pb.PeerEvent.EVENT_TYPE_OFFLINE,
                        peer        = sp,
                        occurred_at = ts_now,
                    )

        # 2. Subscribe to live events using a thread-safe queue bridge.
        # gRPC servicer runs in a thread pool (sync servicer), so we bridge
        # the asyncio event bus to a threading.Queue.
        live_q: queue.Queue = queue.Queue(maxsize=128)

        async def _drain(share_id):
            async with event_bus.subscribe(share_id) as aq:
                while context.is_active():
                    try:
                        event = await asyncio.wait_for(aq.get(), timeout=5.0)
                        live_q.put(event)
                    except asyncio.TimeoutError:
                        continue

        if self._loop is not None:
            # Share the server's event loop so that publish() and subscribe()
            # always run on the same loop (required for asyncio.Lock correctness).
            drain_future = asyncio.run_coroutine_threadsafe(
                _drain(request.share_id), self._loop
            )
            own_loop = None
        else:
            # No server loop — spin up a dedicated one.  Event bus publishes
            # from Announce won't reach this drain (known limitation when
            # RegistryServicer is created without an event_loop).
            own_loop = asyncio.new_event_loop()
            drain_future = asyncio.run_coroutine_threadsafe(
                _drain(request.share_id), own_loop
            )
            t = threading.Thread(target=own_loop.run_forever, daemon=True)
            t.start()

        try:
            while context.is_active():
                try:
                    event = live_q.get(timeout=1.0)
                    if not request.include_offline and (
                        event.type == pb.PeerEvent.EVENT_TYPE_OFFLINE
                    ):
                        continue
                    yield event
                except queue.Empty:
                    continue
        finally:
            drain_future.cancel()
            if own_loop is not None:
                own_loop.call_soon_threadsafe(own_loop.stop)
                t.join(timeout=5)

    # ── Health ────────────────────────────────────────────────────────────────

    def Health(self, request, context):
        return self._pb.HealthResponse(status="ok", version=VERSION)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _publish_event(self, share_id: str, event) -> None:
        """
        Fire-and-forget: publish event to all WatchSharePeers subscribers.

        Must be called from a gRPC handler thread (not from the event loop).
        Requires self._loop to be set; silently skips if not available.
        """
        if self._loop is None or not self._loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(
            event_bus.publish(share_id, event), self._loop
        )

    def _publish_updated(self, share_id: str, peer_id: str):
        """Fire an UPDATED event for a peer (used after ACL changes)."""
        pass  # Extended in future — ACL change notification.


def _remote_ip(context) -> str:
    """Return just the host portion of the gRPC peer string."""
    host, _ = _parse_peer_addr(context.peer() or "")
    return host or ""


def _parse_peer_addr(peer_str: str) -> tuple[str | None, int | None]:
    """
    Parse gRPC peer string like 'ipv4:1.2.3.4:54321' or 'ipv6:[::1]:54321'.
    Returns (host, port) or (None, None).
    """
    try:
        if peer_str.startswith("ipv4:"):
            parts = peer_str[5:].rsplit(":", 1)
            return parts[0], int(parts[1])
        elif peer_str.startswith("ipv6:"):
            addr = peer_str[5:]
            # ipv6:[::1]:port
            bracket_end = addr.index("]")
            host = addr[1:bracket_end]
            port = int(addr[bracket_end + 2:])
            return host, port
    except Exception:
        pass
    return None, None
