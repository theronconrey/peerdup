#!/bin/sh
set -e

DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR"

cat > /tmp/config.toml << TOML
host         = "0.0.0.0"
port         = 50051
database_url = "${DATABASE_URL:-sqlite:///$DATA_DIR/registry.db}"
log_level    = "${LOG_LEVEL:-INFO}"
max_workers  = 10

[tls]
enabled = false
TOML

exec peerdup-registry --config /tmp/config.toml
