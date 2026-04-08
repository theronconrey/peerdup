"""
LAN peer discovery via IPv4 UDP multicast.

Peers broadcast a signed announcement packet every announce_interval seconds.
Listeners verify the Ed25519 signature, then call on_peer_seen for each
(peer_id, share_ids, host, libtorrent_port) tuple — letting the coordinator
inject valid peers directly into libtorrent without going through the registry.

Wire format (version 1, all integers big-endian)
────────────────────────────────────────────────
 Offset  Size  Field
      0     1  version        = 1
      1    32  peer_id_bytes  (raw 32-byte Ed25519 public key)
     33     2  listen_port    (uint16)
     35     2  share_count    (uint16)
     37  32*n  share_id_bytes (share_count × 32-byte Ed25519 public keys)
  37+32n   64  signature      (Ed25519, covers all preceding bytes)

Signature message = everything before the trailing 64 signature bytes.
The signature is verified with the public key embedded as peer_id_bytes,
proving the sender holds the private key whose public key IS their peer_id.

Minimum valid packet (zero shares): 101 bytes.
Maximum practical packet (100 shares): 3301 bytes — well within UDP limits.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from typing import Awaitable, Callable

import base58
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

VERSION      = 1
PEER_ID_LEN  = 32   # raw Ed25519 pubkey
SHARE_ID_LEN = 32
SIG_LEN      = 64
_MIN_PACKET  = 1 + PEER_ID_LEN + 2 + 2 + SIG_LEN  # 101 bytes


# ── Packet codec ─────────────────────────────────────────────────────────────

def pack_packet(identity, share_ids: list[str], listen_port: int) -> bytes:
    """
    Encode and sign an announcement packet.

    identity   — daemon.identity.Identity (provides peer_id + sign())
    share_ids  — base58-encoded share IDs to announce
    listen_port — libtorrent TCP listen port
    """
    peer_id_bytes = base58.b58decode(identity.peer_id)
    share_bytes   = [base58.b58decode(sid) for sid in share_ids]
    n             = len(share_bytes)

    body  = struct.pack("!B", VERSION)
    body += peer_id_bytes
    body += struct.pack("!HH", listen_port, n)
    for sb in share_bytes:
        body += sb

    sig = identity.sign(body)
    return body + sig


def unpack_packet(data: bytes) -> tuple[str, list[str], int] | None:
    """
    Decode and verify an announcement packet.

    Returns (peer_id, share_ids, listen_port) on success, None on any error.
    Does NOT check whether the peer_id is our own — callers must do that.
    """
    if len(data) < _MIN_PACKET:
        return None

    pos = 0
    version = data[pos]; pos += 1
    if version != VERSION:
        log.debug("LAN packet: unknown version %d", version)
        return None

    peer_id_bytes = data[pos:pos + PEER_ID_LEN]; pos += PEER_ID_LEN
    if len(peer_id_bytes) < PEER_ID_LEN:
        return None

    if pos + 4 > len(data):
        return None
    listen_port, n_shares = struct.unpack_from("!HH", data, pos); pos += 4

    expected = 1 + PEER_ID_LEN + 4 + n_shares * SHARE_ID_LEN + SIG_LEN
    if len(data) != expected:
        log.debug("LAN packet: length mismatch (got %d expected %d)",
                  len(data), expected)
        return None

    share_bytes = []
    for _ in range(n_shares):
        share_bytes.append(data[pos:pos + SHARE_ID_LEN]); pos += SHARE_ID_LEN

    body = data[:-SIG_LEN]
    sig  = data[-SIG_LEN:]

    try:
        VerifyKey(peer_id_bytes).verify(body, sig)
    except BadSignatureError:
        log.warning("LAN packet: bad signature from %s",
                    base58.b58encode(peer_id_bytes).decode()[:8])
        return None
    except Exception as exc:
        log.debug("LAN packet: verification error: %s", exc)
        return None

    peer_id   = base58.b58encode(peer_id_bytes).decode()
    share_ids = [base58.b58encode(sb).decode() for sb in share_bytes]
    return peer_id, share_ids, listen_port


# ── asyncio DatagramProtocol ──────────────────────────────────────────────────

class _LanProtocol(asyncio.DatagramProtocol):
    """Asyncio datagram protocol that queues received datagrams."""

    def __init__(self, on_datagram: Callable[[bytes, str], None]):
        self._on_datagram = on_datagram

    def datagram_received(self, data: bytes, addr: tuple):
        host = addr[0]
        self._on_datagram(data, host)

    def error_received(self, exc: Exception):
        log.debug("LAN socket error: %s", exc)

    def connection_lost(self, exc: Exception | None):
        if exc:
            log.debug("LAN socket closed with error: %s", exc)


# ── LanDiscovery ─────────────────────────────────────────────────────────────

class LanDiscovery:
    """
    Combines a multicast announcer and listener for same-subnet peer discovery.

    Parameters
    ──────────
    identity          — daemon Identity (peer_id + signing)
    config            — LanConfig (group, port, interval, interface)
    get_share_ids     — sync callable returning list[str] of active share_ids
    on_peer_seen      — async(peer_id, share_ids, host, port) called on each
                        valid remote announcement; caller performs ACL check
    listen_port       — this peer's libtorrent TCP listen port to advertise
    """

    def __init__(self,
                 identity,
                 config,
                 get_share_ids:  Callable[[], list[str]],
                 on_peer_seen:   Callable[[str, list[str], str, int],
                                          Awaitable[None]],
                 listen_port:    int):
        self._identity      = identity
        self._config        = config
        self._get_share_ids = get_share_ids
        self._on_peer_seen  = on_peer_seen
        self._listen_port   = listen_port

        self._transport: asyncio.BaseTransport | None = None
        self._sender_sock: socket.socket | None = None
        self._announce_task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        loop = asyncio.get_running_loop()

        # Listener socket — bind to the multicast port, join the group.
        listen_sock = self._make_listen_socket()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _LanProtocol(self._on_datagram),
            sock=listen_sock,
        )
        self._transport = transport

        # Sender socket — used only for sending (separate from listener).
        self._sender_sock = self._make_sender_socket()

        # Announce loop.
        self._announce_task = asyncio.create_task(
            self._announce_loop(), name="lan-announce"
        )

        log.info(
            "LAN discovery started group=%s port=%d interval=%ds",
            self._config.multicast_group,
            self._config.multicast_port,
            self._config.announce_interval,
        )

    async def stop(self):
        if self._announce_task:
            self._announce_task.cancel()
            try:
                await self._announce_task
            except asyncio.CancelledError:
                pass
            self._announce_task = None

        if self._transport:
            self._transport.close()
            self._transport = None

        if self._sender_sock:
            try:
                self._sender_sock.close()
            except Exception:
                pass
            self._sender_sock = None

        log.info("LAN discovery stopped")

    # ── Announce loop ─────────────────────────────────────────────────────────

    async def _announce_loop(self):
        """Broadcast a signed packet immediately, then every interval seconds."""
        while True:
            try:
                share_ids = self._get_share_ids()
                if share_ids:
                    packet = pack_packet(self._identity, share_ids,
                                         self._listen_port)
                    dest = (self._config.multicast_group,
                            self._config.multicast_port)
                    await asyncio.get_running_loop().run_in_executor(
                        None, self._sender_sock.sendto, packet, dest
                    )
                    log.debug("LAN announce sent shares=%d bytes=%d",
                              len(share_ids), len(packet))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("LAN announce failed")

            await asyncio.sleep(self._config.announce_interval)

    # ── Datagram handler ──────────────────────────────────────────────────────

    def _on_datagram(self, data: bytes, host: str):
        """Called from the event loop when a datagram arrives."""
        result = unpack_packet(data)
        if result is None:
            return

        peer_id, share_ids, listen_port = result

        # Reject our own packets.
        if peer_id == self._identity.peer_id:
            return

        log.debug("LAN packet from peer=%s shares=%d addr=%s:%d",
                  peer_id[:8], len(share_ids), host, listen_port)

        asyncio.create_task(
            self._on_peer_seen(peer_id, share_ids, host, listen_port),
            name=f"lan-peer-{peer_id[:8]}",
        )

    # ── Socket helpers ────────────────────────────────────────────────────────

    def _make_listen_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass  # not available everywhere

        sock.bind(("", self._config.multicast_port))

        # Join multicast group (optionally on a specific interface).
        iface_ip = self._config.interface or "0.0.0.0"
        mreq = (socket.inet_aton(self._config.multicast_group)
                + socket.inet_aton(iface_ip))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setblocking(False)
        return sock

    def _make_sender_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # TTL 32 — stays within the local network.
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 32)
        # Enable loopback so tests on a single host work.
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        if self._config.interface:
            sock.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                socket.inet_aton(self._config.interface),
            )
        return sock
