#!/bin/sh
set -e

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
CONFIG_FILE="$SCRIPT_DIR/config.toml"

prompt() {
    var="$1"
    msg="$2"
    default="$3"

    while true; do
        if [ -n "$default" ]; then
            printf '%s [%s]: ' "$msg" "$default"
        else
            printf '%s: ' "$msg"
        fi
        read -r value
        value="${value:-$default}"
        if [ -n "$value" ]; then
            eval "$var=\$value"
            break
        fi
        printf 'This field is required.\n'
    done
}

prompt_optional() {
    var="$1"
    msg="$2"

    printf '%s (leave blank to skip): ' "$msg"
    read -r value
    eval "$var=\$value"
}

if [ ! -f "$CONFIG_FILE" ]; then
    printf '\npeerdup registry setup\n'
    printf '======================\n\n'
    printf 'No config.toml found. Answer a few questions to get started.\n\n'

    prompt BIND_PORT "Port to listen on" "50051"
    prompt DB_URL    "Database URL" "sqlite:///registry.db"
    prompt LOG_LEVEL "Log level (DEBUG/INFO/WARNING/ERROR)" "INFO"

    cat > "$CONFIG_FILE" << EOF
host         = "0.0.0.0"
port         = $BIND_PORT
database_url = "$DB_URL"
log_level    = "$LOG_LEVEL"
max_workers  = 10

[tls]
enabled = false
EOF

    printf '\nConfiguration saved to config.toml\n\n'
else
    printf 'Using existing config.toml\n\n'
fi

# Kill any existing registry before starting a fresh one.
pkill -f "peerdup-registry --config" 2>/dev/null && sleep 1 || true

LOG_FILE="${TMPDIR:-/tmp}/peerdup-registry.log"
peerdup-registry --config "$CONFIG_FILE" >"$LOG_FILE" 2>&1 &
REGISTRY_PID=$!

sleep 2

if kill -0 "$REGISTRY_PID" 2>/dev/null; then
    printf '\033[1;32mRegistry started successfully (pid %s).\033[0m\n' "$REGISTRY_PID"
    printf 'Logs: %s\n' "$LOG_FILE"
else
    printf '\033[1;31mRegistry failed to start. Logs:\033[0m\n\n'
    cat "$LOG_FILE"
    exit 1
fi
