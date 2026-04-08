"""
Registry server entrypoint.

Usage:
    python -m registry.server [--config config.toml]

Or via the CLI wrapper:
    registry-server --config config.toml
"""

import argparse
import asyncio
import logging
import signal
import sys
from concurrent import futures
from pathlib import Path

import grpc

from registry.db.models import create_tables, make_engine, make_session_factory
from registry.servicer.registry import RegistryServicer
from registry.ttl import ttl_sweep_loop

log = logging.getLogger(__name__)


def load_config(path: str | None) -> dict:
    """Load TOML config or return defaults."""
    defaults = {
        "host":         "0.0.0.0",
        "port":         50051,
        "database_url": "sqlite:///registry.db",
        "tls": {
            "enabled":   False,
            "cert_file": "server.crt",
            "key_file":  "server.key",
            "ca_file":   None,   # Set for mTLS client certificate auth
        },
        "log_level": "INFO",
        "max_workers": 10,
    }

    if path is None:
        return defaults

    try:
        import tomllib  # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # pip install tomli for older Python
        except ImportError:
            log.warning("tomllib not available, using defaults")
            return defaults

    with open(path, "rb") as f:
        user = tomllib.load(f)

    # Shallow merge top-level, deep merge tls section.
    merged = {**defaults, **user}
    merged["tls"] = {**defaults["tls"], **user.get("tls", {})}
    return merged


def build_server_credentials(tls_cfg: dict):
    """Build gRPC SSL credentials from config."""
    cert_file = tls_cfg["cert_file"]
    key_file  = tls_cfg["key_file"]
    ca_file   = tls_cfg.get("ca_file")

    with open(cert_file, "rb") as f:
        cert = f.read()
    with open(key_file, "rb") as f:
        key = f.read()

    root_certs = None
    if ca_file:
        with open(ca_file, "rb") as f:
            root_certs = f.read()

    return grpc.ssl_server_credentials(
        [(key, cert)],
        root_certificates=root_certs,
        require_client_auth=ca_file is not None,
    )


def build_grpc_server(config: dict, session_factory) -> grpc.Server:
    import asyncio
    import registry_pb2_grpc as pb_grpc  # type: ignore

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=config["max_workers"]),
        options=[
            ("grpc.max_send_message_length",    50 * 1024 * 1024),
            ("grpc.max_receive_message_length", 50 * 1024 * 1024),
            ("grpc.keepalive_time_ms",          30_000),
            ("grpc.keepalive_timeout_ms",       10_000),
            ("grpc.keepalive_permit_without_calls", True),
        ],
    )

    servicer = RegistryServicer(session_factory, event_loop=loop)
    pb_grpc.add_RegistryServiceServicer_to_server(servicer, server)

    addr = f"{config['host']}:{config['port']}"

    if config["tls"]["enabled"]:
        creds = build_server_credentials(config["tls"])
        server.add_secure_port(addr, creds)
        log.info("TLS enabled (mTLS=%s)", config["tls"]["ca_file"] is not None)
    else:
        server.add_insecure_port(addr)
        log.warning("TLS disabled — use only on trusted networks or behind a proxy")

    return server


async def serve(config: dict):
    engine          = make_engine(config["database_url"])
    session_factory = make_session_factory(engine)
    create_tables(engine)

    server = build_grpc_server(config, session_factory)
    server.start()

    addr = f"{config['host']}:{config['port']}"
    log.info("Registry listening on %s", addr)

    # Start TTL expiry background task.
    sweep_task = asyncio.create_task(ttl_sweep_loop(session_factory))

    # Graceful shutdown on SIGTERM / SIGINT.
    stop_event = asyncio.Event()

    def _handle_signal():
        log.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal)

    await stop_event.wait()

    log.info("Shutting down...")
    sweep_task.cancel()
    server.stop(grace=5)
    log.info("Done.")


def main():
    parser = argparse.ArgumentParser(description="peerdup registry server")
    parser.add_argument("--config", default=None,
                        help="Path to TOML config file")
    parser.add_argument("--log-level", default=None,
                        help="Override log level (DEBUG, INFO, WARNING, ERROR)")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.log_level:
        config["log_level"] = args.log_level.upper()

    logging.basicConfig(
        level   = config["log_level"],
        format  = "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt = "%Y-%m-%dT%H:%M:%S",
    )

    asyncio.run(serve(config))


if __name__ == "__main__":
    main()
