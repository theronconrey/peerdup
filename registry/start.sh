#!/bin/sh
# peerdup-registry-setup: configure and start the peerdup registry.
# On subsequent runs, restarts the registry (via systemd if installed).
set -e

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
CONFIG_FILE="$SCRIPT_DIR/config.toml"

# ── helpers ───────────────────────────────────────────────────────────────────

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m warn\033[0m %s\n' "$*"; }

prompt() {
    var="$1"; msg="$2"; default="$3"
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

prompt_yn() {
    var="$1"; msg="$2"; default="$3"
    case "$default" in
        [yY]*) hint="Y/n" ;;
        *)     hint="y/N" ;;
    esac
    printf '%s [%s]: ' "$msg" "$hint"
    read -r value
    value="${value:-$default}"
    case "$value" in
        [yY]|[yY][eE][sS]) eval "$var=true"  ;;
        *)                  eval "$var=false" ;;
    esac
}

# ── systemd detection ─────────────────────────────────────────────────────────

HAVE_SYSTEMD=0
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    HAVE_SYSTEMD=1
fi

# If already managed by systemd, just restart and exit.
if [ "$HAVE_SYSTEMD" = "1" ] && systemctl --user is-enabled peerdup-registry >/dev/null 2>&1; then
    info "peerdup-registry is managed by systemd - restarting..."
    systemctl --user restart peerdup-registry
    ok "Restarted."
    printf '\n'
    printf 'Status: systemctl --user status peerdup-registry\n'
    printf 'Logs:   journalctl --user -u peerdup-registry -f\n'
    exit 0
fi

# ── first-run config ──────────────────────────────────────────────────────────

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

# ── start registry ────────────────────────────────────────────────────────────

REGISTRY_BIN="$(command -v peerdup-registry 2>/dev/null || echo "")"
[ -n "$REGISTRY_BIN" ] || { printf '\033[1;31merror:\033[0m peerdup-registry not found - run the installer first\n'; exit 1; }

pkill -f "peerdup-registry --config" 2>/dev/null && sleep 1 || true

LOG_FILE="${TMPDIR:-/tmp}/peerdup-registry.log"
"$REGISTRY_BIN" --config "$CONFIG_FILE" >"$LOG_FILE" 2>&1 &
REGISTRY_PID=$!

sleep 2

if kill -0 "$REGISTRY_PID" 2>/dev/null; then
    ok "Registry started (pid $REGISTRY_PID)."
else
    printf '\033[1;31mRegistry failed to start. Logs:\033[0m\n\n'
    cat "$LOG_FILE"
    exit 1
fi

# ── offer systemd user service ────────────────────────────────────────────────

if [ "$HAVE_SYSTEMD" = "0" ]; then
    printf '\nLogs: %s\n' "$LOG_FILE"
    exit 0
fi

printf '\n'
printf 'Install as a systemd user service?\n'
printf 'This starts the registry automatically at login and restarts it on failure.\n'
prompt_yn INSTALL_SERVICE "Install systemd user service" "y"

if [ "$INSTALL_SERVICE" = "false" ]; then
    printf 'Skipped. Logs: %s\n' "$LOG_FILE"
    exit 0
fi

# Write the unit file.
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/peerdup-registry.service"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_FILE" << EOF
[Unit]
Description=peerdup registry server
Documentation=https://github.com/theronconrey/peerdup
After=network.target

[Service]
Type=simple
ExecStart=$REGISTRY_BIN --config $CONFIG_FILE
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=peerdup-registry

[Install]
WantedBy=default.target
EOF

ok "Unit file written to $UNIT_FILE"

pkill -f "peerdup-registry --config" 2>/dev/null && sleep 1 || true

systemctl --user daemon-reload
systemctl --user enable --now peerdup-registry
ok "peerdup-registry enabled and started via systemd."

printf '\n'
printf 'Enable auto-start at boot (before login)?\n'
printf 'Recommended for always-on servers. Optional for workstations.\n'
prompt_yn ENABLE_LINGER "Enable linger for $USER" "n"

if [ "$ENABLE_LINGER" = "true" ]; then
    loginctl enable-linger "$USER"
    ok "Linger enabled - peerdup-registry will start at boot."
fi

printf '\n'
printf 'Status: systemctl --user status peerdup-registry\n'
printf 'Logs:   journalctl --user -u peerdup-registry -f\n'
