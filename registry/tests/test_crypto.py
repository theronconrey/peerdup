"""
Tests for registry.auth.crypto

These tests run without grpc or generated stubs — just nacl and base58.
Install: pip install pynacl base58
"""

import struct
import pytest

from nacl.signing import SigningKey
import base58

from registry.auth.crypto import (
    canonical_addrs,
    generate_token,
    hash_token,
    verify_announce,
    verify_create_share,
    verify_register_peer,
    verify_token,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_peer():
    """Return (signing_key, peer_id_base58)."""
    sk = SigningKey.generate()
    peer_id = base58.b58encode(bytes(sk.verify_key)).decode()
    return sk, peer_id


class FakeAddr:
    def __init__(self, host, port, is_lan=False):
        self.host   = host
        self.port   = port
        self.is_lan = is_lan


# ── Token tests ───────────────────────────────────────────────────────────────

def test_generate_token_length():
    t = generate_token()
    assert len(t) >= 32


def test_token_round_trip():
    t = generate_token()
    h = hash_token(t)
    assert verify_token(t, h)


def test_wrong_token_rejected():
    t1 = generate_token()
    t2 = generate_token()
    h  = hash_token(t1)
    assert not verify_token(t2, h)


# ── RegisterPeer signature ────────────────────────────────────────────────────

def test_register_peer_valid_signature():
    sk, peer_id = make_peer()
    name        = "test-node"
    peer_id_bytes = base58.b58decode(peer_id)
    message     = peer_id_bytes + name.encode("utf-8")
    sig         = sk.sign(message).signature
    assert verify_register_peer(peer_id, name, sig)


def test_register_peer_wrong_name_rejected():
    sk, peer_id = make_peer()
    name        = "test-node"
    peer_id_bytes = base58.b58decode(peer_id)
    message     = peer_id_bytes + name.encode("utf-8")
    sig         = sk.sign(message).signature
    assert not verify_register_peer(peer_id, "other-name", sig)


def test_register_peer_wrong_key_rejected():
    sk, peer_id = make_peer()
    sk2, _      = make_peer()
    name        = "test-node"
    peer_id_bytes = base58.b58decode(peer_id)
    message     = peer_id_bytes + name.encode("utf-8")
    sig         = sk2.sign(message).signature   # signed with wrong key
    assert not verify_register_peer(peer_id, name, sig)


# ── CreateShare signature ─────────────────────────────────────────────────────

def test_create_share_valid_signature():
    sk, peer_id  = make_peer()
    _, share_id  = make_peer()   # share has its own keypair
    name         = "my-photos"
    share_id_bytes = base58.b58decode(share_id)
    message      = share_id_bytes + name.encode("utf-8")
    sig          = sk.sign(message).signature
    assert verify_create_share(peer_id, share_id, name, sig)


def test_create_share_tampered_name_rejected():
    sk, peer_id  = make_peer()
    _, share_id  = make_peer()
    name         = "my-photos"
    share_id_bytes = base58.b58decode(share_id)
    message      = share_id_bytes + name.encode("utf-8")
    sig          = sk.sign(message).signature
    assert not verify_create_share(peer_id, share_id, "other-name", sig)


# ── Announce signature ────────────────────────────────────────────────────────

def _make_announce_sig(sk, peer_id, share_id, addrs, ttl):
    from registry.auth.crypto import canonical_addrs as ca, decode_peer_id
    message = (
        decode_peer_id(share_id)
        + decode_peer_id(peer_id)
        + ca(addrs)
        + struct.pack(">I", ttl)
    )
    return sk.sign(message).signature


def test_announce_valid_signature():
    sk, peer_id = make_peer()
    _, share_id = make_peer()
    addrs       = [FakeAddr("192.168.1.10", 55000, is_lan=True),
                   FakeAddr("10.0.0.5",     55001, is_lan=True)]
    ttl         = 300
    sig         = _make_announce_sig(sk, peer_id, share_id, addrs, ttl)
    assert verify_announce(peer_id, share_id, addrs, ttl, sig)


def test_announce_addr_order_independent():
    """canonical_addrs sorts, so order of addresses in request doesn't matter."""
    sk, peer_id = make_peer()
    _, share_id = make_peer()
    addrs_a     = [FakeAddr("192.168.1.10", 55000), FakeAddr("10.0.0.5", 55001)]
    addrs_b     = [FakeAddr("10.0.0.5", 55001),     FakeAddr("192.168.1.10", 55000)]
    ttl         = 300
    sig         = _make_announce_sig(sk, peer_id, share_id, addrs_a, ttl)
    # Signature over addrs_a should verify against addrs_b (same canonical form).
    assert verify_announce(peer_id, share_id, addrs_b, ttl, sig)


def test_announce_tampered_addr_rejected():
    sk, peer_id = make_peer()
    _, share_id = make_peer()
    addrs       = [FakeAddr("192.168.1.10", 55000)]
    ttl         = 300
    sig         = _make_announce_sig(sk, peer_id, share_id, addrs, ttl)
    tampered    = [FakeAddr("192.168.1.99", 55000)]   # different host
    assert not verify_announce(peer_id, share_id, tampered, ttl, sig)


def test_announce_tampered_ttl_rejected():
    sk, peer_id = make_peer()
    _, share_id = make_peer()
    addrs       = [FakeAddr("192.168.1.10", 55000)]
    ttl         = 300
    sig         = _make_announce_sig(sk, peer_id, share_id, addrs, ttl)
    assert not verify_announce(peer_id, share_id, addrs, 9999, sig)


# ── canonical_addrs ───────────────────────────────────────────────────────────

def test_canonical_addrs_sorted():
    addrs = [FakeAddr("z.example.com", 1), FakeAddr("a.example.com", 2)]
    result = canonical_addrs(addrs).decode()
    parts = result.split("\x00")
    assert parts == sorted(parts)


def test_canonical_addrs_empty():
    assert canonical_addrs([]) == b""
