"""peerdup-relay entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import sys


def setup_logging(level: str):
    numeric = getattr(logging, level.upper(), logging.INFO)
    fmt = "%(levelname)s %(name)s: %(message)s"
    try:
        from systemd.journal import JournalHandler  # type: ignore
        handler = JournalHandler(SYSLOG_IDENTIFIER="peerdup-relay")
        handler.setFormatter(logging.Formatter(fmt))
    except ImportError:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s " + fmt, datefmt="%Y-%m-%dT%H:%M:%S"
        ))
    logging.root.addHandler(handler)
    logging.root.setLevel(numeric)


async def run(config):
    from relay.server import RelayServer
    srv = RelayServer(
        host            = config.relay.host,
        port            = config.relay.port,
        session_timeout = config.relay.session_timeout,
        max_waiting     = config.relay.max_waiting,
    )
    await srv.start()


def main():
    parser = argparse.ArgumentParser(description="peerdup relay server")
    parser.add_argument("--config",    default=None, help="Path to TOML config")
    parser.add_argument("--host",      default=None, help="Override bind host")
    parser.add_argument("--port",      default=None, type=int, help="Override port")
    parser.add_argument("--log-level", default=None, help="DEBUG|INFO|WARNING|ERROR")
    args = parser.parse_args()

    from relay.config import load_config
    config = load_config(args.config)

    if args.host:      config.relay.host  = args.host
    if args.port:      config.relay.port  = args.port
    if args.log_level: config.logging.level = args.log_level.upper()

    setup_logging(config.logging.level)
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
