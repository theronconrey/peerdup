"""
Authentication helpers.

- Ed25519 signature verification via PyNaCl
- Bearer token generation and hashing
- gRPC metadata extraction
"""

import hashlib
import hmac
import os
import secrets
import struct

import base58
import grpc
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey


# ── Token management ──────────────────────────────────────────────────────────

def generate_token() -> str:
    """Generate a cryptographically random bearer token."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 hash of a token for storage. Not bcrypt — tokens are random
    enough (256-bit entropy) that a fast hash is acceptable here."""
    return hashlib.sha256(token.encode()).hexdigest()


def verify_token(token: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), stored_hash)


def extract_bearer_token(context: grpc.ServicerContext) -> str | None:
    """Pull the bearer token from gRPC call metadata."""
    metadata = dict(context.invocation_metadata())
    auth = metadata.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:]
    return None


# ── Ed25519 signature verification ───────────────────────────────────────────

def decode_peer_id(peer_id: str) -> bytes:
    """Base58-decode a peer_id / share_id to raw bytes."""
    return base58.b58decode(peer_id)


def verify_register_peer(peer_id: str, name: str, signature: bytes) -> bool:
    """
    Verify registration signature.
    message = peer_id_bytes || utf8(name)
    """
    try:
        key = VerifyKey(decode_peer_id(peer_id))
        message = decode_peer_id(peer_id) + name.encode("utf-8")
        key.verify(message, signature)
        return True
    except (BadSignatureError, Exception):
        return False


def verify_create_share(peer_id: str, share_id: str, name: str,
                        signature: bytes) -> bool:
    """
    Verify share creation signature. Signed with peer key (not share key),
    proving the peer authorised this share creation.
    message = share_id_bytes || utf8(name)
    """
    try:
        key = VerifyKey(decode_peer_id(peer_id))
        message = decode_peer_id(share_id) + name.encode("utf-8")
        key.verify(message, signature)
        return True
    except (BadSignatureError, Exception):
        return False


def canonical_addrs(addrs) -> bytes:
    """
    Deterministic serialisation of a list of PeerAddress proto messages.

    Each address serialised as:  "host:port:is_lan\x00"
    List sorted lexicographically before joining.
    Null-byte separator prevents length-extension confusion.
    """
    parts = []
    for addr in addrs:
        parts.append(f"{addr.host}:{addr.port}:{int(addr.is_lan)}")
    parts.sort()
    return "\x00".join(parts).encode("utf-8")


def verify_announce(peer_id: str, share_id: str, internal_addrs,
                    ttl_seconds: int, signature: bytes) -> bool:
    """
    Verify announce signature.
    message = share_id_bytes || peer_id_bytes || canonical_addrs || ttl_uint32_be
    """
    try:
        key = VerifyKey(decode_peer_id(peer_id))
        message = (
            decode_peer_id(share_id)
            + decode_peer_id(peer_id)
            + canonical_addrs(internal_addrs)
            + struct.pack(">I", ttl_seconds)
        )
        key.verify(message, signature)
        return True
    except (BadSignatureError, Exception):
        return False
