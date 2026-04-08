#!/bin/sh
set -e

SCRIPT_DIR="$(dirname "$0")"
CONFIG_FILE="$SCRIPT_DIR/config.toml"

prompt() {
    # prompt <var_name> <prompt_text> [default]
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
    # prompt_optional <var_name> <prompt_text> [hint]
    var="$1"
    msg="$2"
    hint="$3"

    if [ -n "$hint" ]; then
        printf '%s (leave blank to skip - %s): ' "$msg" "$hint"
    else
        printf '%s (leave blank to skip): ' "$msg"
    fi
    read -r value
    eval "$var=\$value"
}

prompt_yn() {
    # prompt_yn <var_name> <prompt_text> <default y|n>
    var="$1"
    msg="$2"
    default="$3"

    while true; do
        printf '%s [%s]: ' "$msg" "$default"
        read -r value
        value="${value:-$default}"
        case "$value" in
            y|Y|yes|YES) eval "$var=true";  break ;;
            n|N|no|NO)   eval "$var=false"; break ;;
            *) printf 'Please enter y or n.\n' ;;
        esac
    done
}

if [ ! -f "$CONFIG_FILE" ]; then
    printf '\npeerdup daemon setup\n'
    printf '====================\n\n'
    printf 'No config.toml found. Answer a few questions to get started.\n\n'

    prompt PEER_NAME "Name for this machine (e.g. nas, laptop)"
    prompt_optional REGISTRY_ADDRESS "Registry address (host:port)" "set later in config.toml"
    LISTEN_PORT="55000"

    printf '\n'
    if [ -n "$REGISTRY_ADDRESS" ]; then
        prompt_yn TLS_ENABLED "Use TLS for registry connection" "y"
        if [ "$TLS_ENABLED" = "true" ]; then
            printf 'CA cert file path (leave blank to use system CAs): '
            read -r CA_FILE
        fi
    else
        TLS_ENABLED="true"
        printf '\nRegistry not set - TLS will default to enabled when you add it.\n'
    fi

    printf '\n'
    prompt_yn LAN_ENABLED "Enable LAN multicast discovery" "y"

    printf '\n'
    prompt_optional RELAY_ADDRESS "Relay address (host:port) for when two peers can't reach each other directly" "leave blank to skip"
    if [ -n "$RELAY_ADDRESS" ]; then
        RELAY_ENABLED="true"
        RELAY_TIMEOUT="120"
    else
        RELAY_ENABLED="false"
    fi

    printf '\n'
    prompt LOG_LEVEL "Log level (DEBUG/INFO/WARNING/ERROR)" "INFO"

    # Build ca_file line only if provided
    CA_FILE_LINE=""
    if [ -n "$CA_FILE" ]; then
        CA_FILE_LINE="
ca_file = \"$CA_FILE\""
    fi

    # Build relay section
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
tls     = $TLS_ENABLED$CA_FILE_LINE

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
        printf '\n\033[1;31mRegistry not configured - rerun peerdup-setup when ready.\033[0m\n'
        exit 0
    fi

    printf '\n'
else
    printf 'Using existing config.toml\n\n'
fi

peerdup-daemon --config "$CONFIG_FILE" "$@" &
DAEMON_PID=$!

sleep 2

if kill -0 "$DAEMON_PID" 2>/dev/null; then
    printf '\033[1;32mDaemon started successfully (pid %s).\033[0m\n' "$DAEMON_PID"
else
    printf '\033[1;31mDaemon failed to start. Check logs with:\033[0m\n'
    printf '    journalctl --user -xe | grep peerdup\n'
    exit 1
fi
