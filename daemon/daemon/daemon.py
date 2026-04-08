"""
peerdup-daemon entrypoint.

Boots:
  1. Config + logging
  2. Identity (load or generate keypair)
  3. Local state DB
  4. Registry gRPC client (connect + register)
  5. LibtorrentSession
  6. SyncCoordinator
  7. ControlService on Unix socket (for CLI)

Handles SIGTERM/SIGINT gracefully.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import sys
from concurrent import futures
from pathlib import Path

import grpc

from daemon.config import Config, load_config
from daemon.control.servicer import ControlServicer
from daemon.identity import load_or_create
from daemon.registry.client import RegistryClient
from daemon.state.db import StateDB, make_state_db
from daemon.sync.coordinator import SyncCoordinator
from daemon.torrent.session import LibtorrentSession

log = logging.getLogger(__name__)


# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging(level: str):
    """
    Log to systemd journal if available (structured), else stderr.
    Uses python-systemd if installed; falls back gracefully.
    """
    numeric = getattr(logging, level.upper(), logging.INFO)
    fmt     = "%(levelname)s %(name)s: %(message)s"

    try:
        from systemd.journal import JournalHandler  # type: ignore
        handler = JournalHandler(SYSLOG_IDENTIFIER="peerdup-daemon")
        handler.setFormatter(logging.Formatter(fmt))
        logging.root.addHandler(handler)
    except ImportError:
        # No systemd bindings — fall back to stderr.
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s " + fmt, datefmt="%Y-%m-%dT%H:%M:%S"
        ))
        logging.root.addHandler(handler)

    logging.root.setLevel(numeric)


# ── Control socket server ─────────────────────────────────────────────────────

def build_control_server(socket_path: str, coordinator: SyncCoordinator) -> grpc.Server:
    from daemon import control_pb2_grpc  # type: ignore

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4),
        options=[("grpc.so_reuseport", 0)],
    )
    servicer = ControlServicer(coordinator)
    control_pb2_grpc.add_ControlServiceServicer_to_server(servicer, server)

    # Unix domain socket — no TLS, access controlled by filesystem permissions.
    socket_dir = os.path.dirname(socket_path)
    os.makedirs(socket_dir, exist_ok=True)

    # Remove stale socket if present.
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass

    server.add_insecure_port(f"unix://{socket_path}")

    # Restrict socket to owner only.
    # The socket file is created by gRPC; we chmod after start.
    return server


# ── Main ──────────────────────────────────────────────────────────────────────

async def run(config: Config):
    log.info("peerdup-daemon starting peer_name=%s", config.identity.name)

    # Identity
    identity = load_or_create(config.identity.key_file)
    log.info("Identity loaded peer_id=%s", identity.peer_id)

    # State DB
    sf       = make_state_db(config.daemon.data_dir)
    state_db = StateDB(sf)

    # Registry client
    registry = RegistryClient(
        address   = config.registry.address,
        tls       = config.registry.tls,
        ca_file   = config.registry.ca_file,
        cert_file = config.registry.cert_file,
        key_file  = config.registry.key_file,
    )
    registry.connect()

    # libtorrent session
    lt_session = LibtorrentSession(
        listen_interfaces   = config.libtorrent.listen_interfaces,
        upload_rate_limit   = config.libtorrent.upload_rate_limit,
        download_rate_limit = config.libtorrent.download_rate_limit,
    )

    # Coordinator
    coordinator = SyncCoordinator(
        identity     = identity,
        state_db     = state_db,
        registry     = registry,
        lt_session   = lt_session,
        peer_name    = config.identity.name,
        listen_port  = config.daemon.listen_port,
        data_dir     = config.daemon.data_dir,
        relay_config = config.relay,
    )

    # Boot coordinator (registers with registry, resumes shares, starts LAN)
    loop = asyncio.get_running_loop()
    await coordinator.start(loop, lan_config=config.lan)

    # Control server on Unix socket
    control_server = build_control_server(config.daemon.socket_path, coordinator)
    control_server.start()

    # Chmod socket after creation.
    try:
        os.chmod(config.daemon.socket_path, 0o600)
    except Exception:
        pass

    log.info("Control socket ready at %s", config.daemon.socket_path)

    # Graceful shutdown
    stop_event = asyncio.Event()

    def _signal_handler():
        log.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    await stop_event.wait()

    log.info("Shutting down...")
    await coordinator.stop()
    control_server.stop(grace=3)
    registry.close()
    log.info("peerdup-daemon stopped")


def main():
    parser = argparse.ArgumentParser(description="peerdup peer daemon")
    parser.add_argument("--config",    default=None, help="Path to TOML config")
    parser.add_argument("--log-level", default=None, help="DEBUG|INFO|WARNING|ERROR")
    parser.add_argument("--name",      default=None, help="Override peer name")
    parser.add_argument("--registry",  default=None, help="Override registry address")
    parser.add_argument("--socket",    default=None, help="Override control socket path")
    args = parser.parse_args()

    overrides = {}
    if args.log_level: overrides["logging.level"]     = args.log_level.upper()
    if args.name:       overrides["identity.name"]     = args.name
    if args.registry:   overrides["registry.address"]  = args.registry
    if args.socket:     overrides["daemon.socket_path"] = args.socket

    config = load_config(args.config, overrides or None)
    setup_logging(config.logging.level)

    asyncio.run(run(config))


if __name__ == "__main__":
    main()
