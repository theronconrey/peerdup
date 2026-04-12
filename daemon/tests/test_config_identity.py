"""
Tests for config loading and identity module.
No gRPC stubs or libtorrent required.
"""

import os
import stat
import struct
import tempfile
from pathlib import Path

import base58
import pytest
from nacl.signing import SigningKey

from daemon.config import Config, ShareConfig, load_config
from daemon.identity import Identity, load_or_create, _canonical_addrs


# ── Config tests ──────────────────────────────────────────────────────────────

def test_load_config_defaults():
    cfg = load_config(path=None)
    assert cfg.daemon.listen_port   == 55000
    assert cfg.registry.tls        == False
    assert cfg.logging.level       == "INFO"


def test_load_config_toml(tmp_path):
    toml = tmp_path / "config.toml"
    toml.write_text("""
[daemon]
name = "test-node"
listen_port = 12345

[registry]
address = "reg.example.com:50051"
tls = true

[[shares]]
share_id   = "ATestShareId"
local_path = "/tmp/test-share"
permission = "ro"
""")
    cfg = load_config(str(toml))
    assert cfg.daemon.name          == "test-node"
    assert cfg.daemon.listen_port   == 12345
    assert cfg.registry.address     == "reg.example.com:50051"
    assert cfg.registry.tls         == True
    assert len(cfg.shares)          == 1
    assert cfg.shares[0].permission == "ro"


def test_load_config_cli_overrides(tmp_path):
    toml = tmp_path / "config.toml"
    toml.write_text('[daemon]\nname = "original"\n')
    cfg = load_config(str(toml), overrides={"daemon.name": "overridden",
                                             "registry.address": "other:9999"})
    assert cfg.daemon.name        == "overridden"
    assert cfg.registry.address   == "other:9999"


def test_invalid_permission_raises(tmp_path):
    toml = tmp_path / "config.toml"
    toml.write_text("""
[[shares]]
share_id   = "ATestShareId"
local_path = "/tmp/test"
permission = "superuser"
""")
    with pytest.raises(ValueError, match="invalid permission"):
        load_config(str(toml))


def test_invalid_log_level_raises(tmp_path):
    toml = tmp_path / "config.toml"
    toml.write_text('[logging]\nlevel = "NOISIEST"\n')
    with pytest.raises(ValueError, match="Invalid log level"):
        load_config(str(toml))


def test_paths_resolved(tmp_path):
    toml = tmp_path / "config.toml"
    toml.write_text(f'[daemon]\ndata_dir = "{tmp_path}/../relative"\n')
    cfg = load_config(str(toml))
    assert not cfg.daemon.data_dir.startswith("..")
    assert cfg.daemon.data_dir == str((tmp_path / ".." / "relative").resolve())


# ── Identity tests ────────────────────────────────────────────────────────────

def test_generate_identity(tmp_path):
    key_file = str(tmp_path / "identity.key")
    identity = load_or_create(key_file)
    assert len(identity.peer_id) > 20
    assert os.path.exists(key_file)
    # Key file should be 600.
    mode = stat.S_IMODE(os.stat(key_file).st_mode)
    assert mode == 0o600


def test_identity_roundtrip(tmp_path):
    key_file = str(tmp_path / "identity.key")
    id1 = load_or_create(key_file)
    id2 = load_or_create(key_file)
    assert id1.peer_id == id2.peer_id


def test_unsafe_permissions_rejected(tmp_path):
    key_file = str(tmp_path / "identity.key")
    SigningKey.generate()
    Path(key_file).write_bytes(bytes(SigningKey.generate()))
    os.chmod(key_file, 0o644)   # world-readable
    with pytest.raises(PermissionError):
        load_or_create(key_file)


def test_sign_register(tmp_path):
    key_file = str(tmp_path / "identity.key")
    identity = load_or_create(key_file)
    sig = identity.sign_register("my-node")
    # Verify signature using nacl directly.
    from nacl.signing import VerifyKey
    vk  = VerifyKey(base58.b58decode(identity.peer_id))
    msg = base58.b58decode(identity.peer_id) + b"my-node"
    vk.verify(msg, sig)   # raises if invalid


def test_sign_announce_covers_addresses(tmp_path):
    key_file = str(tmp_path / "identity.key")
    identity  = load_or_create(key_file)
    sk2       = SigningKey.generate()
    share_id  = base58.b58encode(bytes(sk2.verify_key)).decode()

    addrs = [
        {"host": "192.168.1.10", "port": 55000, "is_lan": True},
        {"host": "10.0.0.5",     "port": 55001, "is_lan": True},
    ]
    ttl = 300
    sig = identity.sign_announce(share_id, addrs, ttl)

    # Verify.
    from nacl.signing import VerifyKey
    vk  = VerifyKey(base58.b58decode(identity.peer_id))
    msg = (
        base58.b58decode(share_id)
        + base58.b58decode(identity.peer_id)
        + _canonical_addrs(addrs)
        + struct.pack(">I", ttl)
    )
    vk.verify(msg, sig)


def test_sign_announce_addr_order_independent(tmp_path):
    """Same signature regardless of address list order."""
    key_file = str(tmp_path / "identity.key")
    identity  = load_or_create(key_file)
    sk2       = SigningKey.generate()
    share_id  = base58.b58encode(bytes(sk2.verify_key)).decode()

    addrs_a = [{"host": "10.0.0.1", "port": 1}, {"host": "10.0.0.2", "port": 2}]
    addrs_b = [{"host": "10.0.0.2", "port": 2}, {"host": "10.0.0.1", "port": 1}]

    sig_a = identity.sign_announce(share_id, addrs_a, 300)
    sig_b = identity.sign_announce(share_id, addrs_b, 300)
    assert sig_a == sig_b
