#!/bin/sh
# peerdup-setup: configure and start the peerdup daemon.
# On subsequent runs, restarts the daemon (via systemd if installed).
set -e

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
CONFIG_FILE="$SCRIPT_DIR/config.toml"

# ── flags ─────────────────────────────────────────────────────────────────────

if [ "${1:-}" = "--uninstall" ]; then
    UNINSTALL_SCRIPT="$SCRIPT_DIR/../uninstall.sh"
    if [ ! -f "$UNINSTALL_SCRIPT" ]; then
        printf '\033[1;31merror:\033[0m uninstall.sh not found at %s\n' "$UNINSTALL_SCRIPT" >&2
        exit 1
    fi
    exec sh "$UNINSTALL_SCRIPT"
fi

UPDATE_MODE=0
if [ "${1:-}" = "--update" ]; then
    UPDATE_MODE=1
fi

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
    # prompt_optional <var_name> <prompt_text> [current_value]
    # With a current value: Enter keeps it, '-' clears it.
    # Without a current value: Enter leaves blank.
    var="$1"; msg="$2"; current="${3:-}"
    if [ -n "$current" ]; then
        printf '%s [%s] (Enter to keep, - to clear): ' "$msg" "$current"
        read -r value
        case "$value" in
            '')  eval "$var=\$current" ;;
            '-') eval "$var=" ;;
            *)   eval "$var=\$value" ;;
        esac
    else
        printf '%s (leave blank to skip): ' "$msg"
        read -r value
        eval "$var=\$value"
    fi
}

prompt_yn() {
    # prompt_yn <var_name> <prompt_text> <default: y|n>
    # Sets var to "true" or "false"
    var="$1"; msg="$2"; default="$3"
    case "$default" in
        [yY]*) hint="Y/n" ;;
        *)     hint="y/N" ;;
    esac
    while true; do
        printf '%s [%s]: ' "$msg" "$hint"
        read -r value
        value="${value:-$default}"
        case "$value" in
            [yY]|[yY][eE][sS]) eval "$var=true";  return ;;
            [nN]|[nN][oO])     eval "$var=false"; return ;;
            *) printf '  please enter y or n\n' ;;
        esac
    done
}

_cfg_get() {
    # _cfg_get <section> <key> <default>
    # Reads a value from $CONFIG_FILE using Python tomllib.
    python3 - "$CONFIG_FILE" "$1" "$2" "$3" << 'PYEOF'
import sys
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        sys.stdout.write(sys.argv[4] + '\n'); sys.exit(0)
_, cfg_file, section, key, default = sys.argv
try:
    with open(cfg_file, 'rb') as f:
        cfg = tomllib.load(f)
    val = cfg.get(section, {}).get(key)
    if val is None:
        val = default
    elif isinstance(val, bool):
        val = 'true' if val else 'false'
    sys.stdout.write(str(val) + '\n')
except Exception:
    sys.stdout.write(default + '\n')
PYEOF
}

# ── systemd detection ─────────────────────────────────────────────────────────

HAVE_SYSTEMD=0
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    HAVE_SYSTEMD=1
fi

# If already managed by systemd and not in update mode, just restart and exit.
if [ "$UPDATE_MODE" = "0" ] && \
   [ "$HAVE_SYSTEMD" = "1" ] && \
   systemctl --user is-enabled peerdup-daemon >/dev/null 2>&1; then
    info "peerdup-daemon is managed by systemd - restarting..."
    systemctl --user restart peerdup-daemon
    ok "Restarted."
    printf '\n'
    printf 'Status: systemctl --user status peerdup-daemon\n'
    printf 'Logs:   journalctl --user -u peerdup-daemon -f\n'
    exit 0
fi

# ── config wizard ─────────────────────────────────────────────────────────────

if [ ! -f "$CONFIG_FILE" ] || [ "$UPDATE_MODE" = "1" ]; then

    if [ "$UPDATE_MODE" = "1" ] && [ -f "$CONFIG_FILE" ]; then
        printf '\npeerdup daemon update\n'
        printf '=====================\n\n'
        printf 'Current settings loaded. Press Enter to keep each value.\n\n'

        _DEF_NAME=$(_cfg_get       "identity"  "name"     "")
        _DEF_REGISTRY=$(_cfg_get   "registry"  "address"  "")
        _DEF_CA=$(_cfg_get         "registry"  "ca_file"  "")
        _DEF_CERT=$(_cfg_get       "registry"  "cert_file" "")
        _DEF_KEY=$(_cfg_get        "registry"  "key_file"  "")
        _DEF_LAN=$(_cfg_get        "lan"       "enabled"  "true")
        _DEF_LAN_IFACE=$(_cfg_get  "lan"       "interface" "")
        _DEF_RELAY_ADDR=$(_cfg_get "relay"     "address"   "")
        _DEF_LOG=$(_cfg_get        "logging"   "level"    "INFO")
    else
        printf '\npeerdup daemon setup\n'
        printf '====================\n\n'
        printf 'No config.toml found. Answer a few questions to get started.\n\n'

        _DEF_NAME=""
        _DEF_REGISTRY=""
        _DEF_CA=""
        _DEF_CERT=""
        _DEF_KEY=""
        _DEF_LAN="true"
        _DEF_LAN_IFACE=""
        _DEF_RELAY_ADDR=""
        _DEF_LOG="INFO"
    fi

    LISTEN_PORT="55000"

    prompt PEER_NAME "Name for this machine (e.g. nas, laptop)" "$_DEF_NAME"
    prompt_optional REGISTRY_ADDRESS "Registry address (host:port)" "$_DEF_REGISTRY"

    if [ -n "$REGISTRY_ADDRESS" ]; then
        TLS_ENABLED="true"

        printf '\n'
        printf 'Optional: mTLS certificate paths\n'
        printf '(Leave blank to skip - only needed if your registry requires client certificates)\n\n'
        prompt_optional CA_FILE   "CA certificate file for verifying registry" "$_DEF_CA"
        prompt_optional CERT_FILE "Client certificate file for mTLS"           "$_DEF_CERT"
        if [ -n "$CERT_FILE" ]; then
            prompt_optional KEY_FILE "Client private key file for mTLS" "$_DEF_KEY"
        else
            KEY_FILE=""
        fi
    else
        TLS_ENABLED="false"
        CA_FILE=""
        CERT_FILE=""
        KEY_FILE=""
    fi

    printf '\n'
    _yn_lan_default="$_DEF_LAN"
    [ -z "$_yn_lan_default" ] && _yn_lan_default="y"
    prompt_yn LAN_ENABLED "Enable LAN multicast discovery" "$_yn_lan_default"

    LAN_INTERFACE=""
    if [ "$LAN_ENABLED" = "true" ]; then
        DETECTED_IFACE=$(python3 -c "
import socket
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(('8.8.8.8', 80))
    print(s.getsockname()[0])
    s.close()
except Exception:
    print('')
" 2>/dev/null)

        printf '\n'
        _iface_default="${_DEF_LAN_IFACE:-$DETECTED_IFACE}"
        if [ -n "$_iface_default" ]; then
            printf 'LAN interface [%s]: ' "$_iface_default"
            read -r iface_input
            LAN_INTERFACE="${iface_input:-$_iface_default}"
        else
            printf 'Could not auto-detect LAN interface.\n'
            printf 'Enter the IP address of the interface to use for multicast discovery\n'
            printf '(run `ip -4 addr show` to list options): '
            read -r LAN_INTERFACE
        fi
    fi

    printf '\n'
    prompt_optional RELAY_ADDRESS "Relay server address (host:port) for peers behind symmetric NAT" "$_DEF_RELAY_ADDR"
    if [ -n "$RELAY_ADDRESS" ]; then
        RELAY_ENABLED="true"
        RELAY_TIMEOUT="120"
    else
        RELAY_ENABLED="false"
    fi

    printf '\n'
    prompt LOG_LEVEL "Log level (DEBUG/INFO/WARNING/ERROR)" "$_DEF_LOG"

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

    REGISTRY_CA_LINE=""
    REGISTRY_CERT_LINE=""
    REGISTRY_KEY_LINE=""
    if [ -n "$CA_FILE" ]; then
        REGISTRY_CA_LINE="ca_file   = \"$CA_FILE\""
    fi
    if [ -n "$CERT_FILE" ]; then
        REGISTRY_CERT_LINE="cert_file = \"$CERT_FILE\""
    fi
    if [ -n "$KEY_FILE" ]; then
        REGISTRY_KEY_LINE="key_file  = \"$KEY_FILE\""
    fi

    cat > "$CONFIG_FILE" << EOF
[daemon]
name        = "$PEER_NAME"
listen_port = $LISTEN_PORT

[registry]
$REGISTRY_LINE
tls     = $TLS_ENABLED
$REGISTRY_CA_LINE
$REGISTRY_CERT_LINE
$REGISTRY_KEY_LINE

[identity]
name = "$PEER_NAME"

[libtorrent]
listen_interfaces   = "0.0.0.0:$LISTEN_PORT"
upload_rate_limit   = 0
download_rate_limit = 0

[lan]
enabled           = $LAN_ENABLED
interface         = "$LAN_INTERFACE"
announce_interval = 30
multicast_group   = "239.193.0.0"
multicast_port    = 49152
$RELAY_SECTION

[logging]
level = "$LOG_LEVEL"
EOF

    printf '\nConfiguration saved to config.toml\n'

    if [ -z "$REGISTRY_ADDRESS" ]; then
        printf '\n\033[1;33m note\033[0m Running in LAN-only mode. To add a registry later, run peerdup-setup --update.\n'
    fi

    # In update mode, restart via systemd if available and exit.
    if [ "$UPDATE_MODE" = "1" ]; then
        printf '\n'
        if [ "$HAVE_SYSTEMD" = "1" ] && systemctl --user is-enabled peerdup-daemon >/dev/null 2>&1; then
            info "Restarting peerdup-daemon..."
            systemctl --user restart peerdup-daemon
            ok "Restarted."
            printf '\n'
            printf 'Status: systemctl --user status peerdup-daemon\n'
            printf 'Logs:   journalctl --user -u peerdup-daemon -f\n'
        else
            pkill -f "peerdup-daemon --config" 2>/dev/null && sleep 1 || true
            DAEMON_BIN="$(command -v peerdup-daemon 2>/dev/null || echo "")"
            [ -n "$DAEMON_BIN" ] || { printf '\033[1;31merror:\033[0m peerdup-daemon not found\n'; exit 1; }
            LOG_FILE="${TMPDIR:-/tmp}/peerdup-daemon.log"
            "$DAEMON_BIN" --config "$CONFIG_FILE" >"$LOG_FILE" 2>&1 &
            sleep 2
            if kill -0 $! 2>/dev/null; then
                ok "Daemon restarted (pid $!)."
            else
                printf '\033[1;31mDaemon failed to start. Logs:\033[0m\n\n'
                cat "$LOG_FILE"
                exit 1
            fi
        fi
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
prompt_yn ENABLE_LINGER "Enable linger for $USER (start at boot without login)" "n"

if [ "$ENABLE_LINGER" = "true" ]; then
    loginctl enable-linger "$USER"
    ok "Linger enabled - peerdup-daemon will start at boot."
fi

printf '\n'
printf 'Status: systemctl --user status peerdup-daemon\n'
printf 'Logs:   journalctl --user -u peerdup-daemon -f\n'
