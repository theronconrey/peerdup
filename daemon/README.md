# peerdup-daemon

Peer daemon for the peerdup private P2P file replication system.

Runs on each machine that participates in a sync. Uses inotify (via watchdog)
to detect local file changes, announces presence to the registry, and uses
libtorrent for direct peer-to-peer transfers - no file data ever passes
through the registry.

## Architecture

```
                    ┌─────────────────┐
                    │    Registry     │
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

### Fedora

```bash
sudo dnf install rb_libtorrent-python3
pip install -e .
```

### Other Linux / macOS

```bash
pip install libtorrent   # or install system package equivalent
pip install -e .
```

Proto stubs are generated automatically during `pip install` - no manual
code generation step required.

## Setup

```bash
./start.sh
```

On first run `start.sh` prompts for your machine name, registry address, and
optional relay/LAN settings, writes `config.toml`, then starts the daemon.
Subsequent runs skip the prompts and go straight to `peerdup-daemon`.

The socket path is auto-detected - no environment variable needed:
- User installs: `$XDG_RUNTIME_DIR/peerdup/control.sock` (e.g. `/run/user/1000/peerdup/control.sock`)
- Root/system installs: `/run/peerdup/control.sock`

Override with `PEERDUP_SOCKET` if you need a non-standard path.

## CLI reference

Share commands accept either the share **name** or the full **share_id**.

```bash
peerdup identity
peerdup share list
peerdup share info   <name-or-id>          # metadata, no peer roster
peerdup share peers  <name-or-id>          # peer roster: online/offline
peerdup share create <name> <path>         # create share, print share_id + fingerprint
peerdup share create <name> <path> --import-key <file>     # import existing keypair
peerdup share create <name> <path> --conflict <strategy>   # set conflict strategy at creation
peerdup share add    <share_id> <path>     # join an existing share
peerdup share add    <share_id> <path> --conflict <strategy>
peerdup share grant  <name-or-id> <peer_id>
peerdup share revoke <name-or-id> <peer_id>
peerdup share remove <name-or-id>
peerdup share set-limit    <name-or-id> --up 10M --down 50M   # 0 = unlimited
peerdup share set-conflict <name-or-id> <strategy>            # change conflict strategy
peerdup share conflicts    <name-or-id>                       # list pending conflicts
peerdup share resolve      <conflict-id> keep-local|keep-remote
peerdup share pause  <name-or-id>
peerdup share resume <name-or-id>
peerdup status
peerdup watch                              # live event stream (includes CONFLICT events)
```

### Typical first-time flow

```bash
# Machine A (owner)
peerdup share create photos ~/Pictures
# → prints share_id + fingerprint

peerdup share grant photos <machine-b-peer-id>

# Machine B
peerdup share add <share_id> ~/Pictures
peerdup share peers photos   # verify both online
```

### Conflict resolution

When two peers independently modify the same file, peerdup detects the
divergence (via mismatched libtorrent info-hashes announced to the registry)
and applies the share's **conflict strategy**:

| Strategy | Behaviour |
|----------|-----------|
| `last_write_wins` | Accept the remote version silently (default) |
| `rename_conflict` | Rename all locally-tracked files to `name.conflict.YYYYMMDD.HHMMSS.PEERID.ext` before accepting the remote version - nothing is lost |
| `ask` | Pause the share and record the conflict in the local DB; resume only after you resolve it manually |

```bash
# Set strategy when creating a share
peerdup share create docs ~/Documents --conflict rename_conflict

# Change strategy on an existing share
peerdup share set-conflict docs ask

# With ask strategy - view pending conflicts
peerdup share conflicts docs
# → shows conflict IDs, remote peer, info-hashes, timestamp

# Resolve: keep your local version (discard remote)
peerdup share resolve 3 keep-local

# Resolve: accept the remote version (your local content replaced)
peerdup share resolve 3 keep-remote
```

`peerdup watch` emits `[CONFLICT]` events in real time when the `ask`
strategy detects a divergence, so you can script or alert on them.

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

**Identity**: Each peer has an Ed25519 keypair generated on first run.
The public key is the `peer_id`. The private key lives at `identity.key_file`
with 0600 permissions - the daemon refuses to start if it's world-readable.

**Announce signatures**: Every announce is signed over
`share_id || peer_id || canonical(addrs) || ttl` - covering addresses
prevents a rogue registry from injecting arbitrary peer addresses.

**LAN discovery**: UDP multicast packets are signed with the peer's Ed25519
key and verified before injection into libtorrent. Only peers already in the
ACL cache are accepted.

**Control socket**: The Unix domain socket is created with 0600 permissions,
accessible only to the daemon's user. No authentication needed - filesystem
permissions are the access control.

**Transport**: TLS to the registry. libtorrent uses protocol encryption
between peers (prefer-encrypted; set `out_enc_policy = forced` for mandatory).

## Deployment (systemd)

```bash
# Create system user
sudo useradd -r -s /sbin/nologin peerdup

# Install
sudo mkdir -p /etc/peerdup /var/lib/peerdup
sudo pip install -e /path/to/peerdup/daemon
sudo chown -R peerdup:peerdup /var/lib/peerdup

# Generate config interactively
sudo -u peerdup /path/to/peerdup/daemon/start.sh
# ^ writes /path/to/peerdup/daemon/config.toml, then move it:
sudo mv /path/to/peerdup/daemon/config.toml /etc/peerdup/config.toml

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
| `[registry]` | `address` | - | `host:port` of registry |
| `[registry]` | `tls` | `true` | Enable TLS to registry |
| `[identity]` | `key_file` | `~/.local/share/peerdup/identity.key` (user) / `/var/lib/peerdup/identity.key` (root) | Ed25519 private key |
| `[libtorrent]` | `upload_rate_limit` | `0` | Global upload cap (bytes/sec) |
| `[libtorrent]` | `download_rate_limit` | `0` | Global download cap (bytes/sec) |
| `[lan]` | `enabled` | `true` | LAN multicast discovery |
| `[lan]` | `multicast_group` | `239.193.0.0` | Multicast group |
| `[lan]` | `multicast_port` | `49152` | Multicast port |
| `[relay]` | `enabled` | `false` | Enable relay fallback for symmetric NAT |
| `[relay]` | `address` | `""` | `host:port` of the relay server |
| `[relay]` | `pair_timeout` | `120` | Seconds to wait for remote peer to connect to relay |
