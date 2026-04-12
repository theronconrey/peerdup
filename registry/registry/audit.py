"""
Structured audit logging for the peerdup registry.

Each authenticated action writes one JSON line to the audit log.
The log is append-only and rotated by size (configurable).

Log format (one JSON object per line):
  {
    "ts":        "2026-04-12T15:04:05.123456+00:00",  # ISO 8601 UTC
    "action":    "announce",                           # RPC name snake_case
    "peer_id":   "AbCd1234...",                        # authenticated peer
    "share_id":  "XyZw5678...",                        # if applicable, else ""
    "remote_ip": "1.2.3.4",                            # client IP (no port)
    "outcome":   "ok"                                  # "ok" | "denied" | "error"
  }
"""

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path


class AuditLogger:
    """
    Thread-safe structured audit logger. Uses Python's logging module
    (which is thread-safe) with a RotatingFileHandler.

    Args:
        log_file:     Path to the audit log file.
        max_bytes:    Maximum size before rotation (default 10 MB).
        backup_count: Number of rotated files to keep (default 5).
    """

    def __init__(
        self,
        log_file: str,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
    ) -> None:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)

        self._logger = logging.getLogger(f"peerdup.audit.{log_file}")
        self._logger.propagate = False  # Don't leak audit lines to root logger
        self._logger.setLevel(logging.INFO)

        if not self._logger.handlers:
            handler = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes    = max_bytes,
                backupCount = backup_count,
                encoding    = "utf-8",
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)

    def log(
        self,
        action:    str,
        peer_id:   str,
        outcome:   str,                # "ok" | "denied" | "error"
        share_id:  str = "",
        remote_ip: str = "",
    ) -> None:
        """Write one audit record."""
        record = {
            "ts":        datetime.now(timezone.utc).isoformat(),
            "action":    action,
            "peer_id":   peer_id,
            "share_id":  share_id,
            "remote_ip": remote_ip,
            "outcome":   outcome,
        }
        self._logger.info(json.dumps(record, separators=(",", ":")))


class _NoOpAuditLogger:
    """Dropped-in when audit logging is disabled."""
    def log(self, *args, **kwargs) -> None:
        pass


def make_audit_logger(config: dict) -> "AuditLogger | _NoOpAuditLogger":
    """
    Build an AuditLogger from the [audit] config section.
    Returns a no-op logger if disabled.

    Expected config shape:
      {"enabled": True, "log_file": "audit.log",
       "max_bytes": 10485760, "backup_count": 5}
    """
    if not config.get("enabled", False):
        return _NoOpAuditLogger()
    return AuditLogger(
        log_file     = config.get("log_file", "audit.log"),
        max_bytes    = config.get("max_bytes", 10 * 1024 * 1024),
        backup_count = config.get("backup_count", 5),
    )
