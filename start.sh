#!/bin/sh
set -e

ENV_FILE="$(dirname "$0")/.env"

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

if [ ! -f "$ENV_FILE" ]; then
    printf '\npeerdup first-run setup\n'
    printf '=======================\n\n'
    printf 'No .env file found. Answer a few questions to get started.\n\n'

    prompt DOMAIN      "Registry domain (e.g. registry.example.com)"
    prompt ACME_EMAIL  "Email for Let's Encrypt notifications"

    printf '\nOptional - press Enter to keep defaults.\n\n'

    prompt LOG_LEVEL       "Log level (DEBUG/INFO/WARNING/ERROR)" "INFO"
    prompt SESSION_TIMEOUT "Relay session timeout in seconds"     "300"
    prompt MAX_WAITING     "Relay max concurrent waiting sessions" "1000"

    cat > "$ENV_FILE" << EOF
DOMAIN=$DOMAIN
ACME_EMAIL=$ACME_EMAIL
LOG_LEVEL=$LOG_LEVEL
SESSION_TIMEOUT=$SESSION_TIMEOUT
MAX_WAITING=$MAX_WAITING
EOF

    printf '\nConfiguration saved to .env\n\n'
else
    printf 'Using existing .env\n\n'
fi

exec docker compose up -d "$@"
