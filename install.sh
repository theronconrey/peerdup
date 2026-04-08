#!/bin/sh
# peerdup daemon installer
# Usage: curl -fsSL https://raw.githubusercontent.com/theronconrey/peerdup/main/install.sh | sh
set -e

REPO="https://github.com/theronconrey/peerdup"
INSTALL_DIR="$HOME/.local/share/peerdup"
BIN_DIR="$HOME/.local/bin"

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m warn\033[0m %s\n' "$*"; }
die()   { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# ── prerequisite checks ────────────────────────────────────────────────────────

command -v python3 >/dev/null 2>&1 || die "python3 not found - install Python 3.11+ first"
command -v git     >/dev/null 2>&1 || die "git not found - install git first"

PY_VERSION=$(python3 -c 'import sys; print(sys.version_info[:2] >= (3,11))')
[ "$PY_VERSION" = "True" ] || die "Python 3.11+ required (found $(python3 --version))"

# ── libtorrent ─────────────────────────────────────────────────────────────────

info "Checking for libtorrent..."

if python3 -c 'import libtorrent' 2>/dev/null; then
    ok "libtorrent already available"
else
    info "Installing libtorrent..."

    OS=""
    if [ -f /etc/os-release ]; then
        # shellcheck disable=SC1091
        OS=$(. /etc/os-release && echo "$ID")
    fi

    case "$OS" in
        fedora|rhel|centos|almalinux|rocky)
            sudo dnf install -y rb_libtorrent-python3
            ;;
        ubuntu|debian|linuxmint|pop)
            sudo apt-get update -qq
            sudo apt-get install -y python3-libtorrent
            ;;
        *)
            if command -v brew >/dev/null 2>&1; then
                # macOS or Linuxbrew
                brew install libtorrent-rasterbar 2>/dev/null || true
            fi
            # Fall back to pip (builds from source - slow but portable)
            warn "No known system package for '$OS' - trying pip install libtorrent"
            "${PIP:-pip3}" install --user libtorrent
            ;;
    esac

    python3 -c 'import libtorrent' 2>/dev/null || die "libtorrent install failed - see https://github.com/theronconrey/peerdup for manual steps"
    ok "libtorrent installed"
fi

# ── clone / update repo ────────────────────────────────────────────────────────

info "Installing peerdup to $INSTALL_DIR..."

if [ -d "$INSTALL_DIR/.git" ]; then
    info "Existing install found - updating..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    git clone --depth 1 "$REPO" "$INSTALL_DIR"
fi

# ── install daemon package ─────────────────────────────────────────────────────

PIP=$(command -v pip3 2>/dev/null || command -v pip 2>/dev/null) \
    || die "pip not found - install python3-pip first"

"$PIP" install --user -e "$INSTALL_DIR/daemon/"

# ── peerdup-setup command ──────────────────────────────────────────────────────

mkdir -p "$BIN_DIR"
ln -sf "$INSTALL_DIR/daemon/start.sh" "$BIN_DIR/peerdup-setup"
ok "peerdup-setup linked to $BIN_DIR/peerdup-setup"

# ── PATH reminder ──────────────────────────────────────────────────────────────

if ! echo "$PATH" | grep -q "$BIN_DIR"; then
    warn "$BIN_DIR is not in your PATH"
    warn "Add this to your shell profile (~/.bashrc, ~/.zshrc, etc.) then reopen your terminal:"
    printf '\n    export PATH="$HOME/.local/bin:$PATH"\n\n'
fi

# ── done ───────────────────────────────────────────────────────────────────────

printf '\n'
printf '\033[1;32mpeerdup installed.\033[0m\n\n'
printf 'To configure and start the daemon:\n\n'
printf '    peerdup-setup\n\n'
