"""
peerdup relay server.

A rendezvous TCP relay for peers behind symmetric NAT.

Protocol
--------
1. Client connects via TCP.
2. Client sends a framed JSON hello:
       [4-byte big-endian length][JSON body]
   Fields:
       share_id     - base58 Ed25519 share pubkey
       peer_id      - base58 Ed25519 peer pubkey (caller's own identity)
       want_peer_id - base58 Ed25519 pubkey of the peer to connect to
       timestamp    - Unix seconds (must be within TIMESTAMP_TOLERANCE of now)
       sig          - hex Ed25519 signature over:
                        share_id_bytes || peer_id_bytes ||
                        want_peer_id_bytes || timestamp_be64

3. Server verifies the signature (peer_id IS the verify key).
4. Server responds with a framed JSON status:
       {"status": "waiting"}   - held until partner connects
       {"status": "connected"} - partner matched; enter pipe mode immediately
       {"status": "error", "reason": "..."}

5. After "connected" both sides enter raw pipe mode - the server forwards
   bytes transparently between the two TCP connections.

Security
--------
Signature verification proves the caller holds the private key for peer_id.
ACL membership (is this peer allowed in this share?) is NOT checked here -
the downstream libtorrent BitTorrent handshake enforces correctness because
only peers with the right info-hash can complete it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from dataclasses import dataclass

import base58
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

log = logging.getLogger(__name__)

TIMESTAMP_TOLERANCE = 60  # seconds
MAX_HELLO_SIZE      = 4096


@dataclass
class _WaitingSession:
    reader:  asyncio.StreamReader
    writer:  asyncio.StreamWriter
    event:   asyncio.Event
    peer_id: str


class RelayServer:

    def __init__(self, host: str, port: int,
                 session_timeout: int = 300,
                 max_waiting:     int = 1000):
        self._host            = host
        self._port            = port
        self._session_timeout = session_timeout
        self._max_waiting     = max_waiting

        # (share_id, peer_id) -> _WaitingSession
        self._waiting: dict[tuple[str, str], _WaitingSession] = {}
        self._lock = asyncio.Lock()

    async def start(self):
        server = await asyncio.start_server(
            self._handle_client, self._host, self._port,
        )
        addrs = [s.getsockname() for s in server.sockets]
        log.info("Relay listening on %s", addrs)
        async with server:
            await server.serve_forever()

    # ── Connection handler ────────────────────────────────────────────────────

    async def _handle_client(self,
                              reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter):
        addr = writer.get_extra_info("peername")
        pipes_took_over = False
        try:
            hello = await self._read_hello(reader, writer)
            if hello is None:
                return

            share_id     = hello["share_id"]
            peer_id      = hello["peer_id"]
            want_peer_id = hello["want_peer_id"]

            key      = (share_id, peer_id)
            want_key = (share_id, want_peer_id)

            matched_session: _WaitingSession | None = None

            async with self._lock:
                if want_key in self._waiting:
                    # Partner is already waiting - pop and pair.
                    matched_session = self._waiting.pop(want_key)
                elif len(self._waiting) >= self._max_waiting:
                    await _send_frame(writer, {"status": "error",
                                               "reason": "relay at capacity"})
                    return
                else:
                    # Register ourselves as waiting.
                    ev = asyncio.Event()
                    self._waiting[key] = _WaitingSession(
                        reader=reader, writer=writer,
                        event=ev, peer_id=peer_id,
                    )
                    await _send_frame(writer, {"status": "waiting"})

            if matched_session is not None:
                # We are the connector - send "connected" to both and pipe.
                matched_session.event.set()
                await _send_frame(matched_session.writer, {"status": "connected"})
                await _send_frame(writer,                 {"status": "connected"})
                pipes_took_over = True
                log.info("Relay paired share=%s  %s <-> %s",
                         share_id[:8], want_peer_id[:8], peer_id[:8])
                await asyncio.gather(
                    _pipe(reader,                 matched_session.writer),
                    _pipe(matched_session.reader, writer),
                )
            else:
                # We are the waiter - hold until partner arrives or timeout.
                session = self._waiting.get(key)
                if session is None:
                    return  # already cleaned up
                try:
                    await asyncio.wait_for(
                        session.event.wait(),
                        timeout=self._session_timeout,
                    )
                    # Partner set our event and created pipe tasks. Just return
                    # so this handler exits cleanly without closing the writer
                    # (the pipe tasks now own it).
                    pipes_took_over = True
                except asyncio.TimeoutError:
                    async with self._lock:
                        self._waiting.pop(key, None)
                    log.debug("Session timed out waiting for partner: %s %s",
                              share_id[:8], peer_id[:8])

        except asyncio.IncompleteReadError:
            pass
        except Exception:
            log.exception("Error handling relay client %s", addr)
        finally:
            if not pipes_took_over:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

    # ── Hello parsing + verification ─────────────────────────────────────────

    async def _read_hello(self, reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> dict | None:
        try:
            header = await asyncio.wait_for(reader.readexactly(4), timeout=10)
        except asyncio.TimeoutError:
            return None

        length = struct.unpack(">I", header)[0]
        if length > MAX_HELLO_SIZE:
            return None

        try:
            body = await asyncio.wait_for(reader.readexactly(length), timeout=10)
        except asyncio.TimeoutError:
            return None

        try:
            hello = json.loads(body)
            for field in ("share_id", "peer_id", "want_peer_id",
                          "timestamp", "sig"):
                if field not in hello:
                    raise ValueError(f"missing field: {field}")
        except Exception as exc:
            await _send_frame(writer, {"status": "error",
                                       "reason": f"bad hello: {exc}"})
            return None

        # Timestamp freshness check.
        now = int(time.time())
        if abs(now - int(hello["timestamp"])) > TIMESTAMP_TOLERANCE:
            await _send_frame(writer, {"status": "error",
                                       "reason": "stale timestamp"})
            return None

        # Signature verification - peer_id IS the Ed25519 verify key.
        try:
            vk = VerifyKey(base58.b58decode(hello["peer_id"]))
            signed_payload = (
                base58.b58decode(hello["share_id"])
                + base58.b58decode(hello["peer_id"])
                + base58.b58decode(hello["want_peer_id"])
                + int(hello["timestamp"]).to_bytes(8, "big")
            )
            vk.verify(signed_payload, bytes.fromhex(hello["sig"]))
        except (BadSignatureError, Exception) as exc:
            await _send_frame(writer, {"status": "error",
                                       "reason": "bad signature"})
            log.debug("Relay rejected bad signature from %s: %s",
                      hello.get("peer_id", "?")[:8], exc)
            return None

        return hello


# ── Framing helpers ───────────────────────────────────────────────────────────

async def _send_frame(writer: asyncio.StreamWriter, obj: dict):
    body = json.dumps(obj).encode()
    writer.write(struct.pack(">I", len(body)) + body)
    await writer.drain()


async def _pipe(reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter):
    """Forward bytes from reader to writer until EOF or error."""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
