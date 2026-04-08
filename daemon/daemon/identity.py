"""
Peer identity management.

Generates an Ed25519 keypair on first run and persists it to disk.
The public key (base58-encoded) IS the peer_id used with the registry.

Key file format: raw 32-byte Ed25519 seed, no framing.
Permissions enforced to 0600 — abort if world-readable.
"""

from __future__ import annotations

import logging
import os
import stat
import struct
from pathlib import Path

import base58
from nacl.signing import SigningKey, SignedMessage

log = logging.getLogger(__name__)


class Identity:
    def __init__(self, signing_key: SigningKey):
        self._sk = signing_key

    @property
    def peer_id(self) -> str:
        """Base58-encoded Ed25519 public key."""
        return base58.b58encode(bytes(self._sk.verify_key)).decode()

    @property
    def verify_key_bytes(self) -> bytes:
        return bytes(self._sk.verify_key)

    def sign(self, message: bytes) -> bytes:
        """Return raw 64-byte Ed25519 signature (not the signed message)."""
        return self._sk.sign(message).signature

    # ── Signing helpers matching registry's expected formats ─────────────────

    def sign_register(self, name: str) -> bytes:
        """sign(peer_id_bytes || utf8(name))"""
        msg = base58.b58decode(self.peer_id) + name.encode("utf-8")
        return self.sign(msg)

    def sign_create_share(self, share_id: str, name: str) -> bytes:
        """sign(share_id_bytes || utf8(name))"""
        msg = base58.b58decode(share_id) + name.encode("utf-8")
        return self.sign(msg)

    def sign_relay_hello(self, share_id: str, want_peer_id: str,
                         timestamp: int) -> bytes:
        """
        sign(share_id_bytes || peer_id_bytes || want_peer_id_bytes || timestamp_be64)

        The relay server verifies this using peer_id as the verify key.
        Covering timestamp prevents replay attacks.
        """
        msg = (
            base58.b58decode(share_id)
            + base58.b58decode(self.peer_id)
            + base58.b58decode(want_peer_id)
            + timestamp.to_bytes(8, "big")
        )
        return self.sign(msg)

    def sign_announce(self, share_id: str, addrs: list[dict],
                      ttl_seconds: int) -> bytes:
        """
        sign(share_id_bytes || peer_id_bytes || canonical_addrs || ttl_be32)

        addrs: list of {"host": str, "port": int, "is_lan": bool}
        """
        canonical = _canonical_addrs(addrs)
        msg = (
            base58.b58decode(share_id)
            + base58.b58decode(self.peer_id)
            + canonical
            + struct.pack(">I", ttl_seconds)
        )
        return self.sign(msg)


def _canonical_addrs(addrs: list[dict]) -> bytes:
    parts = [f"{a['host']}:{a['port']}:{int(a.get('is_lan', False))}"
             for a in addrs]
    parts.sort()
    return "\x00".join(parts).encode("utf-8")


# ── Persistence ───────────────────────────────────────────────────────────────

def load_or_create(key_file: str) -> Identity:
    """
    Load an existing identity from key_file, or generate a new one.
    key_file must be 0600 if it exists (abort otherwise).
    """
    path = Path(key_file)

    if path.exists():
        _check_permissions(path)
        seed = path.read_bytes()
        if len(seed) != 32:
            raise ValueError(
                f"Identity key file {key_file} is {len(seed)} bytes; expected 32"
            )
        sk = SigningKey(seed)
        identity = Identity(sk)
        log.info("Loaded identity peer_id=%s", identity.peer_id)
        return identity

    # Generate new keypair.
    path.parent.mkdir(parents=True, exist_ok=True)
    sk   = SigningKey.generate()
    seed = bytes(sk)
    path.write_bytes(seed)
    path.chmod(0o600)

    identity = Identity(sk)
    log.info("Generated new identity peer_id=%s key_file=%s",
             identity.peer_id, key_file)
    return identity


def _check_permissions(path: Path):
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(
            f"Identity key file {path} has unsafe permissions "
            f"({oct(mode)}). Run: chmod 600 {path}"
        )
