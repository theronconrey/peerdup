"""
Daemon configuration.

Loads from a TOML file, with CLI overrides applied on top.
All paths are resolved to absolute at load time.

Example config.toml:
    [daemon]
    name        = "my-nas"
    data_dir    = "/var/lib/peerdup"
    socket_path = "/run/peerdup/control.sock"
    listen_port = 55000

    [registry]
    address  = "registry.example.com:50051"
    tls      = true
    ca_file  = "/etc/peerdup/ca.crt"
    # client cert for mTLS (optional)
    cert_file = "/etc/peerdup/client.crt"
    key_file  = "/etc/peerdup/client.key"

    [identity]
    # Path to Ed25519 private key (generated on first run if absent)
    key_file = "/var/lib/peerdup/identity.key"
    name     = "my-nas"

    [libtorrent]
    listen_interfaces = "0.0.0.0:55000"
    # Upload/download rate limits in bytes/sec (0 = unlimited)
    upload_rate_limit   = 0
    download_rate_limit = 0

    [logging]
    level = "INFO"   # DEBUG | INFO | WARNING | ERROR

    [[shares]]
    share_id   = "AXyz..."
    local_path = "/mnt/nas/photos"
    permission = "rw"

    [[shares]]
    share_id   = "BAbc..."
    local_path = "/mnt/nas/documents"
    permission = "ro"
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Sub-configs ───────────────────────────────────────────────────────────────

def _default_socket_path() -> str:
    """
    Return the default control socket path.

    Prefers $XDG_RUNTIME_DIR/peerdup/control.sock (writable by the current
    user without root, always set on systemd-based Linux).  Falls back to
    /run/peerdup/control.sock for system (root) installs.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "peerdup", "control.sock")
    return "/run/peerdup/control.sock"


def _default_data_dir() -> str:
    """
    Return the default data directory.

    Uses ~/.local/share/peerdup for user installs,
    /var/lib/peerdup for system installs.
    """
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    # Only use the user path if we're not root.
    if os.getuid() != 0:
        return os.path.join(xdg, "peerdup")
    return "/var/lib/peerdup"


@dataclass
class DaemonConfig:
    name:        str  = "peerdup-daemon"
    data_dir:    str  = field(default_factory=_default_data_dir)
    socket_path: str  = field(default_factory=_default_socket_path)
    listen_port: int  = 55000


@dataclass
class RegistryConfig:
    address:   str            = ""   # empty = no registry (LAN-only mode)
    tls:       bool           = False
    ca_file:   Optional[str]  = None
    cert_file: Optional[str]  = None
    key_file:  Optional[str]  = None


def _default_identity_key() -> str:
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    if os.getuid() != 0:
        return os.path.join(xdg, "peerdup", "identity.key")
    return "/var/lib/peerdup/identity.key"


@dataclass
class IdentityConfig:
    key_file: str  = field(default_factory=_default_identity_key)
    name:     str  = "peerdup-node"


@dataclass
class LibtorrentConfig:
    listen_interfaces:   str = "0.0.0.0:55000"
    upload_rate_limit:   int = 0   # bytes/sec, 0 = unlimited
    download_rate_limit: int = 0


@dataclass
class LanConfig:
    enabled:           bool = True
    interface:         str  = ""              # empty = all interfaces (auto)
    announce_interval: int  = 30              # seconds between broadcasts
    multicast_group:   str  = "239.193.0.0"
    multicast_port:    int  = 49152


@dataclass
class RelayConfig:
    enabled:      bool = False
    address:      str  = ""     # "host:port" of the relay server
    pair_timeout: int  = 120    # seconds to wait for partner to connect


@dataclass
class LoggingConfig:
    level: str = "INFO"


@dataclass
class ShareConfig:
    share_id:   str
    local_path: str
    permission: str = "rw"   # rw | ro | encrypted


@dataclass
class Config:
    daemon:      DaemonConfig     = field(default_factory=DaemonConfig)
    registry:    RegistryConfig   = field(default_factory=RegistryConfig)
    identity:    IdentityConfig   = field(default_factory=IdentityConfig)
    libtorrent:  LibtorrentConfig = field(default_factory=LibtorrentConfig)
    lan:         LanConfig        = field(default_factory=LanConfig)
    relay:       RelayConfig      = field(default_factory=RelayConfig)
    logging:     LoggingConfig    = field(default_factory=LoggingConfig)
    shares:      list[ShareConfig] = field(default_factory=list)


# ── Loader ────────────────────────────────────────────────────────────────────

def load_config(path: str | None = None, overrides: dict | None = None) -> Config:
    """
    Load config from TOML file, then apply CLI overrides dict.

    overrides keys use dot-notation: {"daemon.name": "my-node", ...}
    """
    raw: dict = {}

    if path is not None:
        try:
            import tomllib          # Python 3.11+
        except ImportError:
            import tomli as tomllib  # pip install tomli

        with open(path, "rb") as f:
            raw = tomllib.load(f)

    cfg = _from_dict(raw)

    if overrides:
        _apply_overrides(cfg, overrides)

    _resolve_paths(cfg)
    _validate(cfg)
    return cfg


def _from_dict(raw: dict) -> Config:
    def sub(cls, key):
        return cls(**{k: v for k, v in raw.get(key, {}).items()
                      if k in cls.__dataclass_fields__})

    shares = [
        ShareConfig(**{k: v for k, v in s.items()
                       if k in ShareConfig.__dataclass_fields__})
        for s in raw.get("shares", [])
    ]

    return Config(
        daemon     = sub(DaemonConfig,     "daemon"),
        registry   = sub(RegistryConfig,   "registry"),
        identity   = sub(IdentityConfig,   "identity"),
        libtorrent = sub(LibtorrentConfig, "libtorrent"),
        lan        = sub(LanConfig,        "lan"),
        relay      = sub(RelayConfig,      "relay"),
        logging    = sub(LoggingConfig,    "logging"),
        shares     = shares,
    )


def _apply_overrides(cfg: Config, overrides: dict):
    """Apply dot-notation overrides, e.g. {"registry.address": "host:50051"}."""
    section_map = {
        "daemon":     cfg.daemon,
        "registry":   cfg.registry,
        "identity":   cfg.identity,
        "libtorrent": cfg.libtorrent,
        "lan":        cfg.lan,
        "logging":    cfg.logging,
    }
    for key, value in overrides.items():
        if "." in key:
            section, attr = key.split(".", 1)
            obj = section_map.get(section)
            if obj and hasattr(obj, attr):
                setattr(obj, attr, value)
        else:
            if hasattr(cfg, key):
                setattr(cfg, key, value)


def _resolve_paths(cfg: Config):
    """Expand ~ and make paths absolute."""
    def expand(p: str) -> str:
        return str(Path(p).expanduser().resolve())

    cfg.daemon.data_dir       = expand(cfg.daemon.data_dir)
    cfg.daemon.socket_path    = expand(cfg.daemon.socket_path)
    cfg.identity.key_file     = expand(cfg.identity.key_file)

    if cfg.registry.ca_file:
        cfg.registry.ca_file  = expand(cfg.registry.ca_file)
    if cfg.registry.cert_file:
        cfg.registry.cert_file = expand(cfg.registry.cert_file)
    if cfg.registry.key_file:
        cfg.registry.key_file  = expand(cfg.registry.key_file)

    for share in cfg.shares:
        share.local_path = expand(share.local_path)


def _validate(cfg: Config):
    valid_perms = {"rw", "ro", "encrypted"}
    for share in cfg.shares:
        if share.permission not in valid_perms:
            raise ValueError(
                f"Share {share.share_id}: invalid permission '{share.permission}'. "
                f"Must be one of {valid_perms}"
            )
    valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if cfg.logging.level.upper() not in valid_levels:
        raise ValueError(f"Invalid log level: {cfg.logging.level}")
