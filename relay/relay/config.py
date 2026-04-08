"""
peerdup-relay configuration.

Loaded from a TOML file:

    [relay]
    host    = "0.0.0.0"
    port    = 55002
    session_timeout = 300   # seconds to wait for partner before dropping
    max_waiting     = 1000  # max concurrent unpaired sessions

    [logging]
    level = "INFO"
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RelayServerConfig:
    host:            str = "0.0.0.0"
    port:            int = 55002
    session_timeout: int = 300   # seconds
    max_waiting:     int = 1000


@dataclass
class LoggingConfig:
    level: str = "INFO"


@dataclass
class Config:
    relay:   RelayServerConfig = field(default_factory=RelayServerConfig)
    logging: LoggingConfig     = field(default_factory=LoggingConfig)


def load_config(path: str | None = None) -> Config:
    raw: dict = {}

    if path is not None:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore

        with open(path, "rb") as f:
            raw = tomllib.load(f)

    def sub(cls, key):
        return cls(**{k: v for k, v in raw.get(key, {}).items()
                      if k in cls.__dataclass_fields__})

    return Config(
        relay   = sub(RelayServerConfig, "relay"),
        logging = sub(LoggingConfig,     "logging"),
    )
