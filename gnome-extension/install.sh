#!/usr/bin/env bash
# Install the peerdup GNOME Shell extension.
# Run from the gnome-extension/ directory or the repo root.

set -euo pipefail

UUID="peerdup@peerdup"
DEST="$HOME/.local/share/gnome-shell/extensions/$UUID"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Support running from repo root
EXT_DIR="$SCRIPT_DIR"
if [[ ! -f "$EXT_DIR/metadata.json" ]]; then
    EXT_DIR="$SCRIPT_DIR/gnome-extension"
fi
if [[ ! -f "$EXT_DIR/metadata.json" ]]; then
    echo "Error: cannot find gnome-extension/metadata.json" >&2
    exit 1
fi

if ! command -v gnome-shell &>/dev/null; then
    echo "Error: GNOME Shell not found. This extension only works on GNOME." >&2
    exit 1
fi

echo "Installing peerdup extension to $DEST ..."
mkdir -p "$DEST/icons"
cp "$EXT_DIR/metadata.json"  "$DEST/"
cp "$EXT_DIR/extension.js"   "$DEST/"
cp "$EXT_DIR/stylesheet.css" "$DEST/"
cp "$EXT_DIR/icons/"*.svg    "$DEST/icons/"

echo "Enabling extension ..."
if gnome-extensions enable "$UUID" 2>/dev/null; then
    echo "Extension enabled."
else
    # gnome-extensions enable requires a live session reload.
    # Pre-enable via gsettings so it activates automatically on next login.
    python3 - "$UUID" <<'PYEOF'
import subprocess, ast, sys
uuid = sys.argv[1]
raw = subprocess.run(
    ["gsettings", "get", "org.gnome.shell", "enabled-extensions"],
    capture_output=True, text=True
).stdout.strip()
try:
    exts = ast.literal_eval(raw.lstrip("@as ").strip())
    if not isinstance(exts, list):
        exts = []
except Exception:
    exts = []
if uuid in exts:
    print("Extension already in enabled list.")
    sys.exit(0)
exts.append(uuid)
new_val = "[" + ", ".join(f"'{e}'" for e in exts) + "]"
subprocess.run(["gsettings", "set", "org.gnome.shell", "enabled-extensions", new_val], check=True)
print("Extension pre-enabled via gsettings.")
PYEOF
    echo ""
    echo "Log out and back in - the peerdup icon will appear in your top bar automatically."
fi

echo ""
echo "Done. The peerdup icon will appear in your top bar once the"
echo "daemon is running ('peerdup-setup' to configure if needed)."
