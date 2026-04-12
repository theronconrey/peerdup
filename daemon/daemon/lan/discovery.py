"""
LAN peer discovery via IPv4 UDP multicast.

Peers broadcast a signed announcement packet every announce_interval seconds.
Listeners verify the Ed25519 signature, then call on_peer_seen for each
(peer_id, share_ids, host, libtorrent_port, name) tuple — letting the
coordinator inject valid peers directly into libtorrent without going through
the registry.

Wire format (version 2, all integers big-endian)
────────────────────────────────────────────────
 Offset  Size  Field
      0     1  version        = 2
      1    32  peer_id_bytes  (raw 32-byte Ed25519 public key)
     33     2  listen_port    (uint16)
     35     2  share_count    (uint16)
     37  32*n  share_id_bytes (share_count × 32-byte Ed25519 public keys)
  37+32n    2  name_len       (uint16, byte length of UTF-8 name)
  39+32n    *  name_bytes     (UTF-8 encoded peer name, name_len bytes)
  end-64   64  signature      (Ed25519, covers all preceding bytes)

Signature message = everything before the trailing 64 signature bytes.
The signature is verified with the public key embedded as peer_id_bytes,
proving the sender holds the private key whose public key IS their peer_id.

Version 1 packets (no name field) are still accepted; name defaults to "".

Minimum valid packet v2 (zero shares, empty name): 103 bytes.
Maximum practical packet (100 shares, 64-byte name): 3367 bytes — well
within UDP limits.
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

VERSION      = 2
PEER_ID_LEN  = 32   # raw Ed25519 pubkey
SHARE_ID_LEN = 32
SIG_LEN      = 64
_MIN_PACKET_V1 = 1 + PEER_ID_LEN + 2 + 2 + SIG_LEN        # 101 bytes
_MIN_PACKET_V2 = 1 + PEER_ID_LEN + 2 + 2 + 2 + SIG_LEN    # 103 bytes
_MIN_PACKET    = _MIN_PACKET_V1


def detect_multicast_interface() -> str:
    """
    Return the local IP that would route to the default gateway - the right
    interface for LAN multicast on most machines.

    Uses a non-sending UDP connect to let the kernel pick the outbound
    interface, then reads back the source IP. Returns "" on any failure.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return ""


# ── Packet codec ─────────────────────────────────────────────────────────────

def pack_packet(identity, share_ids: list[str], listen_port: int,
                peer_name: str = "") -> bytes:
    """
    Encode and sign an announcement packet (version 2).

    identity    — daemon.identity.Identity (provides peer_id + sign())
    share_ids   — base58-encoded share IDs to announce
    listen_port — libtorrent TCP listen port
    peer_name   — human-readable machine name (included in v2 packets)
    """
    peer_id_bytes = base58.b58decode(identity.peer_id)
    share_bytes   = [base58.b58decode(sid) for sid in share_ids]
    n             = len(share_bytes)
    name_bytes    = peer_name.encode("utf-8")[:255]   # cap at 255 bytes

    body  = struct.pack("!B", VERSION)
    body += peer_id_bytes
    body += struct.pack("!HH", listen_port, n)
    for sb in share_bytes:
        body += sb
    body += struct.pack("!H", len(name_bytes))
    body += name_bytes

    sig = identity.sign(body)
    return body + sig


def unpack_packet(data: bytes) -> tuple[str, list[str], int, str] | None:
    """
    Decode and verify an announcement packet.

    Returns (peer_id, share_ids, listen_port, name) on success, None on error.
    Accepts both version 1 (no name) and version 2 (with name) packets.
    Does NOT check whether the peer_id is our own — callers must do that.
    """
    if len(data) < _MIN_PACKET:
        return None

    pos = 0
    version = data[pos]; pos += 1
    if version not in (1, 2):
        log.debug("LAN packet: unknown version %d", version)
        return None

    peer_id_bytes = data[pos:pos + PEER_ID_LEN]; pos += PEER_ID_LEN
    if len(peer_id_bytes) < PEER_ID_LEN:
        return None

    if pos + 4 > len(data):
        return None
    listen_port, n_shares = struct.unpack_from("!HH", data, pos); pos += 4

    share_bytes = []
    for _ in range(n_shares):
        if pos + SHARE_ID_LEN > len(data):
            return None
        share_bytes.append(data[pos:pos + SHARE_ID_LEN]); pos += SHARE_ID_LEN

    # Version 2: name field before signature.
    name = ""
    if version == 2:
        if pos + 2 > len(data):
            return None
        name_len, = struct.unpack_from("!H", data, pos); pos += 2
        if pos + name_len > len(data):
            return None
        try:
            name = data[pos:pos + name_len].decode("utf-8"); pos += name_len
        except UnicodeDecodeError:
            pos += name_len   # skip malformed name

    # Everything before the trailing signature is the signed body.
    if len(data) - pos != SIG_LEN:
        log.debug("LAN packet: length mismatch after fields (remaining=%d)",
                  len(data) - pos)
        return None

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
    return peer_id, share_ids, listen_port, name


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
    on_peer_seen      — async(peer_id, share_ids, host, port, name) called on
                        each valid remote announcement; caller performs ACL check
    listen_port       — this peer's libtorrent TCP listen port to advertise
    peer_name         — human-readable name included in outgoing v2 packets
    """

    def __init__(self,
                 identity,
                 config,
                 get_share_ids:  Callable[[], list[str]],
                 on_peer_seen:   Callable[[str, list[str], str, int, str],
                                          Awaitable[None]],
                 listen_port:    int,
                 peer_name:      str = ""):
        self._identity      = identity
        self._config        = config
        self._get_share_ids = get_share_ids
        self._on_peer_seen  = on_peer_seen
        self._listen_port   = listen_port
        self._peer_name     = peer_name

        self._transport: asyncio.BaseTransport | None = None
        self._sender_sock: socket.socket | None = None
        self._announce_task: asyncio.Task | None = None
        self._iface: str = ""   # resolved at start()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        loop = asyncio.get_running_loop()

        # Resolve interface: explicit config wins; otherwise auto-detect.
        if self._config.interface:
            self._iface = self._config.interface
            log.info("LAN discovery using configured interface %s", self._iface)
        else:
            self._iface = detect_multicast_interface()
            if self._iface:
                log.info(
                    "LAN discovery auto-detected interface %s "
                    "(set [lan] interface in config to override)",
                    self._iface,
                )
            else:
                log.warning(
                    "LAN discovery could not detect interface - "
                    "falling back to 0.0.0.0 (multicast may not work on "
                    "multi-homed hosts; set [lan] interface in config)"
                )

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
        """
        Broadcast a signed packet immediately, then every interval seconds.

        Sends to both the multicast group AND the subnet broadcast address so
        that peers on WiFi can reach peers on wired ethernet (and vice versa)
        even when the AP does not forward multicast across the wired/wireless
        boundary.
        """
        loop = asyncio.get_running_loop()
        port = self._config.multicast_port
        dests = [
            (self._config.multicast_group, port),
            ("255.255.255.255", port),
        ]
        while True:
            try:
                share_ids = self._get_share_ids()
                if share_ids:
                    packet = pack_packet(self._identity, share_ids,
                                         self._listen_port, self._peer_name)
                    for dest in dests:
                        try:
                            await loop.run_in_executor(
                                None, self._sender_sock.sendto, packet, dest
                            )
                        except Exception:
                            log.debug("LAN announce to %s failed", dest[0])
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
            log.debug("LAN datagram from %s failed to unpack (len=%d)", host, len(data))
            return

        peer_id, share_ids, listen_port, name = result

        # Reject our own packets.
        if peer_id == self._identity.peer_id:
            return

        log.debug("LAN packet from peer=%s name=%r shares=%d addr=%s:%d",
                  peer_id[:8], name, len(share_ids), host, listen_port)

        asyncio.create_task(
            self._on_peer_seen(peer_id, share_ids, host, listen_port, name),
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

        # Join multicast group on the resolved interface.
        iface_ip = self._iface or "0.0.0.0"
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
        # Enable broadcast so we can send to 255.255.255.255 as a fallback
        # for networks where the AP doesn't forward multicast across the
        # wired/wireless boundary.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if self._iface:
            sock.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                socket.inet_aton(self._iface),
            )
        return sock
