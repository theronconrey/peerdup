# peerdup GNOME Shell extension

Top-bar sync status for peerdup on GNOME desktops (Fedora, Ubuntu GNOME, etc.).

Requires GNOME Shell 45+ and the peerdup daemon running on the same user account.

## What it shows

The top bar displays the current sync state at a glance:

| Icon | State | Meaning |
|------|-------|---------|
| Idle | All shares up to date | Daemon running, nothing transferring |
| Syncing | Active transfer | Files in flight; rates shown inline: `↑1.2M/s ↓4.8M/s` |
| Error | Problem | Daemon unavailable or a share has an error |

Click the icon to open the popup menu, which shows:

- **Per-share rows** - name, peer count (`active/announced`), progress bar, transfer rates, and a Pause/Resume button
- **Registry status** - a dot indicator for the registry connection: filled = connected, half = disconnected, empty = not configured
- **New share... / Join share...** dialogs (requires `zenity`)

## Installation

The main installer handles this automatically and offers to install the extension
when it detects GNOME. To install or reinstall manually:

```bash
# From the repo root
gnome-extension/install.sh

# Or from inside the gnome-extension/ directory
./install.sh
```

The script copies files to `~/.local/share/gnome-shell/extensions/peerdup@peerdup/`
and enables the extension. If `gnome-extensions enable` fails (common in a Wayland
session without a reload), it pre-enables the extension via `gsettings` so it
activates automatically on next login.

## Files

| File | Purpose |
|------|---------|
| `extension.js` | Main extension code (ES module, GNOME Shell 45+ API) |
| `metadata.json` | Extension metadata: UUID, name, supported shell versions |
| `stylesheet.css` | CSS for share name, peer count, and status labels |
| `icons/` | Three symbolic SVG icons: idle, syncing, error |
| `install.sh` | Installation script |

## How it works

The extension spawns two `peerdup` subprocesses:

- `peerdup watch --json` - long-lived; delivers push notifications as events happen. The extension reads one JSON line per event and triggers a poll immediately on any change. Global transfer rates (`global_upload_rate`, `global_download_rate`) are applied directly from the event stream to update the panel label without waiting for the next poll.
- `peerdup share list --json` - short-lived; called every 30 seconds and after each watch event to refresh the full share list and rebuild the menu.
- `peerdup registry status --json` - called after each share list poll to update the registry connection indicator in the menu.

If the daemon is unavailable or the watch process exits, the icon switches to error
state and the extension retries automatically every 5 seconds.

GNOME Shell's subprocess PATH does not include `~/.local/bin`, so the extension
searches for the `peerdup` binary explicitly in `~/.local/bin`, `/usr/local/bin`,
and `/usr/bin`.

## Supported GNOME Shell versions

45, 46, 47, 48, 49

## Dependencies

- peerdup daemon installed and running (`peerdup-setup` to configure)
- `zenity` - optional; enables the New share / Join share dialogs

## Uninstall

```bash
gnome-extensions disable peerdup@peerdup
rm -rf ~/.local/share/gnome-shell/extensions/peerdup@peerdup
```

Or run the full peerdup uninstaller:

```bash
peerdup-setup --uninstall
```
