# peerdup-daemon

Peer daemon for the peerdup private P2P file replication system.

Runs on each machine that participates in a sync. Uses inotify (via watchdog)
to detect local file changes, announces presence to the registry, and uses
libtorrent for direct peer-to-peer transfers - no file data ever passes
through the registry.

## Architecture

```
                    ┌─────────────────┐
                    │    Registry     │   (optional - not needed for local shares)
                    │ (peerdup-registry) │
                    └────────┬────────┘
                             │ gRPC (TLS)
              announce / WatchSharePeers
                    ┌────────┴────────┐
                    │                 │
             ┌──────┴──────┐   ┌──────┴──────┐
             │  Daemon A   │◄──►  Daemon B   │  direct P2P (libtorrent)
             │  (NAS)      │   │  (laptop)   │
             └──────┬──────┘   └──────┬──────┘
                    │    ╲       ╱    │
                    │     ╲     ╱     │        relay path (symmetric NAT fallback)
                    │      ╲   ╱      │
                    │   ┌───┴─┴───┐   │
                    │   │  Relay  │   │
                    │   │ (opt.)  │   │
                    │   └─────────┘   │
                    │                 │
                    ▼ Unix socket
             ┌──────┴──────┐
             │  peerdup CLI │
             └─────────────┘
```

The relay is optional. The daemon tries a direct libtorrent connection and a
relay bridge simultaneously - libtorrent uses whichever connects first.

## Installation

```bash
curl -fsSL https://raw.githubusercontent.com/theronconrey/peerdup/main/install.sh | sh
```

Detects your distro, installs libtorrent, clones the repo to
`~/.local/share/peerdup`, and installs the daemon. Works on Fedora,
Ubuntu/Debian, and macOS.

## Setup

```bash
peerdup-setup
```

On first run prompts for your machine name, registry address, and optional
relay/LAN settings, then writes `config.toml` and starts the daemon.

After the daemon starts successfully, `peerdup-setup` offers to install a
systemd user service (`~/.config/systemd/user/peerdup-daemon.service`).
If you accept, subsequent runs of `peerdup-setup` simply restart the unit
via `systemctl --user restart peerdup-daemon` instead of managing the process
manually.

You will also be asked whether to enable linger (`loginctl enable-linger`),
which makes the service start at boot even before you log in - recommended
for NAS or always-on machines, optional for laptops.

TLS to the registry is enabled automatically when a registry address is set.
Leave the registry address blank if you only need local-only shares.

The socket path is auto-detected:
- User installs: `$XDG_RUNTIME_DIR/peerdup/control.sock`
- Root/system installs: `/run/peerdup/control.sock`

Override with `PEERDUP_SOCKET` if needed.

### Useful systemd commands

```bash
systemctl --user status peerdup-daemon     # current state
systemctl --user stop peerdup-daemon       # stop
systemctl --user start peerdup-daemon      # start
systemctl --user restart peerdup-daemon    # restart
journalctl --user -u peerdup-daemon -f     # live logs
journalctl --user -u peerdup-daemon -n 50  # last 50 lines
```

## CLI reference

Share commands accept either the share **name** or the full **share_id**.

```bash
peerdup identity
peerdup share list
peerdup share info   <name-or-id>          # metadata, no peer roster
peerdup share peers  <name-or-id>          # peer roster: online/offline
peerdup share create <name> <path>         # create share, print share_id + fingerprint
peerdup share create <name> <path> --local             # local-only, no registry
peerdup share create <name> <path> --import-key <file> # import existing keypair
peerdup share create <name> <path> --conflict <strategy>
peerdup share add    <share_id> <path>     # join an existing share
peerdup share add    <share_id> <path> --local         # join a local-only share
peerdup share add    <share_id> <path> --conflict <strategy>
peerdup share grant  <name-or-id> <peer_id>
peerdup share revoke <name-or-id> <peer_id>
peerdup share remove <name-or-id>
peerdup share set-limit    <name-or-id> --up 10M --down 50M   # 0 = unlimited
peerdup share set-conflict <name-or-id> <strategy>
peerdup share conflicts    <name-or-id>                       # list pending conflicts
peerdup share resolve      <conflict-id> keep-local|keep-remote
peerdup share pause  <name-or-id>
peerdup share resume <name-or-id>
peerdup status
peerdup watch                              # live event stream (includes CONFLICT events)
```

`peerdup share list` shows a `MODE` column (`registry` or `local`) and a `PEERS`
column formatted as `active/announced` - active libtorrent connections slash
registry-announced peers. Live transfer rates are printed below the row when
non-zero.

### Registry flow

```bash
# Machine A (owner)
peerdup share create photos ~/Pictures
# → prints share_id + fingerprint

peerdup share grant photos <machine-b-peer-id>

# Machine B
peerdup share add <share_id> ~/Pictures
peerdup share peers photos   # verify both online
```

### Local-only flow (no registry needed)

```bash
# Machine A
peerdup share create photos ~/Pictures --local
# → prints share_id

# Machine B (same LAN)
peerdup share add <share_id> ~/Pictures --local
```

Local shares use LAN multicast discovery only. No ACL is enforced - any peer
on the same LAN that knows the share_id can join.

### Conflict resolution

When two peers independently modify the same file, peerdup detects the
divergence (via mismatched libtorrent info-hashes) and applies the share's
**conflict strategy**:

| Strategy | Behaviour |
|----------|-----------|
| `last_write_wins` | Accept the remote version silently (default) |
| `rename_conflict` | Rename all locally-tracked files to `name.conflict.YYYYMMDD.HHMMSS.PEERID.ext` before accepting the remote version - nothing is lost |
| `ask` | Pause the share and record the conflict in the local DB; resume only after you resolve it manually |

```bash
peerdup share create docs ~/Documents --conflict rename_conflict
peerdup share set-conflict docs ask
peerdup share conflicts docs
peerdup share resolve 3 keep-local
peerdup share resolve 3 keep-remote
```

`peerdup watch` emits `[CONFLICT]` events in real time.

## Component overview

| Module | Responsibility |
|--------|---------------|
| `daemon/identity.py` | Ed25519 keypair generation and signing |
| `daemon/config.py` | TOML config loading with CLI overrides |
| `daemon/state/db.py` | Local SQLite state (shares, files, known peers, conflicts) |
| `daemon/registry/client.py` | gRPC registry client + WatchSharePeers reconnect loop |
| `daemon/watcher/fs.py` | inotify filesystem watcher with debouncing |
| `daemon/torrent/session.py` | libtorrent session, private swarm, peer injection |
| `daemon/lan/discovery.py` | UDP multicast LAN discovery (239.193.0.0:49152) |
| `daemon/relay/client.py` | Relay client - pairs with relay server, bridges a local port for libtorrent |
| `daemon/sync/coordinator.py` | Orchestrates all the above |
| `daemon/control/servicer.py` | ControlService gRPC over Unix socket (for CLI) |
| `daemon/daemon.py` | Entrypoint, boots all components |
| `cli/peerdup.py` | CLI tool - thin gRPC client to control socket |

## Security model

**Identity** - each peer has an Ed25519 keypair generated on first run.
The public key is the `peer_id`. The private key lives at `identity.key_file`
with 0600 permissions - the daemon refuses to start if it's world-readable.

**Announce signatures** - every announce is signed over
`share_id || peer_id || canonical(addrs) || ttl`, preventing a rogue registry
from injecting arbitrary peer addresses.

**LAN discovery** - UDP multicast packets are signed with the peer's Ed25519
key and verified before injection into libtorrent. For registry shares, only
peers in the ACL cache are accepted.

**Control socket** - the Unix domain socket is created with 0600 permissions,
accessible only to the daemon's user.

**Transport** - TLS to the registry (enabled automatically when a registry
address is configured). libtorrent uses protocol encryption between peers.

**Local shares** - no registry ACL. Any peer on the same LAN with the
share_id can join. Use registry shares for anything sensitive.

## Deployment (systemd)

```bash
# Create system user
sudo useradd -r -s /sbin/nologin peerdup

# Install
sudo mkdir -p /etc/peerdup /var/lib/peerdup
sudo pip install -e /path/to/peerdup/daemon
sudo chown -R peerdup:peerdup /var/lib/peerdup

# Generate config interactively
sudo -u peerdup peerdup-setup
# config.toml is written next to start.sh; move it:
sudo mv ~/.local/share/peerdup/daemon/config.toml /etc/peerdup/config.toml

# Install and start service
sudo cp scripts/peerdup-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now peerdup-daemon

# CLI access
sudo -u peerdup peerdup share list
```

## Configuration reference

See [`config.example.toml`](config.example.toml) for all options with comments.

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| `[daemon]` | `name` | - | Human-readable peer name |
| `[daemon]` | `data_dir` | `~/.local/share/peerdup` (user) / `/var/lib/peerdup` (root) | State DB + torrent cache |
| `[daemon]` | `socket_path` | `$XDG_RUNTIME_DIR/peerdup/control.sock` (user) / `/run/peerdup/control.sock` (root) | CLI control socket |
| `[daemon]` | `listen_port` | `55000` | libtorrent listen port |
| `[registry]` | `address` | - | `host:port` of registry; TLS is enabled automatically when set |
| `[identity]` | `key_file` | `~/.local/share/peerdup/identity.key` (user) / `/var/lib/peerdup/identity.key` (root) | Ed25519 private key |
| `[libtorrent]` | `upload_rate_limit` | `0` | Global upload cap (bytes/sec) |
| `[libtorrent]` | `download_rate_limit` | `0` | Global download cap (bytes/sec) |
| `[lan]` | `enabled` | `true` | LAN multicast discovery |
| `[lan]` | `multicast_group` | `239.193.0.0` | Multicast group |
| `[lan]` | `multicast_port` | `49152` | Multicast port |
| `[relay]` | `enabled` | `false` | Enable relay fallback for symmetric NAT |
| `[relay]` | `address` | `""` | `host:port` of the relay server |
| `[relay]` | `pair_timeout` | `120` | Seconds to wait for remote peer to connect to relay |
