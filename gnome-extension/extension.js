/**
 * peerdup GNOME Shell extension
 *
 * Shows peerdup sync status in the top bar. Talks to the daemon by
 * spawning two subprocesses:
 *   - `peerdup watch --json`  — long-lived; delivers push notifications
 *   - `peerdup share list --json` — short-lived poll; refreshes full state
 *
 * Requires GNOME Shell 45+ and peerdup daemon running on the same user account.
 */

import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const POLL_INTERVAL_S = 30;
const RETRY_DELAY_S   = 5;

// GNOME Shell's PATH doesn't include ~/.local/bin; search explicitly.
function _findBin(name) {
    const candidates = [
        `${GLib.get_home_dir()}/.local/bin/${name}`,
        `/usr/local/bin/${name}`,
        `/usr/bin/${name}`,
    ];
    for (const p of candidates) {
        if (GLib.file_test(p, GLib.FileTest.IS_EXECUTABLE))
            return p;
    }
    return name; // fallback — will fail visibly
}

const PEERDUP_BIN = _findBin('peerdup');
const ZENITY_BIN  = _findBin('zenity');
const HAS_ZENITY  = GLib.file_test(ZENITY_BIN, GLib.FileTest.IS_EXECUTABLE);


export default class PeerDupExtension extends Extension {

    // ── Lifecycle ──────────────────────────────────────────────────────────

    enable() {
        this._shares         = new Map();   // share_id -> share dict
        this._indicator      = null;
        this._icon           = null;
        this._pollTimerId    = null;
        this._retryTimerId   = null;
        this._watchProc      = null;
        this._watchStream    = null;
        this._watchCancel    = null;

        this._indicator = new PanelMenu.Button(0.0, this.metadata.name, false);

        this._icon = new St.Icon({style_class: 'system-status-icon'});
        this._setIconState('error');
        this._indicator.add_child(this._icon);

        Main.panel.addToStatusArea(this.uuid, this._indicator);

        this._startWatcher();
        this._pollStatus();
        this._pollTimerId = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT, POLL_INTERVAL_S,
            () => { this._pollStatus(); return GLib.SOURCE_CONTINUE; }
        );
    }

    disable() {
        if (this._pollTimerId !== null) {
            GLib.Source.remove(this._pollTimerId);
            this._pollTimerId = null;
        }
        if (this._retryTimerId !== null) {
            GLib.Source.remove(this._retryTimerId);
            this._retryTimerId = null;
        }
        this._stopWatcher();
        this._indicator?.destroy();
        this._indicator  = null;
        this._icon       = null;
        this._shares     = null;
    }

    // ── Watcher subprocess ─────────────────────────────────────────────────

    _startWatcher() {
        try {
            this._watchCancel = new Gio.Cancellable();
            this._watchProc   = new Gio.Subprocess({
                argv:  [PEERDUP_BIN, 'watch', '--json'],
                flags: Gio.SubprocessFlags.STDOUT_PIPE |
                       Gio.SubprocessFlags.STDERR_SILENCE,
            });
            this._watchProc.init(this._watchCancel);
            this._watchStream = new Gio.DataInputStream({
                base_stream:      this._watchProc.get_stdout_pipe(),
                close_base_stream: true,
            });
            this._readNextLine();
        } catch (_) {
            this._setIconState('error');
            this._scheduleRetry();
        }
    }

    _stopWatcher() {
        this._watchCancel?.cancel();
        this._watchCancel = null;
        try { this._watchProc?.force_exit(); } catch (_) {}
        this._watchProc   = null;
        this._watchStream = null;
    }

    _readNextLine() {
        if (!this._watchStream) return;

        this._watchStream.read_line_async(
            GLib.PRIORITY_LOW, this._watchCancel,
            (stream, res) => {
                try {
                    const [line] = stream.read_line_finish_utf8(res);
                    if (line === null) {
                        // EOF — daemon stopped or restarted
                        this._setIconState('error');
                        this._scheduleRetry();
                        return;
                    }
                    if (line.length > 0) {
                        try {
                            this._onWatchEvent(JSON.parse(line));
                        } catch (_) { /* malformed line */ }
                    }
                    this._readNextLine();
                } catch (e) {
                    if (!e.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                        this._scheduleRetry();
                }
            }
        );
    }

    _scheduleRetry() {
        if (this._retryTimerId !== null) return;
        this._retryTimerId = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT, RETRY_DELAY_S,
            () => {
                this._retryTimerId = null;
                this._stopWatcher();
                this._startWatcher();
                return GLib.SOURCE_REMOVE;
            }
        );
    }

    // ── Event + poll handlers ──────────────────────────────────────────────

    _onWatchEvent(_event) {
        // Any event means state changed — do a fresh poll for full data.
        this._pollStatus();
    }

    _pollStatus() {
        try {
            const proc = new Gio.Subprocess({
                argv:  [PEERDUP_BIN, 'share', 'list', '--json'],
                flags: Gio.SubprocessFlags.STDOUT_PIPE |
                       Gio.SubprocessFlags.STDERR_SILENCE,
            });
            proc.init(null);
            proc.communicate_utf8_async(null, null, (p, res) => {
                try {
                    const [, stdout] = p.communicate_utf8_finish(res);
                    const data = JSON.parse(stdout?.trim() ?? '{}');
                    if (Array.isArray(data.shares)) {
                        this._shares.clear();
                        for (const s of data.shares)
                            this._shares.set(s.share_id, s);
                        this._rebuildMenu();
                        this._updateIconState();
                        return;
                    }
                } catch (_) {}
                this._setMenuError('Daemon unavailable');
                this._setIconState('error');
            });
        } catch (_) {
            this._setMenuError('Daemon unavailable');
            this._setIconState('error');
        }
    }

    _setMenuError(msg) {
        if (!this._indicator) return;
        this._indicator.menu.removeAll();
        this._indicator.menu.addMenuItem(
            new PopupMenu.PopupMenuItem(msg, {reactive: false})
        );
    }

    // ── Menu ──────────────────────────────────────────────────────────────

    _rebuildMenu() {
        this._indicator.menu.removeAll();

        if (this._shares.size === 0) {
            this._indicator.menu.addMenuItem(
                new PopupMenu.PopupMenuItem('No active shares', {reactive: false})
            );
        } else {
            let first = true;
            for (const [, share] of this._shares) {
                if (!first)
                    this._indicator.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
                first = false;
                this._addShareSection(share);
            }
        }

        this._addShareActions();
    }

    _addShareActions() {
        if (!HAS_ZENITY) return;
        this._indicator.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        const createItem = new PopupMenu.PopupMenuItem('New share...');
        createItem.connect('activate', () => this._showCreateShare());
        this._indicator.menu.addMenuItem(createItem);

        const joinItem = new PopupMenu.PopupMenuItem('Join share...');
        joinItem.connect('activate', () => this._showJoinShare());
        this._indicator.menu.addMenuItem(joinItem);
    }

    _addShareSection(share) {
        const isPaused = share.state === 'paused';
        const pct = share.bytes_total > 0
            ? Math.min(1, share.bytes_done / share.bytes_total) : -1;

        // ── Row 1: name + peers ──────────────────────────────────────────

        const headerItem = new PopupMenu.PopupBaseMenuItem({reactive: false});
        const headerBox  = new St.BoxLayout({x_expand: true});

        headerBox.add_child(new St.Label({
            text:        share.name,
            style_class: 'peerdup-share-name',
            x_expand:    true,
        }));

        const peerStr = `${share.lt_peers}/${share.peers_online} peers`;
        headerBox.add_child(new St.Label({
            text:        share.last_error ? `${peerStr} ⚠` : peerStr,
            style_class: share.last_error ? 'peerdup-peers-warn' : 'peerdup-peers-label',
        }));

        headerItem.add_child(headerBox);
        this._indicator.menu.addMenuItem(headerItem);

        // ── Row 2: progress bar + rates ──────────────────────────────────

        const statusItem = new PopupMenu.PopupBaseMenuItem({reactive: false});

        let statusText = '';
        if (pct >= 0) {
            const filled   = Math.round(pct * 14);
            const bar      = '█'.repeat(filled) + '░'.repeat(14 - filled);
            statusText     = `${bar} ${Math.round(pct * 100)}%`;
        }
        if (share.upload_rate > 0 || share.download_rate > 0) {
            const rates = `  ↑${_humanRate(share.upload_rate)} ↓${_humanRate(share.download_rate)}`;
            statusText  = (statusText + rates).trim() || share.state;
        }
        if (!statusText)
            statusText = isPaused ? 'paused' : share.state;

        statusItem.add_child(new St.Label({
            text:        statusText,
            style_class: 'peerdup-status-label',
            x_expand:    true,
        }));
        this._indicator.menu.addMenuItem(statusItem);

        // ── Row 3: pause / resume ────────────────────────────────────────

        const pauseItem = new PopupMenu.PopupMenuItem(
            isPaused ? '▶  Resume' : '⏸  Pause'
        );
        pauseItem.connect('activate', () => this._togglePause(share.share_id, isPaused));
        this._indicator.menu.addMenuItem(pauseItem);
    }

    // ── Share creation / joining ───────────────────────────────────────────

    _showCreateShare() {
        this._zenityEntry('New share', 'Share name:', (name) => {
            if (!name) return;
            this._zenityFolder('Select folder to sync', (path) => {
                if (!path) return;
                this._runCmd([PEERDUP_BIN, 'share', 'create', name, path, '--local'],
                    () => this._pollStatus());
            });
        });
    }

    _showJoinShare() {
        this._zenityEntry('Join share', 'Share ID:', (shareId) => {
            if (!shareId) return;
            this._zenityFolder('Select folder to sync', (path) => {
                if (!path) return;
                this._runCmd([PEERDUP_BIN, 'share', 'add', shareId, path, '--local'],
                    () => this._pollStatus());
            });
        });
    }

    _zenityEntry(title, prompt, callback) {
        this._runZenity(
            [ZENITY_BIN, '--entry', `--title=${title}`, `--text=${prompt}`],
            callback
        );
    }

    _zenityFolder(title, callback) {
        this._runZenity(
            [ZENITY_BIN, '--file-selection', '--directory', `--title=${title}`],
            callback
        );
    }

    _runZenity(argv, callback) {
        try {
            const proc = new Gio.Subprocess({
                argv,
                flags: Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE,
            });
            proc.init(null);
            proc.communicate_utf8_async(null, null, (p, res) => {
                try {
                    const [, stdout] = p.communicate_utf8_finish(res);
                    const result = stdout?.trim() ?? '';
                    callback(p.get_exit_status() === 0 && result ? result : null);
                } catch (_) {
                    callback(null);
                }
            });
        } catch (_) {
            callback(null);
        }
    }

    _runCmd(argv, onDone) {
        try {
            const proc = new Gio.Subprocess({
                argv,
                flags: Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE,
            });
            proc.init(null);
            proc.wait_async(null, () => {
                GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 1,
                    () => { onDone?.(); return GLib.SOURCE_REMOVE; }
                );
            });
        } catch (_) {}
    }

    _togglePause(shareId, currentlyPaused) {
        const cmd = currentlyPaused ? 'resume' : 'pause';
        this._runCmd([PEERDUP_BIN, 'share', cmd, shareId], () => this._pollStatus());
    }

    // ── Icon state ─────────────────────────────────────────────────────────

    _updateIconState() {
        if (!this._shares || this._shares.size === 0) {
            this._setIconState('error');
            return;
        }
        const shares = [...this._shares.values()];
        if (shares.some(s =>
            s.state === 'syncing' || s.state === 'checking' ||
            s.download_rate > 0  || s.upload_rate > 0
        )) {
            this._setIconState('syncing');
        } else if (shares.some(s => s.state === 'error' || !!s.last_error)) {
            this._setIconState('error');
        } else {
            this._setIconState('idle');
        }
    }

    _setIconState(state) {
        if (!this._icon) return;
        const names = {
            idle:    'peerdup-idle-symbolic',
            syncing: 'peerdup-syncing-symbolic',
            error:   'peerdup-error-symbolic',
        };
        const iconFile = Gio.File.new_for_path(
            `${this.path}/icons/${names[state] ?? names.error}.svg`
        );
        this._icon.gicon = new Gio.FileIcon({file: iconFile});
    }
}


// ── Module-level helpers ───────────────────────────────────────────────────

function _humanRate(n) {
    if (n >= 1e9) return `${(n / 1e9).toFixed(1)}G/s`;
    if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M/s`;
    if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K/s`;
    return `${n}B/s`;
}
