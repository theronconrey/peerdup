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
    echo ""
    echo "Could not enable automatically (may need a session restart)."
    echo "After logging back in, run:"
    echo "  gnome-extensions enable $UUID"
fi

echo ""
echo "Done. The peerdup icon will appear in your top bar once the"
echo "daemon is running ('peerdup-setup' to configure if needed)."
