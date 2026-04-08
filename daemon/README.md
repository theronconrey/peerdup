# peerdup-daemon

Peer daemon for the peerdup private P2P file replication system.

Runs on each machine that participates in a sync. Uses inotify (via watchdog)
to detect local file changes, announces presence to the registry, and uses
libtorrent for direct peer-to-peer transfers — no file data ever passes
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
             │  Daemon A   │   │  Daemon B   │
             │  (NAS)      │◄──►  (laptop)  │
             └─────────────┘   └─────────────┘
                  libtorrent  ←direct P2P→  libtorrent
                    ▲
                    │ Unix socket
             ┌──────┴──────┐
             │  peerdup CLI │
             └─────────────┘
```

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

Proto stubs are generated automatically during `pip install` — no manual
code generation step required.

## Setup

```bash
cp config.example.toml config.toml
# edit: set registry.address, identity.name, daemon.data_dir

peerdup-daemon --config config.toml
```

Set `PEERDUP_SOCKET` to avoid passing `--socket` on every CLI command:

```bash
export PEERDUP_SOCKET=/run/peerdup/control.sock
```

## CLI reference

Share commands accept either the share **name** or the full **share_id**.

```bash
peerdup identity
peerdup share list
peerdup share info   <name-or-id>          # metadata, no peer roster
peerdup share peers  <name-or-id>          # peer roster: online/offline
peerdup share create <name> <path>         # create share, print share_id + fingerprint
peerdup share create <name> <path> --import-key <file>   # import existing keypair
peerdup share add    <share_id> <path>     # join an existing share
peerdup share grant  <name-or-id> <peer_id>
peerdup share revoke <name-or-id> <peer_id>
peerdup share remove <name-or-id>
peerdup share set-limit <name-or-id> --up 10M --down 50M  # 0 = unlimited
peerdup share pause  <name-or-id>
peerdup share resume <name-or-id>
peerdup status
peerdup watch                              # live event stream
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

## Component overview

| Module | Responsibility |
|--------|---------------|
| `daemon/identity.py` | Ed25519 keypair generation and signing |
| `daemon/config.py` | TOML config loading with CLI overrides |
| `daemon/state/db.py` | Local SQLite state (shares, files, known peers) |
| `daemon/registry/client.py` | gRPC registry client + WatchSharePeers reconnect loop |
| `daemon/watcher/fs.py` | inotify filesystem watcher with debouncing |
| `daemon/torrent/session.py` | libtorrent session, private swarm, peer injection |
| `daemon/lan/discovery.py` | UDP multicast LAN discovery (239.193.0.0:49152) |
| `daemon/sync/coordinator.py` | Orchestrates all the above |
| `daemon/control/servicer.py` | ControlService gRPC over Unix socket (for CLI) |
| `daemon/daemon.py` | Entrypoint, boots all components |
| `cli/peerdup.py` | CLI tool — thin gRPC client to control socket |

## Security model

**Identity**: Each peer has an Ed25519 keypair generated on first run.
The public key is the `peer_id`. The private key lives at `identity.key_file`
with 0600 permissions — the daemon refuses to start if it's world-readable.

**Announce signatures**: Every announce is signed over
`share_id || peer_id || canonical(addrs) || ttl` — covering addresses
prevents a rogue registry from injecting arbitrary peer addresses.

**LAN discovery**: UDP multicast packets are signed with the peer's Ed25519
key and verified before injection into libtorrent. Only peers already in the
ACL cache are accepted.

**Control socket**: The Unix domain socket is created with 0600 permissions,
accessible only to the daemon's user. No authentication needed — filesystem
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
sudo cp config.example.toml /etc/peerdup/config.toml
sudo chown -R peerdup:peerdup /var/lib/peerdup

# Edit config — set registry.address, data_dir, identity.key_file
sudo nano /etc/peerdup/config.toml

# Install and start service
sudo cp scripts/peerdup-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now peerdup-daemon

# CLI access (add yourself to peerdup group and adjust socket perms, or use sudo)
sudo -u peerdup peerdup share list
```

## Configuration reference

See [`config.example.toml`](config.example.toml) for all options with comments.

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| `[daemon]` | `name` | — | Human-readable peer name |
| `[daemon]` | `data_dir` | `/var/lib/peerdup` | State DB + torrent cache |
| `[daemon]` | `socket_path` | `/run/peerdup/control.sock` | CLI control socket |
| `[daemon]` | `listen_port` | `55000` | libtorrent listen port |
| `[registry]` | `address` | — | `host:port` of registry |
| `[registry]` | `tls` | `true` | Enable TLS to registry |
| `[identity]` | `key_file` | `/var/lib/peerdup/identity.key` | Ed25519 private key |
| `[libtorrent]` | `upload_rate_limit` | `0` | Global upload cap (bytes/sec) |
| `[libtorrent]` | `download_rate_limit` | `0` | Global download cap (bytes/sec) |
| `[lan]` | `enabled` | `true` | LAN multicast discovery |
| `[lan]` | `multicast_group` | `239.193.0.0` | Multicast group |
| `[lan]` | `multicast_port` | `49152` | Multicast port |
