#!/bin/sh
# peerdup-setup: configure and start the peerdup daemon.
# On subsequent runs, restarts the daemon (via systemd if installed).
set -e

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
CONFIG_FILE="$SCRIPT_DIR/config.toml"

# ── helpers ───────────────────────────────────────────────────────────────────

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m warn\033[0m %s\n' "$*"; }

prompt() {
    # prompt <var_name> <prompt_text> [default]
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

prompt_optional() {
    # prompt_optional <var_name> <prompt_text>
    var="$1"; msg="$2"
    printf '%s (leave blank to skip): ' "$msg"
    read -r value
    eval "$var=\$value"
}

prompt_yn() {
    # prompt_yn <var_name> <prompt_text> <default: y|n>
    # Sets var to "true" or "false"
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
if [ "$HAVE_SYSTEMD" = "1" ] && systemctl --user is-enabled peerdup-daemon >/dev/null 2>&1; then
    info "peerdup-daemon is managed by systemd - restarting..."
    systemctl --user restart peerdup-daemon
    ok "Restarted."
    printf '\n'
    printf 'Status: systemctl --user status peerdup-daemon\n'
    printf 'Logs:   journalctl --user -u peerdup-daemon -f\n'
    exit 0
fi

# ── first-run config ──────────────────────────────────────────────────────────

if [ ! -f "$CONFIG_FILE" ]; then
    printf '\npeerdup daemon setup\n'
    printf '====================\n\n'
    printf 'No config.toml found. Answer a few questions to get started.\n\n'

    prompt PEER_NAME "Name for this machine (e.g. nas, laptop)"
    prompt_optional REGISTRY_ADDRESS "Registry address (host:port)"
    LISTEN_PORT="55000"

    if [ -n "$REGISTRY_ADDRESS" ]; then
        TLS_ENABLED="true"
    else
        TLS_ENABLED="false"
    fi

    printf '\n'
    prompt_yn LAN_ENABLED "Enable LAN multicast discovery" "y"

    printf '\n'
    prompt_optional RELAY_ADDRESS "Relay server address (host:port) for peers behind symmetric NAT"
    if [ -n "$RELAY_ADDRESS" ]; then
        RELAY_ENABLED="true"
        RELAY_TIMEOUT="120"
    else
        RELAY_ENABLED="false"
    fi

    printf '\n'
    prompt LOG_LEVEL "Log level (DEBUG/INFO/WARNING/ERROR)" "INFO"

    if [ "$RELAY_ENABLED" = "true" ]; then
        RELAY_SECTION="
[relay]
enabled      = true
address      = \"$RELAY_ADDRESS\"
pair_timeout = $RELAY_TIMEOUT"
    else
        RELAY_SECTION="
[relay]
enabled = false"
    fi

    if [ -z "$REGISTRY_ADDRESS" ]; then
        REGISTRY_LINE="# address = \"\"  # set this before starting the daemon"
    else
        REGISTRY_LINE="address = \"$REGISTRY_ADDRESS\""
    fi

    cat > "$CONFIG_FILE" << EOF
[daemon]
name        = "$PEER_NAME"
listen_port = $LISTEN_PORT

[registry]
$REGISTRY_LINE
tls     = $TLS_ENABLED

[identity]
name = "$PEER_NAME"

[libtorrent]
listen_interfaces   = "0.0.0.0:$LISTEN_PORT"
upload_rate_limit   = 0
download_rate_limit = 0

[lan]
enabled           = $LAN_ENABLED
announce_interval = 30
multicast_group   = "239.193.0.0"
multicast_port    = 49152
$RELAY_SECTION

[logging]
level = "$LOG_LEVEL"
EOF

    printf '\nConfiguration saved to config.toml\n'

    if [ -z "$REGISTRY_ADDRESS" ]; then
        printf '\n\033[1;31mRegistry not configured - edit config.toml and rerun peerdup-setup when ready.\033[0m\n'
        exit 0
    fi

    printf '\n'
else
    printf 'Using existing config.toml\n\n'
fi

# ── start daemon ──────────────────────────────────────────────────────────────

DAEMON_BIN="$(command -v peerdup-daemon 2>/dev/null || echo "")"
[ -n "$DAEMON_BIN" ] || { printf '\033[1;31merror:\033[0m peerdup-daemon not found - run the installer first\n'; exit 1; }

pkill -f "peerdup-daemon --config" 2>/dev/null && sleep 1 || true

LOG_FILE="${TMPDIR:-/tmp}/peerdup-daemon.log"
"$DAEMON_BIN" --config "$CONFIG_FILE" "$@" >"$LOG_FILE" 2>&1 &
DAEMON_PID=$!

sleep 2

if kill -0 "$DAEMON_PID" 2>/dev/null; then
    ok "Daemon started (pid $DAEMON_PID)."
else
    printf '\033[1;31mDaemon failed to start. Logs:\033[0m\n\n'
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
printf 'This starts peerdup automatically at login and restarts it on failure.\n'
prompt_yn INSTALL_SERVICE "Install systemd user service" "y"

if [ "$INSTALL_SERVICE" = "false" ]; then
    printf 'Skipped. Logs: %s\n' "$LOG_FILE"
    exit 0
fi

# Write the unit file.
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/peerdup-daemon.service"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_FILE" << EOF
[Unit]
Description=peerdup peer daemon
Documentation=https://github.com/theronconrey/peerdup
After=network.target

[Service]
Type=simple
ExecStart=$DAEMON_BIN --config $CONFIG_FILE
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=peerdup-daemon
RuntimeDirectory=peerdup
RuntimeDirectoryMode=0700

[Install]
WantedBy=default.target
EOF

ok "Unit file written to $UNIT_FILE"

# Stop the manually-started daemon before handing off to systemd.
pkill -f "peerdup-daemon --config" 2>/dev/null && sleep 1 || true

systemctl --user daemon-reload
systemctl --user enable --now peerdup-daemon
ok "peerdup-daemon enabled and started via systemd."

# Offer linger so the service starts at boot even before login.
printf '\n'
printf 'Enable auto-start at boot (before login)?\n'
printf 'Recommended for NAS or always-on machines. Optional for laptops.\n'
prompt_yn ENABLE_LINGER "Enable linger for %s" "n"

if [ "$ENABLE_LINGER" = "true" ]; then
    loginctl enable-linger "$USER"
    ok "Linger enabled - peerdup-daemon will start at boot."
fi

printf '\n'
printf 'Status: systemctl --user status peerdup-daemon\n'
printf 'Logs:   journalctl --user -u peerdup-daemon -f\n'
