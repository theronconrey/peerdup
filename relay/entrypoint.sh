#!/bin/sh
set -e

cat > /tmp/config.toml << TOML
[relay]
host            = "0.0.0.0"
port            = 55002
session_timeout = ${SESSION_TIMEOUT:-300}
max_waiting     = ${MAX_WAITING:-1000}

[logging]
level = "${LOG_LEVEL:-INFO}"
TOML

exec peerdup-relay --config /tmp/config.toml
