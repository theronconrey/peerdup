"""
Registry gRPC client.

Wraps the generated RegistryService stub with:
  - TLS / mTLS credential setup
  - Automatic reconnect with exponential backoff
  - WatchSharePeers stream management (background task per share)
  - Bearer token injection via interceptor
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator, Callable

import grpc

log = logging.getLogger(__name__)

_BACKOFF_INITIAL = 1.0   # seconds
_BACKOFF_MAX     = 60.0
_BACKOFF_FACTOR  = 2.0


class _BearerInterceptor(grpc.UnaryUnaryClientInterceptor,
                         grpc.UnaryStreamClientInterceptor,
                         grpc.StreamUnaryClientInterceptor,
                         grpc.StreamStreamClientInterceptor):
    """Injects Authorization: Bearer <token> into every call."""

    def __init__(self, token_fn: Callable[[], str | None]):
        self._token_fn = token_fn

    def _inject(self, metadata):
        token = self._token_fn()
        if token:
            return list(metadata or []) + [("authorization", f"Bearer {token}")]
        return metadata

    def intercept_unary_unary(self, continuation, client_call_details, request):
        details = client_call_details._replace(
            metadata=self._inject(client_call_details.metadata)
        )
        return continuation(details, request)

    def intercept_unary_stream(self, continuation, client_call_details, request):
        details = client_call_details._replace(
            metadata=self._inject(client_call_details.metadata)
        )
        return continuation(details, request)

    def intercept_stream_unary(self, continuation, client_call_details,
                               request_iterator):
        details = client_call_details._replace(
            metadata=self._inject(client_call_details.metadata)
        )
        return continuation(details, request_iterator)

    def intercept_stream_stream(self, continuation, client_call_details,
                                request_iterator):
        details = client_call_details._replace(
            metadata=self._inject(client_call_details.metadata)
        )
        return continuation(details, request_iterator)


class RegistryClient:
    """
    Thread-safe registry client. Call connect() before use.
    The bearer token is injected automatically after registration.
    """

    def __init__(self, address: str, tls: bool = False,
                 ca_file: str | None = None,
                 cert_file: str | None = None,
                 key_file: str | None = None):
        self._address   = address
        self._tls       = tls
        self._ca_file   = ca_file
        self._cert_file = cert_file
        self._key_file  = key_file
        self._token: str | None = None
        self._channel = None
        self._stub    = None

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self):
        """Open the gRPC channel. Call once at startup."""
        import registry_pb2_grpc as pb_grpc  # type: ignore

        interceptor = _BearerInterceptor(lambda: self._token)

        if self._tls:
            creds = self._build_credentials()
            channel = grpc.secure_channel(self._address, creds)
        else:
            channel = grpc.insecure_channel(self._address)

        self._channel = grpc.intercept_channel(channel, interceptor)
        self._stub    = pb_grpc.RegistryServiceStub(self._channel)
        log.info("Registry client connected to %s (tls=%s)", self._address, self._tls)

    def close(self):
        if self._channel:
            self._channel.close()

    def _build_credentials(self):
        root_certs = None
        cert_chain = None
        private_key = None

        if self._ca_file:
            with open(self._ca_file, "rb") as f:
                root_certs = f.read()
        if self._cert_file and self._key_file:
            with open(self._cert_file, "rb") as f:
                cert_chain = f.read()
            with open(self._key_file, "rb") as f:
                private_key = f.read()

        return grpc.ssl_channel_credentials(
            root_certificates  = root_certs,
            private_key        = private_key,
            certificate_chain  = cert_chain,
        )

    # ── Auth ──────────────────────────────────────────────────────────────────

    def register_peer(self, peer_id: str, name: str, signature: bytes) -> str:
        """Register peer and store the returned bearer token. Returns token."""
        import registry_pb2 as pb  # type: ignore
        resp = self._stub.RegisterPeer(pb.RegisterPeerRequest(
            peer_id   = peer_id,
            name      = name,
            signature = signature,
        ))
        self._token = resp.token
        log.info("Registered peer_id=%s", peer_id)
        return resp.token

    # ── Announces ─────────────────────────────────────────────────────────────

    def announce(self, share_id: str, peer_id: str,
                 internal_addrs: list[dict],
                 ttl_seconds: int, signature: bytes,
                 info_hash: str = ""):
        """
        Announce presence for a share.
        internal_addrs: list of {"host": str, "port": int, "is_lan": bool}
        Returns (observed_external_host, observed_external_port, granted_ttl).
        """
        import registry_pb2 as pb  # type: ignore

        proto_addrs = [
            pb.PeerAddress(host=a["host"], port=a["port"], is_lan=a.get("is_lan", False))
            for a in internal_addrs
        ]
        resp = self._stub.Announce(pb.AnnounceRequest(
            share_id       = share_id,
            peer_id        = peer_id,
            internal_addrs = proto_addrs,
            ttl_seconds    = ttl_seconds,
            signature      = signature,
            info_hash      = info_hash,
        ))
        return (resp.observed_external.host,
                resp.observed_external.port,
                resp.ttl_seconds)

    # ── Peer discovery ────────────────────────────────────────────────────────

    def get_share_peers(self, share_id: str, online_only: bool = True):
        """Snapshot of peers for a share. Returns list of SharePeer protos."""
        import registry_pb2 as pb  # type: ignore
        resp = self._stub.GetSharePeers(pb.GetSharePeersRequest(
            share_id   = share_id,
            online_only = online_only,
        ))
        return list(resp.peers)

    def watch_share_peers(self, share_id: str, include_offline: bool = True):
        """
        Returns a gRPC streaming iterator of PeerEvent protos.
        Caller is responsible for iterating and handling RpcError / StopIteration.
        """
        import registry_pb2 as pb  # type: ignore
        return self._stub.WatchSharePeers(pb.WatchSharePeersRequest(
            share_id        = share_id,
            include_offline = include_offline,
        ))

    # ── Share management ──────────────────────────────────────────────────────

    def create_share(self, share_id: str, name: str, signature: bytes):
        """Call registry CreateShare. Returns the Share proto."""
        import registry_pb2 as pb  # type: ignore
        return self._stub.CreateShare(pb.CreateShareRequest(
            share_id  = share_id,
            name      = name,
            signature = signature,
        )).share

    def get_share(self, share_id: str):
        import registry_pb2 as pb  # type: ignore
        return self._stub.GetShare(pb.GetShareRequest(share_id=share_id)).share

    def add_peer_to_share(self, share_id: str, peer_id: str,
                          permission_name: str = "PERMISSION_READ_WRITE"):
        """Grant a peer access to a share. permission_name is the proto enum name."""
        import registry_pb2 as pb  # type: ignore
        perm = pb.Permission.Value(permission_name)
        return self._stub.AddPeerToShare(pb.AddPeerToShareRequest(
            share_id   = share_id,
            peer_id    = peer_id,
            permission = perm,
        )).share

    def remove_peer_from_share(self, share_id: str, peer_id: str):
        """Revoke a peer's access to a share."""
        import registry_pb2 as pb  # type: ignore
        self._stub.RemovePeerFromShare(pb.RemovePeerFromShareRequest(
            share_id = share_id,
            peer_id  = peer_id,
        ))

    def health(self) -> bool:
        try:
            import registry_pb2 as pb  # type: ignore
            resp = self._stub.Health(pb.HealthRequest())
            return resp.status == "ok"
        except grpc.RpcError:
            return False


# ── Watch loop with reconnect ─────────────────────────────────────────────────

async def watch_share_peers_loop(
    client: RegistryClient,
    share_id: str,
    on_event,           # async callable: (PeerEvent) -> None
    stop_event: asyncio.Event,
):
    """
    Runs the WatchSharePeers stream with exponential backoff reconnect.
    Calls on_event for each PeerEvent received.
    Stops when stop_event is set.
    """
    backoff = _BACKOFF_INITIAL

    while not stop_event.is_set():
        try:
            log.info("WatchSharePeers stream connecting share=%s", share_id)
            stream = client.watch_share_peers(share_id, include_offline=True)

            async for event in _aiter_stream(stream, stop_event):
                await on_event(event)
                backoff = _BACKOFF_INITIAL  # reset on success

        except grpc.RpcError as e:
            if stop_event.is_set():
                break
            log.warning("WatchSharePeers stream lost share=%s error=%s — "
                        "reconnect in %.1fs", share_id, e.code(), backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX)
        except Exception:
            if stop_event.is_set():
                break
            log.exception("WatchSharePeers unexpected error share=%s — "
                          "reconnect in %.1fs", share_id, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX)


_STREAM_END = object()


async def _aiter_stream(stream, stop_event: asyncio.Event):
    """
    Wrap a synchronous gRPC streaming call in an async iterator.

    Uses run_in_executor so that the blocking next() call doesn't stall
    the event loop while waiting for the next server message.
    """
    loop = asyncio.get_running_loop()
    while not stop_event.is_set():
        try:
            event = await loop.run_in_executor(None, next, stream, _STREAM_END)
        except asyncio.CancelledError:
            stream.cancel()
            raise
        if event is _STREAM_END:
            break
        if stop_event.is_set():
            stream.cancel()
            return
        yield event
