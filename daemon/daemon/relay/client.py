"""
Relay client for the peerdup daemon.

Opens a rendezvous session on the relay server and bridges the resulting
TCP tunnel to a local loopback port so libtorrent can use it as a normal
peer endpoint.

Usage
-----
    port = await open_relay_bridge(
        relay_address = "relay.example.com:55002",
        share_id      = share_id,
        identity      = self._identity,
        want_peer_id  = remote_peer_id,
    )
    # port is now a local TCP port; call:
    lt_session.add_peer(share_id, "127.0.0.1", port)

The bridge is fully async:
  - open_relay_bridge() waits for pairing to complete (or times out)
  - After pairing, a background task accepts exactly one libtorrent
    connection on the local port and pipes it through the relay tunnel
  - When either side closes, both sockets are torn down

"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import struct
import time

log = logging.getLogger(__name__)

# How long to wait for the relay partner to connect.
PAIR_TIMEOUT = 120  # seconds


async def open_relay_bridge(relay_address: str,
                             share_id:      str,
                             identity,
                             want_peer_id:  str,
                             pair_timeout:  float = PAIR_TIMEOUT) -> int:
    """
    Connect to the relay, pair with want_peer_id for share_id, and start
    a local TCP bridge.

    Returns the local port number. Raises on any failure (connection refused,
    signature rejected, timeout, etc.).
    """
    host, port_str = relay_address.rsplit(":", 1)
    relay_port = int(port_str)

    # Connect to relay.
    relay_r, relay_w = await asyncio.open_connection(host, relay_port)
    log.debug("Relay connected share=%s want=%s", share_id[:8], want_peer_id[:8])

    try:
        # Send hello.
        timestamp = int(time.time())
        sig = identity.sign_relay_hello(share_id, want_peer_id, timestamp)
        hello = {
            "share_id":     share_id,
            "peer_id":      identity.peer_id,
            "want_peer_id": want_peer_id,
            "timestamp":    timestamp,
            "sig":          sig.hex(),
        }
        await _send_frame(relay_w, hello)

        # Read initial response.
        resp = await asyncio.wait_for(_recv_frame(relay_r), timeout=15)
        status = resp.get("status")

        if status == "error":
            raise RuntimeError(f"Relay rejected hello: {resp.get('reason')}")

        if status == "waiting":
            # Wait for partner to connect on the other side.
            log.debug("Relay: waiting for partner share=%s", share_id[:8])
            resp = await asyncio.wait_for(_recv_frame(relay_r),
                                          timeout=pair_timeout)
            status = resp.get("status")

        if status != "connected":
            raise RuntimeError(f"Unexpected relay status: {status!r}")

        log.info("Relay paired share=%s <-> peer=%s",
                 share_id[:8], want_peer_id[:8])

    except Exception:
        relay_w.close()
        raise

    # Create a local TCP socket for libtorrent to connect to.
    local_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    local_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    local_sock.bind(("127.0.0.1", 0))
    local_sock.listen(1)
    local_sock.setblocking(False)
    local_port = local_sock.getsockname()[1]

    # Background task: accept libtorrent's connection, then bridge.
    asyncio.create_task(
        _accept_and_bridge(local_sock, relay_r, relay_w,
                           share_id, want_peer_id),
        name=f"relay-bridge-{share_id[:8]}-{want_peer_id[:8]}",
    )

    log.debug("Relay bridge ready on 127.0.0.1:%d", local_port)
    return local_port


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _accept_and_bridge(local_sock: socket.socket,
                              relay_r:    asyncio.StreamReader,
                              relay_w:    asyncio.StreamWriter,
                              share_id:   str,
                              peer_id:    str):
    """
    Accept exactly one connection on local_sock (from libtorrent),
    then pipe it bidirectionally through the relay tunnel.
    """
    loop = asyncio.get_running_loop()
    try:
        conn, _ = await asyncio.wait_for(
            loop.sock_accept(local_sock), timeout=30
        )
    except asyncio.TimeoutError:
        log.warning("Relay bridge: libtorrent never connected (share=%s peer=%s)",
                    share_id[:8], peer_id[:8])
        relay_w.close()
        local_sock.close()
        return
    except Exception:
        log.exception("Relay bridge: accept failed")
        relay_w.close()
        local_sock.close()
        return

    local_sock.close()  # stop accepting more connections

    local_r, local_w = await asyncio.open_connection(sock=conn)
    log.debug("Relay bridge active share=%s peer=%s", share_id[:8], peer_id[:8])

    await asyncio.gather(
        _pipe(local_r, relay_w),
        _pipe(relay_r, local_w),
    )
    log.debug("Relay bridge closed share=%s peer=%s", share_id[:8], peer_id[:8])


async def _pipe(reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter):
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


async def _send_frame(writer: asyncio.StreamWriter, obj: dict):
    body = json.dumps(obj).encode()
    writer.write(struct.pack(">I", len(body)) + body)
    await writer.drain()


async def _recv_frame(reader: asyncio.StreamReader) -> dict:
    header = await reader.readexactly(4)
    length = struct.unpack(">I", header)[0]
    body   = await reader.readexactly(length)
    return json.loads(body)
