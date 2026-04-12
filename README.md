# peerdup

Private peer-to-peer file replication - self-hosted, no central server.

No file data passes through a central server. The registry only brokers peer
discovery; all transfers are direct peer-to-peer via libtorrent.

## How it works

```
┌─────────────────┐
│    Registry     │   gRPC / TLS  - peer discovery, ACL, presence (optional)
└────────┬────────┘
         │ announce / WatchSharePeers
  ┌──────┴──────┐
  │             │
┌─┴──────┐  ┌──┴─────┐
│Daemon A│◄─►Daemon B│  direct P2P via libtorrent
└──┬─────┘  └────────┘
   │    ╲       ╱        relay path (symmetric NAT fallback, optional)
   │     ╲     ╱
   │   ┌──┴─┴──┐
   │   │ Relay │
   │   └───────┘
   ▼ Unix socket
┌──┴──────────┐
│ peerdup CLI │
└─────────────┘
```

Two sync modes:

| Mode | Registry needed | Discovery | Access control |
|------|----------------|-----------|---------------|
| `registry` | Yes | Registry + LAN | ACL enforced by registry |
| `local` | No | LAN multicast only | Anyone on LAN with the share_id |

## Quickstart

### 1. Install (each machine)

```bash
curl -fsSL https://raw.githubusercontent.com/theronconrey/peerdup/main/install.sh | sh
```

The installer asks if you also want to install the registry on this machine.
Say yes on your always-on server; say no on laptops and workstations that
only sync files. Works on Fedora, Ubuntu/Debian, and macOS.

> **LAN-only?** If all your peers are on the same network, skip the registry
> entirely and use `--local` shares instead.

> **Docker instead?** If you prefer to run the registry and relay in
> containers with automatic TLS (Let's Encrypt), see the
> [Docker deployment](#docker-deployment) section below.

### 2. Start the registry (server only, once)

On the machine where you installed the registry:

```bash
peerdup-registry-setup
```

Prompts for port and database path, then starts the registry in the
background. Subsequent runs restart it without re-prompting.

### 3. Configure and start the daemon (every machine)

```bash
peerdup-setup
```

Prompts for your machine name, registry address, and optional settings, then
starts the daemon. Leave registry blank if you're using local-only shares.

### 4. Share a folder

#### With a registry (access-controlled)

```bash
# Machine A - create share and grant access
peerdup share create photos ~/Pictures
peerdup share grant photos <machine-b-peer-id>

# Machine B - join
peerdup share add <share_id> ~/Pictures
peerdup share peers photos
```

#### Local-only (LAN, no registry needed)

```bash
# Machine A
peerdup share create photos ~/Pictures --local
# → prints share_id

# Machine B (same LAN)
peerdup share add <share_id> ~/Pictures --local
```

## Layout

```
peerdup/
├── registry/           # Registry server - peer discovery, ACL, presence
├── relay/              # Relay server - TCP rendezvous for symmetric NAT
├── daemon/             # Peer daemon + CLI
├── start.sh            # Server-side first-run setup + docker compose launcher
├── docker-compose.yml  # Registry + relay + Caddy
├── Caddyfile           # Caddy TLS config
└── .env.example        # Copy to .env if you prefer manual config
```

## CLI reference

Share commands accept either the share **name** or the full **share_id**.

```bash
peerdup identity
peerdup share list
peerdup share info   <name-or-id>
peerdup share peers  <name-or-id>
peerdup share create <name> <path> [--local] [--import-key <file>] [--conflict <strategy>]
peerdup share add    <share_id> <path> [--local] [--conflict <strategy>]
peerdup share grant  <name-or-id> <peer_id>
peerdup share revoke <name-or-id> <peer_id>
peerdup share remove <name-or-id>
peerdup share set-limit    <name-or-id> --up 10M --down 50M
peerdup share set-conflict <name-or-id> last_write_wins|rename_conflict|ask
peerdup share conflicts    <name-or-id>
peerdup share resolve      <conflict-id> keep-local|keep-remote
peerdup share pause  <name-or-id>
peerdup share resume <name-or-id>
peerdup status
peerdup watch
```

The socket path is auto-detected from `$XDG_RUNTIME_DIR` (user installs) or
`/run/peerdup/control.sock` (system installs). Override with `PEERDUP_SOCKET`.

`peerdup share list` shows a `MODE` column (`registry` or `local`) for each share.

## Sync behavior

Changes on any peer replicate to all others - there is no designated
source-of-truth machine. Every peer can read and write.

Each local change increments a monotonic sequence number. When two peers have
different versions of a share, the one with the higher sequence number wins.
This means the most recently modified peer's state propagates rather than an
arbitrary one.

Folder renames and file moves are efficient: when a peer receives a new torrent
layout, peerdup matches existing files by name and size and moves them into
place before libtorrent checks pieces. Files that don't transfer at all - they
just get repositioned. Stale files and empty directories from prior layouts are
cleaned up automatically.

## Conflict resolution

When two peers independently modify the same share before either has seen the
other's changes, peerdup detects the divergence via mismatched sequence numbers
and info-hashes and applies the share's conflict strategy:

| Strategy | Behaviour |
|----------|-----------|
| `last_write_wins` | Accept the remote version silently (default) |
| `rename_conflict` | Rename local files to `name.conflict.TIMESTAMP.PEERID.ext`, then accept the remote |
| `ask` | Pause the share and record the conflict; resolve manually |

```bash
peerdup share create docs ~/Documents --conflict rename_conflict
peerdup share set-conflict docs ask
peerdup share conflicts docs
peerdup share resolve 3 keep-local
peerdup share resolve 3 keep-remote
```

`peerdup watch` emits `[CONFLICT]` events in real time.

## Relay

For peers behind symmetric NAT that can't connect directly, the relay is
included in the Docker stack and exposed on TCP port 55002. Enable it via
`peerdup-setup` prompts or manually in `config.toml`:

```toml
[relay]
enabled = true
address = "your-domain:55002"
```

The daemon tries a direct connection and a relay bridge simultaneously.
libtorrent uses whichever connects first.

## GNOME Shell extension

On GNOME desktops (Fedora, Ubuntu GNOME, etc.), peerdup includes a top-bar
extension that shows sync status at a glance and lets you create or join shares
without opening a terminal.

The installer detects GNOME automatically and offers to install it. To install
or reinstall it manually:

```bash
~/.local/share/peerdup/gnome-extension/install.sh
```

The icon in the top bar reflects the current state:

| Icon | Meaning |
|------|---------|
| Idle | Daemon running, all shares up to date |
| Syncing | Active transfer in progress |
| Error | Daemon unavailable or a share has an error |

Click the icon to see per-share status (peers, progress, transfer rates) and
to pause/resume individual shares. If `zenity` is installed, the menu also
offers **New share...** and **Join share...** dialogs.

Requires GNOME Shell 45+ and the peerdup daemon running on the same user
account.

## Uninstall

```bash
peerdup-setup --uninstall
```

This stops and disables the systemd daemon service, removes the GNOME Shell
extension (if installed), uninstalls all peerdup pip packages, and removes the
`peerdup-setup` symlink. It prompts before deleting your identity key, database,
and config so you can choose to preserve them.

To also remove the registry from a server machine:

```bash
peerdup-registry-setup --uninstall
```

## Docker deployment

If you prefer containers with automatic TLS (Let's Encrypt), you can run the
registry and relay via Docker Compose instead of `peerdup-registry-setup`.
You need a Linux host with a public IP, a DNS A record, and Docker CE.

```bash
git clone https://github.com/theronconrey/peerdup
cd peerdup
./start.sh
```

Prompts for your domain and email, starts the stack, and obtains a
Let's Encrypt certificate via Caddy automatically. Subsequent runs skip the
prompts and restart the stack.
