"""
Unit tests for daemon/lan/discovery.py.

Covers:
  - pack/unpack round-trip with one share, multiple shares, zero shares
  - signature verification: tampered body, tampered signature, wrong key
  - packet validation: version mismatch, truncated packet, wrong length
  - self-packet rejection inside LanDiscovery._on_datagram
  - on_peer_seen not called for own peer_id
"""

import asyncio
import struct

import base58
import pytest
from nacl.signing import SigningKey

from daemon.identity import Identity
from daemon.lan.discovery import (
    INFO_HASH_LEN,
    SEQ_LEN,
    SIG_LEN,
    VERSION,
    LanDiscovery,
    pack_packet,
    unpack_packet,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_identity() -> Identity:
    return Identity(SigningKey.generate())


def _make_share_id() -> str:
    return base58.b58encode(bytes(SigningKey.generate().verify_key)).decode()


def _make_share(info_hash_hex: str = "", seq: int = 0) -> tuple:
    """Return a (share_id, info_hash_hex, seq) triple."""
    return (_make_share_id(), info_hash_hex, seq)


def _make_lan_config(**overrides):
    from daemon.config import LanConfig
    cfg = LanConfig()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


# ── Pack / unpack round-trip ──────────────────────────────────────────────────

class TestPackUnpack:
    def test_single_share_round_trip(self):
        identity = _make_identity()
        shares   = [_make_share()]
        port     = 55001

        data   = pack_packet(identity, shares, port)
        result = unpack_packet(data)

        assert result is not None
        peer_id_out, shares_out, port_out, name_out = result
        assert peer_id_out == identity.peer_id
        assert port_out    == port
        assert len(shares_out) == 1
        assert shares_out[0][0] == shares[0][0]  # share_id matches

    def test_multiple_shares_round_trip(self):
        identity = _make_identity()
        shares   = [_make_share() for _ in range(5)]
        port     = 49152

        data   = pack_packet(identity, shares, port)
        result = unpack_packet(data)

        assert result is not None
        _, shares_out, _, _ = result
        assert len(shares_out) == 5
        assert [s[0] for s in shares_out] == [s[0] for s in shares]

    def test_zero_shares_round_trip(self):
        identity = _make_identity()

        data   = pack_packet(identity, [], 55000)
        result = unpack_packet(data)

        assert result is not None
        peer_id_out, shares_out, port_out, name_out = result
        assert peer_id_out == identity.peer_id
        assert shares_out  == []
        assert port_out    == 55000

    def test_info_hash_and_seq_round_trip(self):
        identity = _make_identity()
        ih_hex   = "a" * 40   # 20 bytes as hex
        shares   = [(_make_share_id(), ih_hex, 42)]

        data   = pack_packet(identity, shares, 55000)
        result = unpack_packet(data)

        assert result is not None
        _, shares_out, _, _ = result
        assert shares_out[0][1] == ih_hex
        assert shares_out[0][2] == 42

    def test_empty_info_hash_round_trip(self):
        """Empty info_hash_hex is encoded as 20 zero bytes and decoded back as ''."""
        identity = _make_identity()
        shares   = [(_make_share_id(), "", 0)]

        data   = pack_packet(identity, shares, 55000)
        result = unpack_packet(data)

        assert result is not None
        _, shares_out, _, _ = result
        assert shares_out[0][1] == ""

    def test_name_round_trip(self):
        identity = _make_identity()

        data   = pack_packet(identity, [], 55000, peer_name="mymachine")
        result = unpack_packet(data)

        assert result is not None
        _, _, _, name_out = result
        assert name_out == "mymachine"

    def test_port_boundary_values(self):
        identity = _make_identity()

        for port in (1, 1024, 49152, 65535):
            data   = pack_packet(identity, [], port)
            result = unpack_packet(data)
            assert result is not None
            assert result[2] == port

    def test_packet_length_minimum(self):
        """Zero-share v4 packet: 1+32+2+2 (header) + 2+0 (name) + 64 (sig) = 103 bytes."""
        identity = _make_identity()
        data     = pack_packet(identity, [], 55000)
        expected = 1 + 32 + 2 + 2 + 2 + 0 + SIG_LEN   # 103
        assert len(data) == expected

    def test_packet_length_with_shares(self):
        """Each share adds 32 (share_id) + 20 (info_hash) + 4 (seq) = 56 bytes."""
        identity = _make_identity()
        shares   = [_make_share() for _ in range(3)]
        data     = pack_packet(identity, shares, 55000)
        per_share = 32 + INFO_HASH_LEN + SEQ_LEN   # 56
        expected  = 103 + 3 * per_share
        assert len(data) == expected

    def test_version_byte(self):
        identity = _make_identity()
        data     = pack_packet(identity, [], 55000)
        assert data[0] == VERSION


# ── Signature verification ────────────────────────────────────────────────────

class TestSignatureVerification:
    def test_tampered_body_rejected(self):
        identity = _make_identity()
        shares   = [_make_share()]
        data     = bytearray(pack_packet(identity, shares, 55000))

        # Flip a byte in the peer_id section.
        data[5] ^= 0xFF

        assert unpack_packet(bytes(data)) is None

    def test_tampered_port_rejected(self):
        identity = _make_identity()
        data     = bytearray(pack_packet(identity, [], 55000))

        # Flip a byte in the listen_port field (offset 33).
        data[33] ^= 0x01

        assert unpack_packet(bytes(data)) is None

    def test_tampered_signature_rejected(self):
        identity = _make_identity()
        shares   = [_make_share()]
        data     = bytearray(pack_packet(identity, shares, 55000))

        # Corrupt the last byte of the signature.
        data[-1] ^= 0xFF

        assert unpack_packet(bytes(data)) is None

    def test_wrong_key_rejected(self):
        """Packet signed by identity_a but claiming to be identity_b."""
        id_a = _make_identity()
        id_b = _make_identity()

        # Pack as id_a, then splice id_b's peer_id into the payload.
        data = bytearray(pack_packet(id_a, [], 55000))
        id_b_bytes = base58.b58decode(id_b.peer_id)
        data[1:1 + 32] = id_b_bytes   # replace peer_id with id_b's

        assert unpack_packet(bytes(data)) is None

    def test_different_share_tampered_rejected(self):
        identity = _make_identity()
        shares   = [_make_share()]
        data     = bytearray(pack_packet(identity, shares, 55000))

        # Flip a byte in the share_id section (offset 37 = after version+peer_id+port+n).
        data[37] ^= 0x01

        assert unpack_packet(bytes(data)) is None


# ── Packet validation ─────────────────────────────────────────────────────────

class TestPacketValidation:
    def test_truncated_packet_rejected(self):
        identity = _make_identity()
        data     = pack_packet(identity, [], 55000)

        for truncated_len in (0, 1, 50, 100):
            assert unpack_packet(data[:truncated_len]) is None

    def test_wrong_version_rejected(self):
        identity = _make_identity()
        data     = bytearray(pack_packet(identity, [], 55000))
        data[0]  = 99   # unknown version

        assert unpack_packet(bytes(data)) is None

    def test_extra_trailing_bytes_rejected(self):
        identity = _make_identity()
        data     = pack_packet(identity, [], 55000) + b"\x00"

        assert unpack_packet(data) is None

    def test_empty_bytes_rejected(self):
        assert unpack_packet(b"") is None

    def test_all_zeros_rejected(self):
        assert unpack_packet(b"\x00" * 200) is None

    def test_share_count_overflow_rejected(self):
        """Packet claiming n_shares but not having the bytes for them."""
        identity = _make_identity()
        # Build a valid zero-share packet then manually set n_shares = 5.
        data = bytearray(pack_packet(identity, [], 55000))
        # n_shares is at offset 35: version(1) + peer_id(32) + port(2) = 35
        struct.pack_into("!H", data, 35, 5)
        assert unpack_packet(bytes(data)) is None


# ── Self-packet rejection ─────────────────────────────────────────────────────

class TestSelfRejection:
    def test_own_packet_not_forwarded(self):
        """LanDiscovery must not call on_peer_seen when peer_id matches self."""
        identity = _make_identity()
        shares   = [_make_share()]
        seen     = []

        async def on_peer_seen(peer_id, sids, host, port, name):
            seen.append(peer_id)

        cfg = _make_lan_config(enabled=True)
        ld  = LanDiscovery(
            identity      = identity,
            config        = cfg,
            get_share_ids = lambda: shares,
            on_peer_seen  = on_peer_seen,
            listen_port   = 55000,
        )

        packet = pack_packet(identity, shares, 55000)
        ld._on_datagram(packet, "127.0.0.1")

        # Run event loop briefly to let any created tasks run.
        async def _run():
            await asyncio.sleep(0)

        asyncio.run(_run())
        assert seen == [], "own packet must not trigger on_peer_seen"

    def test_remote_packet_forwarded(self):
        """LanDiscovery DOES call on_peer_seen for a different peer."""
        id_local  = _make_identity()
        id_remote = _make_identity()
        shares    = [_make_share()]
        seen      = []

        async def on_peer_seen(peer_id, sids, host, port, name):
            seen.append((peer_id, host, port))

        cfg = _make_lan_config(enabled=True)
        ld  = LanDiscovery(
            identity      = id_local,
            config        = cfg,
            get_share_ids = lambda: shares,
            on_peer_seen  = on_peer_seen,
            listen_port   = 55000,
        )

        packet = pack_packet(id_remote, shares, 55001)

        async def _run():
            # _on_datagram creates a task; needs a running loop.
            ld._on_datagram(packet, "192.168.1.42")
            await asyncio.sleep(0)   # let the created task run

        asyncio.run(_run())
        assert len(seen) == 1
        peer_id_seen, host_seen, port_seen = seen[0]
        assert peer_id_seen == id_remote.peer_id
        assert host_seen    == "192.168.1.42"
        assert port_seen    == 55001

    def test_invalid_packet_not_forwarded(self):
        """Garbled data must never trigger on_peer_seen."""
        identity = _make_identity()
        seen     = []

        async def on_peer_seen(peer_id, sids, host, port, name):
            seen.append(peer_id)

        cfg = _make_lan_config(enabled=True)
        ld  = LanDiscovery(
            identity      = identity,
            config        = cfg,
            get_share_ids = lambda: [],
            on_peer_seen  = on_peer_seen,
            listen_port   = 55000,
        )

        ld._on_datagram(b"garbage", "127.0.0.1")

        async def _run():
            await asyncio.sleep(0)

        asyncio.run(_run())
        assert seen == []
