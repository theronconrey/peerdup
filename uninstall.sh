#!/bin/sh
# peerdup uninstaller
set -e

INSTALL_DIR="$HOME/.local/share/peerdup"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m warn\033[0m %s\n' "$*"; }

PIP=$(command -v pip3 2>/dev/null || command -v pip 2>/dev/null || echo "")

printf '\n'
printf '\033[1mpeerdup uninstaller\033[0m\n'
printf '===================\n\n'

# ── stop and disable systemd services ─────────────────────────────────────────

for svc in peerdup-daemon peerdup-registry; do
    if systemctl --user is-active "$svc" >/dev/null 2>&1; then
        info "Stopping $svc..."
        systemctl --user stop "$svc"
        ok "Stopped $svc"
    fi
    if systemctl --user is-enabled "$svc" >/dev/null 2>&1; then
        systemctl --user disable "$svc" 2>/dev/null || true
        ok "Disabled $svc"
    fi
    if [ -f "$UNIT_DIR/$svc.service" ]; then
        rm -f "$UNIT_DIR/$svc.service"
        ok "Removed $UNIT_DIR/$svc.service"
    fi
done

if [ -d "$UNIT_DIR" ]; then
    systemctl --user daemon-reload 2>/dev/null || true
fi

# ── remove GNOME extension ────────────────────────────────────────────────────

EXT_DIR="$HOME/.local/share/gnome-shell/extensions/peerdup@peerdup"
if [ -d "$EXT_DIR" ]; then
    info "Removing GNOME extension..."
    gnome-extensions disable "peerdup@peerdup" 2>/dev/null || true
    rm -rf "$EXT_DIR"
    ok "GNOME extension removed"
fi

# ── uninstall Python packages ─────────────────────────────────────────────────

if [ -n "$PIP" ]; then
    for pkg in peerdup-daemon peerdup-registry peerdup-relay; do
        if "$PIP" show "$pkg" >/dev/null 2>&1; then
            info "Uninstalling $pkg..."
            "$PIP" uninstall -y "$pkg"
            ok "Uninstalled $pkg"
        fi
    done
fi

# ── remove symlinks ────────────────────────────────────────────────────────────

for bin in peerdup-setup peerdup-registry-setup; do
    if [ -L "$BIN_DIR/$bin" ]; then
        rm -f "$BIN_DIR/$bin"
        ok "Removed $BIN_DIR/$bin"
    fi
done

# ── optionally remove state and config ────────────────────────────────────────

printf '\n'
printf 'Remove all state and config? This deletes your identity key, share database,\n'
printf 'and config. \033[1mThis cannot be undone.\033[0m\n\n'
printf 'Remove state and config? [y/N]: '
read -r REMOVE_STATE </dev/tty
case "$REMOVE_STATE" in
    [yY]|[yY][eE][sS])
        for dir in \
            "$INSTALL_DIR" \
            "$HOME/.local/share/peerdup" \
            "/etc/peerdup" \
            "/var/lib/peerdup"
        do
            if [ -d "$dir" ]; then
                rm -rf "$dir"
                ok "Removed $dir"
            fi
        done
        # Runtime socket dir (non-persistent, best-effort)
        rm -rf "/run/user/$(id -u)/peerdup" 2>/dev/null || true
        ok "State and config removed"
        ;;
    *)
        warn "State kept. Config is at $INSTALL_DIR/daemon/config.toml"
        warn "Identity key and DB are in $HOME/.local/share/peerdup/"
        ;;
esac

printf '\n'
printf '\033[1;32mpeerdup uninstalled.\033[0m\n\n'
