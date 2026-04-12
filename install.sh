#!/bin/sh
# peerdup installer
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

# Ensure pip is available - install it if not.
if ! command -v pip3 >/dev/null 2>&1 && ! command -v pip >/dev/null 2>&1; then
    info "pip not found - installing python3-pip..."
    if command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y python3-pip
    elif command -v apt-get >/dev/null 2>&1; then
        sudo apt-get install -y python3-pip
    else
        die "pip not found - install python3-pip manually and retry"
    fi
fi
PIP=$(command -v pip3 2>/dev/null || command -v pip 2>/dev/null)

# ── ask what to install ────────────────────────────────────────────────────────

printf '\n'
printf '\033[1mpeerdup installer\033[0m\n'
printf '=================\n\n'
printf 'Install the \033[1mdaemon\033[0m (required on every machine that syncs files).\n'
printf 'Install the \033[1mregistry\033[0m (required once, on an always-on server or this machine).\n\n'
printf 'Skip the registry if you already have one running elsewhere,\n'
printf 'or if you only need local-only shares (no registry needed for those).\n\n'

printf 'Install the registry on this machine? [y/N]: '
read -r INSTALL_REGISTRY </dev/tty
case "$INSTALL_REGISTRY" in
    [yY]|[yY][eE][sS]) INSTALL_REGISTRY=1 ;;
    *) INSTALL_REGISTRY=0 ;;
esac

# ── libtorrent (daemon only) ───────────────────────────────────────────────────

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
            sudo dnf install -y rb_libtorrent-python3 python3-pip
            ;;
        ubuntu|debian|linuxmint|pop)
            sudo apt-get update -qq
            sudo apt-get install -y python3-libtorrent python3-pip
            ;;
        *)
            if command -v brew >/dev/null 2>&1; then
                brew install libtorrent-rasterbar 2>/dev/null || true
            fi
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

mkdir -p "$BIN_DIR"

# ── install daemon package + generate protos ───────────────────────────────────

info "Installing daemon..."
"$PIP" install --user -e "$INSTALL_DIR/daemon/"

info "Generating daemon gRPC stubs..."
(cd "$INSTALL_DIR/daemon" && python3 -m grpc_tools.protoc \
    -I proto \
    -I "$(python3 -c 'import grpc_tools, os; print(os.path.dirname(grpc_tools.__file__))')/_proto" \
    --python_out=daemon \
    --grpc_python_out=daemon \
    proto/control.proto && \
    sed -i 's/^import control_pb2 as control__pb2$/from daemon import control_pb2 as control__pb2/' \
        daemon/control_pb2_grpc.py)
ok "Daemon stubs generated"

ln -sf "$INSTALL_DIR/daemon/start.sh" "$BIN_DIR/peerdup-setup"
ok "peerdup-setup linked to $BIN_DIR/peerdup-setup"

# ── install registry package + generate protos (optional) ─────────────────────

if [ "$INSTALL_REGISTRY" = "1" ]; then
    info "Installing registry..."
    "$PIP" install --user -e "$INSTALL_DIR/registry/"

    info "Generating registry gRPC stubs..."
    (cd "$INSTALL_DIR/registry" && python3 -m grpc_tools.protoc \
        -I proto \
        -I "$(python3 -c 'import grpc_tools, os; print(os.path.dirname(grpc_tools.__file__))')/_proto" \
        --python_out=registry \
        --grpc_python_out=registry \
        proto/registry.proto && \
        sed -i 's/^import registry_pb2 as registry__pb2$/from registry import registry_pb2 as registry__pb2/' \
            registry/registry_pb2_grpc.py)
    ok "Registry stubs generated"

    ln -sf "$INSTALL_DIR/registry/start.sh" "$BIN_DIR/peerdup-registry-setup"
    ok "peerdup-registry-setup linked to $BIN_DIR/peerdup-registry-setup"
fi

# ── GNOME Shell extension (optional) ──────────────────────────────────────────

_is_gnome() {
    case "${XDG_CURRENT_DESKTOP:-}" in
        *GNOME*|*gnome*) return 0 ;;
    esac
    command -v gnome-shell >/dev/null 2>&1
}

if _is_gnome; then
    printf '\nGNOME Shell detected.\n'
    printf 'Install the peerdup top-bar extension? [y/N]: '
    read -r INSTALL_GNOME </dev/tty
    case "$INSTALL_GNOME" in
        [yY]|[yY][eE][sS])
            info "Installing GNOME Shell extension..."
            sh "$INSTALL_DIR/gnome-extension/install.sh"
            ;;
        *)
            ok "Skipping GNOME extension (run gnome-extension/install.sh later if needed)"
            ;;
    esac
fi

# ── PATH reminder ──────────────────────────────────────────────────────────────

if ! echo "$PATH" | grep -q "$BIN_DIR"; then
    warn "$BIN_DIR is not in your PATH"
    warn "Add this to your shell profile (~/.bashrc, ~/.zshrc, etc.) then reopen your terminal:"
    printf '\n    export PATH="$HOME/.local/bin:$PATH"\n\n'
fi

# ── done ───────────────────────────────────────────────────────────────────────

printf '\n'
printf '\033[1;32mpeerdup installed.\033[0m\n\n'

if [ "$INSTALL_REGISTRY" = "1" ]; then
    printf 'Start the registry first:\n\n'
    printf '    peerdup-registry-setup\n\n'
    printf 'Then configure the daemon on this (and every other) machine:\n\n'
    printf '    peerdup-setup\n\n'
else
    printf 'To configure and start the daemon:\n\n'
    printf '    peerdup-setup\n\n'
fi
